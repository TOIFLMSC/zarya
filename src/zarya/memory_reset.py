"""Offline reset of accumulated content; preserve configuration, permissions and costs."""

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from filelock import FileLock

from zarya import backup
from zarya.avatar_logic import purge as purge_avatars
from zarya.database import Database
from zarya.media_logic import purge as purge_media
from zarya.memory import MemoryEngine
from zarya.memory_logic import empty_snapshot
from zarya.obsidian import ObsidianBridge
from zarya.participant_summaries import purge as purge_summaries
from zarya.research_logic import purge as purge_research
from zarya.source_material import purge as purge_dialogues
from zarya.telegram_store import TelegramStore, stamp

PRESERVED = (
    "admin", "settings", "settings_versions", "configuration_releases",
    "configuration_drafts", "configuration_bridge", "configuration_files",
    "telegram_access", "telegram_state", "telegram_migrations", "access_grants",
    "users", "chats", "group_behavior", "pilot_preferences", "pilot_reviews",
    "obsidian_binding", "schema_migrations", "delivery_limits",
)
COUNTS = {
    "facts": "SELECT COUNT(*) FROM memory_facts WHERE state!='deleted' AND text!=''",
    "sources": "SELECT COUNT(*) FROM memory_sources WHERE valid=1 AND text!=''",
    "recent": "SELECT COUNT(*) FROM recent_messages",
    "personal_windows": "SELECT COUNT(*) FROM participant_messages",
    "personal_summaries": "SELECT COUNT(*) FROM participant_summary_blocks "
    "WHERE result IS NOT NULL",
    "episodes": "SELECT COUNT(*) FROM memory_batches WHERE summary IS NOT NULL",
    "dialogues": "SELECT COUNT(*) FROM dialogue_runs WHERE response IS NOT NULL OR "
    "COALESCE(json_extract(snapshot,'$.invalidated'),0)!=1",
    "events": "SELECT COUNT(*) FROM events WHERE payload!='{}'",
    "photos": "SELECT COUNT(*) FROM photo_items WHERE raw_path IS NOT NULL OR caption!=''",
    "media": "SELECT COUNT(*) FROM media_runs WHERE transcript IS NOT NULL OR result IS NOT NULL",
    "research": "SELECT COUNT(*) FROM research_runs WHERE result IS NOT NULL OR question!=''",
    "avatars": "SELECT COUNT(*) FROM avatar_runs WHERE result IS NOT NULL OR target_name!=''",
    "pending_jobs": "SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')",
    "pending_delivery": "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','sending')",
    "managed_notes": "SELECT COUNT(*) FROM obsidian_files WHERE state NOT IN ('blocked','unowned')",
}


def signature(c: sqlite3.Connection, tables: tuple[str, ...] | None = None) -> str:
    """Logical hash, not WAL/file bytes; never print table contents."""
    digest = hashlib.sha256()
    if tables is None:
        tables = tuple(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ))
    for name in tables:
        digest.update(name.encode())
        for r in c.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'):
            digest.update(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def report(path: Path) -> dict[str, int]:
    with closing(backup.connect(path)) as c:
        backup.check_db(c)
        return {name: c.execute(sql).fetchone()[0] for name, sql in COUNTS.items()}


async def scrub(db: Database) -> None:
    """One offline transaction. Keep IDs/tombstones to prevent reuse and old replay."""
    async with db.transaction() as c:
        async with c.execute("PRAGMA secure_delete=ON") as cursor:
            await cursor.fetchall()
        await c.execute(
            "UPDATE model_calls SET state=CASE WHEN operation='preflight' THEN 'rejected' "
            "ELSE 'unknown' END,cost_usd=CASE WHEN operation='preflight' THEN 0 ELSE cost_usd END,"
            "error_code='memory_reset_interrupted',finished_at=? WHERE state='started'",
            (stamp(),),
        )
        await purge_dialogues(c, "1=1", ())
        await purge_summaries(c, "1=1", ())
        await purge_media(c, "1=1", ())
        await purge_research(c, "1=1", ())
        await purge_avatars(c, "1=1", ())
        await c.execute(
            "INSERT OR IGNORE INTO memory_file_cleanup SELECT bot_id,raw_path FROM photo_items "
            "WHERE raw_path IS NOT NULL"
        )
        await c.execute(
            "INSERT OR IGNORE INTO memory_file_cleanup SELECT bot_id,image_path FROM photo_items "
            "WHERE image_path IS NOT NULL"
        )
        # Existing IDs survive. Reset affects content, not immutable settings or ledger usage.
        for table in (
            "memory_evidence", "memory_shares", "memory_consent_requests", "memory_relations",
            "recent_messages", "participant_messages", "research_messages", "photo_messages",
            "active_dialogues", "context_run_edges", "behavior_decisions", "media_dependencies",
        ):
            await c.execute("DELETE FROM " + table)
        await c.execute("UPDATE memory_epochs SET version=version+1")
        # Tombstones retain only a participant and event boundary, not a semantic key.
        async with c.execute(
            "SELECT bot_id,chat_id,sender_id,MAX(through_event) FROM memory_tombstones "
            "GROUP BY bot_id,chat_id,sender_id"
        ) as cursor:
            boundaries = list(await cursor.fetchall())
        await c.execute("DELETE FROM memory_tombstones")
        await c.executemany("INSERT INTO memory_tombstones VALUES (?,?,?,'*','*',?)", boundaries)
        await c.execute(
            "UPDATE memory_sources SET name='',sender_id='',text='',valid=0,processed=1"
        )
        await c.execute(
            "UPDATE memory_facts SET text='',fact_key='',sender_id='',provenance='',"
            "state='deleted',version=version+1,curated=0,context_dependencies=NULL,"
            "observation_kind='none'"
        )
        await c.execute(
            "UPDATE memory_batches SET state='cancelled',summary=NULL,manifest='[]',"
            "dependencies=NULL,error_code='memory_reset'"
        )
        await c.execute(
            "UPDATE participant_summary_blocks SET manifest='[]',media_refs='[]',sender_id='',"
            "instruction_hash=NULL,error_code='memory_reset'"
        )
        await c.execute(
            "UPDATE jobs SET payload='{}',lease_until=NULL,state=CASE "
            "WHEN state IN ('pending','running') THEN 'cancelled' ELSE state END"
        )
        await c.execute(
            "UPDATE outbox SET payload='{\"text\":\"\"}',state=CASE "
            "WHEN state IN ('pending','sending') THEN 'cancelled' ELSE state END"
        )
        await c.execute("UPDATE events SET payload='{}'")
        await c.execute("UPDATE model_calls SET request_json=NULL")
        await c.execute(
            "UPDATE dialogue_runs SET snapshot=?,response=NULL,error_code='memory_reset'",
            (empty_snapshot(),),
        )
        await c.execute(
            "UPDATE photo_items SET file_id='',file_unique_id='',caption='',state='cancelled',"
            "raw_path=NULL,image_path=NULL,content_hash=NULL,error_code='memory_reset'"
        )
        await c.execute(
            "UPDATE photo_batches SET state='cancelled',result=NULL,manifest=NULL,"
            "fingerprint=NULL,error_code='memory_reset'"
        )
        await c.execute(
            "UPDATE conversation_moods SET tone='neutral',intensity=0,source_run_id=NULL,"
            "epoch=epoch+1,version=version+1"
        )
        await c.execute(
            "UPDATE obsidian_files SET baseline=NULL,prefix='',suffix='',seen_hash=NULL,"
            "state='purge' WHERE state!='unowned'"
        )
        await c.execute(
            "UPDATE obsidian_changes SET before_text=NULL,proposed_text=NULL,"
            "state='invalidated',reason='Полный сброс памяти'"
        )
        await c.execute("UPDATE memory_commands SET before_text=NULL,after_text=NULL")
        async with c.execute("PRAGMA foreign_key_check") as cursor:
            if await cursor.fetchone():
                raise ValueError("Нарушена целостность сброса")


async def cleanup(db: Database, data: Path) -> dict[str, int]:
    """Owned paths only. Queues/registry survive interruption and Sync restoration."""
    removed = dict(photos=0, media=0, notes=0)
    for bot, name in await db.all("SELECT bot_id,path FROM memory_file_cleanup"):
        relative = backup.relative("photos/" + name)
        path = data / relative
        backup.regular(path)
        path.unlink(missing_ok=True)
        async with db.transaction() as c:
            await c.execute(
                "DELETE FROM memory_file_cleanup WHERE bot_id=? AND path=?", (bot, name)
            )
        removed["photos"] += 1
    media_root = data / "media"
    backup.regular(media_root)
    for (name,) in await db.all("SELECT path FROM media_file_cleanup"):
        if not name.isdecimal():
            raise ValueError("Некорректная очередь удаления медиа")
        folder = (media_root / name).absolute()
        if not folder.is_relative_to(media_root.absolute()):
            raise ValueError("Медиа вне рабочей папки")
        backup.regular(folder)
        if folder.exists():
            for path in folder.rglob("*"):
                backup.regular(path)
            shutil.rmtree(folder)
        async with db.transaction() as c:
            await c.execute("DELETE FROM media_file_cleanup WHERE path=?", (name,))
        removed["media"] += 1
    store = TelegramStore(db)
    bot_row = await db.one("SELECT bot_id FROM telegram_state ORDER BY bot_id LIMIT 1")
    bridge = ObsidianBridge(MemoryEngine(store, None, data), data / "obsidian.local.json",
                            lambda: bot_row[0] if bot_row else None)
    owned = await db.all("SELECT path FROM obsidian_files WHERE state='purge'")
    if owned:
        if not bridge.root:
            raise ValueError("Очистка Obsidian требует прежний настроенный путь моста")
        await bridge.bind()
        for (name,) in owned:
            # Reuse the ownership and durable scrub path; never discover/delete vault files.
            from zarya.memory_command_apply import item

            async with db.read_lock:
                f = await item(db.reader, "SELECT * FROM obsidian_files WHERE path=?", (name,))
            assert f is not None
            await bridge.scrub_file(f)
            removed["notes"] += 1
    return removed


async def apply(data: Path, saved: Path) -> dict[str, Any]:
    backup.regular(await asyncio.to_thread(data.absolute))
    backup.regular(await asyncio.to_thread(saved.absolute))
    data = await asyncio.to_thread(data.resolve)
    saved = await asyncio.to_thread(saved.resolve)
    if data == saved or saved.is_relative_to(data) or data.is_relative_to(saved):
        raise ValueError("Backup должен быть отдельным каталогом вне рабочей установки")
    backup.regular(data)
    if (data / backup.MARKER).exists():
        raise ValueError("Нельзя активировать карантинную копию через сброс памяти")
    backup.verify(saved)
    with FileLock(data / "instance.lock", timeout=0):
        with (
            closing(backup.connect(data / "zarya.sqlite3")) as live,
            closing(backup.connect(saved / "zarya.sqlite3")) as old,
        ):
            backup.check_db(live)
            if signature(live) != signature(old):
                raise ValueError("База изменилась после backup; нужна свежая offline копия")
            preserved = signature(live, PRESERVED)
        before = report(data / "zarya.sqlite3")
        db = Database(data / "zarya.sqlite3")
        await db.open()
        try:
            await scrub(db)
            removed = await cleanup(db, data)
            await compact(db)
        finally:
            await db.close()
        after = report(data / "zarya.sqlite3")
        with closing(backup.connect(data / "zarya.sqlite3")) as live:
            if signature(live, PRESERVED) != preserved:
                raise ValueError("Сохранённые настройки или доступы изменились")
        if any(after.values()):
            raise ValueError("Не все накопленные данные очищены")
        return dict(before=before, after=after, removed=removed)


async def resume(data: Path) -> dict[str, Any]:
    """Finish already queued physical cleanup after a committed reset interruption."""
    backup.regular(await asyncio.to_thread(data.absolute))
    if (data / backup.MARKER).exists():
        raise ValueError("Нельзя очищать рабочий vault из карантинной копии")
    with FileLock(data / "instance.lock", timeout=0):
        pending = report(data / "zarya.sqlite3")
        if any(n for k, n in pending.items() if k != "managed_notes"):
            raise ValueError("Это не завершённый сброс; нужен обычный --apply с backup")
        db = Database(data / "zarya.sqlite3")
        await db.open()
        try:
            removed = await cleanup(db, data)
            await compact(db)
        finally:
            await db.close()
        return {"after": report(data / "zarya.sqlite3"), "removed": removed}


async def compact(db: Database) -> None:
    # Close result-bearing PRAGMA cursors before VACUUM; no stale WAL/freelist content.
    async with db.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
        outcome = await cursor.fetchone()
        if outcome and outcome[0] != 0:
            raise ValueError("SQLite checkpoint занят; нужна повторная финализация")
    async with db.writer.execute("VACUUM"):
        pass
    async with db.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
        outcome = await cursor.fetchone()
        if outcome and outcome[0] != 0:
            raise ValueError("SQLite checkpoint занят; нужна повторная финализация")


def main() -> None:
    parser = argparse.ArgumentParser(description="Заря: offline сброс накопленных данных")
    parser.add_argument("data", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply and args.resume:
            parser.error("Выбери --apply или --resume")
        if args.resume:
            value = asyncio.run(resume(args.data))
        elif args.apply:
            if not args.backup:
                parser.error("Перед сбросом нужна свежая проверенная offline backup")
            value = asyncio.run(apply(args.data, args.backup))
        else:
            value = {"preview": report(args.data / "zarya.sqlite3")}
        print(json.dumps(value))
    except Exception as exc:
        # Do not print SQL/paths or private row values from a failing operation.
        parser.exit(1, f"Сброс не завершён ({type(exc).__name__}); приложение не запускай.\n")


if __name__ == "__main__":
    main()
