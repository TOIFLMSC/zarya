import asyncio
import json
import time
from types import SimpleNamespace

from test_dialogue import FakeModel
from test_memory import extract, ready
from test_memory_retrieval import filler, mode
from test_message_batches import pilot, release_batches, text
from test_source_material import answer
from test_telegram import event, store_at

from zarya import message_batches
from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_e5 import text_hash


async def test_e5_reaches_old_memory_and_switches_back(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue = DialogueEngine(store, FakeModel())
        engine = dialogue.memory = MemoryEngine(store, dialogue.adapter, tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
        await extract(engine)
        await filler(store, 151)
        await filler(store, 1, chat="-200")

        async def rank(bot, query):
            if "языке" not in query[0]:
                return None
            assert bot == "999"
            assert not store.db.write_lock.locked() and not store.gate.locked()
            return (
                ("f", 153, 1, text_hash("Любит разные сорта яблок номер 0")),
                ("f", 1, 1, text_hash("Любит Python")),
            )

        monkeypatch.setattr(engine.semantic_index, "schedule", lambda _: None)
        monkeypatch.setattr(engine.semantic_index, "rank", rank)
        await mode(store, "e5")
        await store.ingest("999", "zarya_test", [event(5, -100, "Заря, на каком языке я пишу?")])
        work = await answer(dialogue, None, 5)
        facts = json.loads(work["request"]["input"])["memory"]["facts"]
        assert facts[0]["id"] == 1
        assert 153 not in {f["id"] for f in facts}
        assert work["snapshot"]["retrieval"]["semantic"]["facts"][0]["id"] == 1
        assert work["snapshot"]["retrieval"]["fallback"] is None

        await mode(store, "legacy")
        await store.ingest("999", "zarya_test", [event(6, -100, "Заря, привет")])
        work = await answer(dialogue, None, 6)
        facts = json.loads(work["request"]["input"])["memory"]["facts"]
        assert 1 not in {f["id"] for f in facts}
        assert work["snapshot"]["retrieval"]["mode"] == "legacy"


async def test_e5_drops_revision_changed_during_search_then_falls_back(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue = DialogueEngine(store, FakeModel())
        engine = dialogue.memory = MemoryEngine(store, dialogue.adapter, tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
        await extract(engine)
        await filler(store, 151)
        await mode(store, "e5")
        await store.ingest("999", "zarya_test", [event(5, -100, "Заря, чем я интересовался?")])
        started = asyncio.Event()
        resume = asyncio.Event()

        async def rank(bot, query):
            if "интересовался" not in query[0]:
                return None
            started.set()
            await resume.wait()
            return (("f", 1, 1, text_hash("Любит Python")),)

        monkeypatch.setattr(engine.semantic_index, "schedule", lambda _: None)
        monkeypatch.setattr(engine.semantic_index, "rank", rank)
        pending = asyncio.create_task(answer(dialogue, None, 5))
        try:
            await asyncio.wait_for(started.wait(), 2)
            async with asyncio.timeout(2), store.gate, store.db.transaction() as conn:
                await conn.execute(
                    "UPDATE memory_facts SET version=2,text='Новая версия' WHERE id=1"
                )
        finally:
            resume.set()
            work = await pending
        facts = json.loads(work["request"]["input"])["memory"]["facts"]
        assert 1 not in {f["id"] for f in facts}

        async def unavailable(bot, query):
            return None

        monkeypatch.setattr(engine.semantic_index, "rank", unavailable)
        await store.ingest("999", "zarya_test", [event(6, -100, "Заря, ещё вопрос")])
        result = await answer(dialogue, None, 6)
        diag = result["snapshot"]["retrieval"]
        assert diag["fallback"] == "e5_unavailable" and "graph" in diag


async def test_e5_uses_both_questions_from_a_ready_batch(tmp_path, monkeypatch):
    clock = SimpleNamespace(time=lambda: time.time() + 120)
    monkeypatch.setattr(message_batches, "time", clock)
    async with store_at(tmp_path) as store:
        await pilot(store)
        await mode(store, "e5")
        await filler(store, 1)
        dialogue = DialogueEngine(store, FakeModel())
        engine = dialogue.memory = MemoryEngine(store, dialogue.adapter, tmp_path)
        queries = []

        async def rank(bot, query):
            assert not store.db.write_lock.locked() and not store.db.read_lock.locked()
            queries.append(query)
            return (("f", 1, 1, text_hash("Любит разные сорта яблок номер 0")),)

        monkeypatch.setattr(engine.semantic_index, "schedule", lambda _: None)
        monkeypatch.setattr(engine.semantic_index, "rank", rank)
        first = ("Заря, откуда берутся красные яблоки? " + "Подробнее о сортах. " * 210)[:4050]
        second = "Заря, какие яблоки я предпочитаю?"
        await store.ingest("999", "zarya_test", [text(2, first), text(3, second)])
        assert await dialogue.claim("999") is None
        assert queries == []
        await release_batches(store)
        work = await dialogue.claim("999")
        assert queries == [(first[:4000], second)]
        assert work and not work["skip"]
        request = json.loads(work["request"]["input"])
        assert [m["message_id"] for m in request["current_messages"]] == [2, 3]
        assert request["memory"]["facts"][0]["id"] == 1
        assert work["snapshot"]["retrieval"]["semantic"]["facts"][0]["id"] == 1
