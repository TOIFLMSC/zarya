"""Untrusted research inputs, revision fences and evidence references."""

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiosqlite

RESEARCH_WAIT = (
    "EXISTS (SELECT 1 FROM research_runs rr WHERE rr.job_id=j.id "
    "AND rr.state IN ('queued','fetching','search_started')) "
)

API_FEEDS = frozenset({"oai-finance", "oai-weather", "oai-sports"})


def public_url(value: str) -> str:
    if len(value) > 2000 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise ValueError("unsafe_url")
    parsed = urlsplit(value)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("unsafe_url")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("unsafe_port")
    host = host.encode("idna").decode().lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("private_host") from None
    else:
        if not public_address(str(address)):
            raise ValueError("private_address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_global and not address.is_multicast)


def urls(message: dict[str, Any]) -> list[str]:
    text = str(message.get("text") or message.get("caption") or "")
    values = [trim_url(v) for v in re.findall(r"https?://[^\s<>]+", text)]
    encoded = text.encode("utf-16-le")
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if entity.get("type") == "text_link":
            values.append(str(entity.get("url", "")))
        elif entity.get("type") == "url":
            offset, size = int(entity.get("offset", 0)), int(entity.get("length", 0))
            values.append(encoded[offset * 2 : (offset + size) * 2].decode("utf-16-le", "replace"))
    return list(dict.fromkeys(values))[:3]


def trim_url(value: str) -> str:
    value = value.rstrip(".,!?;:'\"»")
    for closing, opening in ((")", "("), ("]", "[")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def wants_search(question: str) -> bool:
    return bool(
        re.search(
            r"факт.?чек|проверь\w*|провер[иь]\w*|правда\s+ли|так\s+ли|найди\w*|"
            r"поищи\w*|загугл\w*|подтвержд\w*|опроверг\w*|источник\w*|"
            r"свеж\w*|последни\w*\s+новост\w*|что\s+(?:сейчас|сегодня)|"
            r"(?:узнай|посмотри|глянь|гляни).{0,100}\b(?:инет\w*|интернет\w*|сети)\b|"
            r"(?=.*\b(?:btc|биткоин\w*|биток|битка|эфир\w*|eth|доллар\w*|евро)\b)"
            r"(?=.*\b(?:цен\w*|курс\w*|котиров\w*|скок\w*|сколько|поч[её]м)\b)",
            question.casefold(),
        )
    )


async def purge(conn: aiosqlite.Connection, where: str, args: tuple[object, ...]) -> None:
    """Erase artifacts and their dependent answers, preserving accounting only."""
    ids = "SELECT id FROM research_runs WHERE " + where
    jobs = "SELECT job_id FROM research_runs WHERE " + where
    runs = (
        "SELECT id FROM dialogue_runs WHERE json_extract(snapshot,'$.research_id') IN (" + ids + ")"
    )
    await conn.execute("DELETE FROM behavior_decisions WHERE run_id IN (" + runs + ")", args)
    await conn.execute(
        "UPDATE conversation_moods SET tone='neutral',intensity=0,"
        "version=version+1,epoch=epoch+1,source_run_id=NULL WHERE source_run_id IN (" + runs + ")",
        args,
    )
    await conn.execute(
        "DELETE FROM recent_messages WHERE role='assistant' AND EXISTS "
        "(SELECT 1 FROM outbox o WHERE o.bot_id=recent_messages.bot_id "
        "AND o.chat_id=recent_messages.chat_id AND o.message_id=recent_messages.message_id "
        "AND o.job_id IN (" + jobs + "))",
        args,
    )
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE research_run_id IN (" + ids + ")", args
    )
    await conn.execute(
        "UPDATE jobs SET state='cancelled' WHERE id IN ("
        + jobs
        + ") AND state IN ('pending','running')",
        args,
    )
    await conn.execute(
        'UPDATE outbox SET payload=\'{"text":""}\',state=CASE WHEN '
        "state='pending' THEN 'cancelled' ELSE state END WHERE job_id IN (" + jobs + ")",
        args,
    )
    # Paid/recorded replays inherit this reference and cannot extend retention.
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE run_id IN (SELECT id "
        "FROM dialogue_runs WHERE json_extract(snapshot,'$.research_id') IN (" + ids + "))",
        args,
    )
    await conn.execute(
        "UPDATE dialogue_runs SET "
        "snapshot='{\"invalidated\":true}',response=NULL,state='cancelled',error_"
        "code='research_changed' WHERE json_extract(snapshot,'$.research_id') "
        "IN (" + ids + ")",
        args,
    )
    await conn.execute(
        "UPDATE research_runs SET "
        "question='',material='',manifest='[]',sources='[]',result=NULL,actions"
        "='[]',state='cancelled',error_code='source_changed' WHERE " + where,
        args,
    )


async def revision(conn: aiosqlite.Connection, bot: str, chat: str, message: int) -> None:
    from zarya.media_logic import purge as purge_media
    from zarya.media_logic import purge_analysis
    from zarya.source_material import purge as purge_material

    await purge_media(
        conn,
        "bot_id=? AND chat_id=? AND message_id=?",
        (bot, chat, message),
    )
    await purge_analysis(
        conn,
        "bot_id=? AND chat_id=? AND EXISTS "
        "(SELECT 1 FROM json_each(analysis_context,'$.manifest') m "
        "WHERE json_extract(m.value,'$.message_id')=?)",
        (bot, chat, message),
    )

    await purge_material(
        conn,
        "bot_id=? AND chat_id=? AND EXISTS (SELECT 1 FROM "
        "json_each(snapshot,'$.source_manifest') m WHERE json_extract(m.value,'$.message_id')=?)",
        (bot, chat, message),
    )
    await purge(
        conn,
        "bot_id=? AND chat_id=? AND EXISTS (SELECT 1 FROM json_each(manifest) "
        "m WHERE json_extract(m.value,'$.message_id')=?)",
        (bot, chat, message),
    )


def evidence_text(text: str, sources: list[dict[str, Any]], show_links: bool = True) -> str:
    "Replace explicit source references; unknown refs fail instead of"
    "inventing citations."
    mapping = {
        str(s["id"]): s
        for s in sources
        if s.get("url") or (s.get("coverage") == "realtime_feed" and s.get("name") in API_FEEDS)
    }

    def replace(match: re.Match[str]) -> str:
        source = mapping.get(match[1])
        if not source:
            raise ValueError("unknown_source")
        if not show_links:
            return ""
        return " (" + str(source.get("url") or source["name"]) + ") "

    text = re.sub(r"\[S(\d+)\]", replace, text)
    if not show_links:
        from zarya.response_links import without_links

        return without_links(text)
    # Any URL supplied by the synthesis must already belong to the evidence manifest.
    permitted = {str(s.get("url")) for s in sources}
    for value in re.findall(r"https?://[^\s<>]+", text):
        if value not in permitted and trim_url(value) not in permitted:
            raise ValueError("unknown_source")
    return text.strip()
