import json

import pytest
from test_dialogue import FakeModel
from test_memory import context, extract, mutation, ready
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine, extraction_schema
from zarya.memory_context import valid_dependencies, valid_snapshot
from zarya.memory_logic import invalidate
from zarya.openai_adapter import ModelResult


async def finish_summary(engine, work, text):
    await engine.finish(work, ModelResult(text=json.dumps({"summary": text, "facts": []})), 1)


async def test_delete_preserves_independent_episode_and_inflight_batch(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Обсуждали ремонт насоса")])
        independent = await engine.claim("999", force=True)
        await finish_summary(engine, independent, "Ремонт насоса")
        await s.ingest("999", "zarya_test", [event(5, -100, "Люблю Python")])
        await extract(engine)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        # A different participant's extraction does not receive that personal fact.
        await s.ingest(
            "999", "zarya_test", [event(6, -100, "Пойдём на концерт", **{"from": {"id": 77}})]
        )
        pending = await engine.claim("999", force=True)
        assert not pending["existing"]
        await engine.mutate("999", fact["id"], mutation(fact, "delete"))
        await finish_summary(engine, pending, "Обсуждают концерт")
        texts = {r["text"] for r in (await context(engine))["summaries"]}
        assert texts == {"Ремонт насоса", "Обсуждают концерт"}


async def test_dependency_through_existing_fact_and_legacy_purge(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(engine)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await s.ingest("999", "zarya_test", [event(5, -100, "Привет")])
        work = await engine.claim("999", force=True)
        assert work["dependencies"]["facts"]
        await finish_summary(engine, work, "Сводка могла использовать старый факт")
        await engine.mutate("999", fact["id"], mutation(fact, "delete"))
        assert not (await context(engine))["summaries"]
        await s.ingest("999", "zarya_test", [event(6, -100, "Отдельный разговор")])
        work = await engine.claim("999", force=True)
        await finish_summary(engine, work, "Legacy")
        async with s.db.transaction() as conn:
            await conn.execute(
                "UPDATE memory_batches SET dependencies=NULL WHERE id=?", (work["id"],)
            )
            await invalidate(conn, "999", "-100")
        assert not (await context(engine))["summaries"]


async def test_paraphrase_support_adds_evidence_without_reset(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(engine)
        original = (await engine.profile("999", "-100"))["facts"][0]
        await s.ingest("999", "zarya_test", [event(5, -100, "Python мой любимый язык")])
        await extract(
            engine,
            text="Нравится писать на Python",
            relation="supports",
            existing_fact_id=original["id"],
        )
        facts = (await engine.profile("999", "-100"))["facts"]
        assert len(facts) == 1 and len(facts[0]["sources"]) == 2
        assert facts[0]["text"] == original["text"]
        assert facts[0]["version"] == original["version"] and facts[0]["state"] == "active"
        assert len((await context(engine))["summaries"]) == 2
        # Different wording without an explicit relationship is only a proposal.
        await s.ingest("999", "zarya_test", [event(6, -100, "Python люблю")])
        await extract(engine, text="Программирует на Python")
        assert (await context(engine))["facts"][0]["text"] == original["text"]


async def test_human_reply_context_revision_and_derived_fact(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        question = event(4, -100, "Тебе нравится Python?", **{"from": {"id": 77}})
        await s.ingest("999", "zarya_test", [question])
        work = await engine.claim("999", force=True)
        await finish_summary(engine, work, "Вопрос про Python")
        await s.ingest(
            "999", "zarya_test", [event(5, -100, "Да", reply_to_message=question["message"])]
        )
        work = await extract(engine)
        assert work["sources"][0]["reply_context"]["text"] == "Тебе нравится Python?"
        assert work["sources"][0]["reply_context"]["sender_id"] == "77"
        assert (await context(engine))["facts"]
        edit = event(6, -100, "Тебе нравится Rust?", **{"from": {"id": 77}})
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 4
        await s.ingest("999", "zarya_test", [edit])
        assert not (await context(engine))["facts"]
        assert not (await context(engine))["summaries"]
        edited_work = await engine.claim("999", force=True)
        await finish_summary(engine, edited_work, "Исправленный вопрос")
        await s.ingest("999", "zarya_test", [event(7, -100, "Я точно люблю Python")])
        await extract(engine)
        restored = (await context(engine))["facts"]
        assert len(restored) == 1 and restored[0]["text"] == "Любит Python"


async def test_reply_not_retained_and_foreign_chat_do_not_leak(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -200, "Личное из другой группы")])
        work = await engine.claim("999", force=True)
        await finish_summary(engine, work, "Чужая группа")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(
                    5,
                    -100,
                    "Да",
                    reply_to_message={"message_id": 4, "text": "НЕ ДОВЕРЯТЬ ВЛОЖЕННОМУ ТЕКСТУ"},
                )
            ],
        )
        work = await engine.claim("999", force=True)
        assert "reply_context" not in work["sources"][0]


async def test_older_relevant_episode_retrieved_only_in_same_chat(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        texts = [
            "Обсуждали насос Cougar и ремонт водянки",
            "Погода солнечная",
            "Заказали пиццу",
            "Обсуждают концерт",
            "Встретимся завтра",
        ]
        for number, text in enumerate(texts, 4):
            await s.ingest("999", "zarya_test", [event(number, -100, text)])
            work = await engine.claim("999", force=True)
            await finish_summary(engine, work, text)
        async with s.db.transaction() as conn:
            result, _ = await engine.context(
                conn, "999", "-100", 2, ["42"], query="Что было с водянкой Cougar?"
            )
            other, _ = await engine.context(conn, "999", "-200", 2, ["42"], query="Cougar")
        assert texts[0] in [r["text"] for r in result["summaries"]]
        assert len(result["summaries"]) == 3
        assert not other["summaries"]


@pytest.mark.parametrize("bad", [None, {}, {"version": 1}, "bad json"])
async def test_malformed_dependencies_fail_closed(tmp_path, bad):
    async with store_at(tmp_path) as s:
        await ready(s)
        async with s.db.transaction() as conn:
            assert not await valid_dependencies(conn, "999", "-100", 2, bad)


async def test_foreign_fact_reference_rejected_and_schema_strict(tmp_path):
    schema = extraction_schema()["$defs"]["Proposal"]
    assert set(schema["required"]) == set(schema["properties"])
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        work = await extract(engine, relation="supports", existing_fact_id=999999)
        assert (await s.db.one("SELECT state FROM memory_batches WHERE id=?", (work["id"],)))[
            0
        ] == "invalid"
        assert not (await context(engine))["facts"]


async def test_assistant_reply_context_is_retained_sent_and_invalidated(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Заря, спроси меня о любимом языке")])
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Тебе нравится Python?")))
        dialogue.memory = engine
        run = await answer(dialogue, None, 4)
        await deliver(s, run, 1000)
        first = await engine.claim("999", force=True)
        await finish_summary(engine, first, "Вопрос про язык")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(
                    5,
                    -100,
                    "Да",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        work = await extract(engine)
        reply = work["sources"][0]["reply_context"]
        assert reply["role"] == "assistant" and reply["context_only"]
        assert reply["text"] == "Тебе нравится Python?"
        saved_memory = await context(engine)
        assert saved_memory["facts"]
        async with s.db.transaction() as conn:
            # The original bot response is withdrawn; neither episode nor derived fact is valid.
            await conn.execute(
                "UPDATE dialogue_runs SET snapshot=? WHERE id=?",
                ('{"invalidated":true}', run["run_id"]),
            )
            assert not await valid_snapshot(conn, "999", {"memory": saved_memory})
            await invalidate(conn, "999", "-100")
        assert not (await context(engine))["facts"]
        assert work["id"] not in [b["id"] for b in (await context(engine))["summaries"]]


async def test_reply_changes_during_generation_rejects_paid_result(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Любишь Python?")])
        first = await engine.claim("999", force=True)
        await finish_summary(engine, first, "Вопрос")
        await s.ingest(
            "999", "zarya_test", [event(5, -100, "Да", reply_to_message={"message_id": 4})]
        )
        work = await engine.claim("999", force=True)
        edit = event(6, -100, "Любишь Rust?")
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 4
        await s.ingest("999", "zarya_test", [edit])
        await finish_summary(engine, work, "Старый ответ про Python")
        assert (
            await s.db.one("SELECT state,summary FROM memory_batches WHERE id=?", (work["id"],))
        ) == ("cancelled", None)


async def test_exported_reply_fact_withdrawal_scrubs_other_group(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest(
            "999", "zarya_test", [event(4, -100, "Ты любишь Python?", **{"from": {"id": 77}})]
        )
        work = await engine.claim("999", force=True)
        await finish_summary(engine, work, "Вопрос")
        await s.ingest(
            "999", "zarya_test", [event(5, -100, "Да", reply_to_message={"message_id": 4})]
        )
        await extract(engine)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await engine.mutate("999", fact["id"], mutation(fact, "share"))
        await s.ingest("999", "zarya_test", [event(6, -200, "Заря, что я люблю?")])
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Ты любишь Python")))
        dialogue.memory = engine
        work = await answer(dialogue, None, 6)
        assert work["snapshot"]["memory"]["facts"][0]["shared"]
        await deliver(s, work, 1000)
        edit = event(7, -100, "Ты любишь Rust?", **{"from": {"id": 77}})
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 4
        await s.ingest("999", "zarya_test", [edit])
        assert not await s.db.one(
            "SELECT 1 FROM recent_messages WHERE chat_id='-200' AND role='assistant'"
        )
        assert json.loads(
            (await s.db.one("SELECT snapshot FROM dialogue_runs WHERE id=?", (work["run_id"],)))[0]
        )["invalidated"]
        assert not (await context(engine, "-200"))["facts"]
        await s.ingest(
            "999", "zarya_test", [event(8, -200, "Да", reply_to_message={"message_id": 1000})]
        )
        # Process earlier queue entries too; no new extraction can reuse the scrubbed bot text.
        while work := await engine.claim("999", force=True):
            for source in work["sources"]:
                if source["message_id"] == 8:
                    assert "reply_context" not in source
            await finish_summary(engine, work, "Новый разговор")


async def test_confirmation_of_bot_repeating_fact_does_not_create_cycle(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(engine)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, что я люблю?")])
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Ты любишь Python")))
        dialogue.memory = engine
        work = await answer(dialogue, None, 5)
        await deliver(s, work, 1000)
        first = await engine.claim("999", force=True)
        await finish_summary(engine, first, "Заря напомнила об интересе к Python")
        await s.ingest(
            "999",
            "zarya_test",
            [event(6, -100, "Да, всё верно", reply_to_message={"message_id": 1000})],
        )
        await extract(engine, relation="supports", existing_fact_id=fact["id"])
        facts = (await context(engine))["facts"]
        assert len(facts) == 1 and facts[0]["id"] == fact["id"]
        assert facts[0]["version"] == fact["version"]
        assert len((await engine.profile("999", "-100"))["facts"][0]["sources"]) == 2
