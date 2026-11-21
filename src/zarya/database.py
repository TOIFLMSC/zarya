import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import aiosqlite

from zarya.configuration import effects, prompts_at, record
from zarya.models import Settings, SettingsSnapshot
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle


class ConflictError(Exception):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.write_lock = asyncio.Lock()
        self.read_lock = asyncio.Lock()
        self.writer: aiosqlite.Connection
        self.reader: aiosqlite.Connection

    async def open(self) -> None:
        self.writer = await aiosqlite.connect(self.path, isolation_level=None)
        reader_open = False
        try:
            await self.writer.execute("PRAGMA journal_mode=WAL")
            await self.writer.execute("PRAGMA foreign_keys=ON")
            await self.writer.execute("PRAGMA busy_timeout=5000")
            await self.writer.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"
            )
            for migration in sorted(files("zarya").joinpath("migrations").iterdir(), key=str):
                if not migration.name.endswith(".sql"):
                    continue
                async with self.writer.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (migration.name,)
                ) as cursor:
                    applied = await cursor.fetchone()
                if not applied:
                    async with self.transaction() as connection:
                        for statement in migration.read_text(encoding="utf-8").split(";"):
                            if statement.strip():
                                await connection.execute(statement)
                        await connection.execute(
                            "INSERT INTO schema_migrations VALUES (?)", (migration.name,)
                        )
            async with self.transaction() as connection:
                stamp = datetime.now(UTC).isoformat()
                document = Settings().model_dump_json()
                await connection.execute(
                    "INSERT OR IGNORE INTO settings VALUES (1, 1, ?, ?)", (document, stamp)
                )
                await connection.execute(
                    "INSERT OR IGNORE INTO settings_versions VALUES (1, ?, ?)", (document, stamp)
                )
            self.reader = await aiosqlite.connect(self.path, isolation_level=None)
            reader_open = True
            await self.reader.execute("PRAGMA query_only=ON")
        except BaseException:
            try:
                if reader_open:
                    await self.reader.close()
            finally:
                await self.writer.close()
            raise

    async def close(self) -> None:
        try:
            await self.reader.close()
        finally:
            await self.writer.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.write_lock:
            try:
                await self.writer.execute("BEGIN IMMEDIATE")
                self.writer._zarya_transaction = object()  # type: ignore[attr-defined]
                yield self.writer
                await self.writer.commit()
            except BaseException:
                # Finish rollback before releasing the connection to another coroutine.
                rollback = asyncio.create_task(self.writer.rollback())
                try:
                    await asyncio.shield(rollback)
                except asyncio.CancelledError:
                    await rollback
                    raise
                raise
            finally:
                self.writer._zarya_transaction = None  # type: ignore[attr-defined]

    async def one(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        async with self.read_lock, self.reader.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def admin_hash(self) -> str | None:
        row = await self.one("SELECT password_hash FROM admin WHERE id=1")
        return str(row[0]) if row else None

    async def all(self, sql: str, parameters: tuple[object, ...] = ()) -> list[Any]:
        async with self.read_lock, self.reader.execute(sql, parameters) as cursor:
            return list(await cursor.fetchall())

    async def create_admin(self, password_hash: str) -> bool:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "INSERT OR IGNORE INTO admin VALUES (1, ?)", (password_hash,)
            )
            return cursor.rowcount == 1

    async def settings(self) -> SettingsSnapshot:
        row = await self.one("SELECT version, document, updated_at FROM settings WHERE id=1")
        return SettingsSnapshot(
            version=row[0], settings=Settings.model_validate_json(row[1]), updated_at=row[2]
        )

    async def initialize_configuration(self, prompts: PromptBundle) -> None:
        async with self.transaction() as c:
            async with c.execute("SELECT 1 FROM configuration_releases LIMIT 1") as cur:
                if await cur.fetchone():
                    await c.execute(
                        "UPDATE photo_batches SET settings_version=(SELECT MIN(version) FROM "
                        "configuration_releases WHERE origin='bootstrap') WHERE "
                        "settings_version IS NULL AND state IN ('queued','downloading')"
                    )
                    return
            async with c.execute(
                "SELECT version,document FROM settings_versions ORDER BY version"
            ) as cur:
                versions = list(await cur.fetchall())
            for version, raw in versions:
                await record(
                    c,
                    version,
                    Settings.model_validate_json(raw),
                    prompts,
                    "bootstrap" if version == versions[-1][0] else "legacy_bootstrap",
                    None,
                )
            await c.execute(
                "UPDATE photo_batches SET settings_version=? WHERE settings_version IS NULL "
                "AND state IN ('queued','downloading')",
                (versions[-1][0],),
            )

    async def update_settings(self, expected: int, settings: Settings) -> SettingsSnapshot:
        async with self.transaction() as connection:
            return await self._update_settings(connection, expected, settings)

    async def _update_settings(
        self,
        connection: aiosqlite.Connection,
        expected: int,
        settings: Settings,
        prompts: PromptBundle | None = None,
        origin: str = "web",
    ) -> SettingsSnapshot:
        stamp = datetime.now(UTC).isoformat()
        document = settings.model_dump_json()
        cursor = await connection.execute(
            "UPDATE settings SET document=?, version=version+1, updated_at=? "
            "WHERE id=1 AND version=?",
            (document, stamp, expected),
        )
        if cursor.rowcount != 1:
            raise ConflictError
        await connection.execute(
            "INSERT INTO settings_versions VALUES (?, ?, ?)", (expected + 1, document, stamp)
        )
        async with connection.execute(
            "SELECT prompts_json FROM configuration_releases WHERE version=?", (expected,)
        ) as cur:
            release = await cur.fetchone()
        if release:
            active_prompts = prompts or await prompts_at(connection, expected, DEFAULT_PROMPTS)
            await record(connection, expected + 1, settings, active_prompts, origin, expected)
            await effects(connection, settings)
        return SettingsSnapshot(version=expected + 1, settings=settings, updated_at=stamp)
