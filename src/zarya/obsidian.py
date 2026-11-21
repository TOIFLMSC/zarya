"""Supervised local Obsidian bridge: export, observe, explicit owner application."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zarya import obsidian_fs as fs
from zarya.database import ConflictError
from zarya.graph import GraphRepository, identity, vault_files
from zarya.memory import MemoryEngine
from zarya.memory_command_apply import item
from zarya.memory_context import RETENTION, valid_dependencies
from zarya.models import MemoryMutation

logger = logging.getLogger(__name__)
FOLDER = "Память Зари"
MARKER = ".zarya-bridge.json"
MAX_FILES = 5000


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def fact_note(snapshot: dict[str, Any], node: dict[str, Any], epoch: str) -> tuple[str, str]:
    fence = "`" * max(4, max((len(v) + 1 for v in re.findall(r"`+", node["text"])), default=4))
    metadata = {
        "zarya_schema": 1,
        "sync_epoch": epoch,
        "id": node["id"],
        "bot_id": snapshot["bot_id"],
        "chat_id": snapshot["chat_id"],
        "scope": snapshot["scope"],
        "access_version": snapshot["access_version"],
        "fact_id": node["key"],
        "version": node["version"],
        "state": node["state"],
        "category": node["category"],
    }
    prefix = (
        "---\n"
        + "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()
        )
        + "\n---\n\n# "
        + node["label"]
        + "\n\n"
    )
    prefix += (
        "Меняй только текст внутри блока с обратными кавычками. "
        f"Правка появится в Web UI для применения.\n\n{fence}text\n"
    )
    chat_uid = identity(snapshot["bot_id"], snapshot["chat_id"], "chat", snapshot["chat_id"])
    suffix = f"\n{fence}\n\nОбласть: [[Чат {chat_uid}]]\n"
    for edge in snapshot["edges"]:
        if edge["source"] == node["id"] and edge["type"] == "about":
            suffix += f"\nabout: [[Участник {edge['target']}]]\n"
    # Exact surrounding bytes are the editable contract, not authority from YAML.
    return prefix, suffix


class ObsidianBridge:
    def __init__(self, memory: MemoryEngine, config_file: Path, bot: Callable[[], str | None]):
        self.memory, self.db, self.store = memory, memory.db, memory.store
        self.config_file, self.bot = config_file, bot
        self.root: Path | None = None
        self.project_path: str | None = None
        self.epoch = ""
        self.error: str | None = None
        self.last_scan: str | None = None
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.graph = GraphRepository(self.db.path)
        if config_file.exists():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8-sig"))
                if set(config) != {"enabled", "project_path"} or not isinstance(
                    config["enabled"], bool
                ):
                    raise ValueError
                if config["enabled"]:
                    path = Path(config["project_path"])
                    if not path.is_absolute() or not path.is_dir():
                        raise ValueError
                    self.project_path = str(path.absolute())
                    self.root = path.absolute() / FOLDER
            except (OSError, ValueError, TypeError):
                self.error = "Проверь локальный файл obsidian.local.json и абсолютный путь папки"

    def start(self) -> None:
        if self.root:
            self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            try:
                if self.bot():
                    await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "Проверка прервана. Повтори синхронизацию или проверь доступ к папке"
                logger.warning("Obsidian scan failed", exc_info=False)
            await asyncio.sleep(15)

    async def bind(self) -> None:
        assert self.root and self.project_path
        binding = await self.db.one("SELECT root,epoch FROM obsidian_binding WHERE id=1")
        if binding and binding[0] != self.project_path:
            raise ValueError("Мост уже привязан к другой папке; требуется отдельная перепривязка")
        self.epoch = binding[1] if binding else str(uuid4())
        marker = json.dumps({"schema": 1, "epoch": self.epoch}, sort_keys=True).encode()
        if not binding:
            if self.root.exists():
                raise ValueError("Существующая папка «Память Зари» не принадлежит этому мосту")
            async with self.db.transaction() as c:
                await c.execute(
                    "INSERT INTO obsidian_binding VALUES (1,?,?)", (self.project_path, self.epoch)
                )
        # No pre-existing folder is adopted without its ownership marker.
        if not self.root.exists():
            await asyncio.to_thread(fs.write, self.root.parent, f"{FOLDER}/{MARKER}", marker, None)
        else:
            actual = await asyncio.to_thread(fs.read, self.root, MARKER)
            if actual is None and not any(self.root.iterdir()):
                await asyncio.to_thread(fs.write, self.root, MARKER, marker, None)
                actual = await asyncio.to_thread(fs.read, self.root, MARKER)
            if actual != marker:
                raise ValueError("Папка «Память Зари» занята или относится к другой эпохе моста")
        if await asyncio.to_thread(fs.read, self.root, MARKER) != marker:
            raise ValueError("Маркер папки изменился; синхронизация остановлена")
        guide = (
            "# Память Зари\n\nГруппы и лички находятся в отдельных папках. "
            "Открой Graph view в Obsidian для просмотра связей.\n\n"
            "В заметке «Утверждение» меняй только текст внутри блока с обратными кавычками. "
            "Через две проверки (обычно до 30 секунд) предложение появится "
            "в Web UI → Память → Obsidian. "
            "Нажми «Применить текст» после просмотра сравнения.\n\n"
            "Правка использует обычное ручное редактирование: запись становится активной, "
            "согласия на перенос отзываются, зависимые сводки и ответы очищаются. "
            "Отмена восстанавливает только текст новой командой, без источников и согласий.\n\n"
            "Исходники, эпизоды, персональные сводки и служебные поля доступны только для чтения. "
            "Новые и переименованные неизвестные файлы не импортируются. "
            "Удаление файла не удаляет факт; отсутствующая заметка "
            "не восстанавливается автоматически.\n\n"
            "Мост очищает только зарегистрированные заметки в этой папке "
            "при отзыве, удалении и истечении "
            "срока. Корзина Obsidian, версии Sync и backups остаются отдельными копиями. "
            "SQLite, ключи и журналы сюда не копируются.\n"
        ).encode()
        await asyncio.to_thread(fs.write, self.root, "Начало.md", guide, None)

    async def file_valid(self, f: dict[str, Any]) -> bool:
        """Independent lifecycle check: absence in bounded graph is not deletion."""
        async with self.db.read_lock:
            c = self.db.reader
            peer = await item(
                c,
                "SELECT * FROM telegram_access WHERE bot_id=? AND subject_id=?",
                (f["bot_id"], f["chat_id"]),
            )
            if (
                not peer
                or peer["state"] != "approved"
                or peer["version"] != f["access_version"]
                or peer["member_state"] in {"left", "kicked", "migrated"}
            ):
                return False
            cutoff = time.time() - RETENTION
            from zarya.participant_graph import KINDS, current

            if f["kind"] in KINDS:
                return await current(
                    c, f["bot_id"], f["chat_id"], peer["version"], f["kind"],
                    int(f["node_key"]), f["version"],
                )
            if f["kind"] == "fact":
                row = await item(
                    c,
                    "SELECT * FROM memory_facts WHERE id=? AND bot_id=? AND chat_id=?",
                    (int(f["node_key"]), f["bot_id"], f["chat_id"]),
                )
                if (
                    not row
                    or row["state"] in {"deleted", "outdated"}
                    or row["access_version"] != peer["version"]
                    or (row["state"] == "active" and row["reviewed_at"] <= cutoff)
                ):
                    return False
                deps = row["context_dependencies"]
            elif f["kind"] == "source":
                row = await item(
                    c,
                    "SELECT s.* FROM memory_sources s JOIN events e ON e.id=s.event_id "
                    "JOIN research_messages r ON r.event_id=s.event_id "
                    "AND r.bot_id=s.bot_id AND r.chat_id=s.chat_id "
                    "AND r.message_id=s.message_id AND r.access_version=s.access_version "
                    "WHERE s.id=? AND s.bot_id=? AND s.chat_id=? AND s.valid=1 "
                    "AND e.disposition='accepted' AND e.payload!='{}'",
                    (int(f["node_key"]), f["bot_id"], f["chat_id"]),
                )
                return bool(
                    row
                    and row["event_id"] == f["version"]
                    and row["access_version"] == peer["version"]
                    and row["received_at"] > cutoff
                )
            elif f["kind"] == "episode":
                row = await item(
                    c,
                    "SELECT * FROM memory_batches WHERE id=? AND bot_id=? AND chat_id=?",
                    (int(f["node_key"]), f["bot_id"], f["chat_id"]),
                )
                if (
                    not row
                    or row["state"] != "completed"
                    or not row["summary"]
                    or row["access_version"] != peer["version"]
                    or row["created_at"] <= datetime.fromtimestamp(cutoff, UTC).isoformat()
                ):
                    return False
                for ref in json.loads(row["manifest"]):
                    witness = await item(
                        c,
                        "SELECT id FROM memory_sources WHERE id=? AND event_id=? "
                        "AND bot_id=? AND chat_id=? AND access_version=? "
                        "AND valid=1 AND received_at>?",
                        (
                            ref["id"],
                            ref["event_id"],
                            f["bot_id"],
                            f["chat_id"],
                            peer["version"],
                            cutoff,
                        ),
                    )
                    if not witness:
                        return False
                deps = row["dependencies"]
            else:
                return True
            return deps is None or await valid_dependencies(
                c, f["bot_id"], f["chat_id"], peer["version"], deps
            )

    async def scrub_file(self, f: dict[str, Any]) -> None:
        assert self.root
        async with self.db.transaction() as c:
            await c.execute(
                "UPDATE obsidian_files SET baseline=NULL,state='purge' WHERE path=?", (f["path"],)
            )
            await c.execute(
                "UPDATE obsidian_changes SET before_text=NULL,proposed_text=NULL,"
                "state='invalidated',reason='Запись больше недоступна' WHERE path=?",
                (f["path"],),
            )
            if f["kind"] == "fact":
                await c.execute(
                    "UPDATE memory_commands SET before_text=NULL,after_text=NULL "
                    "WHERE bot_id=? AND fact_id=?",
                    (f["bot_id"], int(f["node_key"])),
                )
        await asyncio.to_thread(fs.remove, self.root, f["path"])
        async with self.db.transaction() as c:
            await c.execute("UPDATE obsidian_files SET state='blocked' WHERE path=?", (f["path"],))

    async def observe(self, f: dict[str, Any]) -> None:
        assert self.root
        try:
            data = await asyncio.to_thread(fs.read, self.root, f["path"])
        except (OSError, ValueError):
            self.error = "Есть занятые или некорректные файлы; они не применены"
            return
        if data is None:
            if f["state"] == "writing":
                return
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE obsidian_files SET state='missing' WHERE path=?", (f["path"],)
                )
            return
        if f["state"] == "missing":
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE obsidian_files SET state='ready' WHERE path=?", (f["path"],)
                )
        value = fs.digest(data)
        if f["state"] == "writing" and data == b"":
            return
        if value == f["hash"]:
            if f["state"] == "writing":
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE obsidian_files SET state='ready' WHERE path=?", (f["path"],)
                    )
            if f["seen_hash"]:
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE obsidian_files SET seen_hash=NULL WHERE path=?", (f["path"],)
                    )
            return
        if f["seen_hash"] != value:
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE obsidian_files SET seen_hash=? WHERE path=?", (value, f["path"])
                )
            return
        old = await self.db.one(
            "SELECT id FROM obsidian_changes WHERE path=? AND file_hash=? AND epoch=?",
            (f["path"], value, self.epoch),
        )
        if old:
            return
        proposed: str | None = None
        state = "invalid"
        reason: str | None = "Служебные поля и автоматически созданные заметки менять нельзя"
        if f["kind"] == "fact":
            try:
                text = data.decode("utf-8").replace("\r\n", "\n")
                prefix, suffix = f["prefix"], f["suffix"]
                if text.startswith(prefix) and text.endswith(suffix):
                    proposed = text[len(prefix) : -len(suffix)]
                    if not proposed.strip() or len(proposed) > 500 or "````" in proposed:
                        raise ValueError
                    if proposed == f["baseline"]:
                        # Only line-ending changes; safe to acknowledge without a domain mutation.
                        async with self.db.transaction() as c:
                            await c.execute(
                                "UPDATE obsidian_files SET hash=? WHERE path=?", (value, f["path"])
                            )
                        return
                    live = await self.db.one(
                        "SELECT version FROM memory_facts WHERE id=?", (int(f["node_key"]),)
                    )
                    state = "pending" if live and live[0] == f["version"] else "conflict"
                    reason = None if state == "pending" else "Запись изменилась после выгрузки"
                else:
                    proposed = None
            except (UnicodeError, ValueError):
                proposed, reason = (
                    None,
                    "Текст должен содержать 1–500 символов; формат заметки повреждён",
                )
        async with self.db.transaction() as c:
            # A newer editor save supersedes older pending proposals for the same note.
            await c.execute(
                "UPDATE obsidian_changes SET state='superseded' WHERE path=? "
                "AND state IN ('pending','conflict','invalid')",
                (f["path"],),
            )
            await c.execute(
                "INSERT OR IGNORE INTO obsidian_changes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    f["path"],
                    f["bot_id"],
                    f["chat_id"],
                    int(f["node_key"]) if f["kind"] == "fact" else None,
                    self.epoch,
                    f["version"],
                    f["access_version"],
                    value,
                    f["baseline"],
                    proposed,
                    state,
                    reason,
                    stamp(),
                ),
            )

    async def scan(self) -> None:
        if not self.root:
            raise ValueError(self.error or "Хранилище не настроено")
        if self.lock.locked():
            raise ValueError("Синхронизация уже выполняется")
        bot = self.bot()
        if not bot:
            raise ValueError("Подключи Telegram-бота перед синхронизацией")
        async with self.lock:
            self.error = None
            await self.bind()
            cutoff = datetime.fromtimestamp(time.time() - RETENTION, UTC).isoformat()
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE memory_commands SET before_text=NULL,after_text=NULL "
                    "WHERE created_at<?",
                    (cutoff,),
                )
                await c.execute(
                    "UPDATE obsidian_changes SET before_text=NULL,proposed_text=NULL,"
                    "state='invalidated' WHERE created_at<?",
                    (cutoff,),
                )
            # Registry is the authority, never filesystem discovery across the owner's vault.
            rows = await self.db.all("SELECT path FROM obsidian_files LIMIT ?", (MAX_FILES + 1,))
            if len(rows) > MAX_FILES:
                raise ValueError("Превышен лимит 5000 управляемых заметок")
            for (name,) in rows:
                async with self.store.gate:
                    async with self.db.read_lock:
                        f = await item(
                            self.db.reader, "SELECT * FROM obsidian_files WHERE path=?", (name,)
                        )
                    assert f
                    if f["state"] == "unowned":
                        self.error = "Чужая заметка с совпавшим именем сохранена без изменений"
                    elif f["state"] in {"purge", "blocked"} or not await self.file_valid(f):
                        try:
                            await self.scrub_file(f)
                        except (OSError, ValueError):
                            self.error = "Часть отозванных заметок занята; очистка будет повторена"
                    else:
                        await self.observe(f)
            chats = await self.db.all(
                "SELECT subject_id FROM telegram_access WHERE bot_id=? "
                "AND state='approved' AND member_state NOT IN ('left','kicked','migrated') "
                "ORDER BY subject_id LIMIT 101",
                (bot,),
            )
            if len(chats) > 100:
                raise ValueError("Превышен лимит 100 чатов")
            for (chat,) in chats:
                snapshot = await self.graph.snapshot(bot, chat)
                if snapshot:
                    await self.export(snapshot)
            self.last_scan = stamp()

    async def export(self, snapshot: dict[str, Any]) -> None:
        assert self.root
        files = vault_files(snapshot)
        nodes = {n["id"]: n for n in snapshot["nodes"]}
        branch = "Группы" if snapshot["scope"] == "group" else "Личные"
        base = f"{branch}/Бот {snapshot['bot_id']}/Чат {snapshot['chat_id']}"
        for name, content in files.items():
            # Large raw JSON is only in the standalone ZIP, not the editable vault.
            if not name.startswith("nodes/"):
                continue
            uid = Path(name).stem.split(" ")[-1]
            node = nodes[uid]
            prefix, suffix, baseline = "", "", None
            if node["kind"] == "fact":
                prefix, suffix = fact_note(snapshot, node, self.epoch)
                baseline = node["text"]
                content = prefix + baseline + suffix
            name = f"{base}/{Path(name).name}"
            async with self.store.gate:
                # A withdrawal after snapshot collection must cancel publication.
                peer = await self.db.one(
                    "SELECT version,state,member_state FROM telegram_access "
                    "WHERE bot_id=? AND subject_id=?",
                    (snapshot["bot_id"], snapshot["chat_id"]),
                )
                epoch = await self.db.one(
                    "SELECT version FROM memory_epochs WHERE bot_id=?", (snapshot["bot_id"],)
                )
                if (
                    not peer
                    or peer[0] != snapshot["access_version"]
                    or peer[1] != "approved"
                    or peer[2] in {"left", "kicked", "migrated"}
                    or (epoch[0] if epoch else 1) != snapshot["memory_epoch"]
                ):
                    return
                from zarya.participant_graph import KINDS, current

                if node["kind"] in KINDS:
                    async with self.db.read_lock:
                        fresh = await current(
                            self.db.reader, snapshot["bot_id"], snapshot["chat_id"],
                            snapshot["access_version"], node["kind"], int(node["key"]),
                            node["version"], node.get("result_hash", node.get("text_hash")),
                        )
                    if not fresh:
                        continue
                row = await self.db.one(
                    "SELECT hash,state FROM obsidian_files WHERE path=?", (name,)
                )
                expected = row[0] if row else None
                if row and row[1] == "unowned":
                    continue
                if row and row[1] == "blocked":
                    # Removed copies may be recreated only from current SQL, never from Sync.
                    expected = None
                if row and row[1] == "missing":
                    continue
                if row and row[1] == "writing":
                    actual = await asyncio.to_thread(fs.read, self.root, name)
                    if actual in (None, b""):
                        expected = None if actual is None else fs.digest(actual)
                data = content.encode()
                value = fs.digest(data)
                exists = await self.db.one(
                    "SELECT state,file_hash FROM obsidian_changes WHERE path=? "
                    "AND state IN ('pending','conflict','invalid') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (name,),
                )
                if exists:
                    continue
                # Applied/rejected proposals permit replacement only of their exact observed bytes.
                done = await self.db.one(
                    "SELECT file_hash FROM obsidian_changes WHERE path=? "
                    "AND state IN ('applied','rejected') ORDER BY created_at DESC LIMIT 1",
                    (name,),
                )
                if done:
                    actual = await asyncio.to_thread(fs.read, self.root, name)
                    if actual is not None and fs.digest(actual) == done[0]:
                        expected = done[0]
                if expected == value:
                    continue
                if row is None:
                    count = await self.db.one("SELECT count(*) FROM obsidian_files")
                    if count[0] >= MAX_FILES:
                        raise ValueError("Превышен лимит 5000 управляемых заметок")
                    try:
                        lease = await asyncio.to_thread(fs.Reservation, self.root, name)
                    except FileExistsError:
                        self.error = "Чужая заметка с совпавшим именем сохранена без изменений"
                        continue
                    try:
                        async with self.db.transaction() as c:
                            await c.execute(
                                "INSERT INTO obsidian_files(path,bot_id,chat_id,kind,node_key,"
                                "epoch,"
                                "version,access_version,hash,prefix,suffix,baseline,state) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'writing')",
                                (
                                    name,
                                    snapshot["bot_id"],
                                    snapshot["chat_id"],
                                    node["kind"],
                                    str(node["key"]),
                                    self.epoch,
                                    node.get("version", 1),
                                    snapshot["access_version"],
                                    value,
                                    prefix,
                                    suffix,
                                    baseline,
                                ),
                            )
                        await asyncio.to_thread(lease.publish, data)
                    finally:
                        await asyncio.to_thread(lease.close)
                    expected = value
                if row is not None and not await asyncio.to_thread(
                    fs.write, self.root, name, data, expected
                ):
                    actual = await asyncio.to_thread(fs.read, self.root, name)
                    if actual != data:
                        self.error = (
                            "Есть изменённые или чужие заметки; они сохранены без перезаписи"
                        )
                        if row is None:
                            async with self.db.transaction() as c:
                                await c.execute(
                                    "UPDATE obsidian_files SET state='unowned',baseline=NULL "
                                    "WHERE path=?",
                                    (name,),
                                )
                        continue
                    # Recover a completed file write after crash before registry commit.
                async with self.db.transaction() as c:
                    await c.execute(
                        "INSERT INTO obsidian_files(path,bot_id,chat_id,kind,node_key,epoch,"
                        "version,access_version,hash,prefix,suffix,baseline,state) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'ready') "
                        "ON CONFLICT(path) DO UPDATE SET epoch=excluded.epoch,"
                        "version=excluded.version,"
                        "access_version=excluded.access_version,hash=excluded.hash,prefix=excluded.prefix,"
                        "suffix=excluded.suffix,baseline=excluded.baseline,seen_hash=NULL,state='ready'",
                        (
                            name,
                            snapshot["bot_id"],
                            snapshot["chat_id"],
                            node["kind"],
                            str(node["key"]),
                            self.epoch,
                            node.get("version", 1),
                            snapshot["access_version"],
                            value,
                            prefix,
                            suffix,
                            baseline,
                        ),
                    )
                    await c.execute(
                        "UPDATE obsidian_changes SET file_hash='resolved:'||id "
                        "WHERE path=? AND state IN ('applied','rejected') "
                        "AND file_hash NOT LIKE 'resolved:%'",
                        (name,),
                    )

    async def decide(self, bot: str, change_id: str, action: str) -> None:
        async with self.lock:
            async with self.db.read_lock:
                change = await item(
                    self.db.reader,
                    "SELECT * FROM obsidian_changes WHERE id=? AND bot_id=?",
                    (change_id, bot),
                )
            if not change:
                raise ValueError("Правка не найдена")
            if change["state"] == "applied" and action == "apply":
                return
            if action == "reject":
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE obsidian_changes SET state='rejected' WHERE id=? "
                        "AND state IN ('pending','conflict','invalid')",
                        (change_id,),
                    )
                return
            if change["state"] != "pending" or not self.root:
                raise ValueError("Эту правку нельзя применить; отклони её и обнови синхронизацию")
            data = await asyncio.to_thread(fs.read, self.root, change["path"])
            if data is None or fs.digest(data) != change["file_hash"]:
                raise ValueError("Файл изменился. Обнови синхронизацию")
            body = MemoryMutation(
                bot_id=bot,
                expected_version=change["expected_version"],
                action="edit",
                text=change["proposed_text"],
            )
            try:
                await self.memory.mutate(
                    bot, change["fact_id"], body, operation_id=change_id, change_id=change_id
                )
            except ConflictError:
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE obsidian_changes SET state='conflict',"
                        "reason='Запись изменилась' WHERE id=?",
                        (change_id,),
                    )
                raise

    async def undo(self, bot: str, command_id: str, operation_id: str) -> None:
        async with self.db.read_lock:
            history = await item(
                self.db.reader,
                "SELECT * FROM memory_commands WHERE id=? AND bot_id=?",
                (command_id, bot),
            )
        if not history or history["before_text"] is None:
            raise ValueError("Отмена недоступна")
        body = MemoryMutation(
            bot_id=bot,
            expected_version=history["after_version"],
            action="edit",
            text=history["before_text"],
        )
        await self.memory.mutate(
            bot, history["fact_id"], body, operation_id=operation_id, undo_id=command_id
        )

    async def listing(self, bot: str | None, page: int, history_page: int) -> dict[str, Any]:
        total = await self.db.one(
            "SELECT count(*) FROM obsidian_changes WHERE bot_id=? "
            "AND state IN ('pending','conflict','invalid')",
            (bot,),
        )
        histories = await self.db.one(
            "SELECT count(*) FROM memory_commands WHERE bot_id=? "
            "AND action='edit' AND before_text IS NOT NULL",
            (bot,),
        )
        changes: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        async with self.db.read_lock:
            for table, where, offset, output in (
                ("obsidian_changes", "state IN ('pending','conflict','invalid')", page, changes),
                (
                    "memory_commands",
                    "action='edit' AND before_text IS NOT NULL",
                    history_page,
                    history,
                ),
            ):
                async with self.db.reader.execute(
                    f"SELECT * FROM {table} WHERE bot_id=? AND {where} "
                    "ORDER BY created_at DESC LIMIT 10 OFFSET ?",
                    (bot, offset * 10),
                ) as cursor:
                    for row in await cursor.fetchall():
                        value = dict(zip((col[0] for col in cursor.description), row, strict=True))
                        live = (
                            await item(
                                self.db.reader,
                                "SELECT text,version,state FROM memory_facts WHERE id=?",
                                (value["fact_id"],),
                            )
                            if value["fact_id"]
                            else None
                        )
                        value["current_text"] = (
                            live["text"] if live and live["state"] != "deleted" else None
                        )
                        value["current_version"] = live["version"] if live else None
                        value["can_undo"] = bool(
                            table == "memory_commands"
                            and live
                            and live["state"] not in {"deleted", "outdated"}
                            and live["version"] == value["after_version"]
                        )
                        if (
                            table == "obsidian_changes"
                            and value["state"] == "pending"
                            and (not live or live["version"] != value["expected_version"])
                        ):
                            value["state"] = "conflict"
                            value["reason"] = "Запись изменилась после выгрузки"
                        output.append(value)
        status_counts = await self.db.all(
            "SELECT state,count(*) FROM obsidian_files GROUP BY state"
        )
        return {
            "configured": self.root is not None,
            "path": self.project_path,
            "error": self.error,
            "last_scan": self.last_scan,
            "scanning": self.lock.locked(),
            "file_states": dict(status_counts),
            "changes": changes,
            "total": total[0],
            "page": page,
            "history": history,
            "history_total": histories[0],
            "history_page": history_page,
        }
