"""Per-human windows over accepted current events; no second copy of chat text."""

import json
import time
from typing import Any

import aiosqlite

from zarya.dialogue_logic import topic_id
from zarya.memory_read import MemoryRead
from zarya.models import Settings
from zarya.source_material import accepted, describe, one

RETENTION = 90 * 86400
TOPIC_AGE = 24 * 3600
TOPIC_MESSAGES = 8
TOPIC_CHARS = 2500
UPDATE_MESSAGE = (
    "COALESCE(json_extract(e.payload,'$.edited_message'),json_extract(e.payload,'$.message'))"
)
FORUM_TOPIC = (
    f"json_extract({UPDATE_MESSAGE},'$.chat.is_forum')=1 "
    f"AND COALESCE(json_extract({UPDATE_MESSAGE},'$.message_thread_id'),0)=? "
)
CONTENT = (
    "photo",
    "voice",
    "video",
    "video_note",
    "animation",
    "sticker",
    "audio",
    "document",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
)
CURRENT_ROWS = (
    "FROM participant_messages p JOIN research_messages m ON m.bot_id=p.bot_id "
    "AND m.chat_id=p.chat_id AND m.message_id=p.message_id AND m.event_id=p.event_id "
    "AND m.access_version=p.access_version JOIN events e ON e.id=p.event_id "
    "AND e.bot_id=p.bot_id AND e.chat_id=p.chat_id JOIN telegram_access a ON a.bot_id=p.bot_id "
    "AND a.subject_id=p.chat_id AND a.version=p.access_version AND a.state='approved' "
    "AND a.member_state NOT IN ('left','kicked','migrated') "
)
SCOPE = (
    "WHERE p.bot_id=? AND p.chat_id=? "
    "AND p.access_version=? AND p.received_at>? AND e.disposition='accepted' "
    "AND e.payload!='{}' AND CAST(strftime('%s',e.received_at) AS REAL)>? "
)
ROWS = CURRENT_ROWS + SCOPE


async def valid_participant_sources(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    sources: list[dict[str, Any]],
    cutoff: float,
    *,
    sender: str | None = None,
) -> bool:
    expected = {
        (source["message_id"], source.get("event_id")): source.get("received_at")
        for source in sources
    }
    if len(expected) != len(sources):
        return False
    if not expected:
        return True
    condition = ROWS + (
        "AND (p.message_id,p.event_id) IN ("
        "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)) "
    )
    args: tuple[Any, ...] = (bot, chat, grant, cutoff, cutoff, json.dumps(list(expected)))
    if sender is not None:
        condition += "AND p.sender_id=?"
        args += (sender,)
    async with conn.execute(
        "SELECT p.message_id,p.event_id,p.received_at " + condition, args
    ) as cursor:
        current = {(mid, event): received for mid, event, received in await cursor.fetchall()}
    # Older SQLite JSON decoders can round a fractional timestamp differently
    # from Python's json.loads. Compare the original REAL values exactly here.
    return current == expected


def human(message: dict[str, Any]) -> bool:
    sender = message.get("from") or {}
    return bool(
        sender.get("id")
        and not sender.get("is_bot")
        and not message.get("sender_chat")
        and not str(message.get("text") or "").startswith("/")
        and (message.get("text") or message.get("caption") or any(message.get(k) for k in CONTENT))
    )


async def admit(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    event: int,
    message: dict[str, Any],
    edited: bool,
) -> None:
    if not human(message):
        await conn.execute(
            "DELETE FROM participant_messages WHERE bot_id=? AND chat_id=? AND message_id=?",
            (bot, chat, message["message_id"]),
        )
        return
    previous = await one(
        conn,
        "SELECT received_at FROM participant_messages WHERE bot_id=? AND chat_id=? "
        "AND message_id=?",
        (bot, chat, message["message_id"]),
    )
    original_at = previous[0] if previous else time.time()
    if edited and not previous:
        old = await one(
            conn,
            "SELECT received_at FROM recent_messages WHERE bot_id=? AND chat_id=? "
            "AND message_id=? AND role='user'",
            (bot, chat, message["message_id"]),
        )
        original_at = old[0] if old else message.get("date", 0)
        if not isinstance(original_at, (int, float)) or original_at <= time.time() - RETENTION:
            return
    await conn.execute(
        "INSERT INTO participant_messages VALUES (?,?,?,?,?,?,?) ON "
        "CONFLICT(bot_id,chat_id,message_id) DO UPDATE SET sender_id=excluded.sender_id,"
        "access_version=excluded.access_version,event_id=excluded.event_id",
        (bot, chat, message["message_id"], str(message["from"]["id"]), grant, event, original_at),
    )


async def window(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    sender: str,
    limit: int,
    *,
    before: int | None = None,
    thread: int | None = None,
    exact_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[dict[str, Any]]]:
    cutoff = time.time() - RETENTION
    args: tuple[Any, ...] = (bot, chat, grant, cutoff, cutoff, sender)
    condition = ROWS + "AND p.sender_id=? "
    if before is not None:
        condition += "AND p.message_id<? "
        args += (before,)
    if thread is not None:
        condition += f"AND (({FORUM_TOPIC}) OR p.message_id IN (SELECT value FROM json_each(?))) "
        args += (thread, json.dumps(sorted(exact_ids or ())))
    count = (await one(conn, "SELECT COUNT(*) " + condition, args))[0]
    async with conn.execute(
        "SELECT p.message_id,p.event_id,p.received_at,e.payload "
        + condition
        + "ORDER BY p.message_id DESC LIMIT ?",
        args + (limit,),
    ) as cursor:
        rows = list(await cursor.fetchall())
    result, refs, media = [], [], []
    for mid, event, at, raw in reversed(rows):
        update = json.loads(raw)
        message = update.get("edited_message") or update.get("message") or {}
        if not human(message) or str(message["from"]["id"]) != sender:
            continue
        item = describe(message)
        item.update(
            sender_id=sender,
            name=str(message["from"].get("first_name") or "Участник")[:80],
            role="user",
            thread_id=topic_id(message),
            received_at=at,
            content_kinds=[k for k in CONTENT if message.get(k)],
        )
        if not item["text"]:
            item["text"] = "[Вложение: " + ", ".join(k for k in CONTENT if message.get(k)) + "]"
        if message.get("voice") or message.get("audio"):
            from zarya.media_logic import valid as media_valid

            speech = await one(
                conn,
                "SELECT id,transcript FROM media_runs WHERE bot_id=? AND chat_id=? "
                "AND access_version=? AND event_id=? AND transcript IS NOT NULL "
                "AND state!='cancelled' ORDER BY id DESC LIMIT 1",
                (bot, chat, grant, event),
            )
            if speech and await media_valid(conn, speech[0]):
                item["text"] += "\n[Сохранённая расшифровка] " + speech[1]
                media.append({"id": speech[0]})
                item["media_refs"] = [{"id": speech[0]}]
        result.append(item)
        refs.append({"message_id": mid, "event_id": event, "received_at": at})
    return result, refs, count, media


async def topic_history(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    thread: int,
    before: int,
    excluded: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Use the recent index to constrain the time range, and ROWS to bind access
    # and the current revision. The original timestamp keeps edits in place.
    cutoff = time.time() - TOPIC_AGE
    async with conn.execute(
        "SELECT p.message_id,p.event_id,p.received_at,p.sender_id,e.payload "
        + CURRENT_ROWS
        + "JOIN recent_messages r ON r.bot_id=p.bot_id AND r.chat_id=p.chat_id "
        "AND r.message_id=p.message_id AND r.event_id=p.event_id "
        + SCOPE
        + "AND r.received_at>? AND r.role='user' AND COALESCE(r.thread_id,0)=? AND p.message_id<? "
        + f"AND {FORUM_TOPIC} AND TRIM(json_extract({UPDATE_MESSAGE},'$.text'))!='' "
        + "AND p.message_id NOT IN (SELECT value FROM json_each(?)) "
        + "ORDER BY p.message_id DESC LIMIT ?",
        (
            bot,
            chat,
            grant,
            cutoff,
            cutoff,
            cutoff,
            thread,
            before,
            thread,
            json.dumps(sorted(excluded)),
            TOPIC_MESSAGES,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    result: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    remaining = TOPIC_CHARS
    for mid, event, at, sender, raw in rows:
        update = json.loads(raw)
        message = update.get("edited_message") or update["message"]
        text = message["text"].strip()
        if not text:
            continue
        if len(text) > remaining:
            text = text[: remaining - 1] + "…"
        result.append(
            dict(
                message_id=mid,
                event_id=event,
                received_at=at,
                sender_id=sender,
                thread_id=thread or None,
                name=str(message["from"].get("first_name") or "Участник")[:80],
                forwarded=bool(message.get("forward_origin") or message.get("forward_date")),
                text=text,
            )
        )
        refs.append(dict(message_id=mid, event_id=event, received_at=at))
        remaining -= len(text)
        if remaining == 0:
            break
    return result[::-1], refs[::-1]


async def assistants(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    messages: list[dict[str, Any]],
    *,
    before: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    mids = [m["message_id"] for m in messages]
    if not mids:
        return [], []
    args: tuple[Any, ...] = (bot, chat, grant, json.dumps(mids), time.time() - RETENTION)
    condition = ""
    if before is not None:
        boundary = await one(
            conn,
            "SELECT received_at FROM participant_messages WHERE bot_id=? AND chat_id=? "
            "AND message_id=? AND access_version=?",
            (bot, chat, before, grant),
        )
        if boundary:
            condition = "AND m.received_at<=? "
            args += (boundary[0],)
    async with conn.execute(
        "SELECT r.id,r.snapshot,o.id,o.message_id,o.payload,m.received_at FROM outbox o "
        "JOIN dialogue_runs r ON r.job_id=o.job_id JOIN jobs j ON j.id=r.job_id "
        "JOIN research_messages source ON source.bot_id=r.bot_id AND source.chat_id=r.chat_id "
        "AND source.message_id=r.message_id AND source.event_id=j.event_id "
        "AND source.access_version=o.access_version JOIN recent_messages m "
        "ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id AND m.message_id=o.message_id "
        "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.state='sent' "
        "AND r.mode='live' AND r.state='completed' AND r.message_id IN "
        "(SELECT value FROM json_each(?)) AND m.role='assistant' AND m.received_at>? "
        "AND json_extract(o.payload,'$.text')=m.text " + condition + "ORDER BY m.received_at,o.id",
        args,
    ) as cursor:
        rows = await cursor.fetchall()
    items, refs, seen = [], [], set()
    with MemoryRead(conn) as read:
        from zarya.memory_context import valid_snapshot

        def proof(parts: list[dict[str, int]]) -> dict[str, Any]:
            return {
                "retrieval_chat_id": chat,
                "source_access_version": grant,
                "context_refs": {"humans": [], "assistants": parts, "media": []},
            }

        candidates = [{"run_id": r[0], "outbox_id": r[2]} for r in rows]
        batch_valid = await valid_snapshot(conn, bot, proof(candidates), read=read)
        for rid, raw, oid, mid, document, at in rows:
            if mid in seen:
                continue
            snapshot = json.loads(raw)
            if not batch_valid and not await valid_snapshot(
                conn,
                bot,
                proof([{"run_id": rid, "outbox_id": oid}]),
                read=read,
            ):
                continue
            text = json.loads(document).get("text", "")
            if not text:
                continue
            seen.add(mid)
            refs.append({"run_id": rid, "outbox_id": oid})
            items.append(
                {
                    "message_id": mid,
                    "sender_id": bot,
                    "name": "Заря",
                    "role": "assistant",
                    "text": text,
                    "thread_id": snapshot["incoming"].get("thread_id"),
                    "received_at": at,
                    "in_reply_to": snapshot["incoming"]["message_id"],
                }
            )
    return items, refs


async def select(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    message: dict[str, Any],
    settings: Settings,
    *,
    enqueue_summaries: bool = False,
    thread: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    sender, current = str(message.get("from", {}).get("id", "")), message["message_id"]
    own = await one(
        conn,
        "SELECT event_id,received_at FROM participant_messages WHERE bot_id=? AND chat_id=? "
        "AND access_version=? AND message_id=?",
        (bot, chat, grant, current),
    )
    people = {sender: "speaker"} if sender else {}
    target = (message.get("reply_to_message") or {}).get("message_id")
    visited: set[int] = set()
    for _ in range(6):
        if not target or target in visited:
            break
        visited.add(target)
        original = await accepted(conn, bot, chat, grant, target)
        if original:
            source = original[1]
            if human(source):
                people.setdefault(str(source["from"]["id"]), "reply_chain")
            target = (source.get("reply_to_message") or {}).get("message_id")
        else:
            sent = await one(
                conn,
                "SELECT r.snapshot FROM dialogue_runs r JOIN outbox o ON o.job_id=r.job_id "
                "JOIN jobs j ON j.id=r.job_id JOIN research_messages source "
                "ON source.bot_id=r.bot_id "
                "AND source.chat_id=r.chat_id AND source.message_id=r.message_id "
                "AND source.event_id=j.event_id AND source.access_version=o.access_version "
                "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.message_id=? "
                "AND o.state='sent' AND r.mode='live' AND r.state='completed'",
                (bot, chat, grant, target),
            )
            if not sent:
                break
            snapshot = json.loads(sent[0])
            from zarya.memory_context import valid_snapshot

            if not await valid_snapshot(conn, bot, snapshot):
                break
            target = snapshot.get("incoming", {}).get("message_id")
    limit = settings.participant_context_messages
    included = bool(own)
    context: list[dict[str, Any]] = []
    refs: dict[str, Any] = {"humans": [], "assistants": [], "media": [], "summaries": []}
    coverage = []
    for person, reason in people.items():
        history, sources, available, media = await window(
            conn,
            bot,
            chat,
            grant,
            person,
            limit - int(person == sender and included),
            before=current,
            thread=thread,
            exact_ids=visited,
        )
        bot_parts, bot_refs = await assistants(conn, bot, chat, grant, history, before=current)
        from zarya.participant_summaries import admit as admit_summaries
        from zarya.participant_summaries import compact, enabled

        if enqueue_summaries and enabled(settings):
            await admit_summaries(conn, bot, chat, grant, person, history, sources, media)
        packed, summaries = await compact(
            conn, bot, chat, grant, person, history, sources, settings, exact_ids=visited
        )
        context.extend(packed + bot_parts)
        import hashlib

        refs["summaries"].extend(
            {"id": b["id"], "hash": hashlib.sha256(b["summary"].encode()).hexdigest()}
            for b in summaries["blocks"]
            if b["used_ids"]
        )
        refs["humans"].extend(sources)
        refs["assistants"].extend(bot_refs)
        refs["media"].extend(media)
        coverage.append(
            {
                "sender_id": person,
                "name": history[-1]["name"]
                if history
                else str(message.get("from", {}).get("first_name") or person),
                "reason": reason,
                "available": available + int(person == sender and included),
                "human_messages": len(history) + int(person == sender and included),
                "assistant_messages": len(bot_parts),
                "summaries": summaries,
                "summarized_messages": len(summaries["covered_ids"]),
                "original_messages": len(summaries["original_ids"])
                + int(person == sender and included),
            }
        )
    if included and own:
        refs["humans"].append({"message_id": current, "event_id": own[0], "received_at": own[1]})
    unique = {(m["role"], m["message_id"]): m for m in context}
    context = sorted(unique.values(), key=lambda m: (m["received_at"], m["message_id"]))
    return (
        context,
        {
            "algorithm": "participant-v2",
            "limit": limit,
            "current_included": included,
            "participants": coverage,
            "reply_chain_truncated": bool(target and len(visited) == 6),
            "characters": sum(len(m["text"]) for m in context),
        },
        refs,
    )


async def register(conn: aiosqlite.Connection, run: int, snapshot: dict[str, Any]) -> None:
    snapshot["context_run_id"] = run
    for parent in {r["run_id"] for r in snapshot.get("context_refs", {}).get("assistants", [])}:
        await conn.execute("INSERT INTO context_run_edges VALUES (?,?)", (run, parent))
    await conn.execute(
        "UPDATE dialogue_runs SET snapshot=? WHERE id=?",
        (json.dumps(snapshot, ensure_ascii=False), run),
    )
