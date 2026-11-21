"""A new text revision may replace a waiting question, never an attempted dialogue."""

import asyncio
import json

import pytest
from test_dialogue import FakeModel, drain, private_ready
from test_memory import ready
from test_memory_edits import edit
from test_participant_summaries import Summarizer, choose, prepare
from test_participant_summaries import drain as drain_summaries
from test_research import Fetch, ResearchModel
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_context import valid_snapshot
from zarya.openai_adapter import ModelResult
from zarya.participant_summaries import SummaryEngine
from zarya.research import ResearchEngine
from zarya.source_material import purge


async def test_minimal_purged_snapshot_keeps_details_and_costs_readable(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        engine = DialogueEngine(store, FakeModel())
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        work = await answer(engine, None, 2)
        before = await engine.details(work["run_id"])
        async with store.db.transaction() as conn:
            await purge(conn, "id=?", (work["run_id"],))
        raw = json.loads(
            (
                await store.db.one(
                    "SELECT snapshot FROM dialogue_runs WHERE id=?", (work["run_id"],)
                )
            )[0]
        )
        assert raw == {"invalidated": True}
        detail = await engine.details(work["run_id"])
        assert detail["snapshot"]["invalidated"]
        assert detail["snapshot"]["incoming"]["text"] == ""
        assert detail["snapshot"]["context"] == []
        assert detail["snapshot"]["settings"] == {}
        assert detail["call"] == before["call"]
        assert detail["response"] is None


@pytest.mark.parametrize("search_started", [False, True])
async def test_edit_waiting_reply_answers_current_revision_once(tmp_path, search_started):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Предыдущий ответ")))
        dialogue.memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Заря, привет")])
        prior = await answer(dialogue, None, 4)
        await deliver(store, prior, 1000)
        model = ResearchModel()
        research = ResearchEngine(store, model, Fetch())
        dialogue.research = research
        reply = {"message_id": 1000, "from": {"id": 999, "is_bot": True}}
        await store.ingest(
            "999",
            "zarya_test",
            [event(5, -100, "А что с тими леджерами в Европе?", reply_to_message=reply)],
        )
        assert await dialogue.claim("999") == {"skip": True}
        old = await research.claim("999")
        assert old
        entered, release = asyncio.Event(), asyncio.Event()
        generate = model.generate

        async def blocked(request):
            entered.set()
            await release.wait()
            return await generate(request)

        task = None
        if search_started:
            model.generate = blocked
            task = asyncio.create_task(research.execute(old))
            await entered.wait()
        revised = edit(6, 5, "А что с этими леджерами в Европе?")
        revised["edited_message"]["reply_to_message"] = reply
        await store.ingest("999", "zarya_test", [revised, revised])
        if task:
            release.set()
            await task
            model.generate = generate
        assert (await research.details(old["id"], "999"))["state"] == "cancelled"
        work = await answer(dialogue, research, 5)
        assert work["snapshot"]["incoming"]["text"] == revised["edited_message"]["text"]
        assert (await dialogue.details(work["run_id"]))["trigger"] == "reply"
        assert "Предыдущий ответ" in work["request"]["input"]
        assert "с тими" not in work["request"]["input"]
        assert (
            await store.db.one(
                "SELECT COUNT(*) FROM dialogue_runs WHERE message_id=5 AND state='completed'"
            )
        )[0] == 1
        assert (
            await store.db.one(
                "SELECT COUNT(*) FROM research_runs WHERE job_id=?", (old["job_id"],)
            )
        )[0] == 1
        assert len(model.calls) == (2 if search_started else 1)
        assert (
            json.loads(
                (
                    await store.db.one(
                        "SELECT payload FROM outbox WHERE job_id=? ORDER BY id", (work["job_id"],)
                    )
                )[0]
            )["reply_id"]
            == 5
        )
        assert await dialogue.claim("999") is None


@pytest.mark.parametrize("stage", ["preflight", "started", "sent", "unknown", "newer", "ambient"])
async def test_edit_never_restarts_attempted_or_superseded_dialogue(tmp_path, stage):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        engine = DialogueEngine(store, FakeModel(ModelResult(text="Готово")))
        await store.ingest("999", "zarya_test", [event(2, text="Первый вопрос")])
        if stage == "ambient":
            async with store.db.transaction() as c:
                await c.execute("UPDATE jobs SET payload=json_set(payload,'$.trigger','ambient')")
        elif stage == "newer":
            await store.ingest("999", "zarya_test", [event(3, text="Новый вопрос")])
        else:
            work = await engine.claim("999")
            if stage in {"started", "unknown"}:
                assert await engine.preflight(work, work["request"]) is None
            if stage == "unknown":
                await engine.finish(work, ModelResult(state="unknown", error="interrupted"), 1)
            if stage == "sent":
                await engine.execute(work)
                await deliver(store, work, 1000)
        update = event(4, text="Исправленный вопрос", message_id=2)
        update["edited_message"] = update.pop("message")
        await store.ingest("999", "zarya_test", [update])
        payload = json.loads((await store.db.one("SELECT payload FROM jobs ORDER BY id DESC"))[0])
        assert payload["trigger"] == "edited"
        if stage in {"preflight", "started"}:
            await engine.finish(work, ModelResult(state="cancelled"), 0)
        await drain(engine)
        calls = len(engine.adapter.calls)
        await drain(engine)
        assert len(engine.adapter.calls) == calls


async def test_later_edit_keeps_sent_summary_reply_chain_but_source_edit_purges_it(tmp_path):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        await drain_summaries(SummaryEngine(store, Summarizer()))
        engine = DialogueEngine(store, FakeModel(ModelResult(text="Сохраняем прежний ответ")))
        engine.memory = MemoryEngine(store, FakeModel(), tmp_path)
        assert (await engine.claim("999"))["skip"]
        first = await answer(engine, None, 22)
        assert first["snapshot"]["context_refs"]["summaries"]
        await deliver(store, first, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    23,
                    -100,
                    "Продолжим",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        second = await answer(engine, None, 23)
        await deliver(store, second, 1001)
        await store.ingest("999", "zarya_test", [event(24, -100, "Заря, новый вопрс")])
        await store.ingest("999", "zarya_test", [edit(25, 24, "Заря, новый вопрос")])
        for work in (first, second):
            detail = await engine.details(work["run_id"])
            assert detail["state"] == "completed" and detail["response"]
            assert await valid_snapshot(store.db.writer, "999", detail["snapshot"])
        assert (
            await store.db.one(
                "SELECT COUNT(*) FROM participant_summary_blocks WHERE state='completed'"
            )
        )[0] == 3
        source_id = first["snapshot"]["context_refs"]["humans"][0]["message_id"]
        await store.ingest("999", "zarya_test", [edit(26, source_id, "Другой исходный факт")])
        for work in (first, second):
            detail = await engine.details(work["run_id"])
            assert detail["snapshot"]["invalidated"] and detail["response"] is None
        listing = await engine.listing(0, "all", "999")
        assert all(item["invalidated"] for item in listing["items"])
