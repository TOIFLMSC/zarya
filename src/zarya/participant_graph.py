"""Read-only personal-summary evidence shared by graph and managed-note lifecycle."""

import hashlib
import json
from typing import Any

import aiosqlite

from zarya.dialogue_logic import topic_id
from zarya.participant_context import human
from zarya.participant_summaries import valid_sources
from zarya.source_material import describe, one

KINDS = {"participant_summary", "participant_source"}
ATTACHMENTS = {
    "photo": "фото",
    "voice": "голосовое",
    "video": "видео",
    "video_note": "видеокружок",
    "animation": "анимация",
    "sticker": "стикер",
    "audio": "аудио",
    "document": "документ",
    "contact": "контакт",
    "location": "местоположение",
    "venue": "место",
    "poll": "опрос",
    "dice": "игровой эмодзи",
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def projection(
    c: aiosqlite.Connection, bot: str, chat: str, grant: int, bid: int
) -> dict[str, Any] | None:
    async with c.execute(
        "SELECT * FROM participant_summary_blocks WHERE id=? AND bot_id=? AND chat_id=? "
        "AND access_version=? AND state='completed' AND result IS NOT NULL AND result!=''",
        (bid, bot, chat, grant),
    ) as cursor:
        value = await cursor.fetchone()
        if value is None:
            return None
        block = dict(zip((col[0] for col in cursor.description or []), value, strict=True))
    try:
        block["manifest"] = json.loads(block["manifest"])
        block["media_refs"] = json.loads(block["media_refs"])
        if not await valid_sources(c, block):
            return None
        refs = {(r["message_id"], r["event_id"]) for r in block["manifest"]}
        async with c.execute(
            "SELECT message_id,event_id FROM participant_summary_sources WHERE block_id=?",
            (bid,),
        ) as cursor:
            if {tuple(r) for r in await cursor.fetchall()} != refs:
                return None
        sources = []
        for ref in block["manifest"]:
            raw = await one(c, "SELECT payload FROM events WHERE id=?", (ref["event_id"],))
            update = json.loads(raw[0])
            message = update.get("edited_message") or update.get("message") or {}
            if (
                not human(message)
                or message.get("message_id") != ref["message_id"]
                or str(message["from"]["id"]) != block["sender_id"]
                or str(message["chat"]["id"]) != chat
            ):
                return None
            detail = describe(message)
            kinds = [k for k in ATTACHMENTS if message.get(k)]
            text = detail["text"]
            if detail["forwarded"]:
                text = "Пересланное сообщение; собеседник является отправителем.\n\n" + text
            if kinds:
                text += "\n\nВложения: " + ", ".join(ATTACHMENTS[k] for k in kinds)
                text += ". Файлы и их расшифровки в эту заметку не экспортируются."
            sources.append(
                dict(
                    event_id=ref["event_id"],
                    message_id=ref["message_id"],
                    text=text,
                    sender_id=block["sender_id"],
                    thread_id=topic_id(message),
                    received_at=ref["received_at"],
                    forwarded=detail["forwarded"],
                    attachment_kinds=kinds,
                    urls=detail["urls"],
                    text_hash=digest(text),
                )
            )
        block["sources"] = sources
        block["result_hash"] = digest(block["result"])
        return block
    except (ValueError, TypeError, KeyError, AttributeError):
        # One malformed/stale block cannot authorize exporting its text or witnesses.
        return None


async def current(
    c: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    kind: str,
    key: int,
    version: int,
    expected_hash: str | None = None,
) -> bool:
    """Independent of bounded graph selection; omission never means deletion."""
    if kind == "participant_summary":
        block = await projection(c, bot, chat, grant, key)
        return bool(
            block
            and version == key
            and (expected_hash is None or block["result_hash"] == expected_hash)
        )
    if kind != "participant_source" or key != version:
        return False
    async with c.execute(
        "SELECT b.id FROM participant_summary_blocks b JOIN participant_summary_sources s "
        "ON s.block_id=b.id WHERE s.event_id=? AND b.bot_id=? AND b.chat_id=? "
        "AND b.access_version=? AND b.state='completed' AND b.result IS NOT NULL",
        (key, bot, chat, grant),
    ) as cursor:
        candidates = await cursor.fetchall()
    for (bid,) in candidates:
        block = await projection(c, bot, chat, grant, bid)
        if block:
            for source in block["sources"]:
                if source["event_id"] == key and (
                    expected_hash is None or source["text_hash"] == expected_hash
                ):
                    return True
    return False
