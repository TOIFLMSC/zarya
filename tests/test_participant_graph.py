import json
import sqlite3
import time
from pathlib import Path

import pytest
from test_dialogue import FakeModel, private_ready
from test_memory import extract
from test_participant_context import group_ready, human_event
from test_participant_summaries import TEXT, Summarizer, drain
from test_telegram import approve, store_at

from zarya.app import create_app
from zarya.backup import MARKER, backup, restore
from zarya.config import Config
from zarya.graph import GraphRepository, identity, vault_files
from zarya.memory import MemoryEngine
from zarya.models import Settings
from zarya.obsidian import ObsidianBridge
from zarya.participant_context import select
from zarya.participant_summaries import SummaryEngine, purge


async def seed(store, chat=-100, *, forwarded=False):
    grant = await group_ready(store) if chat < 0 else (await private_ready(store))
    if chat > 0:
        grant = (await store.db.one("SELECT version FROM telegram_access WHERE subject_id='42'"))[0]
    updates = [human_event(n, chat=chat, text=TEXT) for n in range(2, 22)]
    if forwarded:
        updates[1]["message"]["forward_origin"] = {
            "type": "hidden_user",
            "date": 1,
            "sender_user_name": "Чужой автор",
        }
        updates[1]["message"]["photo"] = [{"file_id": "not-exported", "width": 32, "height": 32}]
    question = human_event(22, chat=chat, text="Заря, продолжим")
    await store.ingest("999", "zarya_test", updates + [question])
    async with store.db.transaction() as c:
        await select(
            c, "999", str(chat), grant, question["message"], Settings(), enqueue_summaries=True
        )
    model = Summarizer()
    await drain(SummaryEngine(store, model))
    assert len(model.calls) == 3
    return model


async def bridge_at(store, path):
    project = path / "vault"
    project.mkdir()
    config = path / "obsidian.local.json"
    config.write_text(json.dumps({"enabled": True, "project_path": str(project)}))
    return ObsidianBridge(MemoryEngine(store, None, path), config, lambda: "999")


async def rows(bridge, kind):
    async with bridge.db.reader.execute("SELECT * FROM obsidian_files WHERE kind=?", (kind,)) as c:
        return [
            dict(zip((col[0] for col in c.description), r, strict=True)) for r in await c.fetchall()
        ]


async def test_exact_personal_nodes_namespaces_forwarding_and_no_model_calls(tmp_path):
    async with store_at(tmp_path) as s:
        model = await seed(s, forwarded=True)
        async with s.db.transaction() as c:
            await c.execute("UPDATE memory_sources SET processed=1 WHERE id!=3")
        await extract(MemoryEngine(s, FakeModel(), tmp_path), text="Синтетический факт")
        graph = GraphRepository(s.db.path)
        first = await graph.snapshot("999", "-100")
        second = await graph.snapshot("999", "-100")
        assert first["nodes"] == second["nodes"] and first["edges"] == second["edges"]
        summaries = [n for n in first["nodes"] if n["kind"] == "participant_summary"]
        sources = [n for n in first["nodes"] if n["kind"] == "participant_source"]
        assert len(summaries) == 3 and len(sources) == 15 and len(model.calls) == 3
        for n in summaries:
            assert (
                len(
                    [
                        e
                        for e in first["edges"]
                        if e["source"] == n["id"] and e["type"] == "derived_from"
                    ]
                )
                == 5
            )
            assert any(e["source"] == n["id"] and e["type"] == "about" for e in first["edges"])
            assert not any(
                e["source"] == n["id"] and e["type"] == "supports" for e in first["edges"]
            )
        f = next(n for n in sources if n["message_id"] == 3)
        assert f["forwarded"] and "отправителем" in f["text"] and "Вложения: фото" in f["text"]
        assert "not-exported" not in json.dumps(first)
        assert identity("999", "-100", "source", f["key"]) != f["id"]
        other = next(n for n in first["nodes"] if n["kind"] == "source" and n["key"] == f["key"])
        assert other["id"] != f["id"] and other["version"] != f["version"]
        assert first["coverage"]["included"] == {
            "participant_summaries": 3,
            "participant_sources": 15,
        }
        files = vault_files(first)
        note = next(v for k, v in files.items() if k.startswith("nodes/Персональная сводка"))
        assert "только для чтения" in note and "derived_from" in note and "read_only: true" in note


async def test_private_summary_stays_in_private_scope(tmp_path):
    async with store_at(tmp_path) as s:
        await seed(s, 42)
        await s.ingest("999", "zarya_test", [human_event(23)])
        await approve(s, "group", "-100")
        graph = GraphRepository(s.db.path)
        private = await graph.snapshot("999", "42")
        group = await graph.snapshot("999", "-100")
        assert len([n for n in private["nodes"] if n["kind"] == "participant_summary"]) == 3
        assert not any(n["kind"].startswith("participant_") for n in group["nodes"])
        assert await graph.snapshot("888", "42") is None


@pytest.mark.parametrize("change", ["edit", "erase", "regrant", "expire", "membership"])
async def test_invalidated_sources_disappear_and_managed_notes_are_scrubbed(
    tmp_path, monkeypatch, change
):
    async with store_at(tmp_path) as s:
        await seed(s)
        bridge = await bridge_at(s, tmp_path)
        await bridge.scan()
        registered = await rows(bridge, "participant_summary")
        sources = await rows(bridge, "participant_source")
        assert len(registered) == 3 and len(sources) == 15
        target = next(f for f in registered if f["node_key"] == "1")
        note = bridge.root / target["path"]
        saved = note.read_bytes()
        if change == "edit":
            update = human_event(3, text="Новая редакция исходника")
            await s.ingest(
                "999", "zarya_test", [{"update_id": 100, "edited_message": update["message"]}]
            )
        elif change == "erase":
            async with s.db.transaction() as c:
                await c.execute(
                    "UPDATE events SET payload='{}' WHERE id IN "
                    "(SELECT event_id FROM participant_summary_sources WHERE block_id=1)"
                )
        elif change == "regrant":
            await s.decide("999", "group", "-100", "approved", 2)
        elif change == "expire":
            later = time.time() + 91 * 86400
            monkeypatch.setattr("zarya.participant_summaries.time.time", lambda: later)
        else:
            async with s.db.transaction() as c:
                await c.execute("UPDATE telegram_access SET member_state='left'")
        await bridge.scan()
        assert not note.exists()
        note.write_bytes(saved)  # restored Sync copy is removed again
        await bridge.scan()
        assert not note.exists()
        snapshot = await GraphRepository(s.db.path).snapshot("999", "-100")
        assert snapshot is None or not any(
            n["kind"] == "participant_summary" and n["key"] == 1 for n in snapshot["nodes"]
        )


async def test_export_rechecks_summary_even_without_epoch_change(tmp_path):
    async with store_at(tmp_path) as s:
        await seed(s)
        bridge = await bridge_at(s, tmp_path)
        await bridge.bind()
        old = await bridge.graph.snapshot("999", "-100")
        epoch = old["memory_epoch"]
        async with s.db.transaction() as c:
            await purge(c, "id=1", ())
        assert (await s.db.one("SELECT version FROM memory_epochs"))[0] == epoch
        await bridge.export(old)
        assert not await s.db.all(
            "SELECT path FROM obsidian_files WHERE kind='participant_summary' AND node_key='1'"
        )
        bad_events = await s.db.all(
            "SELECT event_id FROM participant_summary_sources WHERE block_id=1"
        )
        paths = await s.db.all(
            "SELECT node_key FROM obsidian_files WHERE kind='participant_source'"
        )
        assert not {str(r[0]) for r in bad_events} & {p[0] for p in paths}


async def test_readonly_summary_edit_never_changes_sql_or_runs_a_model(tmp_path):
    async with store_at(tmp_path) as s:
        model = await seed(s)
        bridge = await bridge_at(s, tmp_path)
        await bridge.scan()
        f = (await rows(bridge, "participant_summary"))[0]
        note = bridge.root / f["path"]
        before = await s.db.all("SELECT id,result FROM participant_summary_blocks")
        note.write_text(
            note.read_text(encoding="utf-8") + "\nПридуманный результат", encoding="utf-8"
        )
        await bridge.scan()
        await bridge.scan()
        changes = (await bridge.listing("999", 0, 0))["changes"]
        assert len(changes) == 1 and changes[0]["state"] == "invalid"
        assert before == await s.db.all("SELECT id,result FROM participant_summary_blocks")
        assert len(model.calls) == 3 and not await s.db.all("SELECT id FROM memory_commands")


async def test_coverage_omission_does_not_remove_valid_note(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        await seed(s)
        bridge = await bridge_at(s, tmp_path)
        await bridge.scan()
        f = (await rows(bridge, "participant_summary"))[0]
        note = bridge.root / f["path"]
        monkeypatch.setitem(
            __import__("zarya.graph", fromlist=["LIMITS"]).LIMITS, "participant_summaries", 1
        )
        await bridge.scan()
        assert note.exists() and await bridge.file_valid(f)
        limited = await bridge.graph.snapshot("999", "-100")
        assert limited["coverage"]["truncated"]["participant_summaries"]
        assert limited["coverage"]["included"]["participant_summaries"] == 1
        monkeypatch.setitem(
            __import__("zarya.graph", fromlist=["LIMITS"]).LIMITS, "participant_sources", 4
        )
        limited = await bridge.graph.snapshot("999", "-100")
        assert limited["coverage"]["truncated"]["participant_sources"]
        assert not any(n["kind"] == "participant_summary" for n in limited["nodes"])


async def test_malformed_membership_excludes_whole_block(tmp_path):
    async with store_at(tmp_path) as s:
        await seed(s)
        async with s.db.transaction() as c:
            await c.execute(
                "DELETE FROM participant_summary_sources WHERE block_id=1 AND "
                "message_id=(SELECT MIN(message_id) FROM participant_summary_sources "
                "WHERE block_id=1)"
            )
        snapshot = await GraphRepository(s.db.path).snapshot("999", "-100")
        assert snapshot["coverage"]["excluded"]["participant_summaries"] == 1
        assert snapshot["coverage"]["included"]["participant_summaries"] == 2


async def test_media_dependency_invalidates_notes_without_memory_epoch_change(tmp_path):
    async with store_at(tmp_path) as s:
        await seed(s)
        bridge = await bridge_at(s, tmp_path)
        await bridge.scan()
        f = next(n for n in await rows(bridge, "participant_summary") if n["node_key"] == "1")
        epoch = await s.db.one("SELECT version FROM memory_epochs")
        async with s.db.transaction() as c:
            await c.execute(
                "UPDATE participant_summary_blocks SET media_refs='[{\"id\":999}]' WHERE id=1"
            )
        assert not await bridge.file_valid(f)
        await bridge.scan()
        assert not (bridge.root / f["path"]).exists()
        assert await s.db.one("SELECT version FROM memory_epochs") == epoch


async def test_backup_restore_keeps_summary_provenance_cost_and_quarantine(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    async with store_at(data) as s:
        await seed(s)
        async with s.db.transaction() as c:
            await c.execute(
                "UPDATE participant_summary_blocks SET state='unknown',result=NULL WHERE id=1"
            )
            await c.execute("UPDATE model_calls SET state='unknown' WHERE participant_summary_id=1")
        before = await GraphRepository(s.db.path).snapshot("999", "-100")
        costs = await s.db.all("SELECT state,usage_json,cost_usd FROM model_calls ORDER BY id")
    (data / "test.sqlite3").rename(data / "zarya.sqlite3")
    backup(data, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restored")
    restored = tmp_path / "restored"
    assert (restored / MARKER).exists()
    after = await GraphRepository(restored / "zarya.sqlite3").snapshot("999", "-100")
    assert before["nodes"] == after["nodes"] and before["edges"] == after["edges"]
    with sqlite3.connect(restored / "zarya.sqlite3") as c:
        assert (
            c.execute("SELECT state,usage_json,cost_usd FROM model_calls ORDER BY id").fetchall()
            == costs
        )
        assert c.execute("SELECT COUNT(*) FROM participant_summary_sources").fetchone()[0] == 15
    with pytest.raises(RuntimeError, match="карантине"):
        create_app(Config(data_dir=restored, web_dir=Path("web/dist")))
    later = time.time() + 91 * 86400
    monkeypatch.setattr("zarya.participant_summaries.time.time", lambda: later)
    expired = await GraphRepository(restored / "zarya.sqlite3").snapshot("999", "-100")
    assert not any(n["kind"].startswith("participant_") for n in expired["nodes"])
