"""Transactional memory admission, invalidation and participant consent."""

import json
import secrets
import time
from typing import Any

import aiosqlite

from zarya.dialogue_logic import topic_id
from zarya.research_logic import purge as purge_research

RETENTION = 90 * 86400
SHAREABLE = {"name", "interest", "preference"}


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[object, ...] = ()) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def epoch(conn: aiosqlite.Connection, bot: str) -> int:
    await conn.execute("INSERT OR IGNORE INTO memory_epochs(bot_id) VALUES (?)", (bot,))
    value = await one(conn, "SELECT version FROM memory_epochs WHERE bot_id=?", (bot,))
    return int(value[0])


def empty_snapshot() -> str:
    return json.dumps(
        {
            "invalidated": True,
            "incoming": {"text": "", "sender_id": "", "message_id": 0},
            "context": [],
            "owner": False,
            "settings_version": 0,
            "settings": {},
        }
    )


async def invalidate(conn: aiosqlite.Connection, bot: str, chat: str, shared: bool = False) -> None:
    """Conservative scope purge: no old answer, replay or summary can leak a withdrawn fact."""
    await epoch(conn, bot)
    await conn.execute("UPDATE memory_epochs SET version=version+1 WHERE bot_id=?", (bot,))
    shared = await prune_dependencies(conn, bot, chat) or shared
    await conn.execute(
        "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','running') "
        "AND id IN (SELECT job_id FROM dialogue_runs WHERE bot_id=? "
        "AND json_extract(snapshot,'$.memory_epoch') IS NOT NULL)",
        (bot,),
    )
    await conn.execute(
        "UPDATE outbox SET state='cancelled',error_code='memory_changed' "
        "WHERE state='pending' AND job_id IN (SELECT job_id FROM dialogue_runs "
        "WHERE bot_id=? AND json_extract(snapshot,'$.memory_epoch') IS NOT NULL)",
        (bot,),
    )
    affected = "bot_id=? AND (chat_id=?" + (" OR chat_id LIKE '-%'" if shared else "") + ")"
    args = (bot, chat)
    await conn.execute(
        "DELETE FROM behavior_decisions WHERE run_id IN "
        "(SELECT id FROM dialogue_runs WHERE " + affected + ")",
        args,
    )
    await conn.execute(
        "UPDATE conversation_moods SET tone='neutral',intensity=0,source_run_id=NULL,"
        "version=version+1,epoch=epoch+1 WHERE " + affected,
        args,
    )
    await purge_research(conn, affected, args)
    await conn.execute(
        "UPDATE jobs SET state='cancelled',payload=json_remove(payload,'$.service_text') "
        "WHERE event_id IN (SELECT id FROM events WHERE " + affected + ") "
        "AND state IN ('pending','running')",
        args,
    )
    await conn.execute(
        "UPDATE jobs SET payload=json_remove(payload,'$.service_text') "
        "WHERE event_id IN (SELECT id FROM events WHERE " + affected + ")",
        args,
    )
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE run_id IN "
        "(SELECT id FROM dialogue_runs WHERE " + affected + ")",
        args,
    )
    await conn.execute(
        "UPDATE outbox SET payload='{\"text\":\"\"}',state=CASE WHEN state='pending' "
        "THEN 'cancelled' ELSE state END WHERE job_id IN "
        "(SELECT job_id FROM dialogue_runs WHERE " + affected + ")",
        args,
    )
    await conn.execute(
        "UPDATE dialogue_runs SET snapshot=?,response=NULL,error_code='memory_changed',"
        "state=CASE WHEN state='generating' THEN 'cancelled' ELSE state END WHERE " + affected,
        (empty_snapshot(),) + args,
    )
    await conn.execute("DELETE FROM recent_messages WHERE role='assistant' AND " + affected, args)
    # Assistant reply context may have depended on withdrawn memories too.
    newly_shared = await prune_dependencies(conn, bot, chat)
    if newly_shared and not shared:
        await invalidate(conn, bot, chat, True)
    # Extraction logs contain prior fact/context text too; technical usage is retained.
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE memory_batch_id IN "
        "(SELECT id FROM memory_batches WHERE bot_id=? AND chat_id=?)",
        (bot, chat),
    )


async def prune_dependencies(
    conn: aiosqlite.Connection, bot: str, legacy_chat: str | None = None
) -> bool:
    """Retire dependent context only; legacy batches lack a complete dependency manifest."""
    from zarya.memory_context import valid_dependencies
    from zarya.memory_observations import prune_evidence

    withdrawn_share = await prune_evidence(conn, bot)
    async with conn.execute(
        "SELECT id,chat_id,access_version,context_dependencies FROM memory_facts WHERE bot_id=? "
        "AND state NOT IN ('deleted','outdated') AND context_dependencies IS NOT NULL",
        (bot,),
    ) as cursor:
        facts = list(await cursor.fetchall())
    for fact_id, chat, grant, deps in facts:
        if not await valid_dependencies(conn, bot, chat, grant, deps):
            withdrawn_share = withdrawn_share or bool(
                await one(
                    conn,
                    "SELECT 1 FROM memory_shares WHERE fact_id=? AND state='active'",
                    (fact_id,),
                )
            )
            await conn.execute(
                "UPDATE memory_facts SET state='outdated',version=version+1 WHERE id=?", (fact_id,)
            )
            await conn.execute(
                "UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fact_id,)
            )
    async with conn.execute(
        "SELECT id,chat_id,access_version,dependencies FROM memory_batches WHERE bot_id=? "
        "AND (summary IS NOT NULL OR state='started')",
        (bot,),
    ) as cursor:
        batches = list(await cursor.fetchall())
    for batch_id, chat, grant, deps in batches:
        invalid = (
            chat == legacy_chat
            if deps is None
            else not await valid_dependencies(conn, bot, chat, grant, deps)
        )
        if invalid:
            await conn.execute(
                "UPDATE memory_batches SET summary=NULL,"
                "state=CASE WHEN state='started' THEN 'cancelled' "
                "ELSE state END,error_code='memory_changed' WHERE id=?",
                (batch_id,),
            )
            await conn.execute(
                "UPDATE model_calls SET request_json=NULL WHERE memory_batch_id=?", (batch_id,)
            )
    return withdrawn_share


async def suppress_sources(conn: aiosqlite.Connection, bot: str, chat: str, ids: list[int]) -> None:
    for source_id in ids:
        source = await one(
            conn,
            "SELECT message_id,event_id FROM memory_sources WHERE id=? AND bot_id=? AND chat_id=?",
            (source_id, bot, chat),
        )
        if not source:
            continue
        await conn.execute("UPDATE memory_sources SET valid=0,text='' WHERE id=?", (source_id,))
        from zarya.avatar_logic import purge as purge_avatars
        from zarya.media_logic import purge as purge_media
        from zarya.media_logic import purge_analysis

        await purge_media(
            conn,
            "bot_id=? AND chat_id=? AND event_id=?",
            (bot, chat, source[1]),
        )
        await purge_avatars(
            conn,
            "bot_id=? AND chat_id=? AND EXISTS "
            "(SELECT 1 FROM json_each(binding) m "
            "WHERE json_extract(m.value,'$.event_id')=?)",
            (bot, chat, source[1]),
        )
        await purge_analysis(
            conn,
            "bot_id=? AND chat_id=? AND EXISTS "
            "(SELECT 1 FROM json_each(analysis_context,'$.manifest') m "
            "WHERE json_extract(m.value,'$.event_id')=?)",
            (bot, chat, source[1]),
        )
        await conn.execute(
            "DELETE FROM recent_messages WHERE bot_id=? AND chat_id=? AND message_id=?",
            (bot, chat, source[0]),
        )
        await conn.execute("UPDATE events SET payload='{}' WHERE id=?", (source[1],))
        await conn.execute(
            "UPDATE jobs SET state='cancelled' WHERE event_id=? AND state IN ('pending','running')",
            (source[1],),
        )
        # A caption may also survive in vision observations or an album manifest.
        await conn.execute(
            "UPDATE photo_batches SET state='cancelled',result=NULL,manifest=NULL,"
            "error_code='memory_changed' "
            "WHERE id IN (SELECT batch_id FROM photo_items WHERE event_id=?)",
            (source[1],),
        )
        await conn.execute("UPDATE photo_items SET caption='' WHERE event_id=?", (source[1],))
        await conn.execute(
            "UPDATE model_calls SET request_json=NULL WHERE photo_batch_id IN "
            "(SELECT batch_id FROM photo_items WHERE event_id=?)",
            (source[1],),
        )
        await conn.execute(
            "UPDATE memory_facts SET state=CASE WHEN observation_kind!='none' "
            "AND state='proposed' THEN state ELSE 'outdated' END,version=version+1 "
            "WHERE state!='deleted' "
            "AND id IN (SELECT fact_id FROM memory_evidence WHERE source_id=?)",
            (source_id,),
        )
        await conn.execute(
            "UPDATE memory_shares SET state='revoked' WHERE fact_id IN "
            "(SELECT fact_id FROM memory_evidence WHERE source_id=?)",
            (source_id,),
        )


async def admit(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    scope: str,
    access: int,
    event: int,
    message: dict[str, Any],
    edited: bool,
    enabled: bool,
) -> None:
    sender = message.get("from", {})
    text = str(message.get("text") or message.get("caption") or "")[:4000]
    if not sender.get("id") or sender.get("is_bot") or message.get("sender_chat"):
        return
    previous = await one(
        conn,
        "SELECT id,event_id FROM memory_sources WHERE bot_id=? AND chat_id=? AND message_id=?",
        (bot, chat, message["message_id"]),
    )
    if previous and edited:
        sharing = await one(
            conn,
            "SELECT 1 FROM memory_shares s JOIN memory_evidence e ON e.fact_id=s.fact_id "
            "WHERE e.source_id=? AND s.state='active'",
            (previous[0],),
        )
        await suppress_sources(conn, bot, chat, [previous[0]])
        await invalidate(conn, bot, chat, bool(sharing))
    if (
        not enabled
        or not text
        or text.startswith("/")
        or message.get("forward_origin")
        or message.get("forward_date")
    ):
        return
    await epoch(conn, bot)
    await conn.execute(
        "INSERT INTO memory_sources(bot_id,chat_id,scope,message_id,event_id,sender_id,name,"
        "thread_id,text,received_at,access_version) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(bot_id,chat_id,message_id) DO UPDATE SET event_id=excluded.event_id,"
        "text=excluded.text,name=excluded.name,received_at=excluded.received_at,"
        "valid=1,processed=0,access_version=excluded.access_version",
        (
            bot,
            chat,
            scope,
            message["message_id"],
            event,
            str(sender["id"]),
            str(sender.get("first_name") or "Участник")[:80],
            topic_id(message),
            text,
            time.time(),
            access,
        ),
    )


async def command(
    conn: aiosqlite.Connection, bot: str, chat: str, scope: str, sender: str, event: int, text: str
) -> str | None:
    parts = text.split()
    if not parts or parts[0].split("@")[0].casefold() != "/memory":
        return None
    if scope != "private" or sender != chat:
        return "Управление личной памятью — в личке: /memory."
    if len(parts) == 3 and parts[1] == "allow":
        request = await one(
            conn,
            "SELECT r.fact_id,r.fact_version,f.text,f.category,f.chat_id "
            "FROM memory_consent_requests r JOIN memory_facts f ON f.id=r.fact_id "
            "WHERE r.token=? AND r.bot_id=? AND r.sender_id=? AND r.used=0 AND r.expires_at>? "
            "AND f.version=r.fact_version AND f.state='active' AND f.scope='private' "
            "AND f.sender_id=? AND f.chat_id=?",
            (parts[2], bot, sender, time.time(), sender, chat),
        )
        if not request or request[3] not in SHAREABLE:
            return (
                "Разрешение устарело или не относится к тебе. Посмотри актуальные записи: /memory."
            )
        await conn.execute(
            "INSERT INTO memory_shares VALUES (?,?,'active','participant',?,?,datetime('now')) "
            "ON CONFLICT(fact_id) DO UPDATE SET fact_version=excluded.fact_version,state='active',"
            "authority='participant',actor_id=excluded.actor_id,consent_event_id=excluded.consent_event_id,"
            "updated_at=excluded.updated_at",
            (request[0], request[1], sender, event),
        )
        await conn.execute("UPDATE memory_consent_requests SET used=1 WHERE token=?", (parts[2],))
        await invalidate(conn, bot, chat, True)
        return (
            f"Разрешено использовать в других одобренных группах, где ты пишешь: «{request[2]}». "
            f"Отозвать: /memory revoke {request[0]}"
        )
    if len(parts) == 3 and parts[1] in {"revoke", "forget"} and parts[2].isdigit():
        fact = await one(
            conn,
            "SELECT id FROM memory_facts WHERE id=? AND bot_id=? AND sender_id=? "
            "AND chat_id=? AND scope='private' AND state!='deleted'",
            (int(parts[2]), bot, sender, chat),
        )
        if not fact:
            return "Твоей записи с таким номером нет. Посмотри /memory."
        if parts[1] == "forget":
            await erase_fact(conn, bot, fact[0])
            return "Запись удалена из памяти."
        await conn.execute("UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fact[0],))
        await conn.execute("DELETE FROM memory_consent_requests WHERE fact_id=?", (fact[0],))
        await invalidate(conn, bot, chat, True)
        return "Перенос прекращён. Личная запись осталась только здесь."
    async with conn.execute(
        "SELECT id,version,text FROM memory_facts WHERE bot_id=? AND chat_id=? "
        "AND sender_id=? AND scope='private' AND state='active' "
        "AND category IN ('name','interest','preference') ORDER BY id DESC LIMIT 6",
        (bot, chat, sender),
    ) as cursor:
        facts = list(await cursor.fetchall())
    lines = [
        "Мои личные записи о тебе. Разрешение действует на точный текст во всех одобренных "
        "группах, где ты пишешь; без него всё остаётся в личке. Команды действуют сутки."
    ]
    for fact in facts:
        token = secrets.token_hex(8)
        await conn.execute(
            "INSERT INTO memory_consent_requests VALUES (?,?,?,?,?,?,0)",
            (token, bot, sender, fact[0], fact[1], time.time() + 86400),
        )
        lines.append(
            f"№{fact[0]}: {fact[2]}\nРазрешить: /memory allow {token}\n"
            f"Отозвать: /memory revoke {fact[0]}\nУдалить: /memory forget {fact[0]}"
        )
    return "\n\n".join(lines) if facts else "Пока нет активных личных записей для переноса."


async def erase_fact(conn: aiosqlite.Connection, bot: str, fact_id: int) -> None:
    fact = await one(
        conn,
        "SELECT chat_id,sender_id,category,fact_key FROM memory_facts WHERE id=? AND bot_id=?",
        (fact_id, bot),
    )
    if not fact:
        return
    async with conn.execute(
        "SELECT source_id FROM memory_evidence WHERE fact_id=?", (fact_id,)
    ) as cursor:
        ids = [int(r[0]) for r in await cursor.fetchall()]
    latest = await one(
        conn,
        "SELECT COALESCE(MAX(event_id),0) FROM memory_sources WHERE bot_id=? AND chat_id=?",
        (bot, fact[0]),
    )
    await conn.execute(
        "INSERT INTO memory_tombstones VALUES (?,?,?,?,?,?) ON CONFLICT(bot_id,chat_id,sender_id,"
        "category,fact_key) DO UPDATE SET through_event=MAX(through_event,excluded.through_event)",
        (bot, *fact, latest[0]),
    )
    sharing = await source_sharing(conn, ids)
    await suppress_sources(conn, bot, fact[0], ids)
    for source_id in ids:
        await conn.execute(
            "UPDATE memory_facts SET text='',state='deleted',version=version+1 "
            "WHERE id IN (SELECT fact_id FROM memory_evidence WHERE source_id=?)",
            (source_id,),
        )
    await conn.execute(
        "UPDATE memory_facts SET text='',state='deleted',version=version+1 WHERE id=?", (fact_id,)
    )
    await conn.execute("UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fact_id,))
    await conn.execute("DELETE FROM memory_consent_requests WHERE fact_id=?", (fact_id,))
    await invalidate(conn, bot, fact[0], bool(sharing))


async def source_sharing(conn: aiosqlite.Connection, ids: list[int]) -> bool:
    for source_id in ids:
        shared = await one(
            conn,
            "SELECT 1 FROM memory_shares s JOIN memory_evidence e "
            "ON e.fact_id=s.fact_id WHERE e.source_id=? AND s.state='active'",
            (source_id,),
        )
        if shared:
            return True
    return False
