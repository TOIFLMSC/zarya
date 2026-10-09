"""Durable dialogue execution and isolated diagnostic runs. No automatic paid retries."""

import asyncio
import json
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from zarya import behavior
from zarya.avatar_logic import AVATAR_WAIT
from zarya.avatar_logic import valid as avatar_valid
from zarya.chat_style import chat_dashes
from zarya.dialogue_logic import (
    answer_mode,
    delivery_chunks,
    instructions,
    message_text,
    model_input,
    topic_id,
    writing_style,
)
from zarya.media_logic import MEDIA_WAIT
from zarya.media_logic import refs_valid as media_refs_valid
from zarya.models import Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.photo_logic import PHOTO_WAIT
from zarya.research_logic import RESEARCH_WAIT, evidence_text
from zarya.response_links import display_text, message_requests_sources, requested
from zarya.source_material import resolve
from zarya.source_material import valid as source_valid
from zarya.telegram_store import TelegramStore, row, stamp

if TYPE_CHECKING:
    from zarya.avatars import AvatarEngine
    from zarya.media import MediaEngine
    from zarya.memory import MemoryEngine
    from zarya.photos import PhotoEngine
    from zarya.research import ResearchEngine


class DialogueEngine:
    def __init__(self, store: TelegramStore, adapter: ModelAdapter | None):
        self.store = store
        self.db = store.db
        self.adapter = adapter
        self.capacity = asyncio.Semaphore(2)
        self.photos: PhotoEngine | None = None
        self.memory: MemoryEngine | None = None
        self.research: ResearchEngine | None = None
        self.media: MediaEngine | None = None
        self.avatars: AvatarEngine | None = None

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.adapter is not None,
            "models": ["gpt-6-luna", "gpt-6.1-sol"],
            "reasoning_levels": ["low", "medium", "high"],
            "max_concurrency": 2,
        }

    async def claim(self, bot_id: str) -> dict[str, Any] | None:
        async with self.store.gate, self.db.transaction() as conn:
            job = await row(
                conn,
                "SELECT j.id,j.payload,j.access_version,e.payload,j.created_at "
                "FROM jobs j JOIN events e "
                "ON e.id=j.event_id WHERE e.bot_id=? AND j.kind='telegram' AND "
                "j.state='pending' AND "
                "NOT "
                + PHOTO_WAIT
                + "AND NOT "
                + RESEARCH_WAIT
                + "AND NOT "
                + MEDIA_WAIT
                + "AND NOT "
                + AVATAR_WAIT
                + "AND "
                "NOT EXISTS (SELECT 1 FROM jobs busy JOIN events be ON be.id=busy.event_id "
                "WHERE (busy.state='running' OR EXISTS (SELECT 1 FROM model_calls m "
                "WHERE m.job_id=busy.id AND m.state='started')) "
                "AND be.bot_id=e.bot_id AND be.chat_id=e.chat_id) "
                "ORDER BY j.id LIMIT 1",
                (bot_id,),
            )
            if not job:
                return None
            job_id, payload_json, access_version, update_json, created_at = job
            payload, update = json.loads(payload_json), json.loads(update_json)
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = payload["chat_id"]
            access = await row(
                conn,
                "SELECT state,version,member_state FROM telegram_access "
                "WHERE bot_id=? AND scope=? AND subject_id=?",
                (bot_id, payload["scope"], chat_id),
            )
            if (
                not access
                or access[0] != "approved"
                or access[1] != access_version
                or access[2] in {"left", "kicked", "migrated"}
            ):
                await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job_id,))
                return {"skip": True}
            settings_row = await row(conn, "SELECT version,document FROM settings WHERE id=1", ())
            settings = Settings.model_validate_json(settings_row[1])
            trigger = payload.get("trigger", "legacy")
            sender_id = str(message.get("from", {}).get("id", ""))
            thread_id = topic_id(message)
            should_answer = trigger in {"private", "name", "mention", "reply", "continuation"}
            if not settings.dialogue_enabled:
                trigger, should_answer = "disabled", False
            elif self.adapter is None:
                trigger, should_answer = "not_configured", False
            if time.time() - datetime.fromisoformat(created_at).timestamp() > 600:
                trigger, should_answer = "expired", False
            current = await row(
                conn,
                "SELECT event_id FROM recent_messages WHERE bot_id=? "
                "AND chat_id=? AND message_id=?",
                (bot_id, chat_id, payload["message_id"]),
            )
            source = await row(conn, "SELECT event_id FROM jobs WHERE id=?", (job_id,))
            if current and current[0] != source[0]:
                trigger, should_answer = "superseded", False
            newer = await row(
                conn,
                "SELECT 1 FROM jobs j JOIN events e ON e.id=j.event_id "
                "WHERE e.bot_id=? AND e.chat_id=? AND j.id>? AND "
                "json_extract(j.payload,'$.sender_id')=? AND "
                "COALESCE(json_extract(j.payload,'$.thread_id'),0)=? AND "
                "json_extract(j.payload,'$.trigger') "
                "IN ('private','name','mention','reply','continuation') "
                "LIMIT 1",
                (bot_id, chat_id, job_id, sender_id, thread_id or 0),
            )
            if newer and should_answer:
                trigger, should_answer = "superseded", False
            behavior_state = None
            if (should_answer or trigger == "ambient") and not payload.get("service_reply"):
                behavior_state = (
                    await behavior.prepare(
                        conn,
                        bot_id,
                        chat_id,
                        thread_id or 0,
                        job_id,
                        payload,
                        message,
                        settings,
                        should_answer,
                    )
                    if self.adapter is not None
                    else None
                )
                if behavior_state and behavior_state["mode"] in {"shadow", "live"}:
                    if time.time() - datetime.fromisoformat(created_at).timestamp() > 90:
                        behavior_state = None
                    else:
                        should_answer, trigger = True, "initiative"
                        payload["initiative_mode"] = behavior_state["mode"]
            material_message = message
            if (
                message.get("voice")
                and message.get("reply_to_message")
                and not (message.get("forward_origin") or message.get("forward_date"))
            ):
                # Own spoken reply is a question about its reply-chain, not a new post.
                material_message = {key: value for key, value in message.items() if key != "voice"}
            material = await resolve(conn, bot_id, chat_id, access_version, material_message)
            avatar = None
            avatar_context = None
            if (
                should_answer
                and trigger != "initiative"
                and self.avatars
                and not payload.get("service_reply")
            ):
                avatar = await self.avatars.prepare(
                    conn,
                    job_id,
                    bot_id,
                    chat_id,
                    payload["scope"],
                    access_version,
                    message,
                    settings,
                    settings_row[0],
                )
                if avatar:
                    if avatar["waiting"]:
                        return {"skip": True}
                    avatar_context = await self.avatars.context(conn, avatar["id"])
                    material = {
                        "messages": [],
                        "manifest": avatar_context.pop("binding"),
                        "unavailable": False,
                    }
            target_ids = [payload["message_id"]] + [m["message_id"] for m in material["messages"]]
            payload["source_message_ids"] = target_ids
            await conn.execute(
                "UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id)
            )
            if (
                should_answer
                and trigger != "initiative"
                and self.media
                and not avatar
                and not payload.get("service_reply")
            ):
                if await self.media.prepare(
                    conn,
                    job_id,
                    bot_id,
                    chat_id,
                    payload["scope"],
                    access_version,
                    target_ids,
                    settings,
                    settings_row[0],
                ):
                    return {"skip": True}
            if (
                should_answer
                and trigger != "initiative"
                and settings.photo_enabled
                and self.photos
                and not avatar
            ):
                await self.photos.prepare_sources(
                    conn, bot_id, chat_id, payload["scope"], access_version, target_ids, settings
                )
                pending = await row(
                    conn,
                    "SELECT 1 FROM photo_batches b JOIN photo_items i ON i.batch_id=b.id "
                    "JOIN photo_messages current ON current.bot_id=i.bot_id "
                    "AND current.chat_id=i.chat_id "
                    "AND current.message_id=i.message_id AND current.event_id=i.event_id "
                    "WHERE b.bot_id=? AND b.chat_id=? AND b.access_version=? "
                    "AND b.state IN ('queued','downloading','analyzing') "
                    "AND i.message_id IN (SELECT value FROM json_each(?)) LIMIT 1",
                    (bot_id, chat_id, access_version, json.dumps(target_ids)),
                )
                if pending:
                    return {"skip": True}
            if (
                self.media
                and settings.media_enabled
                and message.get("voice")
                and not (message.get("forward_origin") or message.get("forward_date"))
            ):
                spoken = await row(
                    conn,
                    "SELECT transcript FROM media_runs WHERE bot_id=? AND chat_id=? "
                    "AND access_version=? AND event_id=? AND kind='voice' "
                    "AND state IN ('completed','partial')",
                    (bot_id, chat_id, access_version, source[0]),
                )
                if spoken and spoken[0]:
                    message = {**message, "text": spoken[0]}
            show_sources = message_requests_sources(message)
            research_context = None
            if (
                self.research
                and not avatar
                and should_answer
                and trigger != "initiative"
                and not payload.get("service_reply")
            ):
                research_material = {
                    **material,
                    "messages": [dict(m) for m in material["messages"]],
                }
                if self.photos and settings.photo_enabled:
                    await self.photos.enrich(
                        conn, bot_id, chat_id, access_version, research_material["messages"]
                    )
                if self.media and settings.media_enabled:
                    await self.media.enrich(
                        conn,
                        bot_id,
                        chat_id,
                        access_version,
                        research_material["messages"],
                        manifest=research_material["manifest"],
                    )
                    # A spoken request is the question; its transcript is not a public search dump.
                    speech_row = await row(
                        conn,
                        "SELECT transcript FROM media_runs WHERE bot_id=? "
                        "AND chat_id=? AND event_id=? AND kind='voice' AND state IN "
                        "('completed','partial')",
                        (bot_id, chat_id, source[0]),
                    )
                    if speech_row and speech_row[0] and not message.get("forward_origin"):
                        message = {**message, "text": speech_row[0]}
                        research_material["messages"] = [
                            m
                            for m in research_material["messages"]
                            if m["message_id"] != message["message_id"]
                        ]
                waiting, research_context = await self.research.prepare(
                    conn,
                    job_id,
                    bot_id,
                    chat_id,
                    payload["scope"],
                    access_version,
                    source[0],
                    message,
                    settings,
                    settings_row[0],
                    research_material,
                )
                if waiting:
                    return {"skip": True}
            async with conn.execute(
                "SELECT message_id,thread_id,sender_id,name,role,text "
                "FROM recent_messages WHERE bot_id=? AND chat_id=? "
                "AND message_id!=? ORDER BY received_at DESC LIMIT 500",
                (bot_id, chat_id, payload["message_id"]),
            ) as cursor:
                recent = list(await cursor.fetchall())
            # Last twenty turns plus last two turns per participant, within a fixed char budget.
            selected: list[Any] = []
            participant_counts: dict[str, int] = {}
            used = 0
            for index, item in enumerate(recent):
                count = participant_counts.get(item[2], 0)
                if index >= 20 and count >= 2:
                    continue
                if used + len(item[5]) > 22000 or len(selected) >= 40:
                    continue
                selected.append(item)
                used += len(item[5])
                participant_counts[item[2]] = count + 1
            context = [
                dict(
                    zip(
                        ("message_id", "thread_id", "sender_id", "name", "role", "text"),
                        item,
                        strict=True,
                    )
                )
                for item in reversed(selected)
            ]
            if trigger == "initiative":
                context = [m for m in context if (m["thread_id"] or 0) == (thread_id or 0)][-20:]
            incoming = {
                "message_id": payload["message_id"],
                "thread_id": thread_id,
                "sender_id": sender_id,
                "name": str(message.get("from", {}).get("first_name", "Участник"))[:80],
                "text": message_text(message),
            }
            photo_refs: list[dict[str, Any]] = []
            selected_material = [dict(m) for m in material["messages"]]
            if self.photos and settings.photo_enabled and not avatar:
                await self.photos.enrich(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    selected_material + [incoming]
                    if selected_material or material["unavailable"]
                    else context + [incoming],
                )
                photo_refs = await self.photos.refs(
                    conn, bot_id, chat_id, access_version, target_ids
                )
            media_refs = []
            if self.media and settings.media_enabled:
                media_refs = await self.media.enrich(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    selected_material + [incoming],
                    question_id=payload["message_id"],
                    manifest=material["manifest"],
                )
            owner = bool(settings.owner_telegram_id and settings.owner_telegram_id == sender_id)
            mode = answer_mode(message_text(message))
            if research_context and mode == "brief":
                mode = "analysis"
            style = writing_style(random.random())
            memory_context: dict[str, Any] = {}
            memory_epoch = None
            if self.memory:
                memory_context, memory_epoch = await self.memory.context(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    [sender_id] + [str(item["sender_id"]) for item in context],
                    query=message_text(message)
                    + " "
                    + str((message.get("reply_to_message") or {}).get("text", ""))[:4000],
                )
            request: dict[str, Any] = {
                "model": settings.model,
                "reasoning": {"effort": settings.reasoning},
                "text": {"verbosity": "medium" if mode == "detailed" else "low"},
                "max_output_tokens": settings.max_output_tokens,
                "store": False,
                "instructions": instructions(
                    settings,
                    owner,
                    mode,
                    bool(self.photos and settings.photo_enabled),
                    style,
                    sources_requested=show_sources,
                    prompts=self.store.prompts,
                ),
                "input": model_input(context, incoming, memory_context),
            }
            if selected_material or material["unavailable"]:
                request["input"] = json.dumps(
                    {
                        **json.loads(request["input"]),
                        "selected_material": {
                            "messages": selected_material,
                            "unavailable": material["unavailable"],
                        },
                    },
                    ensure_ascii=False,
                )
            if research_context:
                request["input"] = json.dumps(
                    {**json.loads(request["input"]), "research": research_context},
                    ensure_ascii=False,
                )
            if avatar_context:
                request["input"] = json.dumps(
                    {**json.loads(request["input"]), "avatar_profile": avatar_context},
                    ensure_ascii=False,
                )
                request["instructions"] += self.store.prompts.text("dialogue.avatar")
            if behavior_state:
                behavior.configure(request, behavior_state, self.store.prompts)
            snapshot = {
                "incoming": incoming,
                "question_text": message_text(message),
                "sources_requested": show_sources,
                "context": context,
                "settings": settings.model_dump(),
                "settings_version": settings_row[0],
                "owner": owner,
                "prompt_bundle_sha256": self.store.prompts.sha256,
                "answer_mode": mode,
                "writing_style": style,
                "delivery_layout": {
                    "split_paragraphs": random.random() < 0.7,
                    "preferred_chars": {"brief": 320, "analysis": 400, "detailed": 550}[mode],
                },
                "request": request,
                "photo_refs": photo_refs,
                "media_refs": media_refs,
                "avatar_id": avatar["id"] if avatar else None,
                "avatar_profile": avatar_context,
                "source_manifest": material["manifest"],
                "source_access_version": access_version,
                "memory": memory_context,
            }
            if memory_epoch is not None:
                snapshot["memory_epoch"] = memory_epoch
            if behavior_state:
                snapshot["behavior"] = behavior_state
            if research_context:
                snapshot["research_id"] = research_context["id"]
                snapshot["research"] = research_context
            cursor = await conn.execute(
                "INSERT INTO dialogue_runs(job_id,bot_id,chat_id,thread_id,"
                "sender_id,message_id,trigger,state,snapshot,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    bot_id,
                    chat_id,
                    thread_id,
                    sender_id,
                    payload["message_id"],
                    trigger,
                    "generating" if should_answer else "skipped",
                    json.dumps(snapshot, ensure_ascii=False),
                    stamp(),
                ),
            )
            run_id = cursor.lastrowid
            if not should_answer and "initiative_draw" in payload:
                await conn.execute(
                    "INSERT INTO behavior_decisions VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        payload.get("initiative_mode", "shadow"),
                        "not_selected",
                        "{}",
                        "Оценка модели не запускалась: вероятностный фильтр "
                        "или устаревшее сообщение",
                        0,
                        time.time(),
                    ),
                )
            if payload.get("service_reply"):
                should_answer = False
                greeting = (
                    payload.get("service_text")
                    or "Я на связи! Напиши мне — давай разберёмся вместе."
                )
                await conn.execute(
                    "INSERT INTO outbox(job_id,bot_id,scope,chat_id,state,payload,"
                    "created_at,access_version) VALUES (?,?,?,?,'pending',?,?,?)",
                    (
                        job_id,
                        bot_id,
                        payload["scope"],
                        chat_id,
                        json.dumps(
                            {
                                "text": greeting,
                                "thread_id": thread_id,
                                "reply_id": payload["message_id"],
                            },
                            ensure_ascii=False,
                        ),
                        stamp(),
                        access_version,
                    ),
                )
                await conn.execute(
                    "UPDATE dialogue_runs SET state='service',response=? WHERE id=?",
                    (greeting, run_id),
                )
            if should_answer:
                await conn.execute(
                    "INSERT INTO model_calls(job_id,provider,model,state,created_at,"
                    "run_id,settings_version,request_json,pricing_profile) "
                    "VALUES (?,'openai',?,'started',?,?,?,?,?)",
                    (
                        job_id,
                        settings.model,
                        stamp(),
                        run_id,
                        settings_row[0],
                        json.dumps(request, ensure_ascii=False),
                        pricing_profile(settings.model),
                    ),
                )
            await conn.execute(
                "UPDATE jobs SET state=?,attempts=attempts+1 WHERE id=?",
                ("running" if should_answer else "done", job_id),
            )
            return {
                "run_id": run_id,
                "job_id": job_id,
                "bot_id": bot_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "scope": payload["scope"],
                "request": request,
                "access_version": access_version,
                "reply_id": payload["message_id"],
                "snapshot": snapshot,
                "skip": not should_answer,
            }

    async def finish(self, work: dict[str, Any], result: ModelResult, latency: int) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            changed = await conn.execute(
                "UPDATE model_calls SET state=?,model=COALESCE(?,model),usage_json=?,response_id=?,"
                "request_id=?,latency_ms=?,cost_usd=?,error_code=?,finished_at=?,pricing_profile=? "
                "WHERE run_id=? AND state='started'",
                (
                    result.state,
                    result.model,
                    json.dumps(result.usage) if result.usage is not None else None,
                    result.response_id,
                    result.request_id,
                    latency,
                    estimate_cost(result, work["request"]["model"]),
                    result.error,
                    stamp(),
                    pricing_profile(result.model or work["request"]["model"]),
                    work["run_id"],
                ),
            )
            if changed.rowcount != 1:
                return
            live = work.get("mode", "live") == "live"
            persisted = await row(
                conn, "SELECT snapshot FROM dialogue_runs WHERE id=?", (work["run_id"],)
            )
            allowed = bool(persisted and not json.loads(persisted[0]).get("invalidated"))
            if live:
                job = await row(conn, "SELECT state FROM jobs WHERE id=?", (work["job_id"],))
                access = await row(
                    conn,
                    "SELECT state,version FROM telegram_access WHERE bot_id=? "
                    "AND scope=? AND subject_id=?",
                    (work["bot_id"], work["scope"], work["chat_id"]),
                )
                allowed = allowed and bool(
                    job
                    and job[0] == "running"
                    and access
                    and access[0] == "approved"
                    and access[1] == work["access_version"]
                )
                settings_row = await row(conn, "SELECT document FROM settings WHERE id=1", ())
                allowed = allowed and Settings.model_validate_json(settings_row[0]).dialogue_enabled
                if self.photos:
                    for ref in work.get("snapshot", {}).get("photo_refs", []):
                        allowed = allowed and await self.photos.valid(conn, ref["batch_id"])
                allowed = allowed and await source_valid(
                    conn,
                    work["bot_id"],
                    work["chat_id"],
                    work["access_version"],
                    work["snapshot"].get("source_manifest", []),
                )
            if allowed and self.memory:
                allowed = await self.memory.valid(conn, work["bot_id"], work["snapshot"])
                if not allowed:
                    result.error = "memory_changed"
            allowed = allowed and await media_refs_valid(
                conn, work["snapshot"].get("media_refs", [])
            )
            allowed = allowed and await avatar_valid(conn, work["snapshot"].get("avatar_id"))
            if not live and work["snapshot"].get("source_manifest"):
                allowed = allowed and await source_valid(
                    conn,
                    work["bot_id"],
                    work["chat_id"],
                    work["snapshot"]["source_access_version"],
                    work["snapshot"]["source_manifest"],
                )
            if self.research and work["snapshot"].get("research_id"):
                allowed = allowed and await self.research.valid(
                    conn, work["snapshot"]["research_id"]
                )
            state = result.state if allowed else "cancelled"
            bstate = work["snapshot"].get("behavior")
            if live and bstate:
                allowed = allowed and await behavior.valid(
                    conn, work["bot_id"], work["chat_id"], bstate
                )
                if not allowed:
                    state = "cancelled"
            plan = work.get("behavior_plan")
            if allowed and result.state == "completed":
                result.text = chat_dashes(display_text(result.text, work["snapshot"]))
                if not result.text.strip() and (not plan or plan.action == "text"):
                    state, result.error = "invalid", "empty_after_link_filter"
                    allowed = False
                if plan and allowed:
                    plan.text = result.text
                    await behavior.commit(conn, work, plan)
            await conn.execute(
                "UPDATE dialogue_runs SET state=?,response=?,error_code=? WHERE id=?",
                (state, result.text[:8000] if allowed else "", result.error, work["run_id"]),
            )
            if not allowed:
                await conn.execute(
                    "UPDATE model_calls SET request_json=NULL WHERE run_id=?", (work["run_id"],)
                )
            if live:
                await conn.execute(
                    "UPDATE jobs SET state=?,lease_until=NULL WHERE id=?",
                    ("done" if result.state == "completed" and allowed else state, work["job_id"]),
                )
                if (
                    result.state == "completed"
                    and allowed
                    and not (bstate and bstate["mode"] == "shadow")
                ):
                    if plan and plan.action == "reaction":
                        await conn.execute(
                            "INSERT INTO outbox(job_id,bot_id,scope,chat_id,state,payload,"
                            "created_at,"
                            "access_version) VALUES (?,?,?,?,'pending',?,?,?)",
                            (
                                work["job_id"],
                                work["bot_id"],
                                work["scope"],
                                work["chat_id"],
                                json.dumps(
                                    {
                                        "action": "reaction",
                                        "emoji": plan.reaction,
                                        "target_message_id": work["reply_id"],
                                        "text": "",
                                        "thread_id": work["thread_id"],
                                    },
                                    ensure_ascii=False,
                                ),
                                stamp(),
                                work["access_version"],
                            ),
                        )
                    if plan and plan.action != "text":
                        return
                    for index, part in enumerate(delivery_chunks(result.text, work["snapshot"])):
                        await conn.execute(
                            "INSERT INTO outbox(job_id,bot_id,scope,chat_id,state,"
                            "payload,created_at,access_version,part_index,next_attempt) "
                            "VALUES (?,?,?,?,'pending',?,?,?,?,?)",
                            (
                                work["job_id"],
                                work["bot_id"],
                                work["scope"],
                                work["chat_id"],
                                json.dumps(
                                    {
                                        "text": part,
                                        "thread_id": work["thread_id"],
                                        "reply_id": work["reply_id"] if index == 0 else None,
                                    },
                                    ensure_ascii=False,
                                ),
                                stamp(),
                                work["access_version"],
                                index,
                                time.time() + 0.8,
                            ),
                        )

    async def execute(self, work: dict[str, Any]) -> None:
        if self.adapter is None:
            return
        started = time.monotonic()
        try:
            if work["snapshot"].get("behavior") and work.get("mode", "live") == "live":
                async with self.db.transaction() as conn:
                    current_behavior = await behavior.valid(
                        conn, work["bot_id"], work["chat_id"], work["snapshot"]["behavior"]
                    )
                if not current_behavior:
                    await self.finish(
                        work, ModelResult(state="cancelled", error="behavior_changed"), 0
                    )
                    return
            async with self.db.transaction() as conn:
                if not await media_refs_valid(
                    conn, work["snapshot"].get("media_refs", [])
                ) or not await avatar_valid(conn, work["snapshot"].get("avatar_id")):
                    invalid_media = True
                else:
                    invalid_media = False
            if invalid_media:
                await self.finish(work, ModelResult(state="cancelled", error="media_changed"), 0)
                return
            if work["snapshot"].get("source_manifest"):
                async with self.store.gate, self.db.transaction() as conn:
                    current_source = await source_valid(
                        conn,
                        work["bot_id"],
                        work["chat_id"],
                        work["snapshot"]["source_access_version"],
                        work["snapshot"]["source_manifest"],
                    )
                if not current_source:
                    await self.finish(
                        work, ModelResult(state="cancelled", error="source_changed"), 0
                    )
                    return
            request = work["request"]
            refs = work.get("snapshot", {}).get("photo_refs", [])
            if self.photos and refs:
                request = await self.photos.attach(request, refs)
            result = await self.adapter.generate(request)
            if work["snapshot"].get("behavior") and result.state == "completed":
                try:
                    plan = behavior.BehaviorPlan.model_validate_json(result.text)
                    if (
                        plan.action == "reaction"
                        and not work["snapshot"]["behavior"]["reactions_enabled"]
                    ):
                        raise ValueError("reactions_disabled")
                    work["behavior_plan"] = plan
                    result.text = plan.text
                except ValueError:
                    result.text, result.state, result.error = "", "invalid", "invalid_behavior_plan"
            research = work.get("snapshot", {}).get("research")
            if research and result.state == "completed":
                try:
                    result.text = evidence_text(
                        result.text, research["sources"], show_links=requested(work["snapshot"])
                    )
                    if len(result.text.encode("utf-16-le")) // 2 > 8000:
                        result.text, result.state, result.error = "", "invalid", "answer_too_long"
                except ValueError:
                    result.text, result.state, result.error = "", "invalid", "unknown_source"
        except asyncio.CancelledError:
            await self.finish(
                work,
                ModelResult(state="unknown", error="interrupted"),
                int((time.monotonic() - started) * 1000),
            )
            raise
        except ValueError:
            result = ModelResult(state="rejected", error="photo_unavailable")
        except Exception:
            result = ModelResult(state="unknown", error="provider_uncertain")
        await self.finish(work, result, int((time.monotonic() - started) * 1000))

    async def details(self, run_id: int) -> dict[str, Any] | None:
        value = await self.db.one(
            "SELECT id,bot_id,chat_id,thread_id,trigger,state,snapshot,response,"
            "error_code,created_at,mode,replay_of,job_id FROM dialogue_runs "
            "WHERE id=?",
            (run_id,),
        )
        if not value:
            return None
        data = dict(
            zip(
                (
                    "id",
                    "bot_id",
                    "chat_id",
                    "thread_id",
                    "trigger",
                    "state",
                    "snapshot",
                    "response",
                    "error",
                    "created_at",
                    "mode",
                    "replay_of",
                    "job_id",
                ),
                value,
                strict=True,
            )
        )
        data["snapshot"] = json.loads(data["snapshot"])
        typing = await self.db.one(
            "SELECT COALESCE(j.typing_state,r.typing_state),"
            "COALESCE(j.typing_attempts,r.typing_attempts),COALESCE(j.typing_at,r.typing_at),"
            "CASE WHEN j.id IS NULL THEN r.typing_error ELSE j.typing_error END "
            "FROM dialogue_runs r "
            "LEFT JOIN jobs j ON j.id=r.job_id WHERE r.id=?",
            (run_id,),
        )
        data["typing"] = dict(zip(("state", "attempts", "at", "error"), typing, strict=True))
        call = await self.db.one(
            "SELECT model,state,usage_json,latency_ms,cost_usd,pricing_profile,"
            "error_code,request_id FROM model_calls WHERE run_id=?",
            (run_id,),
        )
        data["call"] = (
            dict(
                zip(
                    (
                        "model",
                        "state",
                        "usage",
                        "latency_ms",
                        "cost_usd",
                        "pricing",
                        "error",
                        "request_id",
                    ),
                    call,
                    strict=True,
                )
            )
            if call
            else None
        )
        if data["call"] and data["call"]["usage"]:
            data["call"]["usage"] = json.loads(data["call"]["usage"])
        parts = await self.db.all(
            "SELECT part_index,state,payload,message_id,error_code FROM outbox "
            "WHERE job_id=? ORDER BY part_index",
            (data["job_id"],),
        )
        data["parts"] = [
            {
                "index": p[0],
                "state": p[1],
                "text": json.loads(p[2]).get("text", ""),
                "action": json.loads(p[2]).get("action", "text"),
                "emoji": json.loads(p[2]).get("emoji"),
                "message_id": p[3],
                "error": p[4],
            }
            for p in parts
        ]
        if data["mode"] != "live":
            data["parts"] = [
                {"index": i, "state": "test_only", "text": p, "message_id": None, "error": None}
                for i, p in enumerate(delivery_chunks(data["response"] or "", data["snapshot"]))
            ]
        return data

    async def listing(self, page: int, scope: str, bot_id: str | None) -> dict[str, Any]:
        condition = " WHERE bot_id=?" if bot_id else " WHERE 0"
        args: tuple[object, ...] = (bot_id,) if bot_id else ()
        if scope != "all":
            condition += " AND " + (
                "chat_id LIKE '-%'" if scope == "group" else "chat_id NOT LIKE '-%'"
            )
        values = await self.db.all(
            "SELECT id,chat_id,thread_id,trigger,state,created_at,mode,"
            "json_extract(snapshot,'$.incoming.text') FROM dialogue_runs"
            + condition
            + " ORDER BY id DESC LIMIT 20 OFFSET ?",
            args + (page * 20,),
        )
        total = await self.db.one("SELECT COUNT(*) FROM dialogue_runs" + condition, args)
        cost = await self.db.one(
            "SELECT SUM(m.cost_usd),SUM(CASE WHEN m.cost_usd IS NULL THEN 1 "
            "ELSE 0 END) FROM model_calls m JOIN dialogue_runs r ON r.id=m.run_id "
            "WHERE r.bot_id=?",
            (bot_id,),
        )
        return {
            "items": [
                dict(
                    zip(
                        (
                            "id",
                            "chat_id",
                            "thread_id",
                            "trigger",
                            "state",
                            "created_at",
                            "mode",
                            "input",
                        ),
                        p,
                        strict=True,
                    )
                )
                for p in values
            ],
            "total": total[0],
            "page": page,
            "known_cost_usd": cost[0],
            "unknown_cost_calls": cost[1] or 0,
            **self.status(),
        }

    async def replay(
        self, run_id: int, mode: str, expected_settings_version: int | None = None
    ) -> dict[str, Any]:
        original = await self.details(run_id)
        if not original:
            raise ValueError("Операция не найдена")
        if mode == "paid" and self.adapter is None:
            raise ValueError("Ключ OpenAI не настроен")
        if (
            mode == "recorded"
            and not original["response"]
            and not original["snapshot"].get("behavior", {}).get("plan")
        ):
            raise ValueError("В этой операции нет сохранённого ответа")
        saved = original["snapshot"]
        recorded_text = (
            display_text(original["response"] or "", saved) if mode == "recorded" else None
        )
        if (
            mode == "recorded"
            and not recorded_text
            and saved.get("behavior", {}).get("plan", {}).get("action", "text") == "text"
        ):
            raise ValueError("После удаления непрошенных ссылок в сохранённом ответе нет текста")
        if saved.get("invalidated"):
            raise ValueError("Повторение недоступно: контекст операции удалён или исправлен")
        if self.research and saved.get("research_id"):
            async with self.db.transaction() as conn:
                if not await self.research.valid(conn, saved["research_id"]):
                    raise ValueError("Источник изменился; отправьте новое сообщение")
        if self.memory and "memory_epoch" in saved:
            async with self.db.transaction() as conn:
                if not await self.memory.valid(conn, original["bot_id"], saved):
                    raise ValueError("Память изменилась; для проверки отправьте новое сообщение")
        if mode == "paid":
            settings = await self.db.settings()
            if expected_settings_version != settings.version:
                raise ValueError("Настройки изменились. Откройте параметры платного теста заново")
            reply_mode = answer_mode(saved.get("question_text", saved["incoming"]["text"]))
            style = writing_style(random.random())
            saved = {
                **saved,
                "answer_mode": reply_mode,
                "writing_style": style,
                "delivery_layout": {
                    "split_paragraphs": random.random() < 0.7,
                    "preferred_chars": {"brief": 320, "analysis": 400, "detailed": 550}[reply_mode],
                },
                "settings": settings.settings.model_dump(),
                "settings_version": settings.version,
                "prompt_bundle_sha256": self.store.prompts.sha256,
                "request": {
                    **saved["request"],
                    "model": settings.settings.model,
                    "reasoning": {"effort": settings.settings.reasoning},
                    "text": {"verbosity": "medium" if reply_mode == "detailed" else "low"},
                    "max_output_tokens": settings.settings.max_output_tokens,
                    "instructions": instructions(
                        settings.settings,
                        saved["owner"],
                        reply_mode,
                        bool(self.photos and settings.settings.photo_enabled),
                        style,
                        sources_requested=requested(saved),
                        prompts=self.store.prompts,
                    ),
                },
            }
            if self.photos and saved.get("photo_refs"):
                # Validate assets and current permissions before creating a paid call record.
                await self.photos.attach(saved["request"], saved["photo_refs"])
            if saved.get("behavior"):
                saved["behavior"] = {
                    **saved["behavior"],
                    "applied": False,
                    "reactions_enabled": settings.settings.reactions_enabled,
                    "expressiveness": settings.settings.expressiveness,
                    "emotional_max_parts": settings.settings.emotional_max_parts,
                }
                saved["behavior"].pop("plan", None)
                saved["behavior"].pop("committed_version", None)
                saved["request"]["text"].pop("format", None)
                if settings.settings.behavior_enabled:
                    behavior.configure(saved["request"], saved["behavior"], self.store.prompts)
                else:
                    saved.pop("behavior")
        if mode == "paid" and saved.get("avatar_id"):
            saved["request"]["instructions"] += self.store.prompts.text("dialogue.avatar")
        async with self.capacity:
            async with self.db.transaction() as conn:
                if not await avatar_valid(conn, saved.get("avatar_id")):
                    raise ValueError("Контекст аватарки недоступен; отправь новый запрос")
                if not await media_refs_valid(conn, saved.get("media_refs", [])):
                    raise ValueError("Медиа недоступно; отправьте новое сообщение")
                if saved.get("source_manifest"):
                    if not await source_valid(
                        conn,
                        original["bot_id"],
                        original["chat_id"],
                        saved["source_access_version"],
                        saved["source_manifest"],
                    ):
                        raise ValueError("Источник изменился; повторение недоступно")
                if (
                    self.research
                    and saved.get("research_id")
                    and not await self.research.valid(conn, saved["research_id"])
                ):
                    raise ValueError("Источник изменился; повторение недоступно")
                if self.memory and not await self.memory.valid(conn, original["bot_id"], saved):
                    raise ValueError("Память изменилась; повторение недоступно")
                cursor = await conn.execute(
                    "INSERT INTO dialogue_runs(bot_id,chat_id,thread_id,"
                    "sender_id,message_id,trigger,state,snapshot,created_at,"
                    "replay_of,mode,response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        original["bot_id"],
                        original["chat_id"],
                        original["thread_id"],
                        saved["incoming"]["sender_id"],
                        saved["incoming"]["message_id"],
                        "replay",
                        "generating" if mode == "paid" else "completed",
                        json.dumps(saved, ensure_ascii=False),
                        stamp(),
                        run_id,
                        mode,
                        recorded_text,
                    ),
                )
                new_id = cursor.lastrowid
                if mode == "paid":
                    await conn.execute(
                        "INSERT INTO model_calls(provider,model,state,created_at,run_id,"
                        "settings_version,request_json,pricing_profile) VALUES "
                        "('openai',?,'started',?,?,?,?,?)",
                        (
                            saved["request"]["model"],
                            stamp(),
                            new_id,
                            saved["settings_version"],
                            json.dumps(saved["request"], ensure_ascii=False),
                            pricing_profile(saved["request"]["model"]),
                        ),
                    )
            if mode == "paid":
                await self.execute(
                    {
                        "run_id": new_id,
                        "mode": mode,
                        "bot_id": original["bot_id"],
                        "chat_id": original["chat_id"],
                        "request": saved["request"],
                        "snapshot": saved,
                    }
                )
        assert new_id is not None
        result = await self.details(new_id)
        assert result is not None
        return result

    async def close(self) -> None:
        if self.adapter:
            await self.adapter.close()
