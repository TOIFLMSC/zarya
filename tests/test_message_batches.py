import json
import time
from types import SimpleNamespace

import pytest
from test_dialogue import FakeModel, drain
from test_photos import ready
from test_telegram import event, store_at

from zarya import dialogue as dialogue_module
from zarya import message_batches
from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.participant_context import assistants


@pytest.fixture(autouse=True)
def reception_clock(monkeypatch):
    now = time.time() + 120
    monkeypatch.setattr(message_batches, "time", SimpleNamespace(time=lambda: now))


async def pilot(store, *, research=False):
    await ready(store, -100, message_batches=True)
    settings = await store.db.settings()
    await store.db.update_settings(
        settings.version,
        settings.settings.model_copy(update={"research_enabled": research}),
    )


async def test_group_default_and_individual_opt_out(tmp_path):
    from zarya.database import ConflictError

    async with store_at(tmp_path) as store:
        await store.ingest("999", "zarya_test", [event(1, -100), event(2, -200), event(3)])
        for chat in ("-100", "-200"):
            await store.decide("999", "group", chat, "approved", 1)
        await store.decide("999", "private", "42", "approved", 1)
        by_chat = {p["id"]: p for p in (await store.peers("999", "group", 0))["items"]}
        assert all(p["message_batches_enabled"] for p in by_chat.values())
        await store.set_message_batches("999", "-100", 2, True, False)
        with pytest.raises(ConflictError):
            await store.set_message_batches("999", "-100", 2, True, True)
        assert (await store.peers("999", "private", 0))["items"][0][
            "message_batches_enabled"
        ] is None
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(4, -100, "Заря, без поиска назови число"),
                event(5, -200, "Заря, без поиска назови цвет"),
                event(6, 42, "Заря, без поиска назови день"),
            ],
        )
        assert await store.db.all("SELECT chat_id FROM text_batches") == [("-200",)]
        await store.decide("999", "group", "-100", "revoked", 2)
        await store.decide("999", "group", "-100", "approved", 3)
        by_chat = {p["id"]: p for p in (await store.peers("999", "group", 0))["items"]}
        assert by_chat["-100"]["version"] == 4
        assert by_chat["-100"]["message_batches_enabled"] is False
        assert by_chat["-200"]["message_batches_enabled"] is True


def text(mid, value, topic=8, sender=42, **extra):
    return event(
        mid,
        -100,
        value,
        message_thread_id=topic,
        **{"from": {"id": sender, "first_name": "Участник"}},
        **extra,
    )


def actions(*parts):
    return ModelResult(
        text=json.dumps(
            {
                "answers": [
                    {"reply_to": mid, "covers": covered, "text": answer}
                    for mid, covered, answer in parts
                ]
            },
            ensure_ascii=False,
        )
    )


async def release_batches(store):
    async with store.db.transaction() as conn:
        await conn.execute("UPDATE text_batches SET ready_at=0")


async def delivery(store):
    async with store.db.transaction() as conn:
        await conn.execute("UPDATE delivery_limits SET last_attempt=0")
        await conn.execute("UPDATE outbox SET next_attempt=0")
    return await store.claim_delivery("999")


async def test_fast_questions_are_both_current_and_target_their_own_replies(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(2, "Заря, сколько будет два плюс два?"),
                text(3, "Заря, какого цвета небо?"),
            ],
        )
        first_answer = "Два плюс два — четыре."
        second_answer = "Обычно голубое — оттенок зависит от погоды."
        model = FakeModel(actions((2, [2], first_answer), (3, [3], second_answer)))
        dialogue = DialogueEngine(store, model)
        await drain(dialogue)
        assert not model.calls
        await release_batches(store)
        work = await dialogue.claim("999")
        assert work and not work["skip"]
        assert (await store.claim_typing("999"))["job_id"] == work["job_id"]
        await dialogue.execute(work)
        assert len(model.calls) == 1
        body = json.loads(model.calls[0]["input"])
        assert [m["message_id"] for m in body["current_messages"]] == [2, 3]
        assert "current_message" not in body
        assert not ({2, 3} & {m["message_id"] for m in body["recent_chat"]})
        first = await delivery(store)
        assert (first["reply_id"], first["text"]) == (2, first_answer)
        await store.delivery_result(first["id"], "sent", message_id=4)
        second = await delivery(store)
        assert (second["reply_id"], second["text"]) == (3, second_answer)
        await store.delivery_result(second["id"], "sent", message_id=5)
        async with store.db.transaction() as conn:
            recent, _ = await assistants(conn, "999", "-100", 2, [{"message_id": 3}])
        assert [(m["message_id"], m["in_reply_to"]) for m in recent] == [(5, 3)]
        model.result = ModelResult(text="Пояснение.")
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(
                    6,
                    "А почему?",
                    reply_to_message={"message_id": 5, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        await drain(dialogue)
        followup = json.loads(model.calls[-1]["input"])
        assert followup["selected_material"]["messages"][0]["message_id"] == 3


@pytest.mark.parametrize("edited_id", [2, 3])
async def test_collects_clarification_and_edits_only_its_revision_before_restart(
    tmp_path, edited_id
):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999",
            "zarya_test",
            [text(2, "Заря, составь план на выходные."), text(3, "Только на воскресенье.")],
        )
        changed = text(edited_id, "Уточнение после правки.")["message"]
        await store.ingest("999", "zarya_test", [{"update_id": 4, "edited_message": changed}])
        await store.recover("999")
        await release_batches(store)
        model = FakeModel(actions((3, [2, 3], "План на воскресенье.")))
        await drain(DialogueEngine(store, model))
        assert len(model.calls) == 1
        items = json.loads(model.calls[0]["input"])["current_messages"]
        assert [m["message_id"] for m in items] == [2, 3]
        assert next(m for m in items if m["message_id"] == edited_id)["text"] == changed["text"]
        assert sum(m["text"] == changed["text"] for m in items) == 1
        combined = await delivery(store)
        assert (combined["reply_id"], combined["covers"]) == (3, [2, 3])
        await store.delivery_result(combined["id"], "sent", message_id=5)
        model.result = ModelResult(text="Поясняю план.")
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(
                    6,
                    "А почему?",
                    reply_to_message={"message_id": 5, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        await drain(DialogueEngine(store, model))
        body = json.loads(model.calls[-1]["input"])
        assert [m["message_id"] for m in body["selected_material"]["messages"]] == [2, 3]


@pytest.mark.parametrize("telegram_seconds", [1, 2])
async def test_clarification_after_1_2_seconds_makes_one_call_at_three_second_cap(
    tmp_path, monkeypatch, telegram_seconds
):
    opened_at = time.time()
    now = [opened_at]
    clock = SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic)
    monkeypatch.setattr(message_batches, "time", clock)
    monkeypatch.setattr(dialogue_module, "time", clock)
    async with store_at(tmp_path) as store:
        await pilot(store, research=True)
        await store.ingest(
            "999",
            "zarya_test",
            [text(2, "Заря, без поиска придумай три названия для кофейни", date=100)],
        )
        (initial_deadline,) = await store.db.one("SELECT ready_at FROM text_batches")
        now[0] = opened_at + 1.2
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(
                    3,
                    "Уточнение без поиска: только одно название из двух слов",
                    date=100 + telegram_seconds,
                )
            ],
        )
        assert await store.db.all("SELECT COUNT(*) FROM text_batch_members GROUP BY job_id") == [
            (2,)
        ]
        assert initial_deadline == pytest.approx(opened_at + 2, abs=0.000001)
        (ready_at,) = await store.db.one("SELECT ready_at FROM text_batches")
        assert ready_at == pytest.approx(opened_at + 3, abs=0.000001)
        model = FakeModel(actions((3, [2, 3], "Тёплый угол")))
        engine = DialogueEngine(store, model)
        now[0] = opened_at + 2.999
        await drain(engine)
        assert not model.calls
        now[0] = opened_at + 3
        await drain(engine)
        assert len(model.calls) == 1
        (calls,) = await store.db.one("SELECT COUNT(*) FROM model_calls WHERE operation='dialogue'")
        assert calls == 1
        payload = json.loads(model.calls[0]["input"])
        assert [m["message_id"] for m in payload["current_messages"]] == [2, 3]
        (outgoing,) = await store.db.one("SELECT payload FROM outbox")
        answer = json.loads(outgoing)
        assert (answer["text"], answer["reply_id"], answer["covers"]) == (
            "Тёплый угол",
            3,
            [2, 3],
        )


async def test_deadlines_and_scope_do_not_merge_two_authors_topics_or_late_members(
    tmp_path, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(message_batches, "time", SimpleNamespace(time=lambda: now[0]))
    async with store_at(tmp_path) as store:
        await pilot(store)
        for mid, at in enumerate((1000, 1000.7, 1001.4, 1002.1, 1002.8, 1003.01), 2):
            now[0] = at
            await store.ingest("999", "zarya_test", [text(mid, f"Заря, вопрос {mid}")])
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(8, "Заря, другой топик", topic=9),
                text(9, "Заря, другой автор", sender=65),
                text(10, "Я просто рядом.", sender=66),
            ],
        )
        batches = await store.db.all(
            "SELECT opened_at,ready_at,thread_id,sender_id,(SELECT COUNT(*) FROM "
            "text_batch_members m WHERE m.job_id=b.job_id) FROM text_batches b ORDER BY job_id"
        )
        assert tuple(batches[0]) == (1000, 1003, 8, "42", 5)
        assert [tuple(b[2:]) for b in batches[1:]] == [(8, "42", 1), (9, "42", 1), (8, "65", 1)]


@pytest.mark.parametrize("restart", [False, True])
async def test_no_paid_retry_after_started_batch_or_edit(tmp_path, restart):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999", "zarya_test", [text(2, "Заря, назови цвет"), text(3, "Заря, назови число")]
        )
        await release_batches(store)
        model = FakeModel(actions((2, [2], "Первый"), (3, [3], "Второй")))
        engine = DialogueEngine(store, model)
        work = await engine.claim("999")
        if restart:
            assert await engine.preflight(work, work["request"]) is None
            await store.recover("999")
            assert (await engine.details(work["run_id"]))["call"]["state"] == "unknown"
        else:
            await store.ingest(
                "999",
                "zarya_test",
                [
                    {
                        "update_id": 4,
                        "edited_message": text(3, "Заря, исправленное второе")["message"],
                    }
                ],
            )
            await engine.execute(work)
        await drain(engine)
        assert not model.calls
        assert not await store.db.all("SELECT 1 FROM outbox")


async def test_invalid_batch_target_fails_closed_without_wrong_reply(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999", "zarya_test", [text(2, "Заря, назови цвет"), text(3, "Заря, назови число")]
        )
        await release_batches(store)
        model = FakeModel(actions((98, [2, 3], "Нельзя отправлять")))
        await drain(DialogueEngine(store, model))
        assert len(model.calls) == 1
        delivered = await delivery(store)
        assert delivered["reply_id"] == 2 and delivered["text"] == message_batches.INVALID_RESPONSE


@pytest.mark.parametrize("first_state", ["sent", "unknown"])
async def test_partial_delivery_or_uncertain_send_never_retries_the_batch(tmp_path, first_state):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999", "zarya_test", [text(2, "Заря, назови цвет"), text(3, "Заря, назови число")]
        )
        await release_batches(store)
        model = FakeModel(actions((2, [2], "Синий"), (3, [3], "Четыре")))
        engine = DialogueEngine(store, model)
        await drain(engine)
        first = await delivery(store)
        await store.delivery_result(first["id"], first_state, message_id=4)
        if first_state == "sent":
            await store.ingest(
                "999",
                "zarya_test",
                [{"update_id": 5, "edited_message": text(3, "Число исправили.")["message"]}],
            )
        await store.recover("999")
        assert await delivery(store) is None
        assert await store.db.all("SELECT state FROM outbox ORDER BY part_index") == [
            (first_state,),
            ("cancelled",),
        ]
        await drain(engine)
        assert len(model.calls) == 1


async def test_special_requests_and_other_group_do_not_enter_batch(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store, research=True)
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(2, "Заря, без поиска переведи 'time'."),
                text(3, "Заря, найди официальный источник https://example.com"),
                text(
                    4,
                    "Заря, процитируй это сообщение дословно.",
                    reply_to_message={"message_id": 2},
                ),
                text(5, "Заря, как тебе моя ава?"),
                text(
                    6,
                    "не ищи\nКакие новости по Мосбирже?",
                    entities=[{"type": "blockquote", "offset": 0, "length": 7}],
                ),
            ],
        )
        assert await store.db.all("SELECT message_id FROM text_batch_members") == [(2,)]


async def test_poll_backlog_uses_actual_telegram_send_times(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(2, "Заря, назови цвет", date=100),
                text(3, "Заря, назови число", date=180),
                text(4, "Только чётное.", date=181),
            ],
        )
        assert await store.db.all(
            "SELECT COUNT(*) FROM text_batch_members GROUP BY job_id ORDER BY job_id"
        ) == [(1,), (2,)]


async def test_reply_to_first_answer_does_not_cancel_remaining_part(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await store.ingest(
            "999", "zarya_test", [text(2, "Заря, назови цвет"), text(3, "Заря, назови число")]
        )
        await release_batches(store)
        model = FakeModel(actions((2, [2], "Синий"), (3, [3], "Четыре")))
        await drain(DialogueEngine(store, model))
        first = await delivery(store)
        await store.delivery_result(first["id"], "sent", message_id=4)
        await store.ingest(
            "999",
            "zarya_test",
            [
                text(
                    5,
                    "А почему?",
                    reply_to_message={"message_id": 4, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        second = await delivery(store)
        assert second and (second["reply_id"], second["text"]) == (3, "Четыре")
