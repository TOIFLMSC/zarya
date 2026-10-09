import json

import pytest
from test_dialogue import FakeModel, drain
from test_memory import context, extract, ready
from test_memory_observations import observe
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_context import dependencies, valid_dependencies, valid_snapshot
from zarya.memory_observations import strength
from zarya.openai_adapter import ModelResult


async def chain(conn, template, length):
    """Acyclic retained facts; owner 77 keeps the scaffolding out of retrieval."""
    deps = dependencies()
    for index in range(length):
        cursor = await conn.execute(
            "INSERT INTO memory_facts(bot_id,chat_id,scope,access_version,sender_id,category,"
            "fact_key,text,provenance,state,created_at,updated_at,reviewed_at,"
            "context_dependencies) "
            "SELECT bot_id,chat_id,scope,access_version,'77','interest',?,text,'self','active',"
            "created_at,updated_at,reviewed_at,? FROM memory_facts WHERE id=?",
            (f"chain_{index}", json.dumps(deps), template),
        )
        deps = {**dependencies(), "facts": [{"id": cursor.lastrowid, "version": 1}]}
    return deps


@pytest.mark.parametrize("kind", ["fact", "summary", "tentative"])
async def test_retrieval_reserves_snapshot_depth_and_answer_is_deliverable(tmp_path, kind):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        if kind == "tentative":
            fact, _, _ = await observe(memory, 4, "Пишу код на Python", age=4000)
            fact, _, _ = await observe(
                memory,
                5,
                "Изучаю библиотеки Python",
                age=2000,
                relation="supports",
                existing=fact["id"],
            )
            fact, batch, _ = await observe(
                memory,
                6,
                "Сравниваю версии Python",
                relation="supports",
                existing=fact["id"],
            )
            fid = fact["id"]
        else:
            await store.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
            batch = await extract(memory)
            fid = (await context(memory))["facts"][0]["id"]
        async with store.db.transaction() as conn:
            deps = await chain(conn, fid, 7 if kind == "tentative" else 8)
            if kind == "fact":
                await conn.execute(
                    "UPDATE memory_facts SET context_dependencies=? WHERE id=?",
                    (json.dumps(deps), fid),
                )
            elif kind == "summary":
                await conn.execute(
                    "UPDATE memory_batches SET dependencies=? WHERE id=?",
                    (json.dumps(deps), batch["id"]),
                )
            else:
                await conn.execute(
                    "UPDATE memory_evidence SET context_dependencies=? WHERE fact_id=?",
                    (json.dumps(deps), fid),
                )
                assert (await strength(conn, "999", fid, depth=0))["eligible"]
                assert not (await strength(conn, "999", fid, depth=1))["eligible"]
            if kind != "tentative":
                assert await valid_dependencies(conn, "999", "-100", 2, deps, depth=0)
                assert not await valid_dependencies(conn, "999", "-100", 2, deps, depth=1)
            selected = (await memory.context(conn, "999", "-100", 2, ["42"]))[0]
            assert await valid_snapshot(conn, "999", {"memory": selected})
            if kind == "summary":
                assert batch["id"] not in [r["id"] for r in selected["summaries"]]
                assert fid in [r["id"] for r in selected["facts"]]
            else:
                assert fid not in [r["id"] for r in selected["facts"]]
                assert selected["summaries"]  # Independent episodes remain usable.
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="да, знакомое имя")))
        dialogue.memory = memory
        await drain(dialogue)
        await store.ingest("999", "zarya_test", [event(20, -100, "Заря, ты любишь Алексея?")])
        work = await dialogue.claim("999")
        assert work and not work.get("skip")
        await dialogue.execute(work)
        assert (await dialogue.details(work["run_id"]))["state"] == "completed"
        assert await store.db.one("SELECT 1 FROM outbox WHERE job_id=?", (work["job_id"],))


@pytest.mark.parametrize("revoke", [False, True])
async def test_memory_cancellation_reason_does_not_overwrite_successful_call(tmp_path, revoke):
    async with store_at(tmp_path) as store:
        await ready(store)
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        batch = await extract(memory)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="ответ")))
        dialogue.memory = memory
        await drain(dialogue)
        await store.ingest("999", "zarya_test", [event(5, -100, "Заря, привет")])
        work = await dialogue.claim("999")
        assert work and work["snapshot"]["memory"]["summaries"]
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE memory_batches SET summary=NULL WHERE id=?", (batch["id"],))
            if revoke:
                await conn.execute(
                    "UPDATE telegram_access SET state='revoked' WHERE subject_id='-100'"
                )
        await dialogue.execute(work)
        detail = await dialogue.details(work["run_id"])
        assert detail["state"] == "cancelled"
        assert detail["error"] == (None if revoke else "memory_changed")
        assert await store.db.one(
            "SELECT state,error_code FROM model_calls WHERE run_id=?", (work["run_id"],)
        ) == ("completed", None)
        assert not await store.db.one("SELECT 1 FROM outbox WHERE job_id=?", (work["job_id"],))
