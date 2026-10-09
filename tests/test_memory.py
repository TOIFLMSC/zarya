import asyncio
import json
import re
import time
from datetime import UTC, datetime

import pytest
from test_app import running, setup
from test_dialogue import FakeModel, drain
from test_telegram import approve, event, store_at

from zarya.database import ConflictError
from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine, Proposal
from zarya.memory_logic import command
from zarya.models import MemoryMutation, Settings
from zarya.openai_adapter import ModelResult


async def ready(s):
    await s.ingest("999", "zarya_test", [event(1), event(2, -100), event(3, -200)])
    await approve(s)
    await approve(s, "group", "-100")
    await approve(s, "group", "-200")


async def extract(
    engine,
    *,
    text="Любит Python",
    category="interest",
    key="interest_python",
    provenance="self",
    sender="42",
    force=True,
    relation="new",
    existing_fact_id=None,
):
    work = await engine.claim("999", force=force)
    assert work
    result = ModelResult(
        text=json.dumps(
            {
                "summary": "Разговор о Python.",
                "facts": [
                    {
                        "sender_id": sender,
                        "category": category,
                        "key": key,
                        "text": text,
                        "provenance": provenance,
                        "source_ids": [work["sources"][0]["id"]],
                        "relation": relation,
                        "existing_fact_id": existing_fact_id,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    await engine.finish(work, result, 50)
    return work


async def context(engine, chat="-100", participants=None):
    async with engine.db.transaction() as conn:
        return (await engine.context(conn, "999", chat, 2, participants or ["42"]))[0]


def mutation(fact, action, text=None):
    return MemoryMutation(bot_id="999", expected_version=fact["version"], action=action, text=text)


async def test_group_topics_sharing_and_private_isolation(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python", message_thread_id=7)])
        await extract(engine)
        facts = (await context(engine))["facts"]
        assert facts[0]["text"] == "Любит Python"
        assert not (await context(engine, "-200"))["facts"]
        profile = await engine.profile("999", "-100")
        assert profile["facts"][0]["sources"][0]["thread_id"] == 7
        fact = profile["facts"][0]
        await engine.mutate("999", fact["id"], mutation(fact, "share"))
        assert (await context(engine, "-200"))["facts"][0]["shared"]
        assert not (await context(engine, "42"))["facts"]
        # All forum topics use the same group memory; the prompt preserves private boundaries.
        await s.ingest(
            "999",
            "zarya_test",
            [event(5, -100, "Заря, какой у меня интерес?", message_thread_id=8)],
        )
        dialogue = DialogueEngine(s, FakeModel())
        dialogue.memory = engine
        await drain(dialogue)
        run = await s.db.one("SELECT snapshot FROM dialogue_runs WHERE message_id=5")
        assert json.loads(run[0])["memory"]["facts"][0]["text"] == "Любит Python"
        assert not (await context(engine, "-200", ["77"]))["facts"]


async def test_private_export_auth_exact_version_and_revoke(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, text="Люблю Python")])
        await extract(engine)
        fact = (await engine.profile("999", "42"))["facts"][0]
        with pytest.raises(ValueError, match="собеседник"):
            await engine.mutate("999", fact["id"], mutation(fact, "share"))
        assert not (await context(engine))["facts"]
        async with s.db.transaction() as conn:
            listing = await command(conn, "999", "42", "private", "42", 100, "/memory")
            token = re.search(r"/memory allow (\w+)", listing)[1]
            denied = await command(
                conn, "999", "77", "private", "77", 101, f"/memory allow {token}"
            )
            assert "устарело" in denied
            result = await command(
                conn, "999", "42", "private", "42", 102, f"/memory allow {token}"
            )
            assert "Разрешено" in result
        shared = (await context(engine))["facts"][0]
        assert shared["text"] == "Любит Python" and "source" not in shared
        assert not (await context(engine))["summaries"]
        async with s.db.transaction() as conn:
            assert "прекращён" in await command(
                conn, "999", "42", "private", "42", 103, f"/memory revoke {fact['id']}"
            )
        assert not (await context(engine))["facts"]
        async with s.db.transaction() as conn:
            listing = await command(conn, "999", "42", "private", "42", 104, "/memory")
            token = re.search(r"/memory allow (\w+)", listing)[1]
        await engine.mutate("999", fact["id"], mutation(fact, "edit", "Любит Rust"))
        async with s.db.transaction() as conn:
            assert "устарело" in await command(
                conn, "999", "42", "private", "42", 105, f"/memory allow {token}"
            )


async def test_delete_clears_derivatives_tombstone_and_blocks_replay(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
        work = await extract(engine)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, что я люблю?")])
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Любишь Python.")))
        dialogue.memory = engine
        await drain(dialogue)
        run = await s.db.one("SELECT id FROM dialogue_runs WHERE message_id=5")
        await engine.mutate("999", fact["id"], mutation(fact, "delete"))
        assert not (await context(engine))["facts"]
        assert not (await context(engine))["summaries"]
        assert (
            await s.db.one(
                "SELECT text,valid FROM memory_sources WHERE id=?", (work["sources"][0]["id"],)
            )
        ) == ("", 0)
        assert (await s.db.one("SELECT payload FROM events WHERE update_id=4"))[0] == "{}"
        assert not await s.db.one("SELECT 1 FROM recent_messages WHERE message_id=4")
        assert (await dialogue.details(run[0]))["snapshot"]["invalidated"]
        for mode in ("recorded", "paid"):
            with pytest.raises(ValueError):
                await dialogue.replay(run[0], mode, 1)
        # Reapplying an old batch/proposal cannot resurrect the deleted fact.
        await engine.finish(work, ModelResult(text='{"summary":"bad","facts":[]}'), 1)
        assert (await engine.profile("999", "-100"))["facts"] == []
        assert (await s.db.one("SELECT COUNT(*) FROM memory_tombstones"))[0] == 1


async def test_source_edit_during_paid_call_and_recovery(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        work = await engine.claim("999", force=True)
        edit = event(5, -100, "Не люблю Python")
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 4
        await s.ingest("999", "zarya_test", [edit])
        await engine.finish(
            work,
            ModelResult(
                text='{"summary":"old","facts":[]}', usage={"input_tokens": 10, "output_tokens": 10}
            ),
            20,
        )
        batch = await s.db.one("SELECT state,summary FROM memory_batches WHERE id=?", (work["id"],))
        assert batch == ("cancelled", None)
        assert (
            await s.db.one(
                "SELECT cost_usd FROM model_calls WHERE memory_batch_id=?", (work["id"],)
            )
        )[0] > 0
        work = await engine.claim("999", force=True)
        assert work
        await engine.recover("999")
        assert (
            await s.db.one("SELECT state FROM model_calls WHERE memory_batch_id=?", (work["id"],))
        )[0] == "unknown"
        assert await engine.claim("999", force=True) is None


@pytest.mark.parametrize("kind", ["false_self", "foreign_source", "unknown_person"])
async def test_provenance_validator_rejects_forged_records(tmp_path, kind):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(4, -100, "42 любит Python", **{"from": {"id": 77, "first_name": "Друг"}}),
                event(5, -100, "Привет"),
            ],
        )
        work = await engine.claim("999", force=True)
        fact = {
            "sender_id": "42",
            "category": "interest",
            "key": "interest_python",
            "text": "Любит Python",
            "provenance": "self",
            "source_ids": [work["sources"][0]["id"]],
        }
        if kind == "foreign_source":
            fact["source_ids"] = [999999]
        if kind == "unknown_person":
            fact["sender_id"] = "777777"
        await engine.finish(
            work, ModelResult(text=json.dumps({"summary": "test", "facts": [fact]})), 3
        )
        assert (await s.db.one("SELECT state FROM memory_batches"))[0] == "invalid"
        assert not (await context(engine))["facts"]


async def test_conflict_manual_resolution_and_no_auto_overwrite(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Зови меня Алексей")])
        await extract(engine, text="Называть Алексей", category="name", key="preferred_name")
        await s.ingest("999", "zarya_test", [event(5, -100, "Зови меня Борис")])
        await extract(
            engine,
            text="Называть Борис",
            category="name",
            key="preferred_name",
            relation="contradicts",
            existing_fact_id=1,
        )
        assert not (await context(engine))["facts"]
        facts = (await engine.profile("999", "-100"))["facts"]
        assert all(f["state"] == "disputed" for f in facts)
        await engine.mutate("999", facts[0]["id"], mutation(facts[0], "accept"))
        assert (await context(engine))["facts"][0]["text"] == "Называть Борис"
        with pytest.raises(ConflictError):
            await engine.mutate("999", facts[0]["id"], mutation(facts[0], "edit", "Old"))
        await s.ingest("999", "zarya_test", [event(6, -100, "Зови меня Игорь")])
        await extract(
            engine,
            text="Называть Игорь",
            category="name",
            key="preferred_name",
            relation="contradicts",
            existing_fact_id=facts[0]["id"],
        )
        assert (await context(engine))["facts"][0]["text"] == "Называть Борис"


async def test_revocation_cancel_finish_and_delivery_epoch(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(engine)
        dialogue = DialogueEngine(s, FakeModel())
        dialogue.memory = engine
        await drain(dialogue)
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, привет")])
        work = await dialogue.claim("999")
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await engine.mutate("999", fact["id"], mutation(fact, "edit", "Любит Rust"))
        await dialogue.execute(work)
        assert (await dialogue.details(work["run_id"]))["state"] == "cancelled"
        assert (await dialogue.details(work["run_id"]))["response"] == ""
        assert not await s.db.one("SELECT 1 FROM outbox WHERE state='pending'")
        await s.decide("999", "group", "-100", "revoked", 2)
        assert not (await context(engine))["facts"]
        assert await engine.profile("999", "-100") is None


async def test_batch_delay_disabled_and_no_unapproved_sources(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100, "Люблю Python")])
        assert not await s.db.one("SELECT 1 FROM memory_sources")
        await approve(s, "group", "-100")
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(2, -100, "Люблю Python")])
        assert await engine.claim("999") is None
        snapshot = await s.db.settings()
        await s.db.update_settings(snapshot.version, Settings(memory_enabled=False))
        assert await engine.claim("999", force=True) is None
        assert not (await context(engine))["facts"]


async def test_retention_preserves_facts_clears_text_and_media(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(engine)
        past = time.time() - 91 * 86400
        stamp = datetime.fromtimestamp(past, UTC).isoformat()
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE memory_sources SET received_at=?", (past,))
            await conn.execute("UPDATE events SET received_at=?", (stamp,))
            await conn.execute("UPDATE recent_messages SET received_at=?", (past,))
            await conn.execute("UPDATE memory_batches SET created_at=?", (stamp,))
            await conn.execute("UPDATE model_calls SET created_at=?", (stamp,))
        await engine.cleanup("999", force=True)
        assert (await s.db.one("SELECT text FROM memory_facts"))[0] == "Любит Python"
        assert (await s.db.one("SELECT text FROM memory_sources"))[0] == ""
        assert (await s.db.one("SELECT summary FROM memory_batches"))[0] is None
        assert (await s.db.one("SELECT request_json FROM model_calls"))[0] is None
        assert (await s.db.one("SELECT cost_usd FROM model_calls"))[0] > 0
        assert not await s.db.one("SELECT 1 FROM recent_messages")


async def test_paid_replay_cannot_restore_deleted_memory_during_call(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        memory = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, -100, "Люблю Python")])
        await extract(memory)
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Любишь Python.")))
        dialogue.memory = memory
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, что я люблю?")])
        await drain(dialogue)
        run = await s.db.one("SELECT id FROM dialogue_runs WHERE message_id=5")
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return ModelResult(
                text="Удалённый Python.", usage={"input_tokens": 10, "output_tokens": 10}
            )

        dialogue.adapter.generate = delayed
        task = asyncio.create_task(dialogue.replay(run[0], "paid", 1))
        await entered.wait()
        fact = (await memory.profile("999", "-100"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "delete"))
        release.set()
        replay = await task
        assert replay["state"] == "cancelled" and replay["response"] == ""
        assert replay["snapshot"]["invalidated"]
        assert replay["call"]["cost_usd"] > 0


@pytest.mark.parametrize("action", ["edit", "delete"])
async def test_collateral_export_removal_scrubs_groups(tmp_path, action):
    async with store_at(tmp_path) as s:
        await ready(s)
        memory = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, text="Люблю Python и чай")])
        work = await extract(memory)
        async with s.db.transaction() as conn:
            await memory.apply_proposal(
                conn,
                work,
                Proposal(
                    sender_id="42",
                    category="preference",
                    key="drink",
                    text="Любит чай",
                    provenance="self",
                    source_ids=[work["sources"][0]["id"]],
                ),
            )
        facts = (await memory.profile("999", "42"))["facts"]
        tea, python = facts
        async with s.db.transaction() as conn:
            listing = await command(conn, "999", "42", "private", "42", 100, "/memory")
            token = re.search(r"/memory allow (\w+)", listing)[1]
            await command(conn, "999", "42", "private", "42", 101, f"/memory allow {token}")
        assert (await context(memory))["facts"][0]["text"] == tea["text"]
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, привет")])
        dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Ты любишь чай.")))
        dialogue.memory = memory
        await drain(dialogue)
        await memory.mutate(
            "999",
            python["id"],
            mutation(python, action, "Любит Rust" if action == "edit" else None),
        )
        assert not (await context(memory))["facts"]
        assert not await s.db.one(
            "SELECT 1 FROM recent_messages WHERE chat_id='-100' AND role='assistant'"
        )
        rows = await s.db.all("SELECT snapshot,response FROM dialogue_runs WHERE chat_id='-100'")
        assert all(json.loads(r[0]).get("invalidated") and r[1] is None for r in rows)


async def grant_private(s):
    async with s.db.transaction() as conn:
        listing = await command(conn, "999", "42", "private", "42", 100, "/memory")
        token = re.search(r"/memory allow (\w+)", listing)[1]
        await command(conn, "999", "42", "private", "42", 101, f"/memory allow {token}")


async def group_answer(s, memory):
    await s.ingest("999", "zarya_test", [event(50, -100, "Заря, что мне нравится?")])
    dialogue = DialogueEngine(s, FakeModel(ModelResult(text="Любишь Python.")))
    dialogue.memory = memory
    await drain(dialogue)
    return dialogue


async def test_aging_private_export_scrubs_recent_group_answers(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        memory = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, text="Люблю Python")])
        await extract(memory)
        await grant_private(s)
        await group_answer(s, memory)
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE memory_facts SET reviewed_at=?", (time.time() - 91 * 86400,))
        await memory.cleanup("999", force=True)
        assert not (await context(memory))["facts"]
        assert not await s.db.one(
            "SELECT 1 FROM recent_messages WHERE chat_id='-100' AND role='assistant'"
        )
        assert (await s.db.one("SELECT state FROM memory_shares"))[0] == "revoked"


async def test_accepting_competing_fact_revokes_prior_export(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        memory = MemoryEngine(s, FakeModel(), tmp_path)
        await s.ingest("999", "zarya_test", [event(4, text="Люблю Python")])
        await extract(memory)
        fact = (await memory.profile("999", "42"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "accept"))
        await grant_private(s)
        await s.ingest("999", "zarya_test", [event(5, text="Люблю Rust")])
        await extract(memory, text="Любит Rust")
        await group_answer(s, memory)
        competitor = (await memory.profile("999", "42"))["facts"][0]
        assert competitor["state"] == "proposed"
        await memory.mutate("999", competitor["id"], mutation(competitor, "accept"))
        assert not (await context(memory))["facts"]
        assert not await s.db.one(
            "SELECT 1 FROM recent_messages WHERE chat_id='-100' AND role='assistant'"
        )
        assert (await s.db.one("SELECT state FROM memory_shares"))[0] == "revoked"


async def test_file_cleanup_retries_after_windows_lock(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        memory = MemoryEngine(s, FakeModel(), tmp_path)
        directory = tmp_path / "photos"
        directory.mkdir()
        file = directory / "old.jpg"
        file.write_bytes(b"old")
        async with s.db.transaction() as conn:
            await conn.execute("INSERT INTO memory_file_cleanup VALUES ('999','old.jpg')")
        original = type(file).unlink

        def locked(path, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(type(file), "unlink", locked)
        await memory.cleanup("999", force=True)
        assert file.exists()
        assert await s.db.one("SELECT 1 FROM memory_file_cleanup")
        monkeypatch.setattr(type(file), "unlink", original)
        await memory.cleanup("999", force=True)
        assert not file.exists()
        assert not await s.db.one("SELECT 1 FROM memory_file_cleanup")


async def test_memory_api_auth_csrf_conflicts_and_private_consent(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/memory")).status_code == 401
        assert (await client.get("/api/memory/chats/42")).status_code == 401
        await setup(client, tmp_path)
        app.state.telegram.bot = {"id": "999", "username": "zarya_test"}
        await ready(app.state.telegram_store)
        memory = app.state.memory
        memory.adapter = FakeModel()
        await app.state.telegram_store.ingest("999", "zarya_test", [event(4, text="Люблю Python")])
        await extract(memory)
        result = await client.get("/api/memory/chats/42")
        assert result.status_code == 200
        fact = result.json()["facts"][0]
        payload = mutation(fact, "share").model_dump()
        csrf = client.headers.pop("X-CSRF-Token")
        assert (
            await client.post(f"/api/memory/facts/{fact['id']}", json=payload)
        ).status_code == 403
        client.headers["X-CSRF-Token"] = csrf
        assert (
            await client.post(f"/api/memory/facts/{fact['id']}", json=payload)
        ).status_code == 409
        payload = mutation(fact, "edit", "Любит Rust").model_dump()
        assert (
            await client.post(f"/api/memory/facts/{fact['id']}", json=payload)
        ).status_code == 200
        assert (
            await client.post(f"/api/memory/facts/{fact['id']}", json=payload)
        ).status_code == 409
        await app.state.telegram_store.decide("999", "private", "42", "revoked", 2)
        assert (await client.get("/api/memory/chats/42")).status_code == 404
