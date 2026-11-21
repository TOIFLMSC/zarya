import json
import time

import pytest
from test_dialogue import FakeModel, private_ready
from test_telegram import event, store_at

from zarya import telegram_store
from zarya.dialogue import DialogueEngine
from zarya.telegram_store import stamp


async def seed_job(conn, number, chat, state, call_state=None, bot="999"):
    payload = json.dumps({"chat_id": chat, "scope": "private", "message_id": number})
    await conn.execute(
        "INSERT INTO events(id,bot_id,update_id,chat_id,payload,received_at) VALUES (?,?,?,?,?,?)",
        (number, bot, number, chat, "{}", stamp()),
    )
    await conn.execute(
        "INSERT INTO jobs(id,kind,event_id,state,payload,created_at) VALUES (?,?,?,?,?,?)",
        (number, "telegram", number, state, payload, stamp()),
    )
    if call_state:
        await conn.execute(
            "INSERT INTO model_calls(job_id,provider,model,state,created_at) VALUES (?,?,?,?,?)",
            (number, "test", "test", call_state, stamp()),
        )


@pytest.mark.parametrize(
    ("job_state", "call_state", "chat", "bot", "blocked"),
    [
        ("running", None, "42", "999", True),
        ("done", "started", "42", "999", True),
        ("unknown", "started", "42", "999", True),
        ("unknown", "unknown", "42", "999", False),
        ("running", "started", "77", "999", False),
        ("running", "started", "42", "888", False),
    ],
)
async def test_busy_chat_fences(tmp_path, job_state, call_state, chat, bot, blocked):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        async with store.db.transaction() as conn:
            await seed_job(conn, 100, chat, job_state, call_state, bot)
        work = await DialogueEngine(store, FakeModel()).claim("999")
        assert (work is None) == blocked
        if work:
            assert work["job_id"] != 100  # Never retry an unknown historical call.


async def test_backlog_claim_has_bounded_sql_work(tmp_path):
    async with store_at(tmp_path) as store:
        async with store.db.transaction() as conn:
            for number in range(1, 501):
                await seed_job(conn, number, "77", "done", "completed")
            for number in range(501, 601):
                await seed_job(conn, number, "77", "pending")
        ticks = 0

        def budget():
            nonlocal ticks
            ticks += 1
            return ticks > 5000  # 5M VM operations, independent of machine speed.

        await store.db.writer.set_progress_handler(budget, 1000)
        try:
            work = await DialogueEngine(store, FakeModel()).claim("999")
        finally:
            await store.db.writer.set_progress_handler(None, 0)
        assert work == {"skip": True}  # Unapproved synthetic chat: no model or delivery.
        assert await store.db.one("SELECT state FROM jobs WHERE id=501") == ("cancelled",)


@pytest.mark.parametrize(
    "condition",
    [
        "running",
        "done",
        "backoff",
        "shadow",
        "reaction",
        "rejected",
        "revoked",
        "blocked",
        "other_topic",
        "competing",
        "other_bot",
        "failed_part",
    ],
)
async def test_typing_candidates_equivalent_to_historical_query(tmp_path, monkeypatch, condition):
    original_row = telegram_store.row
    compared = 0

    async def compare(conn, sql, args):
        nonlocal compared
        result = await original_row(conn, sql, args)
        if sql.startswith("WITH live_jobs(id) AS MATERIALIZED"):
            # Differential check against the pre-optimization candidate source,
            # retaining its exact eligibility/ordering and the same clock values.
            legacy = "SELECT j.id,e.chat_id" + sql.split("SELECT j.id,e.chat_id", 1)[1]
            legacy = legacy.replace(
                "FROM live_jobs c CROSS JOIN jobs j ON j.id=c.id JOIN events e",
                "FROM jobs j JOIN events e",
            )
            assert result == await original_row(conn, legacy, args[1:])
            compared += 1
        return result

    monkeypatch.setattr(telegram_store, "row", compare)
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        engine = DialogueEngine(store, FakeModel())
        work = await engine.claim("999")
        if condition in {"done", "backoff", "failed_part"}:
            await engine.execute(work)
        async with store.db.transaction() as conn:
            if condition == "backoff":
                await conn.execute("UPDATE outbox SET next_attempt=9999999999")
            if condition == "failed_part":
                await conn.execute("UPDATE outbox SET state='failed' WHERE part_index=0")
            if condition in {"shadow", "reaction"}:
                path, value = (
                    ("$.initiative_mode", "shadow")
                    if condition == "shadow"
                    else ("$.irrelevant", "unused")
                )
                if condition == "shadow":
                    await conn.execute(
                        "UPDATE jobs SET payload=json_set(payload,?,?)", (path, value)
                    )
                else:
                    await conn.execute(
                        "UPDATE dialogue_runs SET snapshot=json_set(snapshot,"
                        "'$.behavior.plan.action','reaction')"
                    )
            if condition == "rejected":
                await conn.execute("UPDATE jobs SET typing_state='rejected'")
            if condition == "revoked":
                await conn.execute("UPDATE telegram_access SET version=version+1")
            if condition == "blocked":
                await conn.execute("INSERT INTO delivery_limits VALUES ('999','42',0,9999999999)")
            if condition == "other_bot":
                await conn.execute("UPDATE events SET bot_id='888'")
            if condition in {"competing", "other_topic"}:
                await seed_job(conn, 100, "42", "done")
                await conn.execute(
                    "UPDATE jobs SET typing_at=?,payload=json_set(payload,'$.thread_id',?) "
                    "WHERE id=100",
                    (time.time(), 7 if condition == "other_topic" else 0),
                )
        selected = await store.claim_typing("999")
        assert bool(selected) == (condition in {"running", "done", "backoff", "other_topic"})
        assert compared == 1


async def test_typing_idle_history_has_bounded_sql_work(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        async with store.db.transaction() as conn:
            for number in range(10, 1010):
                await seed_job(conn, number, "42", "done", "completed")
        ticks = 0

        def budget():
            nonlocal ticks
            ticks += 1
            return ticks > 100  # 100k VM operations even with a large history.

        await store.db.writer.set_progress_handler(budget, 1000)
        try:
            assert await store.claim_typing("999") is None
        finally:
            await store.db.writer.set_progress_handler(None, 0)
        assert ticks <= 100
