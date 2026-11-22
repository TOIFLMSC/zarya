"""Bounded local episode retrieval and revision-bound extraction context."""

import json
import re
import time
from typing import Any

import aiosqlite

from zarya.dialogue_logic import explicit_reply
from zarya.memory_read import MemoryRead
from zarya.source_material import accepted
from zarya.source_material import valid as material_valid

RETENTION = 90 * 86400
SOURCE_ROW = (
    "SELECT event_id,valid,received_at FROM memory_sources WHERE id=? "
    "AND bot_id=? AND chat_id=? AND access_version=?"
)
DEPENDENCY_FACT_ROW = (
    "SELECT version,state,context_dependencies FROM memory_facts "
    "WHERE id=? AND bot_id=? AND chat_id=? AND access_version=?"
)
SNAPSHOT_FACT_ROW = (
    "SELECT chat_id,access_version,version,state,context_dependencies "
    "FROM memory_facts WHERE bot_id=? AND id=?"
)
SNAPSHOT_BATCH_ROW = (
    "SELECT chat_id,access_version,summary,dependencies "
    "FROM memory_batches WHERE bot_id=? AND id=? AND state='completed'"
)


def dependencies() -> dict[str, Any]:
    return {"version": 1, "sources": [], "facts": [], "replies": [], "runs": []}


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def valid_dependencies(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    value: Any,
    *,
    depth: int = 0,
    read: MemoryRead | None = None,
) -> bool:
    if read is None:
        with MemoryRead(conn) as read:
            return await valid_dependencies(conn, bot, chat, grant, value, depth=depth, read=read)
    try:
        if depth > 8:
            return False
        deps = json.loads(value) if isinstance(value, str) else value
        if not isinstance(deps, dict) or deps.get("version") != 1:
            return False
        if any(not isinstance(deps.get(k), list) for k in ("sources", "facts", "replies", "runs")):
            return False
        access = await read.one(
            conn,
            "SELECT state,version,member_state FROM telegram_access "
            "WHERE bot_id=? AND subject_id=? AND scope=?",
            (bot, chat, "group" if chat.startswith("-") else "private"),
        )
        if (
            not access
            or access[0] != "approved"
            or access[1] != grant
            or access[2] in {"left", "kicked", "migrated"}
        ):
            return False
        await read.prefetch(
            conn, SOURCE_ROW, [(r["id"], bot, chat, grant) for r in deps["sources"]]
        )
        await read.prefetch(
            conn, DEPENDENCY_FACT_ROW, [(r["id"], bot, chat, grant) for r in deps["facts"]]
        )
        for ref in deps["sources"]:
            source = await read.one(
                conn,
                SOURCE_ROW,
                (ref["id"], bot, chat, grant),
            )
            if (
                not source
                or source[0] != ref["event_id"]
                or not source[1]
                or source[2] <= time.time() - RETENTION
            ):
                return False
        for ref in deps["facts"]:
            fact = await read.one(
                conn,
                DEPENDENCY_FACT_ROW,
                (ref["id"], bot, chat, grant),
            )
            if (
                not fact
                or fact[0] != ref["version"]
                or fact[1] not in {"active", "disputed", "proposed"}
            ):
                return False
            if fact[2] is not None and not await valid_dependencies(
                conn, bot, chat, grant, fact[2], depth=depth + 1, read=read
            ):
                return False
        if not await material_valid(conn, bot, chat, grant, deps["replies"], read=read):
            return False
        for ref in deps["runs"]:
            sent = await read.one(
                conn,
                "SELECT r.snapshot FROM dialogue_runs r JOIN outbox o ON o.job_id=r.job_id "
                "JOIN recent_messages m ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id "
                "AND m.message_id=o.message_id WHERE r.id=? AND o.id=? AND o.bot_id=? "
                "AND o.chat_id=? AND o.access_version=? AND o.state='sent' AND r.state='completed' "
                "AND r.mode='live' AND m.role='assistant' AND m.received_at>?",
                (ref["id"], ref["outbox_id"], bot, chat, grant, time.time() - RETENTION),
                cache=False,
            )
            if not sent:
                return False
            snapshot = json.loads(sent[0])
            if snapshot.get("invalidated") or not await material_valid(
                conn, bot, chat, grant, snapshot.get("source_manifest", []), read=read
            ):
                return False
            if not await valid_snapshot(conn, bot, snapshot, depth=depth + 1, read=read):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


async def reply_context(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    source: dict[str, Any],
    *,
    include_message: bool = False,
    read: MemoryRead | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if read is None:
        with MemoryRead(conn) as read:
            return await reply_context(
                conn, bot, chat, grant, source, include_message=include_message, read=read
            )
    deps = dependencies()
    event = await read.one(
        conn,
        "SELECT payload FROM events WHERE id=? AND bot_id=? AND chat_id=?",
        (source["event_id"], bot, chat),
    )
    if not event:
        return None, deps
    update = json.loads(event[0])
    message = update.get("edited_message") or update.get("message") or {}
    target = (
        message.get("message_id") if include_message else explicit_reply(message).get("message_id")
    )
    if not target:
        return None, deps
    original = await accepted(conn, bot, chat, grant, target)
    if original:
        event_id, body = original
        # Prefer the accepted transcript of a voice message when one exists.
        saved = await read.one(
            conn,
            "SELECT id,text FROM memory_sources WHERE bot_id=? AND chat_id=? "
            "AND access_version=? AND event_id=? AND valid=1 AND received_at>?",
            (bot, chat, grant, event_id, time.time() - RETENTION),
            cache=False,
        )
        text = saved[1] if saved else str(body.get("text") or body.get("caption") or "")
        if not text:
            return None, deps
        deps["replies"] = [{"message_id": target, "event_id": event_id}]
        if saved:
            deps["sources"] = [{"id": saved[0], "event_id": event_id}]
        return {
            "message_id": target,
            "role": "user",
            "sender_id": str((body.get("from") or {}).get("id", "")),
            "text": text[:2000],
            "forwarded": bool(body.get("forward_origin") or body.get("forward_date")),
        }, deps
    sent = await read.one(
        conn,
        "SELECT r.id,o.id,m.text FROM outbox o JOIN dialogue_runs r ON r.job_id=o.job_id "
        "JOIN recent_messages m ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id "
        "AND m.message_id=o.message_id "
        "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.message_id=? "
        "AND o.state='sent' AND r.mode='live' AND r.state='completed' AND m.role='assistant' "
        "AND m.received_at>?",
        (bot, chat, grant, target, time.time() - RETENTION),
        cache=False,
    )
    if not sent:
        return None, deps
    deps["runs"] = [{"id": sent[0], "outbox_id": sent[1]}]
    if not await valid_dependencies(conn, bot, chat, grant, deps, read=read):
        return None, dependencies()
    return {
        "message_id": target,
        "role": "assistant",
        "text": sent[2][:2000],
        "context_only": True,
    }, deps


def merge_dependencies(target: dict[str, Any], extra: dict[str, Any]) -> None:
    for kind in ("sources", "facts", "replies", "runs"):
        for ref in extra[kind]:
            if ref not in target[kind]:
                target[kind].append(ref)


async def valid_snapshot(
    conn: aiosqlite.Connection,
    bot: str,
    snapshot: dict[str, Any],
    *,
    depth: int = 0,
    read: MemoryRead | None = None,
) -> bool:
    """Revalidate selected memory at generation, replay and the last delivery boundary."""
    if read is None:
        with MemoryRead(conn) as read:
            return await valid_snapshot(conn, bot, snapshot, depth=depth, read=read)
    if depth > 8 or snapshot.get("invalidated"):
        return False
    from zarya.participant_provenance import valid as participant_valid

    if not await participant_valid(conn, bot, snapshot, depth=depth, read=read):
        return False
    memory = snapshot.get("memory", {})
    chat = snapshot.get("chat_id") or snapshot.get("retrieval_chat_id")
    grant = snapshot.get("source_access_version")
    if memory.get("conversation_dependencies") is not None:
        if (
            not chat
            or grant is None
            or not await valid_dependencies(
                conn,
                bot,
                chat,
                grant,
                memory["conversation_dependencies"],
                depth=depth + 1,
                read=read,
            )
        ):
            return False
    if memory.get("conflicts"):
        from zarya.memory_retrieval import valid_relation

        if not chat or grant is None:
            return False
        for ref in memory["conflicts"]:
            if not await valid_relation(conn, bot, chat, grant, ref, depth=depth, read=read):
                return False
    await read.prefetch(
        conn, SNAPSHOT_FACT_ROW, [(bot, r["id"]) for r in memory.get("facts", [])], id_slot=1
    )
    await read.prefetch(
        conn, SNAPSHOT_BATCH_ROW, [(bot, r["id"]) for r in memory.get("summaries", [])], id_slot=1
    )
    for ref in snapshot.get("memory", {}).get("facts", []):
        fact = await read.one(
            conn,
            SNAPSHOT_FACT_ROW,
            (bot, ref["id"]),
        )
        if not fact or fact[2] != ref["version"] or fact[3] not in {"active", "proposed"}:
            return False
        if chat and not await valid_dependencies(
            conn, bot, fact[0], fact[1], dependencies(), depth=depth + 1, read=read
        ):
            return False
        if chat and fact[0] != chat:
            if not chat.startswith("-") or not ref.get("shared"):
                return False
            shared = await read.one(
                conn,
                "SELECT 1 FROM memory_shares WHERE fact_id=? AND fact_version=? AND state='active'",
                (ref["id"], ref["version"]),
            )
            if not shared:
                return False
        if fact[3] == "proposed":
            from zarya.memory_observations import strength

            if (
                not ref.get("tentative")
                or not (await strength(conn, bot, ref["id"], depth=depth + 1, read=read))[
                    "eligible"
                ]
            ):
                return False
        if fact[4] is not None and not await valid_dependencies(
            conn, bot, fact[0], fact[1], fact[4], depth=depth + 1, read=read
        ):
            return False
    for ref in snapshot.get("memory", {}).get("summaries", []):
        batch = await read.one(
            conn,
            SNAPSHOT_BATCH_ROW,
            (bot, ref["id"]),
        )
        if not batch or batch[2] is None:
            return False
        if chat and (batch[0] != chat or batch[1] != grant):
            return False
        if batch[3] is not None and not await valid_dependencies(
            conn, bot, batch[0], batch[1], batch[3], depth=depth + 1, read=read
        ):
            return False
    return True


def select_episodes(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Two recent episodes plus up to three lexical matches, never beyond 5000 chars."""

    def words(text: str) -> set[str]:
        return {w[:6] for w in re.findall(r"[\w]+", text.casefold()) if len(w) >= 4} - {
            "заря",
            "помниш",
            "расска",
            "которы",
            "какой",
            "этого",
            "чтобы",
            "тогда",
            "было",
        }

    terms = words(query)
    candidates = [(len(terms & words(row["text"])), row) for row in rows[2:]]
    matched = [
        r for score, r in sorted(candidates, key=lambda pair: pair[0], reverse=True) if score
    ][:3]
    selected, size = [], 0
    for row in rows[:2] + matched:
        if size + len(row["text"]) <= 5000:
            selected.append(row)
            size += len(row["text"])
    return selected
