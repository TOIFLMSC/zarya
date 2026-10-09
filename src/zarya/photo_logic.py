"""Transactional photo admission. Album tails never cause a second automatic reply."""

import json
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from zarya.models import Settings
from zarya.source_material import visual_sizes

PHOTO_REASONING = "high"
PROCESSING_VERSION = "vision-v3-auto-high-jpeg2048-alpha-white"

# Outer query aliases: jobs j, events e. Used by both dialogue and typing admission.
PHOTO_WAIT = (
    "EXISTS (SELECT 1 FROM photo_batches p WHERE p.bot_id=e.bot_id "
    "AND p.chat_id=e.chat_id AND p.access_version=j.access_version "
    "AND p.state IN ('queued','downloading','analyzing') AND (p.leader_job_id=j.id "
    "OR p.album_key IN (SELECT b.album_key FROM photo_batches b JOIN photo_items i "
    "ON i.batch_id=b.id JOIN photo_messages current ON current.bot_id=i.bot_id "
    "AND current.chat_id=i.chat_id AND current.message_id=i.message_id "
    "AND current.event_id=i.event_id AND current.access_version=b.access_version "
    "WHERE b.bot_id=e.bot_id AND b.chat_id=e.chat_id AND b.access_version=j.access_version "
    "AND (i.message_id=COALESCE(json_extract(e.payload,'$.message.reply_to_message.message_id'),"
    "json_extract(e.payload,'$.edited_message.reply_to_message.message_id')) "
    "OR i.message_id IN (SELECT value FROM json_each(j.payload,'$.source_message_ids')))))) "
)


async def admit(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    scope: str,
    version: int,
    event: int,
    message: dict[str, Any],
    settings: Settings,
    edited: bool,
    *,
    addressed_source: bool = False,
) -> tuple[int | None, int | None, bool]:
    """Return batch, existing album job, and whether a new answer job may be made."""
    if message.get("sticker") and not addressed_source and not edited:
        return None, None, True
    if (
        settings.media_enabled
        and not (message.get("photo") or message.get("sticker"))
        and not edited
    ):
        return None, None, True
    async with conn.execute(
        "SELECT DISTINCT b.id,b.leader_job_id FROM photo_batches b JOIN photo_items i "
        "ON i.batch_id=b.id WHERE i.bot_id=? AND i.chat_id=? AND i.message_id=?",
        (bot, chat, message["message_id"]),
    ) as cursor:
        old = list(await cursor.fetchall())
    async with conn.execute(
        "SELECT album_key FROM photo_messages WHERE bot_id=? AND chat_id=? AND message_id=?",
        (bot, chat, message["message_id"]),
    ) as cursor:
        revision = await cursor.fetchone()
    if visual_sizes(message) or old or revision:
        await conn.execute(
            "INSERT INTO photo_messages(bot_id,chat_id,message_id,event_id,"
            "access_version,album_key) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(bot_id,chat_id,message_id) DO UPDATE SET "
            "event_id=excluded.event_id,access_version=excluded.access_version,"
            "album_key=COALESCE(excluded.album_key,photo_messages.album_key)",
            (
                bot,
                chat,
                message["message_id"],
                event,
                version,
                "album:" + str(message["media_group_id"])
                if message.get("media_group_id")
                else None,
            ),
        )
    if edited:
        for batch, job in old:
            await conn.execute(
                "UPDATE photo_batches SET state='cancelled',result=NULL,error_code='edited' "
                "WHERE id=?",
                (batch,),
            )
            if job:
                await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
                await conn.execute(
                    "UPDATE outbox SET state='cancelled' WHERE job_id=? AND state='pending'",
                    (job,),
                )
            # A separate reply to this photo can already be generating or queued for delivery.
            await conn.execute(
                "UPDATE jobs SET state='cancelled' WHERE id IN (SELECT r.job_id "
                "FROM dialogue_runs r,json_each(r.snapshot,'$.photo_refs') ref "
                "WHERE json_extract(ref.value,'$.batch_id')=?) AND state IN ('running','pending')",
                (batch,),
            )
            await conn.execute(
                "UPDATE outbox SET state='cancelled',error_code='photo_edited' "
                "WHERE state='pending' AND job_id IN (SELECT r.job_id "
                "FROM dialogue_runs r,json_each(r.snapshot,'$.photo_refs') ref "
                "WHERE json_extract(ref.value,'$.batch_id')=?)",
                (batch,),
            )
    sizes = visual_sizes(message)
    if message.get("sticker") and not addressed_source:
        sizes = []
    if not sizes or not settings.photo_enabled or not settings.dialogue_enabled:
        return None, None, True
    key = (
        "album:" + str(message["media_group_id"])
        if message.get("media_group_id")
        else (f"photo:{message['message_id']}:{event}")
    )
    unchanged: list[Any] = []
    if edited and (message.get("media_group_id") or (revision and revision[0])):
        if message.get("media_group_id"):
            key = "album:" + str(message["media_group_id"])
        else:
            assert revision is not None
            key = revision[0]
        async with conn.execute(
            "SELECT p.message_id,p.event_id,e.payload FROM photo_messages p "
            "JOIN events e ON e.id=p.event_id WHERE p.bot_id=? AND p.chat_id=? "
            "AND p.access_version=? AND p.album_key=? AND p.message_id!=? "
            "ORDER BY p.message_id LIMIT 9",
            (bot, chat, version, key, message["message_id"]),
        ) as c:
            candidates = await c.fetchall()
        for other_message, other_event, other_document in candidates:
            other_update = json.loads(other_document)
            current_message = (
                other_update.get("message") or other_update.get("edited_message") or {}
            )
            current_sizes = visual_sizes(current_message)
            if current_sizes:
                current_photo = max(
                    current_sizes, key=lambda item: item.get("width", 0) * item.get("height", 0)
                )
                unchanged.append(
                    (
                        other_message,
                        other_event,
                        current_photo["file_id"],
                        current_photo.get("file_unique_id", current_photo["file_id"]),
                        current_message.get("caption", "")[:3000],
                    )
                )
    async with conn.execute(
        "SELECT id,generation,state,leader_job_id,collect_deadline FROM photo_batches "
        "WHERE bot_id=? AND chat_id=? AND access_version=? AND album_key=? "
        "ORDER BY generation DESC LIMIT 1",
        (bot, chat, version, key),
    ) as cursor:
        previous = await cursor.fetchone()
    reuse = previous and previous[2] == "queued" and not edited
    existing_job = previous[3] if previous else None
    if reuse:
        assert previous is not None
        batch = previous[0]
        async with conn.execute("SELECT COUNT(*) FROM photo_items WHERE batch_id=?", (batch,)) as c:
            count_row = await c.fetchone()
            assert count_row is not None
            count = count_row[0]
        if count >= 10:
            reuse = False
    if not reuse:
        async with conn.execute(
            "SELECT COUNT(*) FROM photo_batches WHERE bot_id=? "
            "AND state IN ('queued','downloading','analyzing')",
            (bot,),
        ) as c:
            pending_row = await c.fetchone()
            assert pending_row is not None
            overflow = pending_row[0] >= 100
        cursor = await conn.execute(
            "INSERT INTO photo_batches(bot_id,chat_id,scope,access_version,album_key,generation,"
            "state,model,processing_version,created_at,collect_until,collect_deadline,"
            "leader_job_id,leader_message_id,error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot,
                chat,
                scope,
                version,
                key,
                previous[1] + 1 if previous else 1,
                "error" if overflow else "queued",
                settings.photo_model,
                PROCESSING_VERSION,
                datetime.now(UTC).isoformat(),
                time.time() + (1 if message.get("media_group_id") else 0),
                time.time() + 4,
                existing_job,
                message["message_id"],
                "queue_limit" if overflow else None,
            ),
        )
        batch = cursor.lastrowid
    else:
        await conn.execute(
            "UPDATE photo_batches SET collect_until=MIN(?,collect_deadline) WHERE id=?",
            (time.time() + 1, batch),
        )
    photo = max(sizes, key=lambda item: item.get("width", 0) * item.get("height", 0))
    for other_message, other_event, other_file, other_unique, other_caption in unchanged:
        await conn.execute(
            "INSERT INTO photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,"
            "file_unique_id,caption) VALUES (?,?,?,?,?,?,?,?)",
            (batch, bot, chat, other_message, other_event, other_file, other_unique, other_caption),
        )
    await conn.execute(
        "INSERT INTO photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,"
        "file_unique_id,caption) VALUES (?,?,?,?,?,?,?,?)",
        (
            batch,
            bot,
            chat,
            message["message_id"],
            event,
            photo["file_id"],
            photo.get("file_unique_id", photo["file_id"]),
            message.get("caption", "")[:3000],
        ),
    )
    # Only the collecting first generation can promote/replace its pending question.
    return batch, existing_job if reuse else None, not previous and not edited


async def link_job(
    conn: aiosqlite.Connection, batch: int, job: int, event: int, payload: dict[str, Any]
) -> None:
    await conn.execute("UPDATE photo_batches SET leader_job_id=? WHERE id=?", (job, batch))
    payload["photo_batch_id"] = batch
    await conn.execute(
        "UPDATE jobs SET event_id=?,payload=? WHERE id=? AND state='pending'",
        (event, json.dumps(payload), job),
    )
