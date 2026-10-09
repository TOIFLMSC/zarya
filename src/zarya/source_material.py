"""Resolve retained reply provenance; never substitute the latest chat attachment."""

import json
import time
from typing import Any

import aiosqlite

from zarya.research_logic import urls


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...]) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


def visual_sizes(message: dict[str, Any]) -> list[dict[str, Any]]:
    if message.get("photo"):
        return list(message["photo"])
    sticker = message.get("sticker")
    if sticker:
        if not sticker.get("is_animated") and not sticker.get("is_video"):
            return [sticker] if sticker.get("file_id") else []
        thumbnail = sticker.get("thumbnail")
        return [thumbnail] if thumbnail else []
    for kind in ("video", "animation", "video_note"):
        thumbnail = (message.get(kind) or {}).get("thumbnail")
        if thumbnail:
            return [thumbnail]
    return []


def describe(message: dict[str, Any]) -> dict[str, Any]:
    attachments = []
    for kind in ("photo", "video", "animation", "video_note", "voice", "sticker"):
        if message.get(kind):
            attachments.append(
                {
                    "kind": kind,
                    "coverage": "image"
                    if kind == "photo"
                    or (
                        kind == "sticker"
                        and not message[kind].get("is_animated")
                        and not message[kind].get("is_video")
                    )
                    else "thumbnail_only"
                    if visual_sizes(message)
                    else "unavailable",
                }
            )
    return {
        "message_id": message["message_id"],
        "text": str(message.get("text") or message.get("caption") or "")[:8000],
        "urls": urls(message),
        "attachments": attachments,
        "forwarded": bool(message.get("forward_origin") or message.get("forward_date")),
    }


async def accepted(
    conn: aiosqlite.Connection, bot: str, chat: str, grant: int, message_id: int
) -> tuple[int, dict[str, Any]] | None:
    target = await one(
        conn,
        "SELECT e.id,e.payload FROM research_messages m JOIN events e ON e.id=m.event_id "
        "WHERE m.bot_id=? AND m.chat_id=? AND m.message_id=? AND m.access_version=? "
        "AND e.disposition='accepted' AND CAST(strftime('%s',e.received_at) AS REAL)>=?",
        (bot, chat, message_id, grant, time.time() - 90 * 86400),
    )
    if not target:
        target = await one(
            conn,
            "SELECT e.id,e.payload FROM events e JOIN recent_messages r ON r.event_id=e.id "
            "AND r.bot_id=e.bot_id AND r.chat_id=e.chat_id WHERE e.bot_id=? AND e.chat_id=? "
            "AND r.message_id=? AND r.role='user' AND e.disposition='accepted' "
            "AND CAST(strftime('%s',e.received_at) AS REAL)>=? "
            "AND EXISTS (SELECT 1 FROM jobs j WHERE j.event_id=e.id AND j.access_version=?)",
            (bot, chat, message_id, time.time() - 90 * 86400, grant),
        )
        if target and target[1] != "{}":
            await conn.execute(
                "INSERT OR IGNORE INTO research_messages VALUES (?,?,?,?,?)",
                (bot, chat, message_id, target[0], grant),
            )
    if not target or target[1] == "{}":
        return None
    update = json.loads(target[1])
    message = update.get("edited_message") or update.get("message") or {}
    return (target[0], message) if message else None


async def valid(
    conn: aiosqlite.Connection, bot: str, chat: str, grant: int, manifest: list[dict[str, Any]]
) -> bool:
    if manifest:
        access = await one(
            conn,
            "SELECT state,version,member_state FROM telegram_access WHERE bot_id=? "
            "AND subject_id=? AND scope=?",
            (bot, chat, "group" if chat.startswith("-") else "private"),
        )
        if (
            not access
            or access[0] != "approved"
            or access[1] != grant
            or access[2] in {"left", "kicked", "migrated"}
        ):
            return False
    for ref in manifest:
        current = await accepted(conn, bot, chat, grant, ref["message_id"])
        if not current or current[0] != ref["event_id"]:
            return False
    return True


async def resolve(
    conn: aiosqlite.Connection, bot: str, chat: str, grant: int, message: dict[str, Any]
) -> dict[str, Any]:
    """Walk only actual sent outbox messages and accepted current user revisions."""
    result: dict[str, Any] = {
        "messages": [],
        "manifest": [],
        "unavailable": False,
        "via_assistant": False,
    }
    own_material = bool(
        message.get("forward_origin")
        or message.get("forward_date")
        or visual_sizes(message)
        or urls(message)
        or any(message.get(k) for k in ("video", "animation", "voice", "video_note", "sticker"))
    )
    target_id = (
        message["message_id"]
        if own_material
        else (message.get("reply_to_message") or {}).get("message_id")
    )
    if not target_id:
        return result
    seen: set[int] = set()
    for _ in range(24):
        if target_id in seen:
            break
        seen.add(target_id)
        source = await accepted(conn, bot, chat, grant, target_id)
        if source:
            event, original = source
            result["manifest"].append({"message_id": target_id, "event_id": event})
            reply = (original.get("reply_to_message") or {}).get("message_id")
            material = bool(
                original.get("forward_origin")
                or original.get("forward_date")
                or urls(original)
                or any(
                    original.get(k)
                    for k in ("photo", "video", "animation", "voice", "video_note", "sticker")
                )
            )
            if material or not reply:
                result["messages"].append(describe(original))
                album = original.get("media_group_id")
                if album:
                    async with conn.execute(
                        "SELECT m.message_id FROM research_messages m "
                        "JOIN events e ON e.id=m.event_id "
                        "WHERE m.bot_id=? AND m.chat_id=? AND m.access_version=? AND COALESCE("
                        "json_extract(e.payload,'$.edited_message.media_group_id'),"
                        "json_extract(e.payload,'$.message.media_group_id'))=? "
                        "ORDER BY m.message_id LIMIT 10",
                        (bot, chat, grant, str(album)),
                    ) as cursor:
                        siblings = await cursor.fetchall()
                    for (sibling,) in siblings:
                        if sibling == target_id:
                            continue
                        other = await accepted(conn, bot, chat, grant, sibling)
                        if other:
                            result["manifest"].append({"message_id": sibling, "event_id": other[0]})
                            result["messages"].append(describe(other[1]))
                return result
            target_id = reply
            continue
        sent = await one(
            conn,
            "SELECT j.event_id,r.snapshot FROM outbox o JOIN jobs j ON j.id=o.job_id "
            "JOIN dialogue_runs r ON r.job_id=j.id JOIN recent_messages recent ON "
            "recent.bot_id=o.bot_id AND recent.chat_id=o.chat_id "
            "AND recent.message_id=o.message_id "
            "WHERE o.bot_id=? AND o.chat_id=? AND o.message_id=? AND o.access_version=? "
            "AND o.state='sent' AND r.mode='live' AND r.state='completed' "
            "AND recent.role='assistant' AND recent.received_at>=?",
            (bot, chat, target_id, grant, time.time() - 90 * 86400),
        )
        if not sent:
            break
        result["via_assistant"] = True
        snapshot = json.loads(sent[1])
        if snapshot.get("invalidated") or not await valid(
            conn, bot, chat, grant, snapshot.get("source_manifest", [])
        ):
            break
        root = await one(
            conn,
            "SELECT payload FROM events WHERE id=? AND bot_id=? AND chat_id=?",
            (sent[0], bot, chat),
        )
        if not root:
            break
        update = json.loads(root[0])
        original = update.get("edited_message") or update.get("message") or {}
        current = await accepted(conn, bot, chat, grant, original.get("message_id", 0))
        if not current or current[0] != sent[0]:
            break
        target_id = original["message_id"]
    result["messages"] = []
    result["unavailable"] = True
    return result


async def purge(conn: aiosqlite.Connection, where: str, args: tuple[object, ...]) -> None:
    """Erase source-dependent answers and replays, including already sent context."""
    ids = "SELECT id FROM dialogue_runs WHERE " + where
    jobs = "SELECT job_id FROM dialogue_runs WHERE " + where
    await conn.execute("DELETE FROM behavior_decisions WHERE run_id IN (" + ids + ")", args)
    await conn.execute(
        "UPDATE conversation_moods SET tone='neutral',intensity=0,"
        "version=version+1,epoch=epoch+1,source_run_id=NULL WHERE source_run_id IN (" + ids + ")",
        args,
    )
    await conn.execute(
        "DELETE FROM recent_messages WHERE role='assistant' AND EXISTS (SELECT 1 FROM outbox o "
        "WHERE o.bot_id=recent_messages.bot_id AND o.chat_id=recent_messages.chat_id "
        "AND o.message_id=recent_messages.message_id AND o.job_id IN (" + jobs + "))",
        args,
    )
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE run_id IN (" + ids + ")", args
    )
    await conn.execute(
        "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','running') "
        "AND id IN (" + jobs + ")",
        args,
    )
    await conn.execute(
        'UPDATE outbox SET payload=\'{"text":""}\',state=CASE WHEN '
        "state='pending' THEN 'cancelled' ELSE state END WHERE job_id IN (" + jobs + ")",
        args,
    )
    await conn.execute(
        "UPDATE dialogue_runs SET snapshot='{\"invalidated\":true}',response=NULL,"
        "state='cancelled',error_code='source_changed' WHERE " + where,
        args,
    )
