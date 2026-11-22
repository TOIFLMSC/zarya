import json

import pytest
from test_dialogue import FakeModel, drain
from test_photos import ready
from test_telegram import event, store_at

from zarya import task_state
from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_reset import scrub
from zarya.openai_adapter import ModelResult
from zarya.task_state import TaskStateEngine

BOT = "999"
CHAT = "-100"
ANCHOR = "Заря, давай составим план поездки в Тулу."
GOAL = "план поездки в Тулу"
NO_PAPERS = "Подтверждающих документов нет"
QUESTION = "Открытый вопрос — проверяем пятницу"
LIMIT = "Только четверг, пятница отпала"
REPLY = "Заря, продолжим?"


def post(mid, text, *, topic=8, sender=42, reply=None, bot_reply=False, edited=None):
    source_id = edited or mid
    extra = {
        "from": {"id": sender, "first_name": "Автор", "is_bot": False},
        "message_thread_id": topic,
    }
    if reply is not None:
        extra["reply_to_message"] = {
            "message_id": reply,
            "from": {"id": 999 if bot_reply else 42, "is_bot": bot_reply},
        }
    if text.startswith("@zarya_test"):
        extra["entities"] = [{"type": "mention", "offset": 0, "length": len("@zarya_test")}]
    update = event(source_id, -100, text, **extra)
    if edited:
        update = {"update_id": mid, "edited_message": update["message"]}
    return update


def extraction(*fields, status="open"):
    return ModelResult(
        text=json.dumps(
            {
                "status": status,
                "fields": [
                    {"kind": kind, "message_id": mid, "quote": quote} for kind, mid, quote in fields
                ],
            },
            ensure_ascii=False,
        )
    )


async def ingest(store, *updates):
    await store.ingest(BOT, "zarya_test", list(updates))


async def background(engine):
    work = await engine.claim(BOT)
    assert work and not work.get("skip")
    await engine.execute(work)
    return work


async def pilot(store):
    await ready(store, -100)
    settings = await store.db.settings()
    await store.db.update_settings(
        settings.version,
        settings.settings.model_copy(update={"participant_context_messages": 2}),
    )


@pytest.fixture(autouse=True)
def ready_tasks(monkeypatch):
    monkeypatch.setattr(task_state, "DEBOUNCE", 0)


async def test_long_task_keeps_exact_sources_and_fences_pending_delivery_and_replay(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(
            store,
            post(2, ANCHOR),
            post(3, NO_PAPERS, sender=43, reply=2),
            post(4, QUESTION, sender=43, reply=2),
        )
        calls = FakeModel(
            extraction(
                ("goal", 2, GOAL),
                ("constraint", 3, NO_PAPERS),
                ("question", 4, QUESTION),
            )
        )
        worker = TaskStateEngine(store, calls)
        work = await background(worker)
        assert [m["message_id"] for m in work["messages"]] == [2, 3, 4]
        model = FakeModel(ModelResult(text="Продолжаем план с учётом условий."))
        dialogue = DialogueEngine(store, model)
        await drain(dialogue)
        await ingest(store, *(post(mid, "Обсуждаем другую тему.") for mid in range(5, 14)))
        await ingest(store, post(14, REPLY))
        await drain(dialogue)
        request = json.loads(model.calls[-1]["input"])
        assert not any(m.get("message_id") in {2, 3, 4} for m in request["recent_chat"])
        assert request["task_state"]["for_message_ids"] == [14]
        assert [(f["message_id"], f["quote"]) for f in request["task_state"]["fields"]] == [
            (2, GOAL),
            (3, NO_PAPERS),
            (4, QUESTION),
        ]
        (live, job) = await store.db.one(
            "SELECT id,job_id FROM dialogue_runs WHERE message_id=14 AND mode='live'"
        )
        count = len(model.calls)
        recorded = await dialogue.replay(live, "recorded")
        assert recorded["snapshot"]["task_state"]["fields"] == request["task_state"]["fields"]
        assert len(model.calls) == count
        saved = await store.db.settings()
        paid = await dialogue.replay(live, "paid", saved.version)
        assert paid["snapshot"]["task_state"]["fields"] == request["task_state"]["fields"]
        assert len(model.calls) == count + 1
        assert "Состояние задачи" in model.calls[-1]["instructions"]

        await ingest(store, post(15, LIMIT, sender=43, reply=4))
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
        assert await store.claim_delivery(BOT) is None
        assert await store.db.one("SELECT state FROM outbox WHERE job_id=?", (job,)) == (
            "cancelled",
        )
        for mode in ("recorded", "paid"):
            with pytest.raises(ValueError, match="Контекст изменился"):
                await dialogue.replay(live, mode, saved.version)
        await ingest(store, post(16, REPLY))
        await drain(dialogue)
        assert "task_state" not in json.loads(model.calls[-1]["input"])
        calls.result = extraction(
            ("goal", 2, GOAL), ("constraint", 3, NO_PAPERS), ("decision", 15, LIMIT)
        )
        await background(worker)
        await ingest(store, post(17, REPLY, sender=44))
        await drain(dialogue)
        context = json.loads(model.calls[-1]["input"])["task_state"]["fields"]
        assert [f["quote"] for f in context] == [GOAL, NO_PAPERS, LIMIT]
        assert not any(f["kind"] == "question" for f in context)
        assert await worker.claim(BOT) is None


async def test_multiple_tasks_and_topic_selection_and_closure(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(
            store,
            post(2, ANCHOR),
            post(3, "Заря, новая задача: тестовая кофейня."),
            post(4, "Заря, новая задача: тестовые курсы.", topic=9),
        )
        model = FakeModel()
        worker = TaskStateEngine(store, model)
        for mid, goal in [(2, GOAL), (3, "тестовая кофейня"), (4, "тестовые курсы")]:
            model.result = extraction(("goal", mid, goal))
            await background(worker)
        await ingest(
            store,
            post(5, REPLY),
            post(6, REPLY, topic=9, sender=43),
            post(7, REPLY, sender=44, reply=2),
            post(8, REPLY, topic=9, sender=45, reply=2),
        )
        async with store.db.transaction() as conn:

            async def select(mid, thread):
                return await task_state.context(conn, BOT, CHAT, 2, thread, [mid])

            assert await select(5, 8) is None
            assert (await select(6, 9))["fields"][0]["quote"] == "тестовые курсы"
            assert (await select(7, 8))["fields"][0]["quote"] == GOAL
            assert await select(8, 9) is None
        # Resume via the bot's retained sent reply also works.
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Продолжим.")))
        await drain(dialogue)
        (outbox_id,) = await store.db.one(
            "SELECT o.id FROM outbox o JOIN dialogue_runs d ON o.job_id=d.job_id "
            "WHERE d.message_id=7"
        )
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0 WHERE id=?", (outbox_id,))
        outgoing = await store.claim_delivery(BOT)
        assert outgoing["id"] == outbox_id
        await store.delivery_result(outbox_id, "sent", message_id=90)
        await ingest(store, post(9, REPLY, sender=44, reply=90, bot_reply=True))
        async with store.db.transaction() as conn:
            assert (await task_state.context(conn, BOT, CHAT, 2, 8, [9]))["fields"][0][
                "quote"
            ] == GOAL
        await ingest(store, post(10, "Эта задача полностью закрыта.", sender=43, reply=2))
        model.result = extraction(("closure", 10, "задача полностью закрыта"), status="closed")
        await background(worker)
        await ingest(store, post(11, REPLY))
        async with store.db.transaction() as conn:
            chosen = await task_state.context(conn, BOT, CHAT, 2, 8, [11])
            assert chosen["fields"][0]["quote"] == "тестовая кофейня"


@pytest.mark.parametrize("action", ["edit", "revoke", "retention"])
async def test_invalid_source_erases_state_and_rejects_in_flight_extraction(tmp_path, action):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR), post(3, NO_PAPERS, reply=2))
        model = FakeModel(extraction(("goal", 2, GOAL), ("constraint", 3, NO_PAPERS)))
        worker = TaskStateEngine(store, model)
        await background(worker)
        await ingest(store, post(4, REPLY))
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Учитываю данные.")))
        await drain(dialogue)
        await ingest(store, post(5, QUESTION, reply=2))
        work = await worker.claim(BOT)
        assert work and not work.get("skip")
        if action == "edit":
            await ingest(store, post(6, "Документы уже нашли.", edited=3, reply=2))
        elif action == "revoke":
            await store.decide(BOT, "group", CHAT, "revoked", 2)
        else:
            async with store.db.transaction() as conn:
                await conn.execute(
                    "UPDATE participant_messages SET received_at=0 WHERE message_id=3"
                )
            await MemoryEngine(store, None, tmp_path).cleanup(BOT, force=True)
        await worker.execute(work)
        assert len(model.calls) == 1
        assert await store.db.one("SELECT result,proof,ready_revision FROM task_states") == (
            None,
            None,
            None,
        )
        assert await store.db.one(
            "SELECT json_extract(snapshot,'$.invalidated'),response FROM dialogue_runs "
            "WHERE message_id=4"
        ) == (1, None)
        assert await store.db.one(
            "SELECT state,request_json FROM model_calls WHERE id=?", (work["call"],)
        ) == ("cancelled", None)
        if action == "revoke":
            assert await store.db.all("SELECT event_id FROM task_members") == []
        if action == "edit":
            model.result = extraction(("goal", 2, GOAL), ("constraint", 3, "Документы уже нашли"))
            updated = await background(worker)
            assert any(m["text"] == "Документы уже нашли." for m in updated["messages"])


async def test_restart_and_invalid_quote_never_retry_the_same_revision(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR))
        worker = TaskStateEngine(store, FakeModel())
        assert await worker.claim(BOT)
        await worker.recover(BOT)
        assert await worker.claim(BOT) is None
        assert await store.db.one(
            "SELECT state,error_code FROM model_calls WHERE operation='task_state'"
        ) == ("unknown", "interrupted")
        await ingest(store, post(3, NO_PAPERS, reply=2))
        model = FakeModel(extraction(("goal", 2, "Выдуманная цель")))
        worker = TaskStateEngine(store, model)
        await background(worker)
        assert await store.db.one(
            "SELECT state,error_code FROM model_calls WHERE operation='task_state' ORDER BY id DESC"
        ) == ("error", "invalid_task_state")
        assert await store.db.one("SELECT result FROM task_states") == (None,)
        assert await worker.claim(BOT) is None


async def test_current_correction_keeps_the_goal_and_late_background_result_is_discarded(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR), post(3, QUESTION, reply=2))
        extraction_model = FakeModel(extraction(("goal", 2, GOAL), ("question", 3, QUESTION)))
        worker = TaskStateEngine(store, extraction_model)
        await background(worker)
        await ingest(store, post(4, "Заря, уточнение: документов нет. Что делать?", reply=2))
        model = FakeModel(ModelResult(text="Продолжаем."))
        dialogue = DialogueEngine(store, model)
        await drain(dialogue)
        request = json.loads(model.calls[-1]["input"])
        assert request["task_state"]["for_message_ids"] == [4]
        assert request["task_state"]["fields"][0]["quote"] == GOAL
        assert "документов нет" in request["current_message"]["text"]

        class CorrectDuringCall(FakeModel):
            async def generate(self, request):
                await ingest(store, post(5, LIMIT, reply=3, sender=43))
                return await super().generate(request)

        late_model = CorrectDuringCall(
            extraction(("goal", 2, GOAL), ("constraint", 4, "документов нет"))
        )
        active = TaskStateEngine(store, late_model)
        work = await background(active)
        assert len(late_model.calls) == 1
        assert await store.db.one("SELECT state FROM model_calls WHERE id=?", (work["call"],)) == (
            "completed",
        )
        assert await store.db.one("SELECT ready_revision<revision FROM task_states") == (1,)
        await ingest(store, post(6, REPLY, sender=44))
        await drain(dialogue)
        assert "task_state" not in json.loads(model.calls[-1]["input"])


async def test_budget_does_not_hide_a_contribution_or_truncate_a_long_correction(
    tmp_path, monkeypatch
):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR), post(3, QUESTION, reply=2))
        model = FakeModel(extraction(("goal", 2, GOAL), ("question", 3, QUESTION)))
        worker = TaskStateEngine(store, model)
        await background(worker)
        long_text = "Детали обсуждения " * 90 + LIMIT
        await ingest(store, post(4, long_text, reply=2))
        model.result = extraction(("goal", 2, GOAL), ("decision", 4, LIMIT))
        work = await background(worker)
        assert work["messages"] == [{"message_id": 4, "sender_id": "42", "text": long_text}]
        assert await store.db.one("SELECT ready_revision=revision FROM task_states") == (1,)
        monkeypatch.setattr(task_state, "MAX_RECENT", 2)
        await ingest(
            store,
            post(5, "Четверг отменяется.", reply=2),
            post(6, "Проверим варианты жилья.", reply=2),
            post(7, "Отправлю расписание.", reply=2),
            post(8, REPLY),
        )
        assert (await worker.claim(BOT)) == {"skip": True}
        assert len(model.calls) == 2
        async with store.db.transaction() as conn:
            assert await task_state.context(conn, BOT, CHAT, 2, 8, [8]) is None
        await ingest(store, post(9, "Заря, давай составим план поездки в Казань.", edited=2))
        assert (await worker.claim(BOT)) == {"skip": True}
        assert await store.db.one("SELECT retired FROM task_states") == (0,)
        assert await store.db.one("SELECT COUNT(*) FROM task_members") == (7,)


async def test_reply_keeps_its_task_and_unrelated_dialogue_does_not_join(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR))
        worker = TaskStateEngine(store, FakeModel(extraction(("goal", 2, GOAL))))
        await background(worker)
        await ingest(store, post(3, "Заря, помоги подобрать жильё.", reply=2))
        async with store.db.transaction() as conn:
            associated = await task_state.context(conn, BOT, CHAT, 2, 8, [3])
            assert associated["fields"][0]["quote"] == GOAL
        assert await store.db.one("SELECT COUNT(*) FROM task_states") == (1,)

        model = FakeModel(ModelResult(text="Попробуй простой рецепт."))
        dialogue = DialogueEngine(store, model)
        await ingest(store, post(4, "Заря, как испечь хлеб?"))
        await drain(dialogue)
        (outbox_id,) = await store.db.one(
            "SELECT o.id FROM outbox o JOIN dialogue_runs d ON o.job_id=d.job_id "
            "WHERE d.message_id=4"
        )
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0 WHERE id=?", (outbox_id,))
        outgoing = await store.claim_delivery(BOT)
        assert outgoing["id"] == outbox_id
        await store.delivery_result(outbox_id, "sent", message_id=90)
        await ingest(store, post(5, "Мука уже есть."))
        assert await store.db.one(
            "SELECT json_extract(payload,'$.trigger') FROM jobs "
            "WHERE json_extract(payload,'$.message_id')=5"
        ) == ("continuation",)
        assert await store.db.all("SELECT message_id FROM task_members ORDER BY message_id") == [
            (2,),
            (3,),
        ]
        await scrub(store.db)
        assert await store.db.all("SELECT event_id FROM task_members") == []


async def test_task_reply_verbatim_quote_does_not_queue_a_model_call(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(store, post(2, ANCHOR))
        worker = TaskStateEngine(store, FakeModel(extraction(("goal", 2, GOAL))))
        await background(worker)
        model = FakeModel()
        dialogue = DialogueEngine(store, model)
        await drain(dialogue)
        initial = len(model.calls)
        await ingest(store, post(3, "Заря, процитируй это сообщение дословно.", reply=2))
        await drain(dialogue)
        assert len(model.calls) == initial
        assert await worker.claim(BOT) is None
        assert await store.db.one("SELECT response FROM dialogue_runs WHERE message_id=3") == (
            ANCHOR,
        )


async def test_start_in_reply_to_an_untracked_message_and_resume_via_username(tmp_path):
    async with store_at(tmp_path) as store:
        await pilot(store)
        await ingest(
            store,
            post(2, "Электрички ходят утром.", sender=43),
            post(3, ANCHOR, reply=2),
        )
        worker = TaskStateEngine(store, FakeModel(extraction(("goal", 3, GOAL))))
        await background(worker)
        await ingest(store, post(4, "@zarya_test, продолжим?"))
        model = FakeModel(ModelResult(text="Продолжим."))
        await drain(DialogueEngine(store, model))
        data = json.loads(model.calls[-1]["input"])["task_state"]
        assert data["for_message_ids"] == [4]
        assert data["fields"][0]["quote"] == GOAL
