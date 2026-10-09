import asyncio
import json

import httpx
import pytest
from test_telegram import FakeTransport, approve, event, store_at

from zarya.app import create_app
from zarya.config import Config
from zarya.dialogue import DialogueEngine
from zarya.dialogue_logic import answer_mode, chunks, direct_trigger, max_parts, topic_id
from zarya.models import Settings
from zarya.openai_adapter import ModelResult, OpenAIAdapter, estimate_cost, pricing_profile
from zarya.telegram_runtime import TelegramRuntime


class FakeModel:
    def __init__(self, result=None):
        self.calls = []
        self.closed = False
        self.result = result or ModelResult(
            text=("Первая развёрнутая мысль. " * 15) + "\n\n" + ("Вторая отдельная мысль. " * 15),
            usage={
                "input_tokens": 100,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
                "output_tokens_details": {"reasoning_tokens": 10},
            },
        )

    async def generate(self, request):
        self.calls.append(request)
        return self.result

    async def close(self):
        self.closed = True


async def private_ready(store):
    await store.ingest("999", "zarya_test", [event(1)])
    await approve(store)


async def run_one(engine):
    work = await engine.claim("999")
    assert work
    if not work["skip"]:
        await engine.execute(work)
    return work


async def drain(engine):
    while work := await engine.claim("999"):
        if not work["skip"]:
            await engine.execute(work)


@pytest.mark.parametrize(
    ("question", "mode", "limit"),
    [
        ("Чем факт отличается от мнения?", "brief", 2),
        ("Приведи ещё один пример", "brief", 2),
        ("Разбери статью https://example.org", "analysis", 3),
        ("Теперь объясни подробнее", "detailed", 4),
        ("Расскажи по шагам", "detailed", 4),
        ("Не надо подробно, коротко скажи главное", "brief", 2),
    ],
)
async def test_answer_budget_and_first_reply_only(tmp_path, question, mode, limit, monkeypatch):
    monkeypatch.setattr("zarya.dialogue.random.random", lambda: 0.0)
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text=question)])
        text = "\n\n".join(f"Мысль {i}. " + "Пояснение. " * 12 for i in range(6))
        engine = DialogueEngine(s, FakeModel(ModelResult(text=text)))
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert answer_mode(question) == mode and max_parts(mode) == limit
        assert detail["snapshot"]["answer_mode"] == mode
        assert detail["snapshot"]["request"]["text"] == {
            "verbosity": "medium" if mode == "detailed" else "low"
        }
        assert len(detail["parts"]) == limit
        assert all(f"Мысль {i}." in " ".join(p["text"] for p in detail["parts"]) for i in range(6))
        assert [
            json.loads(x[0])["reply_id"]
            for x in await s.db.all("SELECT payload FROM outbox ORDER BY part_index")
        ] == [2] + [None] * (limit - 1)
        # Also protect legacy pending rows created before this change.
        async with s.db.transaction() as c:
            await c.execute(
                "UPDATE outbox SET payload=json_set(payload,'$.reply_id',2),next_attempt=0"
            )
        transport = FakeTransport()
        sent_replies = []

        async def send(chat, text, thread, reply_id=None):
            sent_replies.append(reply_id)
            return 100 + len(sent_replies)

        transport.send = send
        runtime = TelegramRuntime(s, transport=transport)
        for _ in range(limit):
            async with s.db.transaction() as c:
                await c.execute("UPDATE delivery_limits SET last_attempt=0")
            await runtime.deliver_one("999")
        assert sent_replies == [2] + [None] * (limit - 1)
        recorded = await engine.replay(work["run_id"], "recorded")
        assert len(recorded["parts"]) == limit


def test_budget_repartition_preserves_utf16_text():
    text = "🙂" * 4000
    parts = chunks(text, 2)
    assert len(parts) == 2 and "".join(parts) == text
    assert all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)


@pytest.mark.parametrize(("draw", "count"), [(0.0, 2), (0.699, 2), (0.7, 1), (0.99, 1)])
async def test_short_paragraphs_follow_saved_probability(tmp_path, monkeypatch, draw, count):
    monkeypatch.setattr("zarya.dialogue.random.random", lambda: draw)
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="Чем факт отличается от мнения?")])
        text = "Факт можно проверить.\n\nМнение - твоя оценка."
        engine = DialogueEngine(s, FakeModel(ModelResult(text=text)))
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert len(detail["parts"]) == count
        assert detail["snapshot"]["delivery_layout"]["split_paragraphs"] == (draw < 0.7)
        assert "\n\n".join(p["text"] for p in detail["parts"]) == text
        # Recorded replay and repeated diagnostics do not draw another layout.
        monkeypatch.setattr("zarya.dialogue.random.random", lambda: 0.99 if draw < 0.7 else 0.0)
        recorded = await engine.replay(work["run_id"], "recorded")
        assert [p["text"] for p in recorded["parts"]] == [p["text"] for p in detail["parts"]]
        assert recorded["snapshot"]["delivery_layout"] == detail["snapshot"]["delivery_layout"]
        paid = await engine.replay(work["run_id"], "paid", 1)
        assert len(paid["parts"]) == (1 if draw < 0.7 else 2)


async def test_persona_save_applies_to_next_claim_and_paid_replay(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.db.update_settings(1, Settings(persona="Характер до сохранения"))
        await s.ingest("999", "zarya_test", [event(2, text="Привет")])
        model = FakeModel()
        engine = DialogueEngine(s, model)
        work = await engine.claim("999")
        await s.db.update_settings(2, Settings(persona="Новый характер: любишь короткие шутки"))
        await engine.execute(work)
        assert "Характер до сохранения" in model.calls[0]["instructions"]
        assert "Новый характер" not in model.calls[0]["instructions"]
        await s.ingest("999", "zarya_test", [event(3, text="Теперь объясни подробнее")])
        new_work = await run_one(engine)
        new_detail = await engine.details(new_work["run_id"])
        assert new_detail["snapshot"]["settings_version"] == 3
        assert "Новый характер" in model.calls[1]["instructions"]
        assert new_detail["snapshot"]["answer_mode"] == "detailed"
        paid = await engine.replay(work["run_id"], "paid", 3)
        assert "Новый характер" in model.calls[2]["instructions"]
        assert paid["snapshot"]["answer_mode"] == "brief"


def test_triggers_utf16_forward_and_reply_identity():
    m = event(2, -100, "✨ @zarya_test")["message"]
    m["entities"] = [{"type": "mention", "offset": 2, "length": 11}]
    assert direct_trigger(m, "999", "zarya_test", "Заря") == "mention"
    m["forward_origin"] = {"type": "user"}
    assert direct_trigger(m, "999", "zarya_test", "Заря") == "forwarded"
    m = event(2, -100, "да", reply_to_message={"from": {"id": 888}})["message"]
    assert direct_trigger(m, "999", "zarya_test", "Заря") == "ambient"
    m["reply_to_message"]["from"]["id"] = 999
    assert direct_trigger(m, "999", "zarya_test", "Заря") == "reply"
    assert (
        direct_trigger(event(2, -100, "Зарядка")["message"], "999", "zarya_test", "Заря")
        == "ambient"
    )


def test_cost_cache_write_reasoning_not_double_counted():
    result = FakeModel().result
    assert estimate_cost(result, "gpt-6.1-sol") == pytest.approx(
        (70 * 2 + 20 * 0.1 + 10 * 2.5 + 30 * 10) / 1e6
    )
    assert estimate_cost(ModelResult(), "gpt-6.1-sol") is None
    result.service_tier = "priority"
    assert estimate_cost(result, "gpt-6.1-sol") is None
    parts = chunks("Первый смысловой блок.\n\nВторой блок.")
    assert len(parts) == 2
    assert all(len(p) <= 4096 for p in chunks("слово " * 1500))


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-luna-2026-09-01"])
def test_luna_cost_and_actual_response_model(model):
    result = FakeModel().result
    result.model = model
    assert estimate_cost(result, "gpt-6.1-sol") == pytest.approx(
        (70 * 0.10 + 20 * 0.01 + 10 * 0.125 + 30 * 0.50) / 1e6
    )
    assert pricing_profile(model) == "luna-standard-2026-10-07"
    result.model = "gpt-6.1-sol"
    assert estimate_cost(result, "gpt-6-luna") == pytest.approx(
        (70 * 2 + 20 * 0.1 + 10 * 2.5 + 30 * 10) / 1e6
    )
    result.model = "gpt-6-luna-unknown"
    assert estimate_cost(result, "gpt-6-luna") is None
    assert pricing_profile(result.model) is None


async def test_model_switch_keeps_snapshots_and_replay_pricing(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        assert (await s.db.settings()).settings.model == "gpt-6-luna"
        await s.ingest("999", "zarya_test", [event(2, text="Привет")])
        engine = DialogueEngine(s, FakeModel())
        work = await engine.claim("999")
        await s.db.update_settings(1, Settings(model="gpt-6.1-sol", reasoning="medium"))
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert detail["call"]["model"] == "gpt-6-luna"
        assert detail["call"]["pricing"] == "luna-standard-2026-10-07"
        assert detail["call"]["cost_usd"] == pytest.approx(0.00002345)
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["snapshot"]["request"]["model"] == "gpt-6-luna"
        paid = await engine.replay(work["run_id"], "paid", 2)
        assert paid["call"]["model"] == "gpt-6.1-sol"
        assert paid["snapshot"]["request"]["reasoning"] == {"effort": "medium"}
        assert paid["call"]["pricing"] == "sol-standard-2026-10-07"
        assert paid["call"]["cost_usd"] == pytest.approx(0.000467)


async def test_private_generation_owner_and_bounded_context(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.db.update_settings(1, Settings(owner_telegram_id="42", owner_username="@someone"))
        await s.ingest("999", "zarya_test", [event(2, text="Привет, есть вопрос")])
        model = FakeModel()
        engine = DialogueEngine(s, model)
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert detail["snapshot"]["owner"] is True
        assert detail["snapshot"]["settings_version"] == 2
        assert len(model.calls) == 1 and model.calls[0]["store"] is False
        assert model.calls[0]["reasoning"] == {"effort": "low"}
        assert detail["call"]["cost_usd"] is not None
        assert [p["state"] for p in detail["parts"]] == ["pending", "pending"]
        assert [
            json.loads(p[0])["reply_id"]
            for p in await s.db.all("SELECT payload FROM outbox ORDER BY part_index")
        ] == [2, None]
        assert not await engine.claim("999")


async def test_common_topic_context_private_separate_and_nested_reply_excluded(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, -100, "group")])
        await approve(s, "group", "-100")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(3, text="личный секрет"),
                event(4, -100, "тема из соседнего топика", message_thread_id=10),
                event(
                    5,
                    -100,
                    "Заря, поясни",
                    message_thread_id=20,
                    reply_to_message={"message_id": 9999, "text": "не принятый секрет"},
                ),
            ],
        )
        engine = DialogueEngine(s, FakeModel())
        await drain(engine)
        detail = await engine.details((await s.db.one("SELECT MAX(id) FROM dialogue_runs"))[0])
        raw = json.dumps(detail["snapshot"]["context"], ensure_ascii=False)
        assert "соседнего топика" in raw and "личный секрет" not in raw
        assert "не принятый секрет" not in raw
        assert detail["thread_id"] == 20
        assert all(
            json.loads(x[0])["thread_id"] == 20
            for x in await s.db.all("SELECT payload FROM outbox WHERE chat_id='-100'")
        )


async def test_edited_question_cancels_generation_and_no_duplicate_context(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="старый вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await engine.claim("999")
        edited = event(3, text="исправленный вопрос", message_id=2)
        edited["edited_message"] = edited.pop("message")
        await s.ingest("999", "zarya_test", [edited])
        await engine.execute(work)
        await drain(engine)
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0
        assert await s.db.one("SELECT text FROM recent_messages WHERE message_id=2") == (
            "исправленный вопрос",
        )
        assert (await engine.details(work["run_id"]))["state"] == "cancelled"
        assert (await engine.details(work["run_id"]))["call"]["cost_usd"] is not None
        assert len(engine.adapter.calls) == 1


async def test_revoke_while_model_waits_preserves_cost_no_delivery(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="привет")])
        engine = DialogueEngine(s, FakeModel())
        work = await engine.claim("999")
        await s.decide("999", "private", "42", "revoked", 2)
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "cancelled" and detail["call"]["cost_usd"] is not None
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0


async def test_restart_after_paid_claim_marks_unknown_never_replays(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        work = await DialogueEngine(s, FakeModel()).claim("999")
        assert not work["skip"]
    async with store_at(tmp_path) as s:
        engine = DialogueEngine(s, FakeModel())
        assert await engine.claim("999") is None
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "unknown" and detail["call"]["state"] == "unknown"
        assert detail["call"]["cost_usd"] is None
        assert (await s.db.one("SELECT state FROM jobs"))[0] == "unknown"


async def test_delivery_rate_order_unknown_stops_series_and_fresh_context(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
        runtime = TelegramRuntime(s, transport=FakeTransport())
        await runtime.deliver_one("999")
        await runtime.deliver_one("999")
        assert len(runtime.transport.sent) == 1
        assert (await s.db.one("SELECT COUNT(*) FROM recent_messages WHERE role='assistant'"))[
            0
        ] == 1
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE delivery_limits SET last_attempt=0")
        runtime.transport.fail_send = TimeoutError()
        await runtime.deliver_one("999")
        assert [p["state"] for p in (await engine.details(work["run_id"]))["parts"]] == [
            "sent",
            "unknown",
        ]
        assert (await s.db.one("SELECT COUNT(*) FROM recent_messages WHERE role='assistant'"))[
            0
        ] == 1


async def test_new_direct_question_cancels_remaining_chunks(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        await s.ingest("999", "zarya_test", [event(3, text="уточнение")])
        assert all(
            p["state"] == "cancelled" for p in (await engine.details(work["run_id"]))["parts"]
        )


async def test_group_continuation_same_sender_topic_and_bounded(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest("999", "zarya_test", [event(2, -100, "Заря, расскажи", message_thread_id=7)])
        engine = DialogueEngine(s, FakeModel(ModelResult(text="Короткий ответ.")))
        await run_one(engine)
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
        await TelegramRuntime(s, transport=FakeTransport()).deliver_one("999")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(3, -100, "поясни", message_thread_id=8),
                event(
                    4,
                    -100,
                    "ещё",
                    message_thread_id=7,
                    **{"from": {"id": 77, "first_name": "Другой"}},
                ),
                event(5, -100, "а почему?", message_thread_id=7),
            ],
        )
        await drain(engine)
        triggers = dict(await s.db.all("SELECT message_id,trigger FROM dialogue_runs"))
        assert triggers[3] == triggers[4] == "ambient" and triggers[5] == "continuation"
        assert len(engine.adapter.calls) == 2


async def test_replay_does_not_mutate_working_memory_jobs_outbox(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        before = [
            await s.db.all(f"SELECT * FROM {t}")
            for t in ("jobs", "outbox", "recent_messages", "active_dialogues", "telegram_state")
        ]
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["mode"] == "recorded" and len(engine.adapter.calls) == 1
        paid = await engine.replay(work["run_id"], "paid", 1)
        assert paid["mode"] == "paid" and paid["call"]["cost_usd"] is not None
        assert len(engine.adapter.calls) == 2
        after = [
            await s.db.all(f"SELECT * FROM {t}")
            for t in ("jobs", "outbox", "recent_messages", "active_dialogues", "telegram_state")
        ]
        assert before == after
        assert all(p["state"] == "test_only" for p in paid["parts"])


async def test_api_replay_auth_csrf_bot_scope_and_error_sanitized(tmp_path):
    model = FakeModel(ModelResult(state="unknown", error="connection_uncertain"))
    app = create_app(Config(data_dir=tmp_path, web_dir=tmp_path / "web"), model_adapter=model)
    async with app.router.lifespan_context(app):
        app.state.telegram.bot = await FakeTransport().identity()
        s, engine = app.state.telegram_store, app.state.dialogue
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        work = await run_one(engine)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8787",
            headers={"Origin": "http://127.0.0.1:8787"},
        ) as c:
            assert (await c.get("/api/dialogues")).status_code == 401
            setup = await c.post(
                "/api/setup",
                json={
                    "token": (tmp_path / "bootstrap-token.txt").read_text(),
                    "password": "test-password-123",
                },
            )
            assert (
                await c.post("/api/dialogues/replay", json={"run_id": work["run_id"]})
            ).status_code == 403
            c.headers["X-CSRF-Token"] = setup.json()["csrf"]
            data = (await c.get("/api/dialogues")).json()
            assert data["unknown_cost_calls"] == 1 and data["known_cost_usd"] is None
            assert (
                await c.post("/api/dialogues/replay", json={"run_id": work["run_id"]})
            ).status_code == 409
            assert (await c.get("/api/dialogues/99999")).status_code == 404
            for version in (None, 999):
                payload = {"run_id": work["run_id"], "mode": "paid"}
                if version is not None:
                    payload["expected_settings_version"] = version
                assert (await c.post("/api/dialogues/replay", json=payload)).status_code == 409
            assert len(model.calls) == 1
    assert model.closed


async def test_real_adapter_request_contract_offline():
    adapter = OpenAIAdapter("test-local-key")
    captured = []

    class Response:
        status = "completed"
        output_text = "Ответ"
        usage = None
        id = "test-response"
        _request_id = "test-request"
        service_tier = "default"
        model = "gpt-6.1-sol"

    async def create(**request):
        captured.append(request)
        return Response()

    adapter.client.responses.create = create
    try:
        result = await adapter.generate(
            {
                "model": "gpt-6.1-sol",
                "store": False,
                "reasoning": {"effort": "low"},
                "input": "Привет",
                "max_output_tokens": 2500,
            }
        )
        assert adapter.client.max_retries == 0 and result.text == "Ответ"
        assert captured[0]["store"] is False
    finally:
        await adapter.close()


async def test_disable_during_generation_and_pending_delivery(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await engine.claim("999")
        await s.db.update_settings(1, Settings(dialogue_enabled=False))
        await engine.execute(work)
        assert (await engine.details(work["run_id"]))["state"] == "cancelled"
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0
        await s.db.update_settings(2, Settings())
        await s.ingest("999", "zarya_test", [event(3, text="новый вопрос")])
        await run_one(engine)
        await s.db.update_settings(3, Settings(dialogue_enabled=False))
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
        t = FakeTransport()
        await TelegramRuntime(s, transport=t).deliver_one("999")
        assert t.sent == []
        assert all(x[0] == "cancelled" for x in await s.db.all("SELECT state FROM outbox"))


async def test_restart_in_middle_of_series_cancels_tail(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        await run_one(DialogueEngine(s, FakeModel()))
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET state='sending' WHERE part_index=0")
    async with store_at(tmp_path) as s:
        assert await s.db.all("SELECT state FROM outbox ORDER BY part_index") == [
            ("unknown",),
            ("cancelled",),
        ]
        await s.dismiss("999", 1)
        t = FakeTransport()
        await TelegramRuntime(s, transport=t).deliver_one("999")
        assert not t.sent


async def test_group_delivery_limit_shared_across_topics(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(2, -100, "Заря, вопрос", message_thread_id=7),
                event(
                    3,
                    -100,
                    "Заря, второй",
                    message_thread_id=8,
                    **{"from": {"id": 77, "first_name": "Второй"}},
                ),
            ],
        )
        engine = DialogueEngine(s, FakeModel(ModelResult(text="Ответ")))
        await drain(engine)
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
        t = FakeTransport()
        r = TelegramRuntime(s, transport=t)
        await r.deliver_one("999")
        await r.deliver_one("999")
        assert len(t.sent) == 1
        last = (await s.db.one("SELECT last_attempt FROM delivery_limits"))[0]
        async with s.db.transaction() as c:
            await c.execute("UPDATE delivery_limits SET last_attempt=?", (last - 2,))
        await r.deliver_one("999")
        assert len(t.sent) == 1
        async with s.db.transaction() as c:
            await c.execute("UPDATE delivery_limits SET last_attempt=?", (last - 4,))
        await r.deliver_one("999")
        assert len(t.sent) == 2 and {x[2] for x in t.sent} == {7, 8}


async def test_cancelled_model_still_serializes_same_chat_and_new_context(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(s, FakeModel())
        first = await engine.claim("999")
        await s.ingest("999", "zarya_test", [event(3, text="уточнение")])
        assert await engine.claim("999") is None
        await engine.execute(first)
        second = await engine.claim("999")
        assert second["reply_id"] == 3
        await engine.execute(second)
        detail = await engine.details(second["run_id"])
        assert detail["snapshot"]["incoming"]["text"] == "уточнение"
        assert all(m["role"] != "assistant" for m in detail["snapshot"]["context"])


async def test_continuation_cancels_remaining_direct_answer(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest("999", "zarya_test", [event(2, -100, "Заря, вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
        await TelegramRuntime(s, transport=FakeTransport()).deliver_one("999")
        await s.ingest("999", "zarya_test", [event(3, -100, "а поясни")])
        assert [p["state"] for p in (await engine.details(work["run_id"]))["parts"]] == [
            "sent",
            "cancelled",
        ]
        new = await engine.claim("999")
        assert (await engine.details(new["run_id"]))["trigger"] == "continuation"


async def test_runtime_typing_reply_target_and_worker_shutdown(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.db.create_admin("test-hash")
        await s.ingest("999", "zarya_test", [event(2, text="Привет")])
        entered = asyncio.Event()
        release = asyncio.Event()
        model = FakeModel()

        async def generate(request):
            model.calls.append(request)
            entered.set()
            await release.wait()
            return model.result

        model.generate = generate
        typed, replies = [], []
        transport = FakeTransport()

        async def typing(chat, thread):
            typed.append((chat, thread))
            return True

        async def send(chat, text, thread, reply_id=None):
            replies.append(reply_id)
            return 123

        transport.typing, transport.send = typing, send
        engine = DialogueEngine(s, model)
        runtime = TelegramRuntime(s, transport=transport, dialogue=engine)
        runtime.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(20):
                if typed:
                    break
                await asyncio.sleep(0.02)
            assert typed == [("42", None)]
            release.set()
            for _ in range(150):
                if replies:
                    break
                await asyncio.sleep(0.02)
            assert replies == [2]
            assert runtime.healthy
        finally:
            await runtime.stop()
        assert transport.closed and not runtime.tasks


async def test_nonforum_reply_thread_is_not_a_topic(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        incoming = event(
            2,
            -100,
            "Ещё пример",
            message_thread_id=6,
            reply_to_message={"message_id": 6, "from": {"id": 999}},
        )
        incoming["message"]["chat"]["is_forum"] = False
        assert topic_id(incoming["message"]) is None
        await s.ingest("999", "zarya_test", [incoming])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert detail["trigger"] == "reply" and detail["thread_id"] is None
        assert detail["snapshot"]["incoming"]["thread_id"] is None
        assert all(
            json.loads(r[0])["thread_id"] is None
            for r in await s.db.all("SELECT payload FROM outbox")
        )
        async with s.gate:
            pulse = await s.claim_typing("999")
        assert pulse["thread_id"] is None
        # Original accepted update remains available for diagnosis without changing its raw fields.
        raw = json.loads((await s.db.one("SELECT payload FROM events WHERE update_id=2"))[0])
        assert raw["message"]["message_thread_id"] == 6


async def test_typing_survives_generation_and_renews_between_parts_then_stops(tmp_path):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="Вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        t = FakeTransport()
        pulses = []

        async def typing(chat, thread):
            pulses.append((chat, thread))
            return True

        t.typing = typing
        runtime = TelegramRuntime(s, transport=t)
        await runtime.type_one("999")
        await runtime.type_one("999")
        assert len(pulses) == 1
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
        await runtime.deliver_one("999")
        await runtime.type_one("999")
        assert len(pulses) == 2
        async with s.db.transaction() as c:
            await c.execute("UPDATE delivery_limits SET last_attempt=0")
        await runtime.deliver_one("999")
        await runtime.type_one("999")
        assert len(pulses) == 2
        info = (await engine.details(work["run_id"]))["typing"]
        assert info["state"] == "accepted" and info["attempts"] == 2


async def test_typing_cooldown_disables_both_action_and_delivery(tmp_path):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendChatAction

    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="Вопрос")])
        await run_one(DialogueEngine(s, FakeModel()))
        attempts = []
        t = FakeTransport()

        async def typing(*args):
            attempts.append(args)
            raise TelegramRetryAfter(
                method=SendChatAction(chat_id=42, action="typing"), message="wait", retry_after=10
            )

        t.typing = typing
        runtime = TelegramRuntime(s, transport=t)
        await runtime.type_one("999")
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
            await c.execute("UPDATE jobs SET typing_due=0")
        await runtime.type_one("999")
        await runtime.deliver_one("999")
        assert len(attempts) == 1 and t.sent == []
        assert (await s.db.one("SELECT typing_state FROM jobs"))[0] == "rate_limited"


@pytest.mark.parametrize("condition", ["revoke", "disable", "expired", "replay"])
async def test_typing_suppression_conditions(tmp_path, condition):
    async with store_at(tmp_path) as s:
        await private_ready(s)
        await s.ingest("999", "zarya_test", [event(2, text="Вопрос")])
        engine = DialogueEngine(s, FakeModel())
        work = await run_one(engine)
        if condition == "revoke":
            await s.decide("999", "private", "42", "revoked", 2)
        elif condition == "disable":
            await s.db.update_settings(1, Settings(dialogue_enabled=False))
        elif condition == "expired":
            async with s.db.transaction() as c:
                await c.execute("UPDATE dialogue_runs SET created_at='2000-01-01T00:00:00+00:00'")
        else:
            await engine.replay(work["run_id"], "paid", 1)
            async with s.db.transaction() as c:
                await c.execute("UPDATE outbox SET state='sent'")
        async with s.gate:
            assert await s.claim_typing("999") is None


async def test_typing_rejection_no_retries_deduplicates_runs_per_chat(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(2, -100, "Заря, вопрос"),
                event(3, -100, "Заря, другой", **{"from": {"id": 77, "first_name": "Другой"}}),
            ],
        )
        await drain(DialogueEngine(s, FakeModel()))
        t = FakeTransport()
        pulses = []

        async def typing(*args):
            pulses.append(args)
            return False

        t.typing = typing
        runtime = TelegramRuntime(s, transport=t)
        await runtime.type_one("999")
        await runtime.type_one("999")
        assert len(pulses) == 1
        assert (await s.db.one("SELECT COUNT(*) FROM jobs WHERE typing_state='rejected'"))[0] == 1
