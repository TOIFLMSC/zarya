"""One transactional owner command for Web UI, Obsidian and guarded text undo."""

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from zarya.database import ConflictError
from zarya.memory_context import valid_dependencies
from zarya.models import MemoryMutation

if TYPE_CHECKING:
    from zarya.memory import MemoryEngine


async def item(
    c: aiosqlite.Connection,
    sql: str,
    args: tuple[object, ...],
) -> dict[str, Any] | None:
    async with c.execute(sql, args) as cursor:
        row = await cursor.fetchone()
        return (
            dict(zip((col[0] for col in cursor.description or []), row, strict=True))
            if row
            else None
        )


async def apply_command(
    engine: "MemoryEngine",
    c: aiosqlite.Connection,
    bot: str,
    fact_id: int,
    body: MemoryMutation,
    operation_id: str | None,
    change_id: str | None,
    undo_id: str | None,
) -> None:
    operation_id = operation_id or body.operation_id or str(uuid4())
    origin = "obsidian" if change_id else "undo" if undo_id else "web"
    payload = json.dumps(
        [bot, fact_id, body.model_dump(exclude={"operation_id"}), origin, change_id, undo_id],
        sort_keys=True,
    )
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    previous = await item(c, "SELECT payload_hash FROM memory_commands WHERE id=?", (operation_id,))
    if previous:
        if previous["payload_hash"] != payload_hash:
            raise ConflictError
        return
    fact = await item(c, "SELECT * FROM memory_facts WHERE id=? AND bot_id=?", (fact_id, bot))
    if not fact or fact["state"] == "deleted" or fact["version"] != body.expected_version:
        raise ConflictError
    if body.action == "edit" and (body.text is None or not body.text.strip()):
        raise ValueError("Введите непустой текст записи")
    if change_id:
        change = await item(c, "SELECT * FROM obsidian_changes WHERE id=?", (change_id,))
        binding = await item(c, "SELECT epoch FROM obsidian_binding WHERE id=1", ())
        if (
            not change
            or not binding
            or any(
                (
                    change["state"] != "pending",
                    change["epoch"] != binding["epoch"],
                    change["bot_id"] != bot,
                    change["chat_id"] != fact["chat_id"],
                    change["fact_id"] != fact_id,
                    change["expected_version"] != fact["version"],
                    change["access_version"] != fact["access_version"],
                    change["proposed_text"] != body.text,
                    body.action != "edit",
                )
            )
        ):
            raise ConflictError
    if change_id or undo_id:
        deps = fact["context_dependencies"]
        if deps and not await valid_dependencies(
            c, bot, fact["chat_id"], fact["access_version"], deps
        ):
            raise ValueError("Источники записи изменились")
    if undo_id:
        history = await item(c, "SELECT * FROM memory_commands WHERE id=?", (undo_id,))
        if not history or any(
            (
                history["bot_id"] != bot,
                history["fact_id"] != fact_id,
                history["after_version"] != fact["version"],
                history["action"] != "edit",
                history["before_text"] is None,
                history["before_text"] != body.text,
                history["after_text"] != fact["text"],
                body.action != "edit",
            )
        ):
            raise ConflictError
    await engine._mutate(c, bot, fact_id, body)
    after = await item(c, "SELECT version,text FROM memory_facts WHERE id=?", (fact_id,))
    assert after is not None
    # Journal contains only manual text edits; no restoration of evidence or consent.
    before_text = fact["text"] if body.action == "edit" else None
    after_text = after["text"] if body.action == "edit" else None
    await c.execute(
        "INSERT INTO memory_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            operation_id,
            payload_hash,
            bot,
            fact["chat_id"],
            fact_id,
            origin,
            body.action,
            fact["version"],
            after["version"],
            before_text,
            after_text,
            datetime.now(UTC).isoformat(),
        ),
    )
    if change_id:
        await c.execute("UPDATE obsidian_changes SET state='applied' WHERE id=?", (change_id,))
