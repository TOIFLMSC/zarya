import asyncio
import copy
import json

import httpx
import pytest
from openai import APIStatusError
from test_dialogue import FakeModel, private_ready, run_one
from test_participant_context import group_ready, human_event
from test_telegram import FakeTransport, event, store_at

from zarya import context_budget
from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult, OpenAIAdapter, TokenCountResult
from zarya.operations import Operations
from zarya.telegram_runtime import TelegramRuntime


class Counter(FakeModel):
    def __init__(self, count=100, state="completed"):
        super().__init__(
            ModelResult(text="Короткий ответ", usage={"input_tokens": 100, "output_tokens": 10})
        )
        self.count, self.state, self.counts = count, state, []

    async def count_input(self, request):
        self.counts.append(copy.deepcopy(request))
        return TokenCountResult(
            self.state,
            self.count if self.state == "completed" else None,
            "count_uncertain" if self.state == "unknown" else None,
        )


def profile(monkeypatch):
    monkeypatch.setitem(context_budget.PROFILES, "gpt-6-luna", (22000, 128000))


async def large(store, engine):
    await private_ready(store)
    await store.ingest("999", "zarya_test", [event(2, text="объясни " + "x" * 20000)])
    work = await engine.claim("999")
    assert work and not work["skip"]
    return work


async def test_normal_fast_path_preserves_full_wire_input_and_no_count(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет, Заря")])
        model = Counter()
        engine = DialogueEngine(store, model)
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        budget = detail["snapshot"]["context_budget"]
        assert not model.counts and len(model.calls) == 1
        assert budget["request_hash"] == context_budget.fingerprint(model.calls[0])
        assert budget["provider_attempted"] and budget["state"] == "fits_estimate"
        assert model.calls[0]["truncation"] == "disabled"
        assert json.loads(model.calls[0]["input"])["current_message"]["text"] == "Привет, Заря"


@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_exact_boundary_count_and_single_service_without_generate(
    tmp_path, monkeypatch, delta
):
    profile(monkeypatch)
    model = Counter(22000 - 2500 - context_budget.MARGIN + delta)
    async with store_at(tmp_path) as store:
        engine = DialogueEngine(store, model)
        work = await large(store, engine)
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        budget = detail["snapshot"]["context_budget"]
        assert len(model.counts) == 1 and len(model.calls) == int(delta <= 0)
        assert budget["api_count"]["tokens"] == model.count
        assert budget["state"] == ("fits_api" if delta <= 0 else "overflow")
        count_row = await store.db.one(
            "SELECT request_json FROM model_calls WHERE context_count_run_id=?", (work["run_id"],)
        )
        assert count_row[0] is None
        if delta > 0:
            assert detail["call"]["state"] == "rejected" and detail["call"]["cost_usd"] == 0
            assert detail["call"]["latency_ms"] is None and not detail["response"]
            assert len(detail["parts"]) == 1
            assert not await engine.claim("999")
            await engine.execute(work)
            assert len(model.counts) == 1  # terminal work cannot be counted/retried again
            assert (await store.db.one("SELECT COUNT(*) FROM outbox"))[0] == 1
            transport = FakeTransport()
            runtime = TelegramRuntime(store, dialogue=engine, transport=transport)
            await runtime.deliver_one("999")
            assert len(transport.sent) == 1 and "Контекст не помещается" in transport.sent[0][1]
        else:
            assert budget["request_hash"] == context_budget.fingerprint(model.counts[0])
            assert model.counts[0] == model.calls[0]
        data = await Operations(store.db).overview(bot="999", scope="private")
        assert "context_count" in {r["task"] for r in data["calls"]}
        assert data["summary"]["unknown_cost_calls"] == 1


@pytest.mark.parametrize("state", ["unknown", "rejected", "overflow"])
async def test_count_failure_no_retry_no_generation_and_unknown_cost(tmp_path, monkeypatch, state):
    profile(monkeypatch)
    model = Counter(state=state)
    async with store_at(tmp_path) as store:
        engine = DialogueEngine(store, model)
        work = await large(store, engine)
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert len(model.counts) == 1 and not model.calls
        assert not detail["snapshot"]["context_budget"]["provider_attempted"]
        assert detail["error"] == (
            "context_overflow" if state == "overflow" else "budget_unverified"
        )
        assert len(detail["parts"]) == 1
        count = await store.db.one(
            "SELECT state,cost_usd FROM model_calls WHERE context_count_run_id=?", (work["run_id"],)
        )
        assert count == (state, None)
        await store.recover("999")
        assert await engine.claim("999") is None


@pytest.mark.parametrize("change", ["revoke", "edit", "forget", "disabled", "restart"])
async def test_change_during_count_blocks_generation_without_restoring_snapshot(
    tmp_path, monkeypatch, change
):
    profile(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Counter):
        async def count_input(self, request):
            self.counts.append(copy.deepcopy(request))
            entered.set()
            await release.wait()
            return TokenCountResult("completed", 100)

    async with store_at(tmp_path) as store:
        model = Slow()
        engine = DialogueEngine(store, model)
        work = await large(store, engine)
        task = asyncio.create_task(engine.execute(work))
        await asyncio.wait_for(entered.wait(), 2)
        if change == "revoke":
            peer = await store.db.one("SELECT version FROM telegram_access WHERE subject_id='42'")
            await store.decide("999", "private", "42", "revoked", peer[0])
        elif change == "edit":
            updated = event(2, text="Новая версия источника")
            await store.ingest(
                "999", "zarya_test", [{"update_id": 3, "edited_message": updated["message"]}]
            )
        elif change == "forget":
            async with store.db.transaction() as c:
                await c.execute(
                    "UPDATE events SET payload='{}' WHERE id=(SELECT event_id "
                    "FROM jobs WHERE id=?)",
                    (work["job_id"],),
                )
        elif change == "disabled":
            saved = await store.db.settings()
            await store.db.update_settings(
                saved.version, saved.settings.model_copy(update={"dialogue_enabled": False})
            )
        else:
            await store.recover("999")
        release.set()
        await task
        assert not model.calls
        assert not await store.db.all("SELECT id FROM outbox WHERE job_id=?", (work["job_id"],))
        main = await store.db.one(
            "SELECT state,cost_usd,latency_ms FROM model_calls WHERE run_id=?", (work["run_id"],)
        )
        assert main[0] in {"cancelled", "rejected"} and main[1:] == (0, None)
        stats = await Operations(store.db).overview(bot="999")
        assert stats["summary"]["unknown_cost_calls"] == 1  # only the counter, not generation
        if change in {"revoke", "edit"}:
            snapshot = json.loads(
                (
                    await store.db.one(
                        "SELECT snapshot FROM dialogue_runs WHERE id=?", (work["run_id"],)
                    )
                )[0]
            )
            assert snapshot["invalidated"] and "context_budget" not in snapshot


async def test_cancel_during_count_records_only_counter_as_unknown(tmp_path, monkeypatch):
    profile(monkeypatch)
    entered = asyncio.Event()

    class Slow(Counter):
        async def count_input(self, request):
            self.counts.append(copy.deepcopy(request))
            entered.set()
            await asyncio.Event().wait()

    async with store_at(tmp_path) as store:
        model = Slow()
        engine = DialogueEngine(store, model)
        work = await large(store, engine)
        task = asyncio.create_task(engine.execute(work))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        main = await store.db.one(
            "SELECT state,cost_usd,latency_ms,error_code FROM model_calls WHERE run_id=?",
            (work["run_id"],),
        )
        assert main == ("rejected", 0, None, "preflight_interrupted")
        count = await store.db.one(
            "SELECT state,cost_usd,error_code FROM model_calls WHERE context_count_run_id=?",
            (work["run_id"],),
        )
        assert count == ("unknown", None, "interrupted")
        assert not model.calls and not await store.db.all("SELECT id FROM outbox")
        assert (await Operations(store.db).overview(bot="999"))["summary"][
            "unknown_cost_calls"
        ] == 1
        await store.recover("999")
        assert not await engine.claim("999")


async def test_n100_and_reply_participants_survive_count_without_optional_trimming(
    tmp_path, monkeypatch
):
    profile(monkeypatch)
    async with store_at(tmp_path) as store:
        await group_ready(store)
        s = await store.db.settings()
        await store.db.update_settings(
            s.version, s.settings.model_copy(update={"participant_context_messages": 100})
        )
        posts = [human_event(i, text="не менять 153.14 USDT " + "x" * 1800) for i in range(2, 103)]
        await store.ingest("999", "zarya_test", posts)
        # Skip addressed-less work; one actual addressed request selects its exact window.
        async with store.db.transaction() as c:
            await c.execute("UPDATE jobs SET state='done'")
        question = human_event(
            103, text="Заря, посчитай по сообщению выше", reply_to_message=posts[-1]["message"]
        )
        await store.ingest("999", "zarya_test", [question])
        model = Counter()
        engine = DialogueEngine(store, model)
        work = await run_one(engine)
        data = json.loads(model.calls[0]["input"])
        assert len([m for m in data["recent_chat"] if m["role"] == "user"]) == 99
        assert work["snapshot"]["participant_context"]["participants"][0]["human_messages"] == 100
        assert data["selected_material"]["messages"][-1]["text"] == posts[-1]["message"]["text"]
        assert len(work["snapshot"]["context_refs"]["humans"]) == 100
        assert len(model.counts) == 1 and not work["snapshot"]["context_budget"]["excluded"]


def test_full_budget_accounts_instructions_schema_sources_and_image_not_base64():
    request = dict(
        model="gpt-6-luna",
        max_output_tokens=2500,
        instructions="private instruction",
        input=json.dumps(
            {
                "current_message": {"text": "Вопрос"},
                "recent_chat": [],
                "selected_material": {"messages": [{"text": "Пост"}]},
                "research": {"summary": "Анализ"},
                "behavior": {"mode": "addressed"},
                "memory": {"facts": [{"text": "Факт"}]},
            }
        ),
        text={"format": {"type": "json_schema", "schema": {"a": "b" * 500}}},
    )
    original = copy.deepcopy(request)
    budget = context_budget.inspect(request)
    assert {s["kind"] for s in budget["sections"]} >= {
        "instructions",
        "current_message",
        "material",
        "memory",
        "research",
        "behavior",
        "schema_tools",
    }
    assert "private instruction" not in json.dumps(budget)
    assert request == original
    wire = context_budget.pack(request)
    assert json.loads(wire["input"]) == json.loads(request["input"])
    wire["input"] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": wire["input"]},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + "A" * 2000000,
                    "detail": "auto",
                },
            ],
        }
    ]
    image = context_budget.inspect(wire, expected_images=2)
    assert image["estimate"] < 2000000 and image["state"] == "fits_estimate"
    assert image["excluded"] == [{"kind": "images", "count": 1, "reason": "image_byte_limit"}]
    assert "base64" not in json.dumps(image)


async def test_counter_mutating_request_cannot_bypass_measured_fingerprint(tmp_path, monkeypatch):
    profile(monkeypatch)

    class Mutating(Counter):
        async def count_input(self, request):
            request["instructions"] += "changed"
            return await super().count_input(request)

    async with store_at(tmp_path) as store:
        model = Mutating()
        engine = DialogueEngine(store, model)
        work = await large(store, engine)
        await engine.execute(work)
        assert not model.calls
        assert (await engine.details(work["run_id"]))["error"] == "budget_unverified"


async def test_paid_replay_new_profile_checks_and_recorded_does_not_count(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        engine = DialogueEngine(store, Counter())
        work = await large(store, engine)  # large under real million-token profile
        await engine.execute(work)
        old = (await engine.details(work["run_id"]))["snapshot"]["context_budget"]
        assert old["state"] == "fits_estimate"
        profile(monkeypatch)
        record = await engine.replay(work["run_id"], "recorded")
        assert record["snapshot"]["context_budget"] == old and not engine.adapter.counts
        engine.adapter.count = 50000
        before = (await store.db.one("SELECT COUNT(*) FROM outbox"))[0]
        paid = await engine.replay(work["run_id"], "paid", (await store.db.settings()).version)
        assert paid["error"] == "context_overflow" and len(engine.adapter.calls) == 1
        assert len(engine.adapter.counts) == 1
        assert (await store.db.one("SELECT COUNT(*) FROM outbox"))[0] == before


async def test_restart_before_generation_is_not_paid_unknown_and_never_resubmits(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="вопрос")])
        engine = DialogueEngine(store, Counter())
        work = await engine.claim("999")
        await store.recover("999")
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "rejected" and detail["call"]["state"] == "rejected"
        assert (
            detail["call"]["cost_usd"] == 0
            and not detail["snapshot"]["context_budget"]["provider_attempted"]
        )
        assert await engine.claim("999") is None
        assert not engine.adapter.calls and not engine.adapter.counts


async def test_real_count_adapter_filters_contract_and_sanitizes_overflow_offline():
    adapter = OpenAIAdapter("test-local-key")
    captured = []

    class Count:
        input_tokens = 123

    async def count(**kwargs):
        captured.append(kwargs)
        return Count()

    adapter.client.responses.input_tokens.count = count
    try:
        request = {
            "model": "gpt-6-luna",
            "input": "Private text",
            "instructions": "rules",
            "text": {"format": {"type": "json_schema"}},
            "reasoning": {"effort": "low"},
            "store": False,
            "max_output_tokens": 2500,
            "truncation": "disabled",
        }
        result = await adapter.count_input(request)
        assert result.tokens == 123 and adapter.client.max_retries == 0
        assert set(captured[0]) == set(request) - {"store", "max_output_tokens"}

        async def failure(**kwargs):
            raise APIStatusError(
                "PRIVATE_ERROR",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
                ),
                body={"error": {"code": "context_length_exceeded", "message": "PRIVATE_ERROR"}},
            )

        adapter.client.responses.input_tokens.count = failure
        assert (await adapter.count_input(request)).state == "overflow"
        adapter.client.responses.create = failure
        rejected = await adapter.generate(request)
        assert rejected.error == "context_overflow" and "PRIVATE" not in repr(rejected)
    finally:
        await adapter.close()
