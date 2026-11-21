import json
import time

import pytest
from test_dialogue import FakeModel, drain
from test_memory import extract, mutation, ready
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.configuration import canonical, document, validate
from zarya.dialogue import DialogueEngine
from zarya.graph import GraphRepository
from zarya.memory import MemoryEngine
from zarya.memory_context import valid_snapshot
from zarya.memory_logic import invalidate
from zarya.memory_retrieval import candidates, select_context
from zarya.openai_adapter import ModelResult
from zarya.prompts import DEFAULT_PROMPTS
from zarya.telegram_store import stamp


async def mode(store, value):
    current = await store.db.settings()
    await store.db.update_settings(
        current.version, current.settings.model_copy(update={"memory_retrieval": value})
    )


async def filler(store, n=30, chat="-100", person="42"):
    async with store.db.transaction() as conn:
        for i in range(n):
            await conn.execute(
                "INSERT INTO memory_facts(bot_id,chat_id,scope,access_version,sender_id,category,"
                "fact_key,text,provenance,state,created_at,updated_at,reviewed_at) "
                "VALUES ('999',?,'group',2,?,'interest',?,?,'self','active',?,?,?)",
                (
                    chat,
                    person,
                    f"item_{i}",
                    f"Любит разные сорта яблок номер {i}",
                    stamp(),
                    stamp(),
                    time.time(),
                ),
            )


async def selected(engine, value="graph", query="Python", event_id=None, recent=None):
    diagnostic = {}
    async with engine.db.transaction() as conn:
        result = await select_context(
            conn,
            "999",
            "-100",
            2,
            ["42"],
            query,
            value,
            1,
            diagnostic,
            current_query=query,
            recent=recent or [],
            event_id=event_id,
            speaker="42",
            thread=None,
        )
    return result, diagnostic


async def test_shadow_preserves_exact_baseline_but_finds_older_relevant_fact(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
        await extract(memory)
        await filler(store)
        baseline, _ = await selected(memory, "legacy")
        shadow, diagnostic = await selected(memory, "shadow")
        graph, again = await selected(memory)
        assert shadow == baseline and len(baseline["facts"]) == 25
        assert [f["id"] for f in baseline["facts"]] == list(range(31, 6, -1))
        assert all(f["text"] != "Любит Python" for f in baseline["facts"])
        assert graph["facts"][0]["text"] == "Любит Python"
        assert diagnostic["graph"] == again["graph"]
        assert "query_match" in diagnostic["graph"]["facts"][0]["reasons"]
        assert "Любит Python" not in json.dumps(diagnostic, ensure_ascii=False)
        assert "query" not in diagnostic and diagnostic["fallback"] is None


async def test_shadow_one_call_and_identical_model_input(tmp_path, monkeypatch):
    monkeypatch.setattr("zarya.dialogue.random.random", lambda: 0.5)
    requests = []
    for value in ("legacy", "shadow"):
        (tmp_path / value).mkdir()
        async with store_at(tmp_path / value) as store:
            await ready(store)
            await mode(store, value)
            adapter = FakeModel(ModelResult(text="Привет"))
            dialogue = DialogueEngine(store, adapter)
            dialogue.memory = MemoryEngine(store, adapter, tmp_path / value)
            await store.ingest("999", "zarya_test", [event(4, -100, "Заря, привет")])
            work = await answer(dialogue, None, 4)
            assert len(adapter.calls) == 1
            requests.append(adapter.calls[0])
            detail = await dialogue.details(work["run_id"])
            assert detail["snapshot"]["retrieval"]["mode"] == value
    assert requests[0] == requests[1]


async def test_reply_chain_keeps_rate_after_busy_chat_and_pins_provenance(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        await mode(store, "graph")
        adapter = FakeModel(ModelResult(text="В старом снимке было 87,21-88,20 руб за USDT"))
        memory = MemoryEngine(store, adapter, tmp_path)
        dialogue = DialogueEngine(store, adapter)
        dialogue.memory = memory
        await store.ingest("999", "zarya_test", [event(4, -100, "Заря, курс USDT?")])
        original = await answer(dialogue, None, 4)
        await deliver(store, original, 1000)
        adapter.result = ModelResult(text="Другой ответ")
        for i in (5, 6):
            await store.ingest("999", "zarya_test", [event(i, -100, "Заря, привет")])
            other = await answer(dialogue, None, i)
            await deliver(store, other, 1000 + i)
        for i in range(7, 68):
            await store.ingest("999", "zarya_test", [event(i, -100, "Обычный разговор")])
        await drain(dialogue)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    68,
                    -100,
                    "Сколько рублей за 153.14 USDT?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        followup = await answer(dialogue, None, 68)
        saved = (await dialogue.details(followup["run_id"]))["snapshot"]
        assert not any("87,21" in m["text"] for m in saved["context"])
        chain = saved["memory"]["conversation"]
        assert [m["message_id"] for m in chain] == [4, 1000]
        assert "87,21-88,20" in chain[-1]["text"]
        assert chain[-1]["context_only"]
        assert "87,21-88,20" in followup["request"]["input"]
        async with store.db.transaction() as conn:
            assert await valid_snapshot(conn, "999", saved)
            await conn.execute("UPDATE outbox SET state='failed' WHERE message_id=1000")
            assert not await valid_snapshot(conn, "999", saved)


async def test_unaccepted_nested_reply_cannot_seed_graph(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    4,
                    -100,
                    "Да",
                    reply_to_message={"message_id": 9999, "text": "PRIVATE_SECRET_NEVER_SEED"},
                )
            ],
        )
        event_id = (await store.db.one("SELECT id FROM events WHERE update_id=4"))[0]
        result, diagnostic = await selected(memory, query="Да", event_id=event_id)
        assert "conversation" not in result and not diagnostic["graph"]["reply_messages"]
        assert "PRIVATE_SECRET" not in json.dumps(result)


async def test_new_explicit_conflict_separate_and_revision_guarded(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Зови меня Алексей")])
        await extract(memory, text="Имя Алексей", category="name", key="name")
        await store.ingest("999", "zarya_test", [event(5, -100, "Зови меня Борис")])
        await extract(
            memory,
            text="Имя Борис",
            category="name",
            key="name",
            relation="contradicts",
            existing_fact_id=1,
        )
        result, _ = await selected(memory, query="Какое имя Борис?")
        assert not result["facts"] and len(result["conflicts"]) == 1
        pair = result["conflicts"][0]
        assert {s["state"] for s in pair["statements"]} == {"disputed"}
        assert pair["status"] == "unresolved_not_established_fact"
        snapshot = {"memory": result, "retrieval_chat_id": "-100", "source_access_version": 2}
        graph = await GraphRepository(store.db.path).snapshot("999", "-100")
        assert any(e["type"] == "contradicts" for e in graph["edges"])
        async with store.db.transaction() as conn:
            assert await valid_snapshot(conn, "999", snapshot)
            await conn.execute("UPDATE memory_facts SET version=version+1 WHERE id=1")
            assert not await valid_snapshot(conn, "999", snapshot)
        changed, _ = await selected(memory, query="Борис")
        assert not changed.get("conflicts")


async def test_wording_without_explicit_relation_is_not_conflict(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Python")])
        await extract(memory)
        await store.ingest("999", "zarya_test", [event(5, -100, "Python снова")])
        await extract(memory, text="Пишет на Python")
        assert (await store.db.one("SELECT count(*) FROM memory_relations"))[0] == 0
        result, _ = await selected(memory)
        assert not result.get("conflicts")


async def test_shared_fact_terminal_and_revocation_checked(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -200, "Люблю Python PRIVATE_BODY")])
        await extract(memory)
        fact = (await memory.profile("999", "-200"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "share"))
        result, diagnostic = await selected(memory)
        assert len(result["facts"]) == 1 and result["facts"][0]["shared"]
        assert not result["summaries"] and diagnostic["edges"] == 0
        assert "PRIVATE_BODY" not in json.dumps(result)
        snapshot = {"memory": result, "retrieval_chat_id": "-100", "source_access_version": 2}
        async with store.db.transaction() as conn:
            assert await valid_snapshot(conn, "999", snapshot)
            await conn.execute("UPDATE memory_shares SET state='revoked'")
            assert not await valid_snapshot(conn, "999", snapshot)


async def test_graph_fallback_and_shadow_metadata_has_no_response_dependencies(
    tmp_path, monkeypatch
):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await filler(store)
        baseline, _ = await selected(memory, "legacy")

        def broken(*args, **kwargs):
            raise ValueError("synthetic failure")

        monkeypatch.setattr("zarya.memory_retrieval.rank", broken)
        actual, diagnostic = await selected(memory)
        assert actual == baseline and diagnostic["fallback"] == "graph_unavailable"


async def test_diagnostics_invalidated_even_for_preserved_snapshot(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Привет")))
        dialogue.memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Заря, привет")])
        work = await answer(dialogue, None, 4)
        await deliver(store, work, 1000)
        assert (await dialogue.details(work["run_id"]))["snapshot"]["retrieval"]
        async with store.db.transaction() as conn:
            await invalidate(conn, "999", "-100", before_event=9999)
        snapshot = (await dialogue.details(work["run_id"]))["snapshot"]
        assert "retrieval" not in snapshot
        assert not snapshot.get("invalidated")  # The intentional sent-answer exception survives.


async def test_candidate_window_and_final_text_budget(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        await filler(store, 165)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        async with store.db.transaction() as conn:
            facts, episodes = await candidates(conn, "999", "-100", 2, ["42"], time.time())
            denied, _ = await candidates(conn, "999", "-100", 2, ["77"], time.time())
        assert len(facts) == 150 and not episodes and not denied
        result, diagnostic = await selected(memory)
        assert len(result["facts"]) <= 25
        assert sum(len(f["text"]) for f in result["facts"]) <= 5000
        assert diagnostic["candidates"]["facts"] == 150


def test_pre_g6_configuration_still_loads_with_safe_default():
    from zarya.models import Settings

    before = document(Settings(), DEFAULT_PROMPTS)
    before["settings"].pop("memory_retrieval")
    settings, prompts = validate(canonical(before))
    assert settings.memory_retrieval == "shadow" and prompts.sha256 == DEFAULT_PROMPTS.sha256
    before["settings"].pop("memory_enabled")
    with pytest.raises(ValueError):
        validate(canonical(before))


async def test_edge_budget_falls_back_without_partial_context(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        await filler(store, 1)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        async with store.db.transaction() as conn:
            for i in range(601):
                cursor = await conn.execute(
                    "INSERT INTO memory_sources(bot_id,chat_id,scope,message_id,event_id,sender_id,"
                    "name,text,received_at,access_version) VALUES ('999','-100','group',?,?,"
                    "'42','Test','Synthetic source',?,2)",
                    (i + 1000, i + 1000, time.time()),
                )
                await conn.execute(
                    "INSERT INTO memory_evidence(fact_id,source_id,event_id,"
                    "supporting) VALUES (1,?,?,1)",
                    (cursor.lastrowid, i + 1000),
                )
        baseline, _ = await selected(memory, "legacy")
        actual, diagnostic = await selected(memory)
        assert actual == baseline and diagnostic["fallback"] == "edge_budget"
        assert diagnostic["capped"] and "graph" not in diagnostic


@pytest.mark.parametrize("length,count", [(40, 6), (2000, 3)])
async def test_reply_chain_limits(length, count, tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        previous = None
        for number in range(4, 15):
            extra = {"reply_to_message": {"message_id": previous}} if previous else {}
            await store.ingest("999", "zarya_test", [event(number, -100, "X" * length, **extra)])
            previous = number
        event_id = (await store.db.one("SELECT id FROM events WHERE update_id=14"))[0]
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        result, _ = await selected(memory, event_id=event_id)
        assert len(result["conversation"]) == count
        assert sum(len(m["text"]) for m in result["conversation"]) <= 6000


async def test_conflicts_share_fact_text_budget(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Python")])
        await extract(memory, text="Python " + "A" * 490)
        await store.ingest("999", "zarya_test", [event(5, -100, "Rust")])
        await extract(memory, text="Rust " + "B" * 490, relation="contradicts", existing_fact_id=1)
        await filler(store, 30)
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE memory_facts SET text=? WHERE id>2", ("C" * 400,))
        result, _ = await selected(memory, query="Python")
        assert result["conflicts"]
        chars = sum(len(f["text"]) for f in result["facts"]) + sum(
            len(s["text"]) for c in result["conflicts"] for s in c["statements"]
        )
        assert chars <= 5000 and len(result["facts"]) < 12


@pytest.mark.parametrize("stale", ["ttl", "epoch"])
async def test_api_filters_stale_diagnostics_without_changing_answer(tmp_path, stale):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Привет")))
        dialogue.memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Заря, привет")])
        work = await answer(dialogue, None, 4)
        async with store.db.transaction() as conn:
            if stale == "ttl":
                await conn.execute(
                    "UPDATE dialogue_runs SET snapshot=json_set(snapshot,"
                    "'$.retrieval.at',0) WHERE id=?",
                    (work["run_id"],),
                )
            else:
                await conn.execute("UPDATE memory_epochs SET version=version+1 WHERE bot_id='999'")
        detail = await dialogue.details(work["run_id"])
        assert "retrieval" not in detail["snapshot"] and detail["response"] == "Привет"
