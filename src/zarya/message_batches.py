"""Durable collection and addressed output for plain Telegram text bursts."""

import json
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from zarya.avatar_logic import AVATAR_WORD, FOLLOWUP
from zarya.dialogue_logic import explicit_reply, topic_id
from zarya.models import Settings
from zarya.research_intent import speaker_text
from zarya.research_logic import urls, wants_search
from zarya.response_links import message_requests_sources
from zarya.source_material import accepted, one

QUIET = 2.0
MAX_WAIT = 3.0
MAX_MEMBERS = 5
DIRECT = {"name", "mention", "reply", "continuation"}
POLICY = (
    "\nВ этом запросе current_messages заменяет current_message. "
    "Ответь на current_messages в хронологическом порядке. Это все текущие обращения "
    "одного человека. Ответь на каждый самостоятельный вопрос отдельно, а уточнение "
    "объедини с вопросом, к которому оно относится. Размести ID всех обработанных "
    "сообщений ровно по одному разу в covers. У каждого ответа reply_to выбирается "
    "из его covers и задаёт точную реплику для отправки. "
    "Не используй ID из истории и не пропускай вопросы."
)
INVALID_RESPONSE = (
    "Не удалось привязать ответы к сообщениям серии. Повтори нужные вопросы отдельно."
)


def eligible(message: dict[str, Any], settings: Settings) -> bool:
    text = message.get("text") or ""
    return bool(
        text.strip()
        and not text.startswith("/")
        and not (message.get("from") or {}).get("is_bot")
        and not message.get("sender_chat")
        and not message.get("forward_origin")
        and not message.get("forward_date")
        and not explicit_reply(message)
        and not urls(message)
        and not message_requests_sources(message)
        and not AVATAR_WORD.search(text)
        and not FOLLOWUP.fullmatch(text)
        and type(message.get("date")) is int
        and not (settings.research_enabled and wants_search(speaker_text(message)))
    )


async def collect(
    conn: aiosqlite.Connection,
    bot: str,
    payload: dict[str, Any],
    grant: int,
    event: int,
    message_date: int,
) -> bool:
    """Join the current burst, or open one for a directly addressed message."""
    now = time.time()
    chat = payload["chat_id"]
    key = (bot, chat, payload["thread_id"] or 0, payload["sender_id"], grant)
    current = await one(
        conn,
        "SELECT b.job_id,b.opened_at FROM text_batches b JOIN jobs j ON j.id=b.job_id "
        "WHERE b.bot_id=? AND b.chat_id=? AND b.thread_id=? AND b.sender_id=? "
        "AND b.access_version=? AND b.sealed=0 AND b.ready_at>? "
        "AND b.first_message_at>? AND b.last_message_at BETWEEN ? AND ? "
        "AND j.state='pending' AND j.attempts=0 "
        "AND (SELECT COUNT(*) FROM text_batch_members m WHERE m.job_id=b.job_id)<? "
        "ORDER BY b.job_id DESC LIMIT 1",
        (*key, now, message_date - MAX_WAIT, message_date - QUIET, message_date, MAX_MEMBERS),
    )
    trigger = payload["trigger"]
    if not current:
        if trigger not in DIRECT:
            return False
        job = await conn.execute(
            "INSERT INTO jobs(kind,event_id,state,payload,created_at,access_version) "
            "VALUES ('telegram',?,'pending',?,?,?)",
            (event, json.dumps(payload), datetime.now(UTC).isoformat(), grant),
        )
        leader = job.lastrowid
        await conn.execute(
            "INSERT INTO text_batches(job_id,bot_id,chat_id,thread_id,sender_id,access_version,"
            "opened_at,ready_at,first_message_at,last_message_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (leader, *key, now, now + QUIET, message_date, message_date),
        )
    else:
        leader, opened_at = current
        await conn.execute(
            "UPDATE text_batches SET ready_at=?,last_message_at=? WHERE job_id=?",
            (min(opened_at + MAX_WAIT, now + QUIET), message_date, leader),
        )
    await conn.execute(
        "INSERT INTO text_batch_members VALUES (?,?,?,?)",
        (leader, payload["message_id"], event, trigger),
    )
    return True


async def member(
    conn: aiosqlite.Connection, bot: str, chat: str, grant: int, message: dict[str, Any]
) -> tuple[int, str, bool] | None:
    item = await one(
        conn,
        "SELECT m.job_id,m.trigger,b.sealed=0 AND j.state='pending' AND j.attempts=0 "
        "FROM text_batch_members m JOIN text_batches b ON b.job_id=m.job_id "
        "JOIN jobs j ON j.id=b.job_id WHERE m.message_id=? AND b.bot_id=? "
        "AND b.chat_id=? AND b.access_version=? AND b.sender_id=? AND b.thread_id=? "
        "ORDER BY m.job_id DESC LIMIT 1",
        (
            message["message_id"],
            bot,
            chat,
            grant,
            str(message.get("from", {}).get("id", "")),
            topic_id(message) or 0,
        ),
    )
    return (item[0], item[1], bool(item[2])) if item else None


async def replace(conn: aiosqlite.Connection, job: int, mid: int, event: int) -> None:
    old = await one(
        conn, "SELECT event_id FROM text_batch_members WHERE job_id=? AND message_id=?", (job, mid)
    )
    await conn.execute(
        "UPDATE text_batch_members SET event_id=? WHERE job_id=? AND message_id=?",
        (event, job, mid),
    )
    await conn.execute("UPDATE jobs SET event_id=? WHERE id=? AND event_id=?", (event, job, old[0]))


async def remove(conn: aiosqlite.Connection, job: int, mid: int) -> None:
    await conn.execute("DELETE FROM text_batch_members WHERE job_id=? AND message_id=?", (job, mid))
    replacement = await one(
        conn,
        "SELECT message_id,event_id,trigger FROM text_batch_members "
        "WHERE job_id=? ORDER BY message_id LIMIT 1",
        (job,),
    )
    if replacement:
        await conn.execute(
            "UPDATE jobs SET event_id=?,payload=json_set(payload,'$.message_id',?,'$.trigger',?) "
            "WHERE id=? AND json_extract(payload,'$.message_id')=?",
            (
                replacement[1],
                replacement[0],
                replacement[2] if replacement[2] in DIRECT else "continuation",
                job,
                mid,
            ),
        )
    else:
        await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))


async def forget(conn: aiosqlite.Connection, event: int) -> None:
    item = await one(
        conn,
        "SELECT m.job_id,m.message_id FROM text_batch_members m "
        "JOIN text_batches b ON b.job_id=m.job_id "
        "JOIN jobs j ON j.id=b.job_id WHERE m.event_id=? AND b.sealed=0 AND j.state='pending'",
        (event,),
    )
    if item:
        await remove(conn, item[0], item[1])


async def freeze(
    conn: aiosqlite.Connection, job: int, bot: str, chat: str, grant: int
) -> list[dict[str, Any]] | None:
    if not await one(conn, "SELECT 1 FROM text_batches WHERE job_id=?", (job,)):
        return None
    await conn.execute("UPDATE text_batches SET sealed=1 WHERE job_id=?", (job,))
    return await current_members(conn, job, bot, chat, grant)


async def current_members(
    conn: aiosqlite.Connection, job: int, bot: str, chat: str, grant: int
) -> list[dict[str, Any]]:
    async with conn.execute(
        "SELECT message_id,event_id FROM text_batch_members WHERE job_id=? ORDER BY message_id",
        (job,),
    ) as cursor:
        refs = await cursor.fetchall()
    members = []
    for mid, event in refs:
        original = await accepted(conn, bot, chat, grant, mid)
        if original and original[0] == event:
            members.append({"message_id": mid, "event_id": event, "text": original[1]["text"]})
    return members


def configure(request: dict[str, Any], members: list[dict[str, Any]]) -> None:
    body = json.loads(request["input"])
    body.pop("current_message", None)
    body["current_messages"] = members
    request["input"] = json.dumps(body, ensure_ascii=False)
    allowed = {"type": "integer", "enum": [member["message_id"] for member in members]}
    part = {
        "type": "object",
        "properties": {
            "reply_to": allowed,
            "covers": {"type": "array", "items": allowed},
            "text": {"type": "string"},
        },
        "required": ["reply_to", "covers", "text"],
        "additionalProperties": False,
    }
    request["text"]["format"] = {
        "type": "json_schema",
        "name": "message_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answers": {"type": "array", "items": part}},
            "required": ["answers"],
            "additionalProperties": False,
        },
    }
    request["instructions"] += POLICY


def answers(raw: str, members: list[dict[str, Any]]) -> list[tuple[int, list[int], str]]:
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document) != {"answers"}:
        raise ValueError("invalid batch object")
    remaining = {member["message_id"] for member in members}
    parts = document["answers"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= len(remaining):
        raise ValueError("invalid batch parts")
    result = []
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"reply_to", "covers", "text"}:
            raise ValueError("invalid batch part")
        target, covers, text = part["reply_to"], part["covers"], part["text"]
        if (
            type(target) is not int
            or not isinstance(covers, list)
            or not covers
            or not all(type(mid) is int for mid in covers)
            or len(set(covers)) != len(covers)
            or not remaining.issuperset(covers)
            or target not in covers
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise ValueError("invalid batch targets")
        remaining.difference_update(covers)
        result.append((target, sorted(covers), text))
    if remaining:
        raise ValueError("missing batch answers")
    return sorted(result, key=lambda part: part[1][0])
