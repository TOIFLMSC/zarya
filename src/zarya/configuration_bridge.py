"""Bounded Obsidian configuration drafts, explicit atomic release and rollback."""

import asyncio
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from zarya import obsidian_fs as fs
from zarya.configuration import canonical, digest, document, validate
from zarya.database import ConflictError
from zarya.models import Settings
from zarya.prompts import DEFAULT_PROMPTS, MAX_BYTES, _unique
from zarya.telegram_store import TelegramStore, row

FOLDER = "Конфигурация Зари"
MARKER = ".zarya-configuration.json"
MAX_NOTE = MAX_BYTES + 4096
GROUPS = {
    "Характер": ("display_name", "persona", "tone"),
    "Владелец": ("owner_telegram_id", "owner_username", "owner_name"),
    "Поведение": ("behavior_enabled", "reactions_enabled", "expressiveness", "emotional_max_parts"),
}
GROUPS["Модели и лимиты"] = tuple(
    k for k in Settings.model_fields if k not in sum(GROUPS.values(), ())
)


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def bodies(doc: dict[str, Any]) -> dict[str, str]:
    settings = Settings.model_validate(doc["settings"]).model_dump()
    result = {
        f"{name}.md": json.dumps({k: settings[k] for k in keys}, ensure_ascii=False, indent=2)
        for name, keys in GROUPS.items()
    }
    result.update({f"Инструкции/{k}.md": v for k, v in doc["prompts"]["fragments"].items()})
    return result


def note(path: str, body: str, version: int, epoch: str) -> tuple[str, str, bytes]:
    import re

    fence = "`" * max(4, max((len(v) + 1 for v in re.findall(r"`+", body)), default=4))
    prefix = (
        f"---\nzarya_configuration: 1\nepoch: {epoch}\nversion: {version}\n---\n\n"
        f"# {Path(path).stem}\n\n"
        "Меняй только содержимое блока. Активация — в Web UI → Настройки.\n\n"
        f"{fence}\n"
    )
    suffix = f"\n{fence}\n\n[[Начало]]\n"
    return prefix, suffix, (prefix + body + suffix).encode()


class FrozenFiles:
    """Retain exclusive handles across the SQL activation, including the marker."""

    def __init__(self, root: Path, expected: dict[str, str]):
        self.stack = ExitStack()
        try:
            for name, value in sorted(expected.items()):
                path = self.stack.enter_context(fs.parents(root, name))
                stream = self.stack.enter_context(fs.opened(path))
                raw = stream.read(MAX_NOTE + 1)
                if len(raw) > MAX_NOTE or fs.digest(raw) != value:
                    raise ConflictError
        except BaseException:
            self.stack.close()
            raise

    def close(self) -> None:
        self.stack.close()


class ConfigurationBridge:
    def __init__(self, store: TelegramStore, config_file: Path):
        self.store, self.db = store, store.db
        self.root: Path | None = None
        self.project_path = ""
        self.epoch = ""
        self.error: str | None = None
        self.last_scan: str | None = None
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        if config_file.exists():
            try:
                conf = json.loads(config_file.read_text(encoding="utf-8-sig"))
                if set(conf) != {"enabled", "project_path"} or type(conf["enabled"]) is not bool:
                    raise ValueError
                if conf["enabled"]:
                    path = Path(conf["project_path"])
                    if not path.is_absolute() or not path.is_dir():
                        raise ValueError
                    self.project_path = str(path.absolute())
                    self.root = path.absolute() / FOLDER
            except (ValueError, TypeError, OSError):
                self.error = "Проверь obsidian.local.json и папку проекта"

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
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "Не удалось проверить конфигурацию. Проверь доступ к папке и повтори"
            await asyncio.sleep(15)

    def marker(self) -> bytes:
        return canonical({"schema": 1, "epoch": self.epoch}).encode()

    async def bind(self) -> None:
        assert self.root
        binding = await self.db.one("SELECT root,epoch FROM configuration_bridge WHERE id=1")
        if binding and binding[0] != self.project_path:
            raise ValueError("Мост конфигурации уже привязан к другой папке")
        if not binding:
            if self.root.exists():
                raise ValueError("Папка конфигурации занята; чужие заметки не импортируются")
            self.epoch = str(uuid4())
            async with self.db.transaction() as c:
                await c.execute(
                    "INSERT INTO configuration_bridge VALUES (1,?,?,NULL)",
                    (self.project_path, self.epoch),
                )
        else:
            self.epoch = binding[1]
        if not self.root.exists():
            await asyncio.to_thread(
                fs.write, self.root.parent, f"{FOLDER}/{MARKER}", self.marker(), None
            )
        marker = await asyncio.to_thread(fs.read, self.root, MARKER)
        if marker is None and not any(self.root.iterdir()):
            await asyncio.to_thread(fs.write, self.root, MARKER, self.marker(), None)
            marker = await asyncio.to_thread(fs.read, self.root, MARKER)
        if marker != self.marker():
            raise ValueError("Маркер конфигурации изменён; синхронизация остановлена")
        links = [f"[[{Path(p).stem}]]" for p in bodies(document(Settings(), DEFAULT_PROMPTS))]
        guide = (
            "# Конфигурация Зари\n\n"
            + "\n".join(links)
            + "\n\nМеняй только блоки в управляемых заметках. JSON настроек сохран"
            "яет все поля и типы. "
            "Через две проверки открой Web UI → Настройки → Конфигурация · Obsidian, "
            "просмотри изменения и активируй выпуск. До этого Заря использует прежнюю версию. "
            "Откат создаёт новый выпуск и не восстанавливает отменённые задания.\n\n"
            "Ключи, доступ к чатам, настроение и режим инициативы отдельных гр"
            "упп здесь не хранятся. "
            "Промпты приватны; версии Sync и резервные копии остаются отдельными копиями. "
            "После первого импорта SQL — источник активной конфигурации; promp"
            "ts.json больше её не меняет.\n"
        ).encode()
        await asyncio.to_thread(fs.write, self.root, "Начало.md", guide, None)

    async def current(self) -> dict[str, Any]:
        r = await self.db.one(
            "SELECT version,settings_json,prompts_json,hash,created_at,origin "
            "FROM configuration_releases ORDER BY version DESC LIMIT 1"
        )
        if not r:
            raise ValueError("Активная конфигурация отсутствует")
        return {
            "version": r[0],
            "document": {"settings": json.loads(r[1]), "prompts": json.loads(r[2])},
            "hash": r[3],
            "created_at": r[4],
            "origin": r[5],
        }

    async def export(self, active: dict[str, Any]) -> None:
        assert self.root
        for path, body in bodies(active["document"]).items():
            f = await self.db.one(
                "SELECT hash,state,previous_hash FROM configuration_files WHERE path=?", (path,)
            )
            prefix, suffix, raw = note(path, body, active["version"], self.epoch)
            if not f:
                lease = await asyncio.to_thread(fs.Reservation, self.root, path, limit=MAX_NOTE)
                try:
                    async with self.db.transaction() as c:
                        await c.execute(
                            "INSERT INTO configuration_files VALUES (?,?,?,?,?,?,'writing',NULL)",
                            (path, active["version"], fs.digest(raw), prefix, suffix, body),
                        )
                    await asyncio.to_thread(lease.publish, raw)
                finally:
                    await asyncio.to_thread(lease.close)
            else:
                actual = await asyncio.to_thread(fs.read, self.root, path, limit=MAX_NOTE)
                if actual is None:
                    if f[1] == "writing":
                        if not await asyncio.to_thread(
                            fs.write, self.root, path, raw, None, limit=MAX_NOTE
                        ):
                            raise ConflictError
                        actual = raw
                    else:
                        raise ValueError(
                            f"Отсутствует заметка: {path}. Удаление файла не меняет конфигурацию"
                        )
                expected = fs.digest(actual)
                if expected != f[0] and not (
                    f[1] == "writing" and (not actual or expected == f[2])
                ):
                    raise ConflictError
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE configuration_files SET version=?,hash=?,prefix=?,suffix=?,"
                        "body=?,state='writing',previous_hash=? WHERE path=?",
                        (active["version"], fs.digest(raw), prefix, suffix, body, expected, path),
                    )
                if actual != raw and not await asyncio.to_thread(
                    fs.write, self.root, path, raw, expected, limit=MAX_NOTE
                ):
                    raise ConflictError
                async with self.db.transaction() as c:
                    await c.execute(
                        "UPDATE configuration_files SET version=?,hash=?,prefix=?,suffix=?"
                        ",body=?,state='ready' WHERE path=?",
                        (active["version"], fs.digest(raw), prefix, suffix, body, path),
                    )
            async with self.db.transaction() as c:
                await c.execute(
                    "UPDATE configuration_files SET state='ready' WHERE path=?", (path,)
                )

    async def scan(self) -> None:
        if not self.root:
            raise ValueError("Мост конфигурации не настроен")
        async with self.lock:
            self.error = None
            await self.bind()
            active = await self.current()
            files = await self.db.all(
                "SELECT path,version,hash,prefix,suffix,body,state FROM configurat"
                "ion_files ORDER BY path"
            )
            if not files:
                await self.export(active)
                self.last_scan = stamp()
                return
            raw_bodies, hashes, versions = {}, {}, set()
            changed = False
            for path, version, expected, prefix, suffix, _body, state in files:
                raw = await asyncio.to_thread(fs.read, self.root, path, limit=MAX_NOTE)
                if state == "writing":
                    # Durable creation intent: recover only our empty reserved file.
                    await self.export(active)
                    return
                if raw is None:
                    self.error = f"Отсутствует заметка: {path}"
                    return
                value = raw.decode("utf-8").replace("\r\n", "\n")
                if not value.startswith(prefix) or not value.endswith(suffix):
                    self.error = f"Некорректная структура заметки: {path}"
                    return
                proposed = value[len(prefix) : -len(suffix)]
                same_body = proposed == _body.replace("\r\n", "\n")
                raw_bodies[path] = _body if same_body else proposed
                hashes[path] = fs.digest(raw)
                versions.add(version)
                changed |= not same_body and not (state == "refresh" and hashes[path] == expected)
                if same_body and hashes[path] != expected:
                    async with self.db.transaction() as c:
                        await c.execute(
                            "UPDATE configuration_files SET hash=? WHERE path=?",
                            (hashes[path], path),
                        )
            if not changed:
                desired = bodies(active["document"])
                if (
                    any(f[6] == "refresh" for f in files)
                    or any(v != active["version"] for v in versions)
                    or len(files) != len(desired)
                    or any(desired.get(f[0]) != f[5] for f in files)
                ):
                    await self.export(active)
                self.last_scan = stamp()
                return
            if len(versions) != 1:
                self.error = "Комплект заметок неполный или содержит разные версии"
                return
            base = next(iter(versions))
            doc: dict[str, Any] = {
                "settings": {},
                "prompts": {"schema_version": 1, "fragments": {}},
            }
            try:
                if set(raw_bodies) != set(bodies(active["document"])):
                    raise ValueError
                for name, keys in GROUPS.items():
                    value = json.loads(raw_bodies[f"{name}.md"], object_pairs_hook=_unique)
                    if "memory_retrieval" in keys and "memory_retrieval" not in value:
                        value["memory_retrieval"] = "shadow"
                    if "participant_context_messages" in keys:
                        value.setdefault("participant_context_messages", 20)
                    for key in (
                        "participant_summaries_enabled",
                        "participant_summary_prompt",
                        "topic_context_chat_id",
                    ):
                        if key in keys:
                            value.setdefault(key, getattr(Settings(), key))
                    if set(value) != set(keys):
                        raise ValueError
                    doc["settings"].update(value)
                doc["prompts"]["fragments"] = {
                    k: raw_bodies[f"Инструкции/{k}.md"] for k in DEFAULT_PROMPTS.fragments
                }
                settings, prompts = validate(canonical(doc))
                doc = document(settings, prompts)
            except (ValueError, TypeError):
                self.error = (
                    "Некорректный черновик. Проверь JSON настроек, обязательные поля и"
                    " лимиты промптов"
                )
                return
            fingerprint = fs.digest(canonical(hashes).encode())
            async with self.db.transaction() as c:
                previous = await row(c, "SELECT seen_hash FROM configuration_bridge WHERE id=1", ())
                await c.execute(
                    "UPDATE configuration_bridge SET seen_hash=? WHERE id=1", (fingerprint,)
                )
                if previous[0] != fingerprint:
                    self.last_scan = stamp()
                    return
                existing = await row(
                    c,
                    "SELECT id FROM configuration_drafts WHERE files_json=? AND state="
                    "'pending' AND origin='obsidian'",
                    (canonical(hashes),),
                )
                if not existing:
                    await c.execute(
                        "UPDATE configuration_drafts SET state='superseded' WHERE state='p"
                        "ending' AND origin='obsidian'"
                    )
                    await c.execute(
                        "INSERT INTO configuration_drafts VALUES (?,?,?,?,?,?,'pending',?,NULL)",
                        (
                            str(uuid4()),
                            base,
                            canonical(doc),
                            digest(doc),
                            canonical(hashes),
                            "obsidian",
                            stamp(),
                        ),
                    )
            self.last_scan = stamp()

    async def listing(self, page: int = 0) -> dict[str, Any]:
        active = await self.current()
        drafts = await self.db.all(
            "SELECT id,base_version,document,hash,origin,created_at FROM confi"
            "guration_drafts WHERE state='pending' ORDER BY created_at DESC LI"
            "MIT 10"
        )
        history = await self.db.all(
            "SELECT version,hash,created_at,origin FROM configuration_releases"
            " WHERE origin!='legacy_bootstrap' ORDER BY version DESC LIMIT 10 "
            "OFFSET ?",
            (page * 10,),
        )
        return {
            "configured": self.root is not None,
            "path": str(self.root) if self.root else None,
            "error": self.error,
            "scanning": self.lock.locked(),
            "last_scan": self.last_scan,
            "current": {k: v for k, v in active.items() if k != "document"},
            "drafts": [
                {
                    "id": d[0],
                    "base_version": d[1],
                    "hash": d[3],
                    "origin": d[4],
                    "created_at": d[5],
                    "conflict": d[1] != active["version"],
                }
                for d in drafts
            ],
            "history": [
                dict(zip(("version", "hash", "created_at", "origin"), h, strict=True))
                for h in history
            ],
            "total": (
                await self.db.one(
                    "SELECT count(*) FROM configuration_releases WHERE origin!='legacy_bootstrap'"
                )
            )[0],
            "page": page,
        }

    async def details(self, draft_id: str) -> dict[str, Any]:
        d = await self.db.one(
            "SELECT base_version,document,hash,origin,state FROM configuration_drafts WHERE id=?",
            (draft_id,),
        )
        if not d:
            raise ValueError("Черновик не найден")
        base = await self.db.one(
            "SELECT settings_json,prompts_json FROM configuration_releases WHERE version=?", (d[0],)
        )
        active = await self.current()
        before = bodies({"settings": json.loads(base[0]), "prompts": json.loads(base[1])})
        proposed, now = bodies(json.loads(d[1])), bodies(active["document"])
        return {
            "id": draft_id,
            "hash": d[2],
            "base_version": d[0],
            "current_version": active["version"],
            "current_hash": active["hash"],
            "origin": d[3],
            "state": d[4],
            "conflict": d[0] != active["version"],
            "nodes": [
                {"path": p, "before": before[p], "current": now[p], "proposed": v}
                for p, v in proposed.items()
                if v != before[p]
            ],
        }

    async def rollback(self, version: int, expected: int) -> str:
        async with self.store.gate, self.db.transaction() as c:
            current = await row(c, "SELECT version FROM settings WHERE id=1", ())
            target = await row(
                c,
                "SELECT settings_json,prompts_json FROM configuration_releases WHE"
                "RE version=? AND origin!='legacy_bootstrap'",
                (version,),
            )
            if current[0] != expected or not target or version == expected:
                raise ConflictError
            doc = {"settings": json.loads(target[0]), "prompts": json.loads(target[1])}
            id_ = str(uuid4())
            await c.execute(
                "INSERT INTO configuration_drafts VALUES (?,?,?,?,?,'rollback','pending',?,NULL)",
                (id_, expected, canonical(doc), digest(doc), "{}", stamp()),
            )
            return id_

    async def decide(self, draft_id: str, action: str, expected: int, hash_: str) -> int:
        async with self.lock, self.store.gate:
            d = await self.db.one(
                "SELECT base_version,document,hash,files_json,state,applied_versio"
                "n,origin FROM configuration_drafts WHERE id=?",
                (draft_id,),
            )
            if not d or d[2] != hash_:
                raise ConflictError
            if d[4] == "applied" and action == "apply":
                return int(d[5])
            if d[4] != "pending":
                raise ConflictError
            lease = None
            try:
                if d[6] == "obsidian":
                    if not self.root:
                        raise ConflictError
                    await self.bind()
                    hashes = json.loads(d[3]) | {MARKER: fs.digest(self.marker())}
                    lease = await asyncio.to_thread(FrozenFiles, self.root, hashes)
                async with self.db.transaction() as c:
                    active = await row(c, "SELECT version FROM settings WHERE id=1", ())
                    if expected != active[0] or (action == "apply" and d[0] != expected):
                        raise ConflictError
                    if action == "apply":
                        settings, prompts = validate(d[1])
                        result = await self.db._update_settings(
                            c, expected, settings, prompts, d[6]
                        )
                        version = result.version
                        await c.execute(
                            "UPDATE configuration_drafts SET state='applied',applied_version=?"
                            " WHERE id=?",
                            (version, draft_id),
                        )
                    else:
                        version = expected
                        await c.execute(
                            "UPDATE configuration_drafts SET state='rejected' WHERE id=?",
                            (draft_id,),
                        )
                    # Only byte-exact accepted/rejected notes become eligible for refresh.
                    for path, value in json.loads(d[3]).items():
                        await c.execute(
                            "UPDATE configuration_files SET hash=?,state='refresh' WHERE path=?",
                            (value, path),
                        )
                    await c.execute("UPDATE configuration_bridge SET seen_hash=NULL WHERE id=1")
                return version
            finally:
                if lease:
                    await asyncio.to_thread(lease.close)
