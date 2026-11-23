"""Durable Telegram inbox, access decisions and delivery state (one process)."""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from zarya import message_batches, timings
from zarya.avatar_logic import AVATAR_WAIT
from zarya.avatar_logic import purge as purge_avatars
from zarya.avatar_logic import valid as avatar_valid
from zarya.configuration import prompts_at
from zarya.database import ConflictError, Database
from zarya.dialogue_logic import direct_trigger, explicit_reply, message_text, topic_id
from zarya.media_logic import MEDIA_WAIT
from zarya.media_logic import refs_valid as media_refs_valid
from zarya.memory_logic import (
    admit as admit_memory,
)
from zarya.memory_logic import (
    command as memory_command,
)
from zarya.memory_logic import (
    invalidate as invalidate_memory,
)
from zarya.models import Settings
from zarya.participant_context import admit as admit_participant
from zarya.photo_logic import PHOTO_WAIT, admit, link_job
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle
from zarya.research_intent import expects_forwarded_quote, text_factcheck
from zarya.research_logic import RESEARCH_WAIT
from zarya.research_logic import revision as research_revision
from zarya.source_material import FORWARD_QUESTION_WAIT, preceding_forward_question
from zarya.source_material import valid as source_valid


def stamp() -> str:
    return datetime.now(UTC).isoformat()


async def row(conn: aiosqlite.Connection, sql: str, args: tuple[object, ...]) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


async def pending_edit(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    message: dict[str, Any],
) -> tuple[int, str] | None:
    """Carry an unanswered text request to its new revision, never retry a dialogue call."""
    if not message.get("text") or str(message["text"]).startswith("/"):
        return None
    previous = await row(
        conn,
        "SELECT j.id,j.state,j.attempts,j.payload,e.payload,j.created_at FROM jobs j "
        "JOIN events e ON e.id=j.event_id JOIN research_messages m ON m.event_id=e.id "
        "AND m.bot_id=e.bot_id AND m.chat_id=e.chat_id "
        "WHERE e.bot_id=? AND e.chat_id=? AND m.message_id=? AND j.access_version=? "
        "AND j.kind='telegram' ORDER BY j.id DESC LIMIT 1",
        (bot, chat, message["message_id"], grant),
    )
    if not previous or previous[1] != "pending" or previous[2] != 0:
        return None
    job_id, _, _, raw, update, created = previous
    payload = json.loads(raw)
    original = json.loads(update)
    original = original.get("message") or original.get("edited_message") or {}
    trigger = payload.get("trigger")
    sender = str(message.get("from", {}).get("id", ""))
    thread = topic_id(message)
    if (
        trigger not in {"private", "name", "mention", "reply", "continuation"}
        or payload.get("sender_id") != sender
        or payload.get("thread_id") != thread
        or payload.get("service_reply")
        or not original.get("text")
        or message.get("from", {}).get("is_bot")
        or message.get("sender_chat")
        or message.get("forward_origin")
        or message.get("forward_date")
        or time.time() - datetime.fromisoformat(created).timestamp() > 600
    ):
        return None
    blocked = await row(
        conn,
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM dialogue_runs WHERE job_id=?) "
        "OR EXISTS (SELECT 1 FROM outbox WHERE job_id=?) "
        "OR EXISTS (SELECT 1 FROM jobs j JOIN events e ON e.id=j.event_id "
        "WHERE e.bot_id=? AND e.chat_id=? AND j.id>? "
        "AND json_extract(j.payload,'$.sender_id')=? "
        "AND COALESCE(json_extract(j.payload,'$.thread_id'),0)=? "
        "AND json_extract(j.payload,'$.trigger') IN "
        "('private','name','mention','reply','continuation'))",
        (job_id, job_id, bot, chat, job_id, sender, thread or 0),
    )
    return None if blocked else (job_id, trigger)


class TelegramStore:
    def __init__(self, db: Database, prompts: PromptBundle = DEFAULT_PROMPTS):
        self.db = db
        self.prompts = prompts
        # Also guards revocation against the sender's final check + bounded send.
        self.gate = asyncio.Lock()
        self.clock = timings.Clock()

    async def prompts_for(
        self, version: int, conn: aiosqlite.Connection | None = None
    ) -> PromptBundle:
        if conn is not None:
            return await prompts_at(conn, version, self.prompts)
        async with self.db.read_lock:
            return await prompts_at(self.db.reader, version, self.prompts)

    async def recover(self, bot_id: str) -> None:
        async with self.gate, self.db.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO telegram_state(bot_id) VALUES (?)", (bot_id,))
            await conn.execute(
                "UPDATE jobs SET typing_state='uncertain',typing_error='interrupted' "
                "WHERE typing_state='sending' AND event_id IN (SELECT id FROM "
                "events WHERE bot_id=?)",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE dialogue_runs SET typing_state='uncertain',"
                "typing_error='interrupted' WHERE bot_id=? AND typing_state='sending'",
                (bot_id,),
            )
            # A preflight crash is not an uncertain generation. No automatic repeat
            # of either the count or the paid request; old runs retain legacy recovery.
            await conn.execute(
                "UPDATE model_calls SET state='rejected',error_code='preflight_interrupted',"
                "finished_at=?,cost_usd=0 "
                "WHERE state='started' AND operation='preflight' AND run_id IN "
                "(SELECT id FROM dialogue_runs WHERE bot_id=?)",
                (stamp(), bot_id),
            )
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='count_interrupted',"
                "finished_at=? WHERE state='started' AND context_count_run_id IN "
                "(SELECT id FROM dialogue_runs WHERE bot_id=?)",
                (stamp(), bot_id),
            )
            await conn.execute(
                "UPDATE jobs SET state='done',lease_until=NULL WHERE state='running' AND id IN "
                "(SELECT c.job_id FROM model_calls c JOIN dialogue_runs r ON r.id=c.run_id "
                "WHERE c.error_code='preflight_interrupted' AND r.bot_id=?)",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE dialogue_runs SET state='rejected',error_code='preflight_interrupted',"
                "snapshot=json_set(snapshot,'$.context_budget.state','unverified',"
                "'$.context_budget.decision','blocked','$.context_budget.api_count.state',"
                "CASE WHEN json_extract(snapshot,'$.context_budget.api_count.attempted')=1 "
                "THEN 'unknown' ELSE 'not_started' END) WHERE bot_id=? AND state='generating' "
                "AND id IN (SELECT run_id FROM model_calls "
                "WHERE error_code='preflight_interrupted')",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted' "
                "WHERE state='started' AND run_id IN (SELECT id FROM dialogue_runs WHERE bot_id=?)",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE jobs SET state='unknown' WHERE state='running' AND id IN "
                "(SELECT job_id FROM model_calls WHERE state='unknown')",
            )
            await conn.execute(
                "UPDATE dialogue_runs SET state='unknown',error_code='interrupted' "
                "WHERE bot_id=? AND state='generating'",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE jobs SET state='pending', lease_until=NULL WHERE state='running' "
                "AND event_id IN (SELECT id FROM events WHERE bot_id=?)",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE outbox SET state='unknown', error_code='interrupted' "
                "WHERE bot_id=? AND state='sending'",
                (bot_id,),
            )
            await conn.execute(
                "UPDATE outbox SET state='cancelled',error_code='previous_part_failed' "
                "WHERE bot_id=? AND state='pending' AND job_id IN "
                "(SELECT job_id FROM outbox WHERE bot_id=? AND state='unknown')",
                (bot_id, bot_id),
            )

    async def offset(self, bot_id: str) -> int | None:
        value = await self.db.one(
            "SELECT next_offset, last_received FROM telegram_state WHERE bot_id=?", (bot_id,)
        )
        # Telegram may restart its ID sequence after a week without new events.
        # Asking for earliest unconfirmed updates is safe; negative offsets are never used.
        if not value or time.time() - value[1] > 6 * 86400:
            return None
        return int(value[0]) if value[0] is not None else None

    async def _cancel(self, conn: aiosqlite.Connection, bot_id: str, subject: str) -> None:
        from zarya.memory_commands import scrub_scope

        await scrub_scope(conn, bot_id, subject)
        from zarya.behavior import erase_chat
        from zarya.media_logic import purge as purge_media

        await purge_media(conn, "bot_id=? AND chat_id=?", (bot_id, subject))
        await purge_avatars(conn, "bot_id=? AND chat_id=?", (bot_id, subject))
        await erase_chat(conn, bot_id, subject)
        from zarya.task_state import scrub as scrub_tasks

        await scrub_tasks(
            conn, "bot_id=? AND chat_id=? AND retired=0", (bot_id, subject), retire=True
        )
        shared = await row(
            conn,
            "SELECT 1 FROM memory_shares s JOIN memory_facts f ON f.id=s.fact_id "
            "WHERE f.bot_id=? AND f.chat_id=? AND s.state='active'",
            (bot_id, subject),
        )
        await invalidate_memory(conn, bot_id, subject, bool(shared))
        await conn.execute(
            "UPDATE memory_shares SET state='revoked' WHERE fact_id IN "
            "(SELECT id FROM memory_facts WHERE bot_id=? AND chat_id=?)",
            (bot_id, subject),
        )
        await conn.execute(
            "UPDATE photo_batches SET state='cancelled',result=NULL,error_code='access_changed' "
            "WHERE bot_id=? AND chat_id=? AND state IN ('queued','downloading','analyzing',"
            "'completed','cache')",
            (bot_id, subject),
        )
        await conn.execute(
            "DELETE FROM active_dialogues WHERE bot_id=? AND chat_id=?", (bot_id, subject)
        )
        await conn.execute(
            "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','running') "
            "AND event_id IN (SELECT id FROM events WHERE bot_id=? AND chat_id=?)",
            (bot_id, subject),
        )
        await conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE bot_id=? AND chat_id=? AND state='pending'",
            (bot_id, subject),
        )

    async def decide(self, bot_id: str, scope: str, subject: str, state: str, version: int) -> None:
        async with self.gate, self.db.transaction() as conn:
            peer = await row(
                conn,
                "SELECT member_state FROM telegram_access WHERE bot_id=? AND scope=? "
                "AND subject_id=? AND version=?",
                (bot_id, scope, subject, version),
            )
            if not peer:
                raise ConflictError
            if state == "approved" and peer[0] in {"left", "kicked", "migrated"}:
                raise ValueError("Бот недоступен в этом чате; дождитесь нового события Telegram")
            await conn.execute(
                "UPDATE telegram_access SET state=?, version=version+1, updated_at=? "
                "WHERE bot_id=? AND scope=? AND subject_id=?",
                (state, stamp(), bot_id, scope, subject),
            )
            # Even reapproval cannot revive work admitted under an older permission.
            await self._cancel(conn, bot_id, subject)

    async def _discover(
        self,
        conn: aiosqlite.Connection,
        bot_id: str,
        scope: str,
        subject: str,
        title: str,
        username: str | None,
    ) -> None:
        await conn.execute(
            "INSERT INTO telegram_access(bot_id,scope,subject_id,title,username,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(bot_id,scope,subject_id) DO UPDATE SET "
            "title=excluded.title, username=excluded.username",
            (bot_id, scope, subject, title[:256], username, stamp()),
        )

    async def set_message_batches(
        self, bot: str, chat: str, grant: int, expected: bool, enabled: bool
    ) -> None:
        async with self.gate, self.db.transaction() as conn:
            changed = await conn.execute(
                "UPDATE telegram_access SET message_batches_enabled=? "
                "WHERE bot_id=? AND subject_id=? AND scope='group' AND version=? "
                "AND state='approved' AND member_state NOT IN ('left','kicked','migrated') "
                "AND message_batches_enabled=?",
                (int(enabled), bot, chat, grant, int(expected)),
            )
            if changed.rowcount != 1:
                raise ConflictError

    async def _migration(
        self, conn: aiosqlite.Connection, bot_id: str, old_id: str, new_id: str, title: str
    ) -> None:
        await conn.execute(
            "INSERT OR IGNORE INTO telegram_migrations VALUES (?,?,?)", (bot_id, old_id, new_id)
        )
        await self._discover(conn, bot_id, "group", new_id, title, None)
        await conn.execute(
            "UPDATE telegram_access SET message_batches_enabled=0 "
            "WHERE bot_id=? AND scope='group' AND subject_id=? AND EXISTS ("
            "SELECT 1 FROM telegram_access WHERE bot_id=? AND scope='group' "
            "AND subject_id=? AND message_batches_enabled=0)",
            (bot_id, new_id, bot_id, old_id),
        )
        # Explicit reapproval avoids merging contradictory scopes or old topic/reply IDs.
        for subject in (old_id, new_id):
            await self._cancel(conn, bot_id, subject)
            await conn.execute(
                "UPDATE telegram_access SET state='revoked', version=version+1, updated_at=?, "
                "member_state=? WHERE bot_id=? AND scope='group' AND subject_id=?",
                (stamp(), "migrated" if subject == old_id else "unknown", bot_id, subject),
            )

    async def ingest(self, bot_id: str, username: str, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        async with self.gate, self.db.transaction() as conn:
            for update in updates:
                update_id = int(update["update_id"])
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO "
                    "events(bot_id,update_id,payload,received_at,disposition) "
                    "VALUES (?,?,'{}',?,'ignored')",
                    (bot_id, update_id, stamp()),
                )
                if cursor.rowcount != 1:
                    continue
                event_id = cursor.lastrowid
                member = update.get("my_chat_member")
                message = update.get("message") or update.get("edited_message")
                if member:
                    chat = member["chat"]
                    kind = chat["type"]
                    if kind not in {"private", "group", "supergroup"}:
                        continue
                    scope = "private" if kind == "private" else "group"
                    subject = str(chat["id"])
                    # Private block/unblock alone does not create an application.
                    if scope == "group":
                        await self._discover(
                            conn, bot_id, scope, subject, chat.get("title", subject), None
                        )
                    member_state = member["new_chat_member"]["status"]
                    if member_state == "restricted" and not member["new_chat_member"].get(
                        "is_member", False
                    ):
                        member_state = "left"
                    await conn.execute(
                        "UPDATE telegram_access SET member_state=?, updated_at=? "
                        "WHERE bot_id=? AND scope=? AND subject_id=?",
                        (member_state, stamp(), bot_id, scope, subject),
                    )
                    if member_state in {"left", "kicked"}:
                        await self._cancel(conn, bot_id, subject)
                        await conn.execute(
                            "UPDATE telegram_access SET state='revoked', version=version+1 "
                            "WHERE bot_id=? AND scope=? AND subject_id=?",
                            (bot_id, scope, subject),
                        )
                    await conn.execute(
                        "UPDATE events SET chat_id=?, disposition='membership' WHERE id=?",
                        (subject, event_id),
                    )
                    continue
                if not message or message["chat"]["type"] not in {"private", "group", "supergroup"}:
                    continue
                chat = message["chat"]
                subject = str(chat["id"])
                scope = "private" if chat["type"] == "private" else "group"
                sender = message.get("from") or {}
                text = message.get("text", "")
                command = text.split(maxsplit=1)[0] if text else ""
                is_start = command.casefold() in {"/start", f"/start@{username}".casefold()}
                private_start = scope == "private" and is_start and "edited_message" not in update
                if scope == "private" and str(sender.get("id")) != subject:
                    continue
                if scope == "group" or private_start:
                    title = (
                        chat.get("title")
                        or " ".join(
                            part
                            for part in (sender.get("first_name"), sender.get("last_name"))
                            if part
                        )
                        or subject
                    )
                    await self._discover(
                        conn,
                        bot_id,
                        scope,
                        subject,
                        title,
                        sender.get("username") if scope == "private" else chat.get("username"),
                    )
                if message.get("migrate_to_chat_id") or message.get("migrate_from_chat_id"):
                    old_id = str(message.get("migrate_from_chat_id", subject))
                    new_id = str(message.get("migrate_to_chat_id", subject))
                    # Same migration may arrive from both groups; handle it once.
                    known = await row(
                        conn,
                        "SELECT 1 FROM telegram_migrations WHERE bot_id=? AND old_id=?",
                        (bot_id, old_id),
                    )
                    if not known:
                        await self._migration(
                            conn, bot_id, old_id, new_id, chat.get("title", new_id)
                        )
                    await conn.execute(
                        "UPDATE events SET chat_id=?, disposition='migration' WHERE id=?",
                        (subject, event_id),
                    )
                    continue
                access = await row(
                    conn,
                    "SELECT state,version,member_state,message_batches_enabled "
                    "FROM telegram_access WHERE bot_id=? "
                    "AND scope=? AND subject_id=?",
                    (bot_id, scope, subject),
                )
                allowed = (
                    access
                    and access[0] == "approved"
                    and access[2] not in {"left", "kicked", "migrated"}
                )
                # No content, user profile or media processing before approval.
                await conn.execute(
                    "UPDATE events SET chat_id=?, payload=?, disposition=? WHERE id=?",
                    (
                        subject,
                        json.dumps(update, ensure_ascii=False) if allowed else "{}",
                        "accepted" if allowed else "request" if private_start else "denied",
                        event_id,
                    ),
                )
                if allowed:
                    assert event_id is not None
                    settings_row = await row(conn, "SELECT document FROM settings WHERE id=1", ())
                    settings = Settings.model_validate_json(settings_row[0])
                    trigger = direct_trigger(message, bot_id, username, settings.display_name)
                    edited = "edited_message" in update
                    batch_member = (
                        await message_batches.member(conn, bot_id, subject, access[1], message)
                        if edited
                        else None
                    )
                    replacement = (
                        await pending_edit(conn, bot_id, subject, access[1], message)
                        if edited and not batch_member
                        else None
                    )
                    if edited:
                        await research_revision(conn, bot_id, subject, message["message_id"])
                        await purge_avatars(
                            conn,
                            "bot_id=? AND chat_id=? AND EXISTS "
                            "(SELECT 1 FROM json_each(binding) m "
                            "WHERE json_extract(m.value,'$.message_id')=?)",
                            (bot_id, subject, message["message_id"]),
                        )
                    await conn.execute(
                        "INSERT INTO research_messages VALUES (?,?,?,?,?) ON "
                        "CONFLICT(bot_id,chat_id,message_id) "
                        "DO UPDATE SET event_id=excluded.event_id,"
                        "access_version=excluded.access_version",
                        (bot_id, subject, message["message_id"], event_id, access[1]),
                    )
                    kept_batch_edit = False
                    if batch_member and batch_member[2]:
                        leader = batch_member[0]
                        kept_batch_edit = message_batches.eligible(message, settings)
                        # Move the pending member before memory erases the old event.
                        if kept_batch_edit:
                            await message_batches.replace(
                                conn, leader, message["message_id"], event_id
                            )
                        else:
                            await message_batches.remove(conn, leader, message["message_id"])
                    sender_id = str(sender.get("id", ""))
                    thread_id = topic_id(message)
                    await admit_participant(
                        conn,
                        bot_id,
                        subject,
                        access[1],
                        event_id,
                        message,
                        edited,
                    )
                    await admit_memory(
                        conn,
                        bot_id,
                        subject,
                        scope,
                        access[1],
                        event_id,
                        message,
                        edited,
                        settings.memory_enabled,
                    )
                    service_text = (
                        await memory_command(
                            conn, bot_id, subject, scope, sender_id, event_id, text
                        )
                        if not edited
                        else None
                    )
                    album = str(message.get("media_group_id") or "")
                    now = time.time()
                    can_pair_post = (
                        scope == "private"
                        and not edited
                        and not private_start
                        and service_text is None
                        and settings.dialogue_enabled
                    )
                    album_leader = (
                        await row(
                            conn,
                            "SELECT j.state FROM jobs j JOIN events e ON e.id=j.event_id "
                            "WHERE e.bot_id=? AND e.chat_id=? AND j.access_version=? "
                            "AND json_extract(j.payload,'$.sender_id')=? "
                            "AND json_extract(j.payload,'$.thread_id') IS ? "
                            "AND json_extract(j.payload,'$.post_album')=? LIMIT 1",
                            (bot_id, subject, access[1], sender_id, thread_id, album),
                        )
                        if can_pair_post and album
                        else None
                    )
                    album_tail = bool(album_leader and album_leader[0] == "pending")
                    previous_question = (
                        await preceding_forward_question(
                            conn, bot_id, subject, access[1], message, now
                        )
                        if can_pair_post and not album_leader
                        else None
                    )
                    batch, album_job, new_photo_job = (
                        (None, None, not album_tail)
                        if previous_question or album_tail
                        else await admit(
                            conn, bot_id, subject, scope, access[1], event_id, message,
                            settings, edited,
                        )
                    )
                    if trigger == "ambient" and not edited and not message.get("photo"):
                        active = await row(
                            conn,
                            "SELECT 1 FROM active_dialogues WHERE bot_id=? AND chat_id=? "
                            "AND thread_id=? AND sender_id=? AND expires_at>? AND remaining>0",
                            (
                                bot_id,
                                subject,
                                thread_id or 0,
                                sender_id,
                                time.time(),
                            ),
                        )
                        if active:
                            trigger = "continuation"
                            await conn.execute(
                                "UPDATE active_dialogues SET remaining=remaining-1 "
                                "WHERE bot_id=? AND chat_id=? AND thread_id=? AND sender_id=?",
                                (bot_id, subject, thread_id or 0, sender_id),
                            )
                    if not service_text:
                        from zarya.task_state import observe as observe_task
                        from zarya.verbatim_quote import match as quote_request

                        if not quote_request(message, settings.display_name, bot_id, username):
                            await observe_task(
                                conn, bot_id, subject, access[1], event_id,
                                message, settings, trigger, username, edited,
                            )
                    # Retire the old revision even when it never reached dialogue_runs.
                    if replacement:
                        await conn.execute(
                            "UPDATE jobs SET state='cancelled' WHERE id=?", (replacement[0],)
                        )
                    # Started dialogue calls are never repeated after an edit.
                    if (edited and not batch_member) or (
                        not album_job
                        and new_photo_job
                        and not album_leader
                        and trigger in {"private", "name", "mention", "reply", "continuation"}
                    ):
                        await conn.execute(
                            "UPDATE jobs SET state='cancelled' WHERE id IN (SELECT job_id FROM "
                            "dialogue_runs WHERE bot_id=? AND chat_id=? AND sender_id=? "
                            "AND COALESCE(thread_id,0)=? "
                            + (
                                "AND message_id=? "
                                if edited
                                else "AND NOT EXISTS (SELECT 1 FROM text_batches b "
                                "WHERE b.job_id=dialogue_runs.job_id) "
                            )
                            + ") AND state IN ('pending','running')",
                            (bot_id, subject, sender_id, thread_id or 0)
                            + ((message["message_id"],) if edited else ()),
                        )
                        await conn.execute(
                            "UPDATE outbox SET state='cancelled' WHERE state='pending' AND job_id "
                            "IN (SELECT job_id FROM dialogue_runs WHERE bot_id=? AND chat_id=? "
                            "AND sender_id=? AND COALESCE(thread_id,0)=? "
                            + (
                                "AND message_id=? "
                                if edited
                                else "AND NOT EXISTS (SELECT 1 FROM text_batches b "
                                "WHERE b.job_id=dialogue_runs.job_id) "
                            )
                            + ")",
                            (bot_id, subject, sender_id, thread_id or 0)
                            + ((message["message_id"],) if edited else ()),
                        )
                    await conn.execute(
                        "INSERT INTO recent_messages VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(bot_id,chat_id,message_id) DO UPDATE SET text=excluded.text, "
                        "event_id=excluded.event_id,name=excluded.name",
                        (
                            bot_id,
                            subject,
                            message["message_id"],
                            thread_id,
                            sender_id,
                            str(sender.get("first_name") or "Участник")[:80],
                            "user",
                            message_text(message),
                            event_id,
                            time.time(),
                        ),
                    )
                    if album_leader:
                        continue
                    # Retention handles history; neighbours must not evict personal windows.
                    payload = {
                        "scope": scope,
                        "bot_username": username,
                        "chat_id": subject,
                        "service_reply": private_start or service_text is not None,
                        "thread_id": thread_id,
                        "message_id": message.get("message_id"),
                        "trigger": (
                            replacement[1] if replacement else "edited" if edited else trigger
                        ),
                        "sender_id": sender_id,
                    }
                    if service_text is not None:
                        payload["service_text"] = service_text
                    if batch_member:
                        _, old_trigger, open_batch = batch_member
                        if not open_batch or kept_batch_edit or not message.get("text"):
                            continue
                        payload["trigger"] = (
                            old_trigger if old_trigger in message_batches.DIRECT else "continuation"
                        )
                        payload["keep_addressed"] = True
                    if batch:
                        payload["photo_batch_id"] = batch
                        if album_job:
                            previous = await row(
                                conn, "SELECT payload FROM jobs WHERE id=?", (album_job,)
                            )
                            previous_payload = json.loads(previous[0])
                            direct = {"private", "name", "mention", "reply"}
                            if (trigger in direct and message.get("caption")) or (
                                trigger in direct and previous_payload.get("trigger") not in direct
                            ):
                                await link_job(conn, batch, album_job, event_id, payload)
                            continue
                        if not new_photo_job:
                            continue
                    if (
                        not batch
                        and not edited
                        and scope == "group"
                        and access[3]
                        and settings.dialogue_enabled
                        and not payload["service_reply"]
                        and message_batches.eligible(message, settings)
                        and await message_batches.collect(
                            conn, bot_id, payload, access[1], event_id, message["date"]
                        )
                    ):
                        continue
                    if previous_question:
                        question_job, payload["post_question"] = previous_question
                        if album:
                            payload["post_album"] = album
                        payload["post_text_first"] = True
                        await conn.execute(
                            "UPDATE jobs SET state='cancelled' WHERE id=?", (question_job,)
                        )
                    elif explicit_reply(message) and text_factcheck(message):
                        payload["post_text_first"] = True
                    elif can_pair_post and expects_forwarded_quote(message):
                        payload["post_wait_until"] = now + FORWARD_QUESTION_WAIT
                    job_cursor = await conn.execute(
                        "INSERT INTO "
                        "jobs(kind,event_id,state,payload,created_at,access_version) "
                        "VALUES ('telegram',?,'pending',?,?,?)",
                        (event_id, json.dumps(payload), stamp(), access[1]),
                    )
                    if batch:
                        await conn.execute(
                            "UPDATE photo_batches SET leader_job_id=? WHERE id=?",
                            (job_cursor.lastrowid, batch),
                        )
            await conn.execute(
                "INSERT INTO telegram_state(bot_id,next_offset,last_received) VALUES (?,?,?) "
                "ON CONFLICT(bot_id) DO UPDATE SET "
                "next_offset=excluded.next_offset,last_received=excluded.last_received",
                (bot_id, max(int(item["update_id"]) for item in updates) + 1, time.time()),
            )

    async def process_one(self, bot_id: str) -> bool:
        async with self.gate, self.db.transaction() as conn:
            job = await row(
                conn,
                "SELECT j.id,j.payload,j.access_version,j.attempts FROM jobs j JOIN events "
                "e ON e.id=j.event_id "
                "WHERE e.bot_id=? AND j.state='pending' AND j.next_attempt<=? ORDER BY "
                "j.id LIMIT 1",
                (bot_id, time.time()),
            )
            if not job:
                return False
            job_id, document, version, attempts = job
            payload = json.loads(document)
            access = await row(
                conn,
                "SELECT state,version FROM telegram_access WHERE bot_id=? AND scope=? AND "
                "subject_id=?",
                (bot_id, payload["scope"], payload["chat_id"]),
            )
            if not access or access[0] != "approved" or access[1] != version:
                await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job_id,))
                return True
            if attempts >= 3:
                await conn.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
                return True
            await conn.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (job_id,)
            )
            if payload["service_reply"]:
                # Stage 2 transport check; no model or conversational reply yet.
                outgoing = {
                    "text": "Доступ разрешён. Я на связи! Диалог с ИИ появится на следующем этапе.",
                    "thread_id": payload["thread_id"],
                }
                await conn.execute(
                    "INSERT OR IGNORE INTO "
                    "outbox(job_id,bot_id,scope,chat_id,state,payload,created_at,access_version) "
                    "VALUES (?,?,?,?,'pending',?,?,?)",
                    (
                        job_id,
                        bot_id,
                        payload["scope"],
                        payload["chat_id"],
                        json.dumps(outgoing, ensure_ascii=False),
                        stamp(),
                        version,
                    ),
                )
            await conn.execute(
                "UPDATE jobs SET state='done',lease_until=NULL WHERE id=?", (job_id,)
            )
            return True

    async def claim_delivery(self, bot_id: str) -> dict[str, Any] | None:
        # Caller holds gate through the subsequent network call and result commit.
        async with self.db.transaction() as conn:
            claim_at = time.time()
            check_start = time.monotonic()
            value = await row(
                conn,
                "SELECT id,chat_id,scope,payload,access_version,attempts,part_index,created_at "
                "FROM outbox "
                "WHERE bot_id=? AND state='pending' AND next_attempt<=? "
                "AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.job_id=outbox.job_id "
                "AND earlier.part_index<outbox.part_index AND earlier.state!='sent') "
                "AND NOT EXISTS (SELECT 1 FROM delivery_limits d WHERE d.bot_id=outbox.bot_id "
                "AND d.chat_id=outbox.chat_id AND (d.blocked_until>? OR d.last_attempt + "
                "CASE WHEN outbox.scope='group' THEN 3.2 ELSE 1.1 END >?)) ORDER BY id LIMIT 1",
                (bot_id, time.time(), time.time(), time.time()),
            )
            if not value:
                return None
            item_id, chat_id, scope, document, version, attempts, part_index, created_at = value
            memory_run = await row(
                conn,
                "SELECT snapshot FROM dialogue_runs WHERE job_id="
                "(SELECT job_id FROM outbox WHERE id=?)",
                (item_id,),
            )
            local_quote = False
            batch_reply = False
            if memory_run:
                snapshot = json.loads(memory_run[0])
                local_quote = bool(snapshot.get("verbatim_quote"))
                batch_reply = bool(snapshot.get("current_messages"))
                if snapshot.get("behavior"):
                    from zarya.behavior import valid

                    valid_behavior = await valid(
                        conn, bot_id, chat_id, snapshot["behavior"], delivery=True
                    )
                    outgoing_action = json.loads(document).get("action", "text")
                    current_settings = Settings.model_validate_json(
                        (await row(conn, "SELECT document FROM settings", ()))[0]
                    )
                    if not valid_behavior or (
                        outgoing_action == "reaction" and not current_settings.reactions_enabled
                    ):
                        await conn.execute(
                            "UPDATE outbox SET state='cancelled',error_code='behavior_changed' "
                            "WHERE job_id=(SELECT job_id FROM outbox WHERE id=?) "
                            "AND state='pending'",
                            (item_id,),
                        )
                        return None
                if not await media_refs_valid(
                    conn, snapshot.get("media_refs", [])
                ) or not await avatar_valid(conn, snapshot.get("avatar_id")):
                    await conn.execute(
                        "UPDATE outbox SET state='cancelled',error_code='media_changed' "
                        "WHERE job_id=(SELECT job_id FROM outbox WHERE id=?) AND state='pending'",
                        (item_id,),
                    )
                    return None
                if snapshot.get("source_manifest") and not await source_valid(
                    conn,
                    bot_id,
                    chat_id,
                    snapshot["source_access_version"],
                    snapshot["source_manifest"],
                ):
                    await conn.execute(
                        "UPDATE outbox SET state='cancelled',error_code='source_changed' "
                        "WHERE state='pending' AND job_id=(SELECT job_id FROM outbox WHERE id=?)",
                        (item_id,),
                    )
                    return None
                current_epoch = await row(
                    conn, "SELECT version FROM memory_epochs WHERE bot_id=?", (bot_id,)
                )
                from zarya.memory_context import valid_snapshot

                if (
                    snapshot.get("invalidated")
                    or (
                        "memory_epoch" in snapshot
                        and current_epoch
                        and snapshot["memory_epoch"] != current_epoch[0]
                    )
                    or not await valid_snapshot(conn, bot_id, snapshot)
                ):
                    await conn.execute(
                        "UPDATE outbox SET state='cancelled',error_code='memory_changed' WHERE "
                        "id=?",
                        (item_id,),
                    )
                    return None
            linked = await row(
                conn,
                "SELECT 1 FROM model_calls WHERE job_id=(SELECT job_id FROM outbox WHERE id=?)",
                (item_id,),
            )
            settings = await row(conn, "SELECT document FROM settings WHERE id=1", ())
            expired = time.time() - datetime.fromisoformat(created_at).timestamp() > 600
            if expired or (
                (linked or local_quote)
                and not Settings.model_validate_json(settings[0]).dialogue_enabled
            ):
                await conn.execute(
                    "UPDATE outbox SET state='cancelled',error_code=? WHERE job_id="
                    "(SELECT job_id FROM outbox WHERE id=?) AND state='pending'",
                    ("expired" if expired else "disabled", item_id),
                )
                return None
            access = await row(
                conn,
                "SELECT state,version,member_state FROM telegram_access WHERE bot_id=? AND "
                "scope=? AND subject_id=?",
                (bot_id, scope, chat_id),
            )
            if (
                not access
                or access[0] != "approved"
                or access[1] != version
                or access[2] in {"left", "kicked", "migrated"}
            ):
                await conn.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (item_id,))
                return None
            if attempts >= 3:
                await conn.execute(
                    "UPDATE outbox SET state='failed',error_code='attempt_limit' WHERE id=?",
                    (item_id,),
                )
                await conn.execute(
                    "UPDATE outbox SET state='cancelled',error_code='previous_part_failed' "
                    "WHERE job_id=(SELECT job_id FROM outbox WHERE id=?) "
                    "AND state='pending'",
                    (item_id,),
                )
                return None
            await conn.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?", (item_id,)
            )
            await conn.execute(
                "INSERT INTO delivery_limits(bot_id,chat_id,last_attempt) VALUES (?,?,?) "
                "ON CONFLICT(bot_id,chat_id) DO UPDATE SET last_attempt=excluded.last_attempt",
                (bot_id, chat_id, time.time()),
            )
            outgoing = json.loads(document)
            if part_index > 0 and not batch_reply:
                outgoing["reply_id"] = None
            measured = {"delivery_check_ms": timings.elapsed(check_start)}
            wait = self.clock.since(datetime.fromisoformat(created_at).timestamp(), claim_at)
            if wait is not None:
                measured["outbox_wait_ms"] = wait
            return {
                "id": item_id,
                "chat_id": chat_id,
                **outgoing,
                "_timings": measured,
                "_attempt": attempts + 1,
            }

    async def delivery_result(
        self,
        item_id: int,
        state: str,
        code: str | None = None,
        message_id: int | None = None,
        delay: float = 0,
        measured: dict[str, float] | None = None,
        attempt: int | None = None,
        confirmed_at: float | None = None,
    ) -> None:
        async with self.db.transaction() as conn:
            write_start = time.monotonic()
            changed = await conn.execute(
                "UPDATE outbox SET state=?,error_code=?,message_id=?,next_attempt=? WHERE "
                "id=? AND state='sending' AND (? IS NULL OR attempts=?)",
                (state, code, message_id, time.time() + delay, item_id, attempt, attempt),
            )
            if changed.rowcount != 1:
                return
            item = await row(
                conn, "SELECT bot_id,chat_id,payload,job_id FROM outbox WHERE id=?", (item_id,)
            )
            if delay:
                await conn.execute(
                    "UPDATE delivery_limits SET blocked_until=? WHERE bot_id=? AND chat_id=?",
                    (time.time() + delay, item[0], item[1]),
                )
            if state in {"failed", "unknown"}:
                await conn.execute(
                    "UPDATE outbox SET state='cancelled',error_code='previous_part_failed' "
                    "WHERE job_id=? AND state='pending' AND id>?",
                    (item[3], item_id),
                )
            if (
                state == "sent"
                and message_id is not None
                and json.loads(item[2]).get("action", "text") == "text"
            ):
                await conn.execute(
                    "UPDATE jobs SET typing_due=0 WHERE id=? AND EXISTS "
                    "(SELECT 1 FROM outbox WHERE job_id=? AND state='pending')",
                    (item[3], item[3]),
                )
                run = await row(
                    conn,
                    "SELECT sender_id,thread_id,trigger,state='completed' OR "
                    "COALESCE(json_extract(snapshot,'$.verbatim_quote'),0)=0 FROM dialogue_runs "
                    "WHERE job_id=? AND mode='live'",
                    (item[3],),
                )
                if run:
                    document = json.loads(item[2])
                    await conn.execute(
                        "INSERT OR REPLACE INTO recent_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            item[0],
                            item[1],
                            message_id,
                            run[1],
                            item[0],
                            "Заря",
                            "assistant",
                            document["text"],
                            None,
                            time.time(),
                        ),
                    )
                    if run[3] and run[2] in {"name", "mention", "reply"}:
                        await conn.execute(
                            "INSERT OR REPLACE INTO active_dialogues VALUES (?,?,?,?,?,?)",
                            (item[0], item[1], run[1] or 0, run[0], time.time() + 90, 2),
                        )
            if measured is not None:
                previous = await row(
                    conn, "SELECT timings_json,part_index FROM outbox WHERE id=?", (item_id,)
                )
                accumulated = timings.clean(previous[0])
                attempt_values = timings.clean({"version": 1, **measured})
                for key in ("delivery_check_ms", "telegram_ms"):
                    if key in attempt_values:
                        accumulated[key] = accumulated.get(key, 0) + attempt_values[key]
                if "outbox_wait_ms" in attempt_values:
                    accumulated["outbox_wait_ms"] = attempt_values["outbox_wait_ms"]
                if (
                    state == "sent"
                    and message_id is not None
                    and previous[1] == 0
                    and (json.loads(item[2]).get("action", "text") == "text")
                    and confirmed_at is not None
                ):
                    live_run = await row(
                        conn,
                        "SELECT timings_json FROM dialogue_runs WHERE job_id=? AND mode='live'",
                        (item[3],),
                    )
                    received = timings.clean(live_run[0]).get("received_at") if live_run else None
                    total = (
                        self.clock.since(received, confirmed_at) if received is not None else None
                    )
                    if total is not None:
                        accumulated["total_first_reply_ms"] = total
                accumulated["result_write_ms"] = accumulated.get("result_write_ms", 0) + (
                    timings.elapsed(write_start)
                )
                await conn.execute(
                    "UPDATE outbox SET timings_json=? WHERE id=?",
                    (timings.encode(accumulated), item_id),
                )

    async def claim_typing(self, bot_id: str, media_enabled: bool = False) -> dict[str, Any] | None:
        # One durable job owns typing across media waiting, generation and delivery.
        # Caller holds gate through the bounded transport call and result commit.
        async with self.db.transaction() as conn:
            settings = Settings.model_validate_json(
                (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            if not settings.dialogue_enabled:
                return None
            now = time.time()
            candidate = await row(
                conn,
                "WITH live_jobs(id) AS MATERIALIZED ("
                "SELECT id FROM jobs WHERE kind='telegram' AND state IN ('pending','running') "
                "UNION SELECT job_id FROM outbox WHERE bot_id=? AND state='pending') "
                "SELECT j.id,e.chat_id,json_extract(j.payload,'$.thread_id'),"
                "MIN(j.created_at,COALESCE(r.created_at,j.created_at)),r.id "
                "FROM live_jobs c CROSS JOIN jobs j ON j.id=c.id "
                "JOIN events e ON e.id=j.event_id "
                "LEFT JOIN dialogue_runs r ON r.job_id=j.id "
                "JOIN telegram_access a ON a.bot_id=e.bot_id AND a.subject_id=e.chat_id "
                "AND a.scope=json_extract(j.payload,'$.scope') "
                "WHERE e.bot_id=? AND j.kind='telegram' AND j.typing_state!='rejected' "
                "AND COALESCE(json_extract(j.payload,'$.initiative_mode'),'')!='shadow' "
                "AND NOT (COALESCE(json_extract(j.payload,'$.initiative_mode'),'')='live' "
                "AND j.state='running') "
                "AND j.typing_attempts<64 AND j.typing_due<=? "
                "AND a.state='approved' AND a.version=j.access_version "
                "AND a.member_state NOT IN ('left','kicked','migrated') "
                "AND COALESCE(json_extract(r.snapshot,'$.behavior.plan.action'),'text')='text' "
                "AND ((j.state='running' AND r.state='generating' AND r.mode='live' "
                "AND EXISTS (SELECT 1 FROM model_calls m WHERE m.run_id=r.id AND "
                "m.state='started')) "
                "OR (j.state='done' AND r.state='completed' AND r.mode='live' AND EXISTS "
                "(SELECT 1 FROM outbox o WHERE o.job_id=j.id AND o.state='pending' "
                "AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.job_id=o.job_id "
                "AND earlier.part_index<o.part_index "
                "AND earlier.state IN ('unknown','failed','cancelled','dismissed')))) "
                "OR (? AND j.state='pending' AND r.id IS NULL "
                "AND json_extract(j.payload,'$.service_reply')=0 "
                "AND json_extract(j.payload,'$.trigger') "
                "IN ('private','name','mention','reply','continuation') AND "
                + "((? AND "
                + PHOTO_WAIT
                + ") OR (? AND "
                + RESEARCH_WAIT
                + ") OR (? AND "
                + MEDIA_WAIT
                + ") OR "
                + AVATAR_WAIT
                + ") "
                + "AND EXISTS (SELECT 1 FROM recent_messages source WHERE source.bot_id=e.bot_id "
                "AND source.chat_id=e.chat_id AND "
                "source.message_id=json_extract(j.payload,'$.message_id') "
                "AND source.event_id=j.event_id) "
                "AND NOT EXISTS (SELECT 1 FROM jobs newer JOIN events ne ON ne.id=newer.event_id "
                "WHERE ne.bot_id=e.bot_id AND ne.chat_id=e.chat_id AND newer.id>j.id "
                "AND "
                "json_extract(newer.payload,'$.sender_id')=json_extract(j.payload,'$.sender_id') "
                "AND COALESCE(json_extract(newer.payload,'$.thread_id'),0)="
                "COALESCE(json_extract(j.payload,'$.thread_id'),0) "
                "AND json_extract(newer.payload,'$.trigger') "
                "IN ('private','name','mention','reply','continuation')))) "
                "AND NOT EXISTS (SELECT 1 FROM delivery_limits d WHERE d.bot_id=e.bot_id "
                "AND d.chat_id=e.chat_id AND d.blocked_until>?) "
                "AND NOT EXISTS (SELECT 1 FROM jobs other JOIN events oe ON oe.id=other.event_id "
                "WHERE oe.bot_id=e.bot_id AND oe.chat_id=e.chat_id AND other.id!=j.id "
                "AND COALESCE(json_extract(other.payload,'$.thread_id'),0)="
                "COALESCE(json_extract(j.payload,'$.thread_id'),0) "
                "AND other.typing_at>? AND other.typing_at>COALESCE "
                "((SELECT last_attempt FROM delivery_limits d WHERE d.bot_id=e.bot_id "
                "AND d.chat_id=e.chat_id),0)) ORDER BY j.typing_due,j.id LIMIT 1",
                (
                    bot_id,
                    bot_id,
                    now,
                    media_enabled,
                    settings.photo_enabled,
                    settings.research_enabled,
                    settings.media_enabled,
                    now,
                    now - 4,
                ),
            )
            if not candidate:
                return None
            job_id, chat_id, thread_id, created_at, run_id = candidate
            if now - datetime.fromisoformat(created_at).timestamp() > 600:
                await conn.execute(
                    "UPDATE jobs SET typing_state='expired',typing_due=? WHERE id=?",
                    (now + 86400, job_id),
                )
                return None
            await conn.execute(
                "UPDATE jobs SET typing_state='sending',typing_at=?,typing_due=?,"
                "typing_attempts=typing_attempts+1,typing_error=NULL WHERE id=?",
                (now, now + 4, job_id),
            )
            return {
                "job_id": job_id,
                "run_id": run_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "bot_id": bot_id,
            }

    async def typing_result(
        self, item: dict[str, Any], state: str, error: str | None = None, delay: float = 0
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE jobs SET typing_state=?,typing_error=? WHERE id=?",
                (state, error, item["job_id"]),
            )
            if delay:
                await conn.execute(
                    "INSERT INTO delivery_limits(bot_id,chat_id,blocked_until) "
                    "VALUES (?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE SET "
                    "blocked_until=MAX(blocked_until,excluded.blocked_until)",
                    (item["bot_id"], item["chat_id"], time.time() + delay),
                )

    async def peers(self, bot_id: str, scope: str, page: int) -> dict[str, Any]:
        total = await self.db.one(
            "SELECT COUNT(*) FROM telegram_access WHERE bot_id=? AND scope=?", (bot_id, scope)
        )
        values = await self.db.all(
            "SELECT subject_id,title,username,state,version,updated_at,member_state,"
            "message_batches_enabled FROM "
            "telegram_access "
            "WHERE bot_id=? AND scope=? ORDER BY updated_at DESC,subject_id LIMIT 20 OFFSET ?",
            (bot_id, scope, page * 20),
        )
        return {
            "items": [
                dict(
                    zip(
                        (
                            "id",
                            "title",
                            "username",
                            "state",
                            "version",
                            "updated_at",
                            "member_state",
                            "message_batches_enabled",
                        ),
                        (*item[:-1], bool(item[-1]) if scope == "group" else None),
                        strict=True,
                    )
                )
                for item in values
            ],
            "total": total[0],
            "page": page,
        }

    async def diagnostics(self, bot_id: str) -> dict[str, Any]:
        jobs = await self.db.all(
            "SELECT j.state,COUNT(*) FROM jobs j JOIN events e ON e.id=j.event_id WHERE "
            "e.bot_id=? GROUP BY j.state",
            (bot_id,),
        )
        outgoing = await self.db.all(
            "SELECT state,COUNT(*) FROM outbox WHERE bot_id=? GROUP BY state", (bot_id,)
        )
        events = await self.db.all(
            "SELECT disposition,COUNT(*) FROM events WHERE bot_id=? GROUP BY disposition", (bot_id,)
        )
        unknown = await self.db.all(
            "SELECT id,chat_id,created_at,error_code FROM outbox WHERE bot_id=? AND "
            "state='unknown' ORDER BY id DESC LIMIT 20",
            (bot_id,),
        )
        return {
            "jobs": dict(jobs),
            "outbox": dict(outgoing),
            "events": dict(events),
            "unknown": [
                dict(zip(("id", "chat_id", "created_at", "code"), value, strict=True))
                for value in unknown
            ],
        }

    async def dismiss(self, bot_id: str, item_id: int) -> None:
        async with self.gate, self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE outbox SET state='dismissed' WHERE bot_id=? AND id=? AND state='unknown'",
                (bot_id, item_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError
