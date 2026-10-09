import json
import sqlite3
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from zarya.app import create_app
from zarya.backup import MARKER, backup, restore, verify
from zarya.config import Config
from zarya.database import Database


async def source_at(path):
    path.mkdir()
    db = Database(path / "zarya.sqlite3")
    await db.open()
    async with db.transaction() as c:
        await c.execute(
            "INSERT INTO "
            "photo_batches(id,bot_id,chat_id,scope,access_version,album_key,gener"
            "ation,model,processing_version,created_at,collect_until,collect_dead"
            "line,leader_message_id) VALUES "
            "(1,'999','42','private',1,'a',1,'luna','1','2026-10-09',0,0,1)"
        )
        await c.execute(
            "INSERT INTO events(id,bot_id,update_id,chat_id,payload,received_at) "
            "VALUES (1,'999',1,'42','{}','2026-10-09')"
        )
        await c.execute(
            "INSERT INTO "
            "photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,file"
            "_unique_id,caption,raw_path,image_path) VALUES "
            "(1,'999','42',1,1,'a','a','','a.raw','a.jpg')"
        )
    await db.close()
    (path / "photos").mkdir()
    (path / "photos/a.raw").write_bytes(b"raw")
    (path / "photos/a.jpg").write_bytes(b"image")
    (path / "photos/orphan.raw").write_bytes(b"exclude")
    (path / "openai-api-key.txt").write_text("secret")
    return path


async def test_backup_wal_assets_verify_restore_quarantine(tmp_path):
    source = await source_at(tmp_path / "data")
    # Keep committed WAL alive to catch unsafe copyfile(database) implementations.
    db = sqlite3.connect(source / "zarya.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("UPDATE settings SET version=99")
    db.commit()
    try:
        backup(source, tmp_path / "backup")
    finally:
        db.close()
    manifest = verify(tmp_path / "backup")
    assert set(manifest["files"]) == {"zarya.sqlite3", "photos/a.raw", "photos/a.jpg"}
    restore(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored" / MARKER).is_file()
    assert not (tmp_path / "restored/openai-api-key.txt").exists()
    with sqlite3.connect(tmp_path / "restored/zarya.sqlite3") as db:
        assert db.execute("SELECT version FROM settings").fetchone()[0] == 99
    with pytest.raises(RuntimeError, match="карантине"):
        create_app(
            Config(
                data_dir=tmp_path / "restored",
                web_dir=Path("web"),
                telegram_token="fake",
                openai_api_key="fake",
            )
        )


async def test_backup_refuses_running_existing_nested_missing_and_tampered(tmp_path):
    source = await source_at(tmp_path / "data")
    with FileLock(source / "instance.lock"):
        with pytest.raises(Timeout):
            backup(source, tmp_path / "busy")
    with pytest.raises(ValueError):
        backup(source, source / "inside")
    backup(source, tmp_path / "ok")
    with pytest.raises(ValueError):
        backup(source, tmp_path / "ok")
    (tmp_path / "ok/photos/a.jpg").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="повреждён"):
        verify(tmp_path / "ok")
    (source / "photos/a.jpg").unlink()
    with pytest.raises(ValueError, match="медиа"):
        backup(source, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


async def test_manifest_traversal_rejected_before_external_file_read(tmp_path):
    source = await source_at(tmp_path / "data")
    backup(source, tmp_path / "backup")
    file = tmp_path / "backup/manifest.json"
    manifest = json.loads(file.read_text())
    manifest["files"]["photos/../../secret.txt"] = {"size": 0, "sha256": "x"}
    file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="путь"):
        restore(tmp_path / "backup", tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


async def test_backup_media_frames_and_invalid_schema(tmp_path):
    source = await source_at(tmp_path / "data")
    (source / "media").mkdir()
    (source / "media/frame.jpg").write_bytes(b"frame")
    with sqlite3.connect(source / "zarya.sqlite3") as db:
        db.execute(
            "INSERT INTO media_runs(bot_id,chat_id,scope,access_version,message_id,"
            "event_id,kind,file_id,processing_version,asr_model,model,"
            "settings_version,limits,created_at,frames) "
            "VALUES ('999','42','private',1,1,1,'video','f','1','asr','luna',1,'{}',"
            "'2026-10-09',?)",
            (json.dumps([{"path": "frame.jpg", "seconds": 0}]),),
        )
    backup(source, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restore")
    assert (tmp_path / "restore/media/frame.jpg").read_bytes() == b"frame"
    with sqlite3.connect(source / "zarya.sqlite3") as db:
        db.execute("DELETE FROM schema_migrations WHERE version='013_pilot.sql'")
    with pytest.raises(ValueError, match="схемы"):
        backup(source, tmp_path / "bad-schema")
    assert not (tmp_path / "bad-schema").exists()
