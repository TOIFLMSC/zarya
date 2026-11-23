"""Copy a retained reply verbatim through the durable Telegram outbox."""

import json
import re
from typing import Any

import aiosqlite

from zarya import timings
from zarya.dialogue_logic import explicit_reply, topic_id
from zarya.research_logic import urls
from zarya.response_links import contains_link
from zarya.source_material import accepted
from zarya.telegram_store import stamp

REQUEST = re.compile(
    r"(?:пожалуйста[ ,]+)?(?:процитируй(?:те)?\s+"
    r"(?:(?:это|исходное|выбранное)\s+)?сообщение(?:\s+дословно)?|"
    r"дословно\s+процитируй(?:те)?\s+(?:это\s+)?сообщение|"
    r"повтори(?:те)?\s+(?:это\s+)?сообщение\s+дословно)"
    r"(?:\s+(?P<links>со ссылк(?:ой|ами)))?[.!?]*",
    re.IGNORECASE,
)


def match(message: dict[str, Any], name: str, bot: str, username: str) -> re.Match[str] | None:
    entities = message.get("entities") or []
    reply = explicit_reply(message)
    if (
        message.get("forward_origin")
        or message.get("forward_date")
        or not reply.get("message_id")
        or (reply.get("from") or {}).get("is_bot")
        or any(
            entity.get("type") in {"blockquote", "expandable_blockquote", "pre", "code"}
            for entity in entities
        )
    ):
        return None
    text = message.get("text") or ""
    leading = next(
        (
            e
            for e in entities
            if e.get("offset") == 0 and e.get("type") in {"mention", "text_mention"}
        ),
        None,
    )
    if leading:
        encoded = text.encode("utf-16-le")
        end = 2 * leading["length"]
        label = encoded[:end].decode("utf-16-le", "replace")
        if (
            leading["type"] == "mention"
            and username
            and label.casefold() == "@" + username.casefold()
        ) or (leading["type"] == "text_mention" and str(leading.get("user", {}).get("id")) == bot):
            text = encoded[end:].decode("utf-16-le", "replace").lstrip(" \t,!:")
    text = re.sub(
        rf"^{re.escape(name)}(?:\s*[,!:]\s*|\s+)", "", text.strip(), count=1, flags=re.IGNORECASE
    )
    return REQUEST.fullmatch(text)


async def enqueue(
    conn: aiosqlite.Connection,
    *,
    bot: str,
    payload: dict[str, Any],
    grant: int,
    event: int,
    job: int,
    message: dict[str, Any],
    trigger: str,
    settings_version: int,
    measured: dict[str, float],
    include_links: bool,
) -> dict[str, Any]:
    chat, current = payload["chat_id"], payload["message_id"]
    thread = topic_id(message)
    target = explicit_reply(message)["message_id"]
    manifest = [{"message_id": current, "event_id": event}]
    response = "Я не вижу сохранённого текста этого сообщения для дословной цитаты."
    state = "service"
    links: list[dict[str, Any]] = []
    original = await accepted(conn, bot, chat, grant, target)
    if original:
        revision, body = original
        manifest.append({"message_id": target, "event_id": revision})
        text = body.get("text") or body.get("caption") or ""
        if text.strip():
            if len(text.encode("utf-16-le")) > 8192:
                response = "Цитата слишком длинная для одного сообщения Telegram."
            elif not include_links and (urls(body) or contains_link(text)):
                response = (
                    "В сообщении есть ссылка. Для дословной цитаты "
                    "попроси процитировать его со ссылкой."
                )
            else:
                response, state = text, "completed"
                entities = body.get("entities" if body.get("text") else "caption_entities") or []
                links = [e for e in entities if e.get("type") == "text_link"]
    snapshot = {
        "verbatim_quote": True,
        "incoming": {
            "message_id": current,
            "sender_id": payload["sender_id"],
            "text": message["text"],
            "thread_id": thread,
        },
        "context": [],
        "settings_version": settings_version,
        "source_manifest": manifest,
        "source_access_version": grant,
        "retrieval_chat_id": chat,
    }
    now = stamp()
    cursor = await conn.execute(
        "INSERT INTO dialogue_runs(job_id,bot_id,chat_id,thread_id,sender_id,message_id,"
        "trigger,state,snapshot,response,created_at,timings_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job,
            bot,
            chat,
            thread,
            payload["sender_id"],
            current,
            trigger,
            state,
            json.dumps(snapshot, ensure_ascii=False),
            response,
            now,
            timings.encode(measured),
        ),
    )
    await conn.execute(
        "INSERT INTO outbox(job_id,bot_id,scope,chat_id,state,payload,created_at,access_version) "
        "VALUES (?,?,?,?,'pending',?,?,?)",
        (
            job,
            bot,
            payload["scope"],
            chat,
            json.dumps(
                {
                    "text": response,
                    "thread_id": thread,
                    "reply_id": current,
                    **({"entities": links} if links else {}),
                },
                ensure_ascii=False,
            ),
            now,
            grant,
        ),
    )
    await conn.execute("UPDATE jobs SET state='done',attempts=attempts+1 WHERE id=?", (job,))
    return {"skip": True, "run_id": cursor.lastrowid}
