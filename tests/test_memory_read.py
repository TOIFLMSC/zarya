import json
import time

import pytest
from test_dialogue import FakeModel
from test_memory import extract, mutation, ready
from test_memory_depth import chain
from test_memory_observations import observe
from test_memory_retrieval import filler
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.dialogue_logic import model_input
from zarya.memory import MemoryEngine
from zarya.memory_context import (
    RETENTION,
    SOURCE_ROW,
    dependencies,
    reply_context,
    valid_dependencies,
    valid_snapshot,
)
from zarya.memory_read import BATCH_SIZE, MemoryRead
from zarya.memory_retrieval import select_context
from zarya.openai_adapter import ModelResult
from zarya.source_material import accepted


async def source_rows(conn, count, *, received=None):
    rows = []
    for number in range(100, 100 + count):
        cursor = await conn.execute(
            "INSERT INTO memory_sources(bot_id,chat_id,scope,message_id,event_id,sender_id,name,"
            "text,received_at,access_version) VALUES ('999','-100','group',?,?,'42','Имя',"
            "'Текст',?,2)",
            (number, number, received if received is not None else time.time()),
        )
        rows.append({"id": cursor.lastrowid, "event_id": number})
    return rows


async def test_many_refs_are_read_in_bounded_batches_with_identical_result(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        async with store.db.transaction() as conn:
            refs = await source_rows(conn, BATCH_SIZE * 2 + 3)
            deps = {**dependencies(), "sources": refs + refs[:2]}
            counts = []
            for enabled in (False, True):
                queries = []
                await conn.set_trace_callback(
                    lambda sql, queries=queries: (
                        queries.append(sql) if sql.startswith("SELECT") else None
                    )
                )
                with MemoryRead(conn, enabled=enabled) as read:
                    assert await valid_dependencies(conn, "999", "-100", 2, deps, read=read)
                await conn.set_trace_callback(None)
                counts.append(len(queries))
                assert read.closed and not read.rows
            assert counts[0] == len(deps["sources"]) + 1
            assert counts[1] == 4  # One access row, three bounded source batches.


@pytest.mark.parametrize("change", ["source", "grant", "delete", "repair"])
async def test_reads_are_invalidated_by_any_write_in_same_transaction(tmp_path, change):
    async with store_at(tmp_path) as store:
        await ready(store)
        await store.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        async with store.db.transaction() as conn:
            ref = (await source_rows(conn, 1))[0]
            deps = {**dependencies(), "sources": [ref]}
            with MemoryRead(conn) as read:
                assert await valid_dependencies(conn, "999", "-100", 2, deps, read=read)
                if change == "source":
                    await conn.execute(
                        "UPDATE memory_sources SET event_id=9999 WHERE id=?", (ref["id"],)
                    )
                elif change == "grant":
                    await conn.execute(
                        "UPDATE telegram_access SET version=version+1 WHERE subject_id='-100'"
                    )
                elif change == "delete":
                    await conn.execute("DELETE FROM memory_sources WHERE id=?", (ref["id"],))
                else:
                    await conn.execute("DELETE FROM research_messages WHERE message_id=4")
                    assert await accepted(conn, "999", "-100", 2, 4)  # Legacy INSERT repair.
                assert await valid_dependencies(conn, "999", "-100", 2, deps, read=read) == (
                    change == "repair"
                )
                assert read.changes == conn.total_changes


async def test_closed_cross_connection_and_cross_transaction_scopes(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        async with store.db.transaction() as conn:
            ref = (await source_rows(conn, 1))[0]
            reader = MemoryRead(conn)
            reader.__enter__()
            assert (await reader.one(conn, SOURCE_ROW, (ref["id"], "999", "-100", 2)))[1] == 1
        # External writer changes do not increment this reader's total_changes.
        async with store.db.read_lock:
            await store.db.reader.execute("PRAGMA query_only=OFF")
            try:
                await store.db.reader.execute(
                    "UPDATE memory_sources SET valid=0 WHERE id=?", (ref["id"],)
                )
            finally:
                await store.db.reader.execute("PRAGMA query_only=ON")
        async with store.db.transaction() as conn:
            assert (await reader.one(conn, SOURCE_ROW, (ref["id"], "999", "-100", 2)))[1] == 0
            assert not reader.enabled and not reader.rows
            with pytest.raises(ValueError, match="scope_mismatch"):
                await reader.one(store.db.reader, SOURCE_ROW, (ref["id"], "999", "-100", 2))
        reader.__exit__()
        with pytest.raises(ValueError, match="scope_mismatch"):
            await reader.one(conn, SOURCE_ROW, (ref["id"], "999", "-100", 2))
        with pytest.raises(ValueError, match="scope_closed"):
            reader.__enter__()


async def test_autocommit_does_not_memoize_rows(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        async with store.db.transaction() as conn:
            ref = (await source_rows(conn, 1))[0]
        with MemoryRead(store.db.reader) as read:
            assert (await read.one(store.db.reader, SOURCE_ROW, (ref["id"], "999", "-100", 2)))[
                1
            ] == 1
            assert not read.rows and not read.enabled
            async with store.db.transaction() as conn:
                await conn.execute("UPDATE memory_sources SET valid=0 WHERE id=?", (ref["id"],))
            assert (await read.one(store.db.reader, SOURCE_ROW, (ref["id"], "999", "-100", 2)))[
                1
            ] == 0


async def test_cached_rows_still_expire_and_keep_scope_checks(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        await ready(store)
        now = time.time()
        async with store.db.transaction() as conn:
            ref = (await source_rows(conn, 1, received=now - RETENTION + 1))[0]
            deps = {**dependencies(), "sources": [ref]}
            with MemoryRead(conn) as read:
                assert await valid_dependencies(conn, "999", "-100", 2, deps, read=read)
                assert not await valid_dependencies(conn, "other", "-100", 2, deps, read=read)
                assert not await valid_dependencies(conn, "999", "-200", 2, deps, read=read)
                assert not await valid_dependencies(conn, "999", "-100", 3, deps, read=read)
                monkeypatch.setattr("zarya.memory_context.time.time", lambda: now + 2)
                assert not await valid_dependencies(conn, "999", "-100", 2, deps, read=read)


@pytest.mark.parametrize("mode", ["legacy", "shadow", "graph"])
async def test_exact_context_and_model_input_with_and_without_cache(tmp_path, mode, monkeypatch):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(memory)
        await filler(store, 35)
        await store.ingest("999", "zarya_test", [event(5, -100, "Имя Алексей")])
        await extract(memory, text="Имя Алексей", category="name", key="name")
        await store.ingest("999", "zarya_test", [event(6, -100, "Имя Борис")])
        await extract(
            memory,
            text="Имя Борис",
            category="name",
            key="name",
            relation="contradicts",
            existing_fact_id=37,
        )
        now = time.time()
        monkeypatch.setattr("zarya.memory_retrieval.time.time", lambda: now)
        selected, diagnostics = [], []
        async with store.db.transaction() as conn:
            for enabled in (False, True):
                diagnostic = {}
                with MemoryRead(conn, enabled=enabled) as read:
                    selected.append(
                        await select_context(
                            conn,
                            "999",
                            "-100",
                            2,
                            ["42"],
                            "Какое имя Борис и что про Python?",
                            mode,
                            1,
                            diagnostic,
                            current_query="Какое имя Борис и что про Python?",
                            recent=[],
                            event_id=None,
                            speaker="42",
                            thread=None,
                            read=read,
                        )
                    )
                diagnostics.append({k: v for k, v in diagnostic.items() if not k.endswith("_ms")})
        assert selected[0] == selected[1]
        assert diagnostics[0] == diagnostics[1]
        incoming = {"text": "Какое имя Борис и что про Python?"}
        assert model_input([], incoming, selected[0]) == model_input([], incoming, selected[1])


async def test_dag_depth_cycle_and_revoked_sharing_are_not_cached_decisions(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -200, "Люблю Python")])
        await extract(memory)
        fact = (await memory.profile("999", "-200"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "share"))
        snapshot = {
            "memory": {"facts": [{"id": fact["id"], "version": fact["version"], "shared": True}]},
            "retrieval_chat_id": "-100",
            "source_access_version": 2,
        }
        async with store.db.transaction() as conn:
            deps = await chain(conn, fact["id"], 8)
            with MemoryRead(conn) as read:
                assert await valid_dependencies(conn, "999", "-200", 2, deps, read=read)
                assert not await valid_dependencies(
                    conn, "999", "-200", 2, deps, depth=1, read=read
                )
                assert await valid_snapshot(conn, "999", snapshot, read=read)
                await conn.execute("UPDATE memory_shares SET state='revoked'")
                assert not await valid_snapshot(conn, "999", snapshot, read=read)
                cyclic = {
                    **dependencies(),
                    "facts": [{"id": fact["id"], "version": fact["version"]}],
                }
                await conn.execute(
                    "UPDATE memory_facts SET context_dependencies=? WHERE id=?",
                    (json.dumps(cyclic), fact["id"]),
                )
                assert not await valid_dependencies(conn, "999", "-200", 2, cyclic, read=read)


async def test_tentative_strength_loses_witness_within_cached_scope(tmp_path):
    from zarya.memory_observations import strength

    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        fact, _, _ = await observe(memory, 4, "Пишу код на Python", age=4000)
        fact, _, _ = await observe(
            memory, 5, "Изучаю Python", age=2000, relation="supports", existing=fact["id"]
        )
        fact, _, _ = await observe(
            memory, 6, "Проверяю библиотеки Python", relation="supports", existing=fact["id"]
        )
        async with store.db.transaction() as conn:
            with MemoryRead(conn) as read:
                assert (await strength(conn, "999", fact["id"], read=read))["eligible"]
                await conn.execute("UPDATE memory_sources SET valid=0 WHERE message_id=4")
                assert not (await strength(conn, "999", fact["id"], read=read))["eligible"]


async def test_sent_reply_is_identical_and_expires_inside_read_scope(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        await ready(store)
        adapter = FakeModel(ModelResult(text="В снимке 87,21-88,20 руб за USDT"))
        dialogue = DialogueEngine(store, adapter)
        dialogue.memory = MemoryEngine(store, adapter, tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Заря, курс USDT?")])
        work = await answer(dialogue, None, 4)
        await deliver(store, work, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [event(5, -100, "Сколько за 153.14 USDT?", reply_to_message={"message_id": 1000})],
        )
        event_id = (await store.db.one("SELECT id FROM events WHERE update_id=5"))[0]
        now = time.time()
        incoming = {"event_id": event_id}
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE recent_messages SET received_at=? WHERE message_id=1000",
                (now - RETENTION + 1,),
            )
            snapshots = []
            for enabled in (False, True):
                with MemoryRead(conn, enabled=enabled) as read:
                    first = await reply_context(conn, "999", "-100", 2, incoming, read=read)
                    assert first[0] and "87,21-88,20" in first[0]["text"]
                    snapshots.append(first)
                    # Integer clock values must not accidentally memoize retention SQL.
                    monkeypatch.setattr("zarya.memory_context.time.time", lambda: int(now + 2))
                    expired = await reply_context(conn, "999", "-100", 2, incoming, read=read)
                    assert expired == (None, dependencies())
                    monkeypatch.setattr("zarya.memory_context.time.time", lambda: now)
            assert snapshots[0] == snapshots[1]
