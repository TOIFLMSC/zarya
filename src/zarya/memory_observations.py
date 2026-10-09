"""Evidence strength is repetition, never a probability of truth or a sharing permission."""

import re
import time
from typing import Any

import aiosqlite

from zarya.memory_context import one, valid_dependencies

KINDS = {"interest": "interest", "nickname": "name", "habit": "observation"}
LABELS = {
    "single": "Единичное наблюдение",
    "some": "Есть повтор",
    "repeated": "Повторяется",
    "none": "Пока нет подходящих подтверждений",
}


async def prune_evidence(conn: aiosqlite.Connection, bot: str) -> bool:
    """Retire withdrawn witnesses, not an entire observation with other sufficient evidence."""
    async with conn.execute(
        "SELECT e.fact_id,e.source_id,e.event_id,e.context_dependencies,s.event_id,s.valid,"
        "s.received_at,f.chat_id,f.access_version FROM memory_evidence e "
        "JOIN memory_facts f ON f.id=e.fact_id JOIN memory_sources s ON s.id=e.source_id "
        "WHERE f.bot_id=? AND f.observation_kind!='none' AND e.supporting=1 "
        "AND f.state!='deleted'",
        (bot,),
    ) as cursor:
        rows = list(await cursor.fetchall())
    changed = set()
    for fid, sid, event, deps, current, valid, received, chat, grant in rows:
        if (
            event != current
            or not valid
            or received <= time.time() - 90 * 86400
            or not await valid_dependencies(conn, bot, chat, grant, deps)
        ):
            await conn.execute(
                "UPDATE memory_evidence SET supporting=0 WHERE fact_id=? AND source_id=?",
                (fid, sid),
            )
            changed.add(fid)
    shared = False
    for fid in changed:
        shared = shared or bool(
            await one(
                conn, "SELECT 1 FROM memory_shares WHERE fact_id=? AND state='active'", (fid,)
            )
        )
        await conn.execute("UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fid,))
        await conn.execute("UPDATE memory_facts SET version=version+1 WHERE id=?", (fid,))
    return shared


async def strength(
    conn: aiosqlite.Connection, bot: str, fact_id: int, *, depth: int = 0
) -> dict[str, Any]:
    fact = await one(
        conn,
        "SELECT chat_id,access_version,sender_id,observation_kind,state,provenance,"
        "curated,category FROM memory_facts WHERE bot_id=? AND id=?",
        (bot, fact_id),
    )
    result: dict[str, Any] = {
        "level": "none",
        "label": LABELS["none"],
        "messages": 0,
        "authors": 0,
        "episodes": 0,
        "first_seen": None,
        "last_seen": None,
        "eligible": False,
        "source_ids": [],
        "kind": "none",
        "reason": "Нет наблюдения",
    }
    if not fact or depth > 8:
        return result
    chat, grant, subject, kind, state, provenance, curated, category = fact
    result["kind"] = kind
    if kind not in KINDS or KINDS[kind] != category:
        result["label"] = (
            "Подтверждено вручную"
            if curated
            else "Со слов собеседника"
            if provenance == "self"
            else "Нужна проверка"
        )
        result["reason"] = "Сила повторения применяется к наблюдениям"
        return result
    async with conn.execute(
        "SELECT s.id,s.message_id,s.sender_id,s.text,s.received_at,e.context_dependencies "
        "FROM memory_evidence e JOIN memory_sources s ON s.id=e.source_id "
        "WHERE e.fact_id=? AND s.bot_id=? AND s.chat_id=? AND s.access_version=? "
        "AND s.valid=1 AND s.event_id=e.event_id AND s.received_at>? AND e.supporting=1 "
        "AND e.provenance IN ('self','inference','other') ORDER BY s.received_at,s.id",
        (fact_id, bot, chat, grant, time.time() - 90 * 86400),
    ) as cursor:
        rows = list(await cursor.fetchall())
    seen, messages, authors, times = set(), set(), set(), []
    for sid, mid, author, text, received, deps in rows:
        if kind in {"interest", "habit"} and author != subject:
            continue
        normalized = " ".join(re.findall(r"\w+", text.casefold()))
        if not normalized or normalized in seen or mid in messages or deps is None:
            continue
        if not await valid_dependencies(conn, bot, chat, grant, deps, depth=depth + 1):
            continue
        seen.add(normalized)
        messages.add(mid)
        authors.add(author)
        times.append(received)
        result["source_ids"].append(sid)
    # Episodes are separated by 30 minutes of source time, not processing/batch time.
    episodes, start = 0, None
    for timestamp in times:
        if start is None or timestamp - start >= 1800:
            episodes += 1
            start = timestamp
    enough = len(messages) >= 3 and episodes >= 2
    if kind == "nickname":
        enough = enough and len(authors) >= 2
    level = (
        "repeated" if enough else "some" if len(messages) >= 2 else "single" if messages else "none"
    )
    result.update(
        level=level,
        label=LABELS[level],
        messages=len(messages),
        authors=len(authors),
        episodes=episodes,
        first_seen=min(times) if times else None,
        last_seen=max(times) if times else None,
    )
    if state != "proposed":
        result["reason"] = "Подтверждено вручную" if curated else "Состояние записи: " + state
    elif not enough:
        result["reason"] = "Нужны 3 разных сообщения в 2 эпизодах с интервалом от 30 минут"
        if kind == "nickname":
            result["reason"] += "; для прозвища — минимум 2 автора"
    else:
        result["eligible"] = True
        result["reason"] = "Учитывается здесь как предположение"
        if kind == "nickname":
            result["reason"] += "; не разрешает так обращаться к человеку"
    return result
