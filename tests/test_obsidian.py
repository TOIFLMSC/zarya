import asyncio
import json
import re
import shutil
import threading
from uuid import uuid4

import pytest
from test_app import running, setup
from test_graph import populated
from test_memory import grant_private, group_answer, mutation
from test_source_material import deliver
from test_telegram import store_at

from zarya import obsidian_fs as fs
from zarya.database import ConflictError
from zarya.memory_context import dependencies
from zarya.memory_logic import command
from zarya.obsidian import ObsidianBridge


async def bridge_at(store, path):
    memory = await populated(store, path)
    project = path / "vault" / "Projects" / "Zarya"
    project.mkdir(parents=True)
    config = path / "obsidian.local.json"
    config.write_text(json.dumps({"enabled": True, "project_path": str(project)}))
    bridge = ObsidianBridge(memory, config, lambda: "999")
    await bridge.scan()
    return bridge, memory


async def file_row(bridge, kind="fact"):
    async with bridge.db.reader.execute("SELECT * FROM obsidian_files WHERE kind=?", (kind,)) as c:
        row = await c.fetchone()
        return dict(zip((col[0] for col in c.description), row, strict=True))


async def edit(bridge, text="Любит Rust"):
    row = await file_row(bridge)
    note = bridge.root / row["path"]
    note.write_text(row["prefix"] + text + row["suffix"], encoding="utf-8")
    await bridge.scan()  # Stable twice; first observation must not create a proposal.
    assert (await bridge.listing("999", 0, 0))["total"] == 0
    await bridge.scan()
    changes = (await bridge.listing("999", 0, 0))["changes"]
    return changes[0], note


async def test_edit_apply_restart_undo_idempotence_and_no_paid_calls(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        calls = await store.db.one("SELECT count(*) FROM model_calls")
        change, note = await edit(bridge)
        assert change["state"] == "pending" and change["before_text"] == "Любит Python"
        await bridge.decide("999", change["id"], "apply")
        first = await store.db.one(
            "SELECT text,version FROM memory_facts WHERE id=?", (change["fact_id"],)
        )
        await bridge.decide("999", change["id"], "apply")
        row = await store.db.one(
            "SELECT text,version FROM memory_facts WHERE id=?", (change["fact_id"],)
        )
        assert row == first and row[0] == "Любит Rust" and row[1] > change["expected_version"]
        restarted = ObsidianBridge(memory, bridge.config_file, lambda: "999")
        await restarted.scan()
        assert restarted.epoch == bridge.epoch and "Любит Rust" in note.read_text(encoding="utf-8")
        history = (await restarted.listing("999", 0, 0))["history"]
        assert history[0]["can_undo"]
        undo_id = str(uuid4())
        await restarted.undo("999", history[0]["id"], undo_id)
        undone = await store.db.one(
            "SELECT text,version FROM memory_facts WHERE id=?", (change["fact_id"],)
        )
        await restarted.undo("999", history[0]["id"], undo_id)
        assert (
            await store.db.one(
                "SELECT text,version FROM memory_facts WHERE id=?", (change["fact_id"],)
            )
        ) == undone
        assert undone[0] == "Любит Python" and undone[1] > row[1]
        assert await store.db.one("SELECT count(*) FROM model_calls") == calls


async def test_simultaneous_web_edit_conflict_preserves_draft_and_reject_exports(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        fact = (await memory.profile("999", "-100"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "edit", "Любит Go"))
        await bridge.scan()
        view = (await bridge.listing("999", 0, 0))["changes"][0]
        assert view["state"] == "conflict" and view["proposed_text"] == "Любит Rust"
        assert "Любит Rust" in note.read_text(encoding="utf-8")
        with pytest.raises(ConflictError):
            await bridge.decide("999", change["id"], "apply")
        await bridge.decide("999", change["id"], "reject")
        await bridge.scan()
        assert "Любит Go" in note.read_text(encoding="utf-8")


async def test_reject_without_sql_change_restores_file(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        await bridge.decide("999", change["id"], "reject")
        await bridge.scan()
        assert "Любит Python" in note.read_text(encoding="utf-8")
        assert (await bridge.listing("999", 0, 0))["total"] == 0


@pytest.mark.parametrize("damage", ["identity", "oversized_text", "partial", "readonly"])
async def test_damaged_or_protected_notes_never_mutate(tmp_path, damage):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        row = await file_row(bridge, "source" if damage == "readonly" else "fact")
        note = bridge.root / row["path"]
        text = note.read_text(encoding="utf-8")
        if damage == "identity":
            text = text.replace('bot_id: "999"', 'bot_id: "888"')
        elif damage == "oversized_text":
            text = row["prefix"] + "a" * 501 + row["suffix"]
        elif damage == "partial":
            text = text[:20]
        else:
            text += "changed"
        note.write_text(text, encoding="utf-8")
        await bridge.scan()
        await bridge.scan()
        change = (await bridge.listing("999", 0, 0))["changes"][0]
        assert change["state"] == "invalid"
        with pytest.raises(ValueError):
            await bridge.decide("999", change["id"], "apply")
        assert (await store.db.one("SELECT text,version FROM memory_facts")) == ("Любит Python", 1)


async def test_missing_file_unknown_note_and_file_compare_guard(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        row = await file_row(bridge)
        note = bridge.root / row["path"]
        original = note.read_bytes()
        note.unlink()
        unknown = bridge.root / "My own note.md"
        unknown.write_text("my data")
        await bridge.scan()
        assert not note.exists() and unknown.read_text() == "my data"
        assert (await store.db.one("SELECT text FROM memory_facts"))[0] == "Любит Python"
        note.write_bytes(original)
        assert not fs.write(bridge.root, row["path"], b"overwrite", "wrong hash")
        assert note.read_bytes() == original


@pytest.mark.parametrize("reason", ["delete", "revoke", "expired"])
async def test_privacy_purges_draft_history_and_sync_return_never_resurrects(tmp_path, reason):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        saved = note.read_bytes()
        fact = (await memory.profile("999", "-100"))["facts"][0]
        if reason == "delete":
            await memory.mutate("999", fact["id"], mutation(fact, "delete"))
        else:
            async with store.db.transaction() as c:
                if reason == "revoke":
                    await c.execute(
                        "UPDATE telegram_access SET state='revoked' WHERE subject_id='-100'"
                    )
                else:
                    await c.execute("UPDATE memory_facts SET reviewed_at=0")
        await bridge.scan()
        assert not note.exists()
        assert (
            await store.db.one(
                "SELECT before_text,proposed_text,state FROM obsidian_changes WHERE id=?",
                (change["id"],),
            )
        ) == (None, None, "invalidated")
        note.write_bytes(saved)  # Delayed old copy returning from Sync.
        await bridge.scan()
        assert not note.exists()
        with pytest.raises(ValueError):
            await bridge.decide("999", change["id"], "apply")


async def test_operation_id_payload_collision_and_undo_stale(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        fact = (await memory.profile("999", "-100"))["facts"][0]
        operation = str(uuid4())
        body = mutation(fact, "edit", "Любит Rust")
        await memory.mutate("999", fact["id"], body, operation_id=operation)
        await memory.mutate("999", fact["id"], body, operation_id=operation)
        with pytest.raises(ConflictError):
            await memory.mutate(
                "999", fact["id"], mutation(fact, "edit", "different"), operation_id=operation
            )
        fact = (await memory.profile("999", "-100"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "edit", "Любит Go"))
        with pytest.raises(ConflictError):
            await bridge.undo("999", operation, str(uuid4()))


async def test_old_epoch_guard_and_coverage_not_deletion(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        monkeypatch.setitem(__import__("zarya.graph", fromlist=["LIMITS"]).LIMITS, "facts", 0)
        await bridge.scan()
        assert note.exists()  # Outside current graph coverage is not a deletion.
        async with store.db.transaction() as c:
            await c.execute("UPDATE obsidian_changes SET epoch='old' WHERE id=?", (change["id"],))
        with pytest.raises(ConflictError):
            await bridge.decide("999", change["id"], "apply")


async def test_changed_file_after_preview_requires_scan(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        note.write_text("partial", encoding="utf-8")
        with pytest.raises(ValueError):
            await bridge.decide("999", change["id"], "apply")


async def test_bridge_api_auth_and_csrf(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/obsidian")).status_code == 401
        await setup(client, tmp_path)
        result = await client.get("/api/obsidian")
        assert result.status_code == 200 and not result.json()["configured"]
        assert result.headers["cache-control"] == "no-store"
        assert (
            await client.post("/api/obsidian/scan", json={}, headers={"X-CSRF-Token": ""})
        ).status_code == 403
        assert (
            await client.post(
                "/api/obsidian/scan",
                json={},
                headers={
                    "Origin": "http://127.0.0.1:8787",
                    "X-CSRF-Token": client.headers["X-CSRF-Token"],
                },
            )
        ).status_code == 409


def test_filesystem_rejects_traversal_and_foreign_existing_file(tmp_path):
    with pytest.raises(ValueError):
        fs.read(tmp_path, "../outside")
    note = tmp_path / "foreign.md"
    note.write_text("mine")
    assert not fs.write(tmp_path, "foreign.md", b"replace", None)
    assert note.read_text() == "mine"


async def test_crash_after_new_note_write_then_revoke_cleans_owned_intent(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        row = await file_row(bridge)
        note = bridge.root / row["path"]
        note.unlink()
        async with store.db.transaction() as c:
            await c.execute("DELETE FROM obsidian_files WHERE path=?", (row["path"],))
        publish = fs.Reservation.publish

        def interrupted(lease, data):
            publish(lease, data)
            if lease.stream.name is not None:
                raise RuntimeError("simulated crash")

        monkeypatch.setattr(fs.Reservation, "publish", interrupted)
        with pytest.raises(RuntimeError):
            await bridge.scan()
        assert note.exists()
        assert (
            await store.db.one("SELECT state FROM obsidian_files WHERE path=?", (row["path"],))
        )[0] == "writing"
        monkeypatch.setattr(fs.Reservation, "publish", publish)
        async with store.db.transaction() as c:
            await c.execute("UPDATE telegram_access SET state='revoked' WHERE subject_id='-100'")
        await bridge.scan()
        assert not note.exists()


async def test_revoke_waits_for_observation_then_scrubs_proposal(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        row = await file_row(bridge)
        note = bridge.root / row["path"]
        note.write_text(row["prefix"] + "Любит Rust" + row["suffix"], encoding="utf-8")
        await bridge.scan()
        entered, release = threading.Event(), threading.Event()
        read = fs.read

        def paused(root, name):
            data = read(root, name)
            if name == row["path"]:
                entered.set()
                assert release.wait(10)
            return data

        monkeypatch.setattr(fs, "read", paused)
        scanning = asyncio.create_task(bridge.scan())
        assert await asyncio.to_thread(entered.wait, 10)
        fact = (await memory.profile("999", "-100"))["facts"][0]
        deleting = asyncio.create_task(memory.mutate("999", fact["id"], mutation(fact, "delete")))
        await asyncio.sleep(0)
        assert not deleting.done()
        release.set()
        await scanning
        await deleting
        rows = await store.db.all("SELECT before_text,proposed_text,state FROM obsidian_changes")
        assert rows and all(r == (None, None, "invalidated") for r in rows)


async def test_missing_parent_folder_and_rejected_draft_can_be_submitted_again(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        change, note = await edit(bridge)
        await bridge.decide("999", change["id"], "reject")
        await bridge.scan()
        change2, _ = await edit(bridge)
        assert change2["id"] != change["id"] and change2["state"] == "pending"
        shutil.rmtree(note.parent)  # Known test-owned temporary directory only.
        await bridge.scan()
        assert not note.exists()
        assert (await store.db.one("SELECT count(*) FROM memory_facts WHERE state='deleted'"))[
            0
        ] == 0


async def test_journal_retention_runs_without_configured_bridge(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        fact = (await memory.profile("999", "-100"))["facts"][0]
        await memory.mutate("999", fact["id"], mutation(fact, "edit", "Любит Rust"))
        async with store.db.transaction() as c:
            await c.execute("UPDATE memory_commands SET created_at='2000-01-01'")
        disabled = ObsidianBridge(memory, tmp_path / "absent.json", lambda: "999")
        assert not disabled.root
        await memory.cleanup("999", force=True)
        assert (await store.db.one("SELECT before_text,after_text FROM memory_commands")) == (
            None,
            None,
        )


async def test_fence_escaped_and_profile_edge_preserved(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        async with store.db.transaction() as c:
            await c.execute(
                "UPDATE memory_facts SET text=?,version=version+1",
                ("text\n````\n![](https://example.org/tracker)\n[[evil]]",),
            )
        await bridge.scan()
        row = await file_row(bridge)
        text = (bridge.root / row["path"]).read_text(encoding="utf-8")
        assert "`````text" in text and "about: [[Участник " in text
        content = re.sub(r"(?ms)^(`{5,})text\n.*?^\1$", "", text)
        assert "https://example.org" not in content and "[[evil]]" not in content


async def test_binding_marker_crash_recovers_same_epoch(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        # Reset only this test's owned temporary binding before bootstrap simulation.
        shutil.rmtree(bridge.root)
        async with store.db.transaction() as c:
            await c.execute("DELETE FROM obsidian_binding")
            await c.execute("DELETE FROM obsidian_files")
        write = fs.write

        def interrupted(root, name, data, expected):
            result = write(root, name, data, expected)
            if name.endswith(".zarya-bridge.json"):
                raise RuntimeError("simulated interruption")
            return result

        monkeypatch.setattr(fs, "write", interrupted)
        with pytest.raises(RuntimeError):
            await bridge.scan()
        saved = (await store.db.one("SELECT epoch FROM obsidian_binding"))[0]
        monkeypatch.setattr(fs, "write", write)
        restarted = ObsidianBridge(bridge.memory, bridge.config_file, lambda: "999")
        await restarted.scan()
        assert restarted.epoch == saved


async def test_empty_reserved_publication_recovers_and_foreign_collision_is_preserved(
    tmp_path, monkeypatch
):
    async with store_at(tmp_path) as store:
        bridge, _ = await bridge_at(store, tmp_path)
        row = await file_row(bridge)
        note = bridge.root / row["path"]
        note.unlink()
        async with store.db.transaction() as c:
            await c.execute("DELETE FROM obsidian_files WHERE path=?", (row["path"],))
        publish = fs.Reservation.publish

        def before_publish(lease, data):
            raise RuntimeError("before bytes")

        monkeypatch.setattr(fs.Reservation, "publish", before_publish)
        with pytest.raises(RuntimeError):
            await bridge.scan()
        assert note.read_bytes() == b""
        monkeypatch.setattr(fs.Reservation, "publish", publish)
        await bridge.scan()
        assert "Любит Python" in note.read_text(encoding="utf-8")
        assert (
            await store.db.one("SELECT state FROM obsidian_files WHERE path=?", (row["path"],))
        )[0] == "ready"
        note.write_text("foreign content", encoding="utf-8")
        async with store.db.transaction() as c:
            await c.execute("DELETE FROM obsidian_files WHERE path=?", (row["path"],))
        await bridge.scan()
        assert (
            await store.db.one("SELECT state FROM obsidian_files WHERE path=?", (row["path"],))
            is None
        )
        async with store.db.transaction() as c:
            await c.execute("UPDATE telegram_access SET state='revoked' WHERE subject_id='-100'")
        await bridge.scan()
        assert note.read_text(encoding="utf-8") == "foreign content"


def test_native_exclusive_file_and_directory_handles(tmp_path):
    if fs.os.name != "nt":
        pytest.skip("Windows sharing semantics")
    parent = tmp_path / "managed"
    parent.mkdir()
    note = parent / "note.md"
    note.write_text("old")
    with fs.opened(note):
        with pytest.raises(OSError):
            note.write_text("new")
    with fs.parents(parent, "note.md"):
        with pytest.raises(OSError):
            parent.rename(tmp_path / "renamed")


async def test_private_fact_edit_and_undo_never_restore_sharing(tmp_path):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        async with store.db.transaction() as c:
            await c.execute(
                "INSERT INTO memory_facts(bot_id,chat_id,scope,access_version,sender_id,category,"
                "fact_key,text,provenance,state,created_at,updated_at,reviewed_at) "
                "SELECT bot_id,'42','private',2,'42',category,'private_python',text,"
                "provenance,state,created_at,updated_at,reviewed_at FROM memory_facts LIMIT 1"
            )
            await c.execute(
                "INSERT INTO memory_shares SELECT id,version,'active','participant','42',NULL,"
                "updated_at FROM memory_facts WHERE chat_id='42'"
            )
        await bridge.scan()
        rows = await store.db.all(
            "SELECT path FROM obsidian_files WHERE chat_id='42' AND kind='fact'"
        )
        assert len(rows) == 1
        async with store.db.read_lock:
            from zarya.memory_command_apply import item

            row = await item(
                store.db.reader, "SELECT * FROM obsidian_files WHERE path=?", (rows[0][0],)
            )
        note = bridge.root / row["path"]
        note.write_text(row["prefix"] + "Любит Rust" + row["suffix"], encoding="utf-8")
        await bridge.scan()
        await bridge.scan()
        change = (await bridge.listing("999", 0, 0))["changes"][0]
        await bridge.decide("999", change["id"], "apply")
        history = (await bridge.listing("999", 0, 0))["history"][0]
        await bridge.undo("999", history["id"], str(uuid4()))
        assert (
            await store.db.one(
                "SELECT state FROM memory_shares WHERE fact_id=?", (change["fact_id"],)
            )
        )[0] == "revoked"
        fact = (await memory.profile("999", "42"))["facts"][0]
        assert fact["text"] == "Любит Python" and fact["share"]["state"] != "active"


@pytest.mark.parametrize("withdraw", ["consent", "access", "forget"])
async def test_private_withdrawal_scrubs_curated_group_history(tmp_path, withdraw):
    async with store_at(tmp_path) as store:
        bridge, memory = await bridge_at(store, tmp_path)
        async with store.db.transaction() as c:
            await c.execute(
                "INSERT INTO memory_facts(bot_id,chat_id,scope,access_version,sender_id,category,"
                "fact_key,text,provenance,state,created_at,updated_at,reviewed_at) "
                "SELECT bot_id,'42','private',2,'42',category,'private_flower','Любит орхидеи',"
                "provenance,state,created_at,updated_at,reviewed_at FROM memory_facts LIMIT 1"
            )
        await grant_private(store)
        await group_answer(store, memory)
        job = await store.db.one(
            "SELECT job_id FROM dialogue_runs WHERE chat_id='-100' ORDER BY id DESC LIMIT 1"
        )
        assert job
        await deliver(store, {"job_id": job[0]}, 1000)
        sent = await store.db.one(
            "SELECT r.id,o.id FROM dialogue_runs r JOIN outbox o ON o.job_id=r.job_id "
            "WHERE r.chat_id='-100' AND o.state='sent' ORDER BY r.id DESC LIMIT 1"
        )
        assert sent
        fact = (await memory.profile("999", "-100"))["facts"][0]
        deps = {**dependencies(), "runs": [{"id": sent[0], "outbox_id": sent[1]}]}
        async with store.db.transaction() as c:
            await c.execute(
                "UPDATE memory_facts SET text='Любит орхидеи',context_dependencies=? WHERE id=?",
                (json.dumps(deps), fact["id"]),
            )
        await bridge.scan()
        await memory.mutate("999", fact["id"], mutation(fact, "edit", "Любит Python"))
        journal = await store.db.one(
            "SELECT before_text FROM memory_commands WHERE fact_id=?", (fact["id"],)
        )
        assert journal == ("Любит орхидеи",)
        assert (
            await store.db.one(
                "SELECT context_dependencies FROM memory_facts WHERE id=?", (fact["id"],)
            )
        ) == (None,)
        # Revocation must work synchronously even with the filesystem bridge stopped.
        if withdraw == "access":
            await store.decide("999", "private", "42", "revoked", 2)
        else:
            private = (await memory.profile("999", "42"))["facts"][0]
            action = "revoke" if withdraw == "consent" else "forget"
            cmd = f"/memory {action} {private['id']}"
            async with store.db.transaction() as c:
                await command(c, "999", "42", "private", "42", 103, cmd)
        assert not (await bridge.listing("999", 0, 0))["history"]
        assert not await store.db.one(
            "SELECT 1 FROM obsidian_files WHERE chat_id='-100' AND baseline IS NOT NULL"
        )
        await bridge.scan()
        assert all(
            "Любит орхидеи" not in p.read_text(encoding="utf-8")
            for p in (bridge.root / "Группы").rglob("*.md")
        )
