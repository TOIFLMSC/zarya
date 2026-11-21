import sqlite3

import pytest
from filelock import FileLock, Timeout
from test_dialogue import FakeModel, run_one
from test_graph import populated
from test_media import engines, media_event, process
from test_memory import extract
from test_obsidian import bridge_at, file_row
from test_photos import PhotoTransport, VisionModel, analyze, photo
from test_telegram import event, store_at

from zarya.backup import backup
from zarya.database import Database
from zarya.dialogue import DialogueEngine
from zarya.graph import GraphRepository
from zarya.memory_reset import PRESERVED, apply, cleanup, report, resume, scrub, signature
from zarya.obsidian import ObsidianBridge
from zarya.photos import PhotoEngine


async def test_full_scrub_keeps_configuration_ledger_ids_and_admits_new_messages(tmp_path):
    async with store_at(tmp_path) as s:
        memory = await populated(s, tmp_path)
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, что я люблю?")])
        model = FakeModel()
        dialogue = DialogueEngine(s, model)
        dialogue.memory = memory
        work = await run_one(dialogue)
        old_run, old_fact = work["run_id"], (await s.db.one("SELECT MAX(id) FROM memory_facts"))[0]
        calls = await s.db.all("SELECT id,state,usage_json,cost_usd FROM model_calls")
        with sqlite3.connect(s.db.path) as c:
            kept = signature(c, PRESERVED)
        await scrub(s.db)
        assert not any(report(s.db.path).values())
        assert await s.db.all("SELECT id,state,usage_json,cost_usd FROM model_calls") == calls
        assert (await s.db.one("SELECT MAX(id) FROM memory_facts"))[0] == old_fact
        with sqlite3.connect(s.db.path) as c:
            assert signature(c, PRESERVED) == kept
        with pytest.raises(ValueError):
            await dialogue.replay(old_run, "paid")
        assert not (await memory.profile("999", "-100"))["facts"]
        graph = await GraphRepository(s.db.path).snapshot("999", "-100")
        assert not any(
            n["kind"] in {"fact", "episode", "source", "profile"} for n in graph["nodes"]
        )
        assert not await memory.claim("999", force=True)
        # A duplicate old Telegram update cannot restore its scrubbed event/source.
        await s.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
        assert not await s.db.all("SELECT text FROM memory_sources WHERE valid=1")
        await s.ingest("999", "zarya_test", [event(6, -100, "Я люблю Rust")])
        await extract(memory, text="Любит Rust", key="interest_rust")
        facts = (await memory.profile("999", "-100"))["facts"]
        assert len(facts) == 1 and facts[0]["text"] == "Любит Rust" and facts[0]["id"] > old_fact
        assert (await s.db.one("SELECT COUNT(*) FROM participant_messages"))[0] == 1


async def test_reset_cancels_old_preflight_and_never_starts_generation(tmp_path):
    async with store_at(tmp_path) as s:
        await populated(s, tmp_path)
        async with s.db.transaction() as c:
            await c.execute("UPDATE jobs SET state='done'")
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, старый вопрос")])
        model = FakeModel()
        dialogue = DialogueEngine(s, model)
        work = await dialogue.claim("999")
        await scrub(s.db)
        await dialogue.execute(work)
        assert not model.calls and not await s.claim_delivery("999")
        main = await s.db.one(
            "SELECT state,cost_usd FROM model_calls WHERE run_id=?", (work["run_id"],)
        )
        assert main == ("rejected", 0)
        await s.recover("999")
        assert not await dialogue.claim("999")


async def test_paid_generation_in_flight_remains_unknown_after_reset(tmp_path):
    async with store_at(tmp_path) as s:
        await populated(s, tmp_path)
        async with s.db.transaction() as c:
            await c.execute("UPDATE jobs SET state='done'")
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, вопрос")])
        dialogue = DialogueEngine(s, FakeModel())
        work = await dialogue.claim("999")
        assert await dialogue.preflight(work, work["request"]) is None
        await scrub(s.db)
        assert await s.db.one(
            "SELECT state,cost_usd FROM model_calls WHERE run_id=?", (work["run_id"],)
        ) == ("unknown", None)


async def test_managed_notes_cleanup_blocks_sync_resurrection_and_preserves_other_files(tmp_path):
    async with store_at(tmp_path) as s:
        bridge, _ = await bridge_at(s, tmp_path)
        f = await file_row(bridge)
        note = bridge.root / f["path"]
        old = note.read_bytes()
        personal = bridge.root / "Моя заметка.md"
        personal.write_text("Собственная заметка", encoding="utf-8")
        await scrub(s.db)
        await cleanup(s.db, tmp_path)
        assert not note.exists() and personal.read_text(encoding="utf-8") == "Собственная заметка"
        note.write_bytes(old)  # Obsidian Sync restores an old registered copy.
        restarted = ObsidianBridge(bridge.memory, bridge.config_file, lambda: "999")
        await restarted.scan()
        assert not note.exists()
        assert not (await restarted.listing("999", 0, 0))["changes"]
        assert not await s.db.all("SELECT id FROM memory_facts WHERE state!='deleted'")
        assert not await s.db.all("SELECT path FROM obsidian_files WHERE baseline IS NOT NULL")


async def test_offline_apply_requires_current_backup_lock_and_survives_restart(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    async with store_at(data) as s:
        await populated(s, data)
    (data / "test.sqlite3").rename(data / "zarya.sqlite3")
    saved = tmp_path / "before"
    backup(data, saved)
    original = (saved / "zarya.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="отдельным"):
        await apply(saved, saved)
    assert (saved / "zarya.sqlite3").read_bytes() == original
    with FileLock(data / "instance.lock", timeout=0):
        with pytest.raises(Timeout):
            await apply(data, saved)
    result = await apply(data, saved)
    assert result["before"]["facts"] == 1 and not any(result["after"].values())
    with pytest.raises(ValueError, match="изменилась"):
        await apply(data, saved)  # old backup cannot authorize a newer state
    db = Database(data / "zarya.sqlite3")
    await db.open()
    await db.close()
    assert not any(report(data / "zarya.sqlite3").values())
    assert not any((await resume(data))["after"].values())


async def test_scrub_queues_assets_and_cleanup_resumes_without_touching_unowned_paths(tmp_path):
    async with store_at(tmp_path) as s:
        await populated(s, tmp_path)
        photo = tmp_path / "photos" / "old.jpg"
        photo.parent.mkdir()
        photo.write_bytes(b"private synthetic photo")
        folder = tmp_path / "media" / "12"
        folder.mkdir(parents=True)
        (folder / "frame.jpg").write_bytes(b"private frame")
        other = tmp_path / "media" / "owner"
        other.mkdir()
        (other / "keep.txt").write_text("keep", encoding="utf-8")
        async with s.db.transaction() as c:
            await c.execute("INSERT INTO memory_file_cleanup VALUES ('999','old.jpg')")
            await c.execute("INSERT INTO media_file_cleanup VALUES ('12')")
        await scrub(s.db)
        assert photo.exists() and (folder / "frame.jpg").exists()
        await cleanup(s.db, tmp_path)
        assert not photo.exists() and not folder.exists() and (other / "keep.txt").exists()
        assert not await s.db.all("SELECT path FROM media_file_cleanup")
        assert not await s.db.all("SELECT path FROM memory_file_cleanup")
        assert (await cleanup(s.db, tmp_path)) == {"photos": 0, "media": 0, "notes": 0}


async def test_unsafe_cleanup_path_is_kept_in_queue(tmp_path):
    async with store_at(tmp_path) as s:
        await populated(s, tmp_path)
        async with s.db.transaction() as c:
            await c.execute("INSERT INTO memory_file_cleanup VALUES ('999','../keep.txt')")
        with pytest.raises(ValueError):
            await cleanup(s.db, tmp_path)
        assert await s.db.all("SELECT path FROM memory_file_cleanup") == [("../keep.txt",)]


async def test_actual_photo_and_media_artifacts_ledger_and_files_are_reset(tmp_path):
    async with store_at(tmp_path) as s:
        await populated(s, tmp_path)
        photos = PhotoEngine(s, VisionModel(), tmp_path)
        for n in (5, 6):
            await s.ingest("999", "zarya_test", [photo(n, -100, "Заря, что тут?", file=str(n))])
            await analyze(photos, PhotoTransport())
        # Distinct album keys with generation=1 must survive reset's unique constraint.
        async with s.db.transaction() as c:
            await c.execute("UPDATE jobs SET state='done'")
        dialogue, media, _, _ = engines(s, tmp_path)
        await s.ingest("999", "zarya_test", [media_event(7, "video", -100, caption="Заря, видео")])
        assert (await dialogue.claim("999"))["skip"]
        await process(media)
        paths = list((tmp_path / "photos").rglob("*.jpg"))
        assert paths and await s.db.one("SELECT result FROM media_runs WHERE state='completed'")
        calls = await s.db.all("SELECT state,usage_json,cost_usd FROM model_calls ORDER BY id")
        await scrub(s.db)
        assert not any(report(s.db.path).values())
        await cleanup(s.db, tmp_path)
        assert all(not p.exists() for p in paths)
        assert not list((tmp_path / "media").glob("*/*.jpg"))
        assert calls == await s.db.all(
            "SELECT state,usage_json,cost_usd FROM model_calls ORDER BY id"
        )


async def test_interrupted_cleanup_requires_resume_and_checks_busy_wal(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    async with store_at(data) as s:
        await populated(s, data)
    (data / "test.sqlite3").rename(data / "zarya.sqlite3")
    saved = tmp_path / "before"
    backup(data, saved)
    reader = sqlite3.connect((data / "zarya.sqlite3").as_uri() + "?mode=ro", uri=True)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM memory_facts").fetchone()
    try:
        with pytest.raises(ValueError, match="checkpoint занят"):
            await apply(data, saved)
        assert not any(report(data / "zarya.sqlite3").values())
        assert (data / "zarya.sqlite3-wal").stat().st_size > 0
    finally:
        reader.close()
    assert not any((await resume(data))["after"].values())
    wal = data / "zarya.sqlite3-wal"
    assert not wal.exists() or wal.stat().st_size == 0
