"""Durable dialogue execution and isolated diagnostic runs. No automatic paid retries."""

import asyncio
import json
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from zarya import behavior, context_budget, message_batches, message_links, timings, verbatim_quote
from zarya.avatar_logic import AVATAR_WAIT
from zarya.avatar_logic import valid as avatar_valid
from zarya.chat_style import chat_dashes
from zarya.dialogue_logic import (
    TOPIC_CONTEXT_POLICY,
    answer_mode,
    delivery_chunks,
    explicit_reply,
    instructions,
    message_text,
    model_input,
    topic_id,
    writing_style,
)
from zarya.media_logic import MEDIA_WAIT
from zarya.media_logic import refs_valid as media_refs_valid
from zarya.memory_e5 import query_identity
from zarya.memory_logic import empty_snapshot
from zarya.models import Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.photo_logic import PHOTO_WAIT
from zarya.research_intent import (
    needs_forwarded_text,
    speaker_text,
    text_factcheck,
    usable_post_text,
)
from zarya.research_logic import RESEARCH_WAIT, research_answer_text, source_failure_text
from zarya.research_plan import answer_mode as research_answer_mode
from zarya.research_plan import answer_policy as research_answer_policy
from zarya.response_links import (
    display_text,
    message_requests_permalink,
    message_requests_sources,
    requested,
)
from zarya.source_material import FORWARD_POLICY, accepted, resolve
from zarya.source_material import valid as source_valid
from zarya.telegram_content import forwarded
from zarya.telegram_store import TelegramStore, row, stamp

if TYPE_CHECKING:
    from zarya.avatars import AvatarEngine
    from zarya.media import MediaEngine
    from zarya.memory import MemoryEngine
    from zarya.photos import PhotoEngine
    from zarya.research import ResearchEngine


PENDING_DIALOGUE = (
    "SELECT j.id,j.payload,j.access_version,e.payload,j.created_at,e.received_at "
    "FROM jobs j JOIN events e ON e.id=j.event_id "
    "LEFT JOIN text_batches b ON b.job_id=j.id "
    "WHERE e.bot_id=? AND COALESCE(b.ready_at,json_extract(j.payload,'$.post_wait_until'),0)<=? "
    "AND j.kind='telegram' AND j.state='pending' AND "
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
    "WHERE busy.state='running' "
    "AND be.bot_id=e.bot_id AND be.chat_id=e.chat_id) "
    "AND NOT EXISTS (SELECT 1 FROM model_calls m "
    "JOIN jobs busy ON busy.id=m.job_id JOIN events be ON be.id=busy.event_id "
    "WHERE m.state='started' AND be.bot_id=e.bot_id AND be.chat_id=e.chat_id) "
    "ORDER BY j.id LIMIT 1"
)


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
        prepared_job = None
        semantic = None
        if self.memory:
            prepared_job, semantic = await self.memory.prepare_search(bot_id, PENDING_DIALOGUE)
        async with self.store.gate, self.db.transaction() as conn:
            claimed_at = time.time()
            claim_start = time.monotonic()
            measured: dict[str, float] = {}
            job = await row(conn, PENDING_DIALOGUE, (bot_id, claimed_at))
            if not job:
                return None
            job_id, payload_json, access_version, update_json, created_at, received_at = job
            received = datetime.fromisoformat(received_at).timestamp()
            measured["received_at"] = received
            queue_ms = self.store.clock.since(received, claimed_at)
            if queue_ms is not None:
                measured["queue_ms"] = queue_ms
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
            if trigger == "shared_post":
                trigger = "private"
            sender_id = str(message.get("from", {}).get("id", ""))
            thread_id = topic_id(message)
            quote = verbatim_quote.match(
                message, settings.display_name, bot_id, payload.get("bot_username", "")
            )
            should_answer = trigger in {"private", "name", "mention", "reply", "continuation"}
            if not settings.dialogue_enabled:
                trigger, should_answer = "disabled", False
            elif self.adapter is None and not quote:
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
            batch = await message_batches.freeze(conn, job_id, bot_id, chat_id, access_version)
            if batch is not None and (
                not batch
                or batch[0]["message_id"] != payload["message_id"]
                or batch[0]["event_id"] != source[0]
            ):
                await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job_id,))
                return {"skip": True}
            current_messages = batch if batch and len(batch) > 1 else []
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
            if newer and should_answer and batch is None and not payload.get("keep_addressed"):
                trigger, should_answer = "superseded", False
            if quote and should_answer and not payload.get("service_reply"):
                measured["live_text"] = 1
                measured["claim_ms"] = timings.elapsed(claim_start)
                return await verbatim_quote.enqueue(
                    conn,
                    bot=bot_id,
                    payload=payload,
                    grant=access_version,
                    event=source[0],
                    job=job_id,
                    message=message,
                    trigger=trigger,
                    settings_version=settings_row[0],
                    measured=measured,
                    include_links=bool(quote.group("links")),
                )
            prompts = await self.store.prompts_for(settings_row[0], conn)
            behavior_state = None
            if (should_answer or trigger == "ambient") and not (
                payload.get("service_reply") or current_messages
            ):
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
            reply = explicit_reply(message)
            if (
                message.get("voice")
                and reply
                and not (message.get("forward_origin") or message.get("forward_date"))
            ):
                # Own spoken reply is a question about its reply-chain, not a new post.
                material_message = {key: value for key, value in message.items() if key != "voice"}
            material = await resolve(conn, bot_id, chat_id, access_version, material_message)
            paired = payload.get("post_question")
            if paired:
                accepted_question = await accepted(
                    conn, bot_id, chat_id, access_version, paired["message_id"]
                )
                if (
                    not accepted_question
                    or accepted_question[0] != paired["event_id"]
                    or not isinstance(accepted_question[1].get("text"), str)
                ):
                    await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job_id,))
                    return {"skip": True}
                material["manifest"].append(paired)
                envelope = ("message_id", "chat", "date", "from", "message_thread_id")
                message = {
                    **{key: message[key] for key in envelope if key in message},
                    "text": accepted_question[1]["text"],
                    "entities": accepted_question[1].get("entities", []),
                }
            forwarded_post = bool(
                paired
                or forwarded(material_message)
                or forwarded(reply)
                or "rich_message" in reply
                or any(item["forwarded"] for item in material["messages"])
            )
            post_unreadable = (
                should_answer
                and not payload.get("service_reply")
                and not message_requests_permalink(message)
                and (
                    forwarded_post
                    or payload["scope"] == "private" and needs_forwarded_text(message)
                )
                and not any(
                    usable_post_text(item["text"]) or item["attachments"] or item["urls"]
                    for item in material["messages"]
                )
            )
            if post_unreadable:
                payload["service_reply"] = True
                payload["service_text"] = (
                    "Не получилось прочитать пересланный пост. "
                    "Пришли его текст, и я отвечу по нему."
                )
                should_answer, behavior_state = False, None
            text_first = text_factcheck(message) and any(
                usable_post_text(item["text"])
                and (paired or item["message_id"] != message["message_id"])
                for item in material["messages"]
            )
            link_route_allowed = (
                should_answer
                and trigger != "initiative"
                and not (payload.get("service_reply") or current_messages)
            )
            find_message_link = link_route_allowed and message_requests_permalink(message)
            avatar = None
            avatar_context = None
            if (
                should_answer
                and trigger != "initiative"
                and self.avatars
                and not find_message_link
                and not (payload.get("service_reply") or current_messages)
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
            research_engine = (
                self.research
                if (
                    not avatar
                    and should_answer
                    and trigger != "initiative"
                    and not (payload.get("service_reply") or current_messages)
                )
                else None
            )
            research_context = None
            if research_engine and text_first and not find_message_link:
                waiting, research_context = await research_engine.prepare(
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
                    material,
                    text_only=True,
                )
                if waiting:
                    return {"skip": True}
            payload.pop("post_text_first", None)
            payload["source_message_ids"] = target_ids
            await conn.execute(
                "UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id)
            )
            if (
                should_answer
                and trigger != "initiative"
                and self.media
                and not avatar
                and not find_message_link
                and not (payload.get("service_reply") or current_messages)
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
                and not find_message_link
                and not current_messages
            ):
                await self.photos.prepare_sources(
                    conn, bot_id, chat_id, payload["scope"], access_version, target_ids, settings,
                    job_id=job_id,
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
                    find_message_link = link_route_allowed and message_requests_permalink(message)
            show_sources = not current_messages and message_requests_sources(message)
            if research_engine and not text_first and not find_message_link:
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
                waiting, research_context = await research_engine.prepare(
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
            from zarya.participant_context import select as select_participant
            from zarya.participant_context import topic_history

            pilot_thread = (
                (thread_id or 0)
                if should_answer
                and trigger != "initiative"
                and chat_id == settings.topic_context_chat_id
                and message.get("chat", {}).get("is_forum")
                and not payload.get("service_reply")
                else None
            )
            context, participant_coverage, context_refs = (
                ([], {}, {"humans": [], "assistants": [], "media": [], "summaries": []})
                if post_unreadable
                else await select_participant(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    message,
                    settings,
                    enqueue_summaries=should_answer
                    and trigger != "initiative"
                    and not payload.get("service_reply")
                    and not (behavior_state and behavior_state.get("mode") == "shadow"),
                    thread=pilot_thread,
                )
            )
            incoming = {
                "message_id": payload["message_id"],
                "thread_id": thread_id,
                "sender_id": sender_id,
                "name": str(message.get("from", {}).get("first_name", "Участник"))[:80],
                "text": message_text(message),
            }
            photo_refs: list[dict[str, Any]] = []
            selected_material = [dict(m) for m in material["messages"]]
            if (
                self.photos
                and settings.photo_enabled
                and not avatar
                and not find_message_link
                and not post_unreadable
            ):
                await self.photos.enrich(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    (selected_material if text_first else selected_material + [incoming])
                    if selected_material or material["unavailable"]
                    else context + [incoming],
                )
                photo_refs = await self.photos.refs(
                    conn, bot_id, chat_id, access_version, target_ids
                )
            media_refs = []
            if self.media and settings.media_enabled and not (find_message_link or post_unreadable):
                media_refs = await self.media.enrich(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    selected_material if text_first else selected_material + [incoming],
                    question_id=payload["message_id"],
                    manifest=material["manifest"],
                )
            owner = bool(settings.owner_telegram_id and settings.owner_telegram_id == sender_id)
            question_text = speaker_text(message) if research_context else message_text(message)
            mode = answer_mode(question_text)
            if research_context:
                mode = research_answer_mode(question_text, mode, research_context)
            style = (
                "relaxed"
                if research_context and mode != "brief"
                else writing_style(random.random())
            )
            media_refs = list({r["id"]: r for r in media_refs + context_refs["media"]}.values())
            task_state = None
            if (
                should_answer
                and trigger != "initiative"
                and settings.memory_enabled
                and not payload.get("service_reply")
                and not avatar
            ):
                from zarya.task_state import context as task_context

                task_state = await task_context(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    thread_id or 0,
                    [m["message_id"] for m in current_messages] or [incoming["message_id"]],
                )
            memory_context: dict[str, Any] = {}
            memory_epoch = None
            retrieval: dict[str, Any] = {}
            if self.memory:
                context_start = time.monotonic()
                question = "\n".join(m["text"] for m in current_messages) or message_text(message)
                memory_context, memory_epoch = await self.memory.context(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    [sender_id] + [str(item["sender_id"]) for item in context],
                    query=question + " " + str(reply.get("text", ""))[:4000],
                    current_query=question,
                    recent=context,
                    event_id=source[0],
                    speaker=sender_id,
                    thread=thread_id,
                    diagnostics=retrieval,
                    semantic=(
                        semantic
                        if prepared_job
                        == (settings_row[0], job, query_identity(message, current_messages))
                        else None
                    ),
                )
                measured["context_ms"] = timings.elapsed(context_start)
            topic_context: list[dict[str, Any]] = []
            if pilot_thread is not None:
                selected_by_id = {m["message_id"]: m for m in selected_material}
                personal_context = []
                for item in context:
                    selected = selected_by_id.get(item["message_id"])
                    if item["role"] == "user" and selected and selected["text"] == item["text"]:
                        selected.update(
                            name=item["name"],
                            sender_id=item["sender_id"],
                            received_at=item["received_at"],
                        )
                    else:
                        personal_context.append(item)
                context = personal_context
                participant_coverage["characters"] = sum(len(m["text"]) for m in context)
                topic_context, topic_refs = await topic_history(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    pilot_thread,
                    incoming["message_id"],
                    {r["message_id"] for r in context_refs["humans"]} | selected_by_id.keys(),
                )
                context_refs["humans"].extend(topic_refs)
            request: dict[str, Any] = {
                "model": settings.model,
                "reasoning": {"effort": settings.reasoning},
                "text": {
                    "verbosity": "medium"
                    if mode == "detailed" or mode == "analysis" and research_context
                    else "low"
                },
                "max_output_tokens": settings.max_output_tokens,
                "store": False,
                "instructions": instructions(
                    settings,
                    owner,
                    mode,
                    bool(self.photos and settings.photo_enabled),
                    style,
                    sources_requested=show_sources,
                    prompts=prompts,
                ),
                "input": model_input(
                    context, incoming, memory_context, topic_context=topic_context
                ),
            }
            if topic_context:
                request["instructions"] += TOPIC_CONTEXT_POLICY
            message_link_replacements = None
            if find_message_link:
                link_context, message_link_replacements = await message_links.prepare(
                    conn,
                    bot_id,
                    chat_id,
                    access_version,
                    message,
                    material["manifest"],
                    context_refs,
                )
                request["instructions"] += message_links.POLICY
                request["input"] = json.dumps(
                    {**json.loads(request["input"]), **link_context}, ensure_ascii=False
                )
            if task_state:
                from zarya.task_state import USE

                request["instructions"] += USE
                request["input"] = json.dumps(
                    {
                        **json.loads(request["input"]),
                        "task_state": {
                            "for_message_ids": task_state["for_message_ids"],
                            "fields": task_state["fields"],
                        },
                    },
                    ensure_ascii=False,
                )
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
                request["instructions"] += (
                    "\nСначала разбери текст выбранного поста и ответь на текущий вопрос. "
                    "Затем учти, что показывают вложения. Для проверки написанной цитаты "
                    "используй результаты поиска по тексту. Если ролик не относится к цитате "
                    "или недоступен, дай результат проверки текста и обозначь границы вывода."
                )
                if forwarded_post:
                    request["instructions"] += FORWARD_POLICY
            if research_context:
                request["instructions"] += research_answer_policy(research_context)
                request["input"] = json.dumps(
                    {**json.loads(request["input"]), "research": research_context},
                    ensure_ascii=False,
                )
            if avatar_context:
                request["input"] = json.dumps(
                    {**json.loads(request["input"]), "avatar_profile": avatar_context},
                    ensure_ascii=False,
                )
                request["instructions"] += prompts.text("dialogue.avatar")
            if behavior_state:
                behavior.configure(request, behavior_state, prompts)
            if current_messages:
                message_batches.configure(request, current_messages)
                material["manifest"].extend(
                    {"message_id": m["message_id"], "event_id": m["event_id"]}
                    for m in current_messages
                )
            request = context_budget.pack(request)
            budget = context_budget.inspect(request)
            budget.update(
                stage="assembled", decision="pending" if should_answer else "not_requested"
            )
            if photo_refs:
                budget["state"] = "preparing"
            snapshot = {
                # Snapshot is assembled inside the ingestion gate/write transaction.
                # A later source revision cannot have influenced an earlier watermark.
                "context_event_watermark": (
                    await row(conn, "SELECT COALESCE(MAX(id),0) FROM events", ())
                )[0],
                "incoming": incoming,
                "question_text": question_text,
                "sources_requested": show_sources,
                **(
                    {"message_link_replacements": message_link_replacements}
                    if message_link_replacements is not None
                    else {}
                ),
                "context": context,
                **({"current_messages": current_messages} if current_messages else {}),
                **({"topic_context": topic_context} if topic_context else {}),
                **({"task_state": task_state} if task_state else {}),
                **({"participant_context": participant_coverage} if participant_coverage else {}),
                "context_refs": context_refs,
                "settings": settings.model_dump(),
                "settings_version": settings_row[0],
                "owner": owner,
                "prompt_bundle_sha256": prompts.sha256,
                "answer_mode": mode,
                "writing_style": style,
                "delivery_layout": {
                    "split_paragraphs": random.random() < 0.7,
                    "preferred_chars": {"brief": 320, "analysis": 400, "detailed": 550}[mode],
                },
                "request": request,
                "context_budget": budget,
                "photo_refs": photo_refs,
                "media_refs": media_refs,
                "avatar_id": avatar["id"] if avatar else None,
                "avatar_profile": avatar_context,
                "source_manifest": material["manifest"],
                **({"forwarded_post": True} if forwarded_post else {}),
                **({"post_unreadable": True} if post_unreadable else {}),
                "source_access_version": access_version,
                "memory": memory_context,
                "retrieval_chat_id": chat_id,
            }
            if retrieval:
                snapshot["retrieval"] = retrieval
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
            assert run_id is not None
            from zarya.participant_context import register

            await register(conn, run_id, snapshot)
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
                    "run_id,settings_version,request_json,pricing_profile,operation) "
                    "VALUES (?,'openai',?,'started',?,?,?,?,?,'preflight')",
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
            measured["live_text"] = float(
                should_answer and payload.get("initiative_mode") != "shadow"
            )
            measured["claim_ms"] = timings.elapsed(claim_start)
            await conn.execute(
                "UPDATE dialogue_runs SET timings_json=? WHERE id=?",
                (timings.encode(measured), run_id),
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
                "timings": measured,
                "skip": not should_answer,
            }

    async def finish(self, work: dict[str, Any], result: ModelResult, latency: int) -> None:
        work["_finish_start"] = time.monotonic()
        async with (
            self.store.gate,
            self.db.transaction() as conn,
            timings.finishing(conn, work),
        ):
            boundary = await row(
                conn, "SELECT operation FROM model_calls WHERE run_id=?", (work["run_id"],)
            )
            preflight_only = bool(boundary and boundary[0] == "preflight")
            if preflight_only and result.state == "unknown":
                result.state = "rejected"
                result.error = (
                    "preflight_interrupted"
                    if result.error == "interrupted"
                    else "preflight_uncertain"
                )
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
                    None
                    if preflight_only
                    else int(work.get("timings", {}).get("model_ms", latency)),
                    0 if preflight_only else estimate_cost(result, work["request"]["model"]),
                    result.error,
                    stamp(),
                    pricing_profile(result.model or work["request"]["model"]),
                    work["run_id"],
                ),
            )
            if changed.rowcount != 1:
                return
            work["_finish_claimed"] = True
            live = work.get("mode", "live") == "live"
            plan = work.get("behavior_plan")
            work.setdefault("timings", {})["live_text"] = float(
                live
                and work["snapshot"].get("behavior", {}).get("mode") != "shadow"
                and (plan is None or plan.action == "text")
            )
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
            from zarya.memory_context import valid_snapshot

            if allowed and not await valid_snapshot(conn, work["bot_id"], work["snapshot"]):
                allowed = False
                result.error = "context_changed"
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
                if not work.get("batch_answers"):
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
                service_text = context_budget.SERVICES.get(work.get("service_error", ""))
                if work.get("service_error") == "invalid_batch_reply":
                    service_text = message_batches.INVALID_RESPONSE
                if result.state == "invalid" and result.error == "unknown_source":
                    service_text = source_failure_text(
                        work["snapshot"].get("research") or {},
                        bool(
                            self.research
                            and work["snapshot"].get("research_id")
                            and requested(work["snapshot"])
                        ),
                    )
                if (
                    allowed
                    and service_text
                    and work["snapshot"].get("behavior", {}).get("mode") not in {"shadow", "live"}
                    and work["snapshot"].get("incoming", {}).get("sender_id")
                ):
                    await conn.execute(
                        "INSERT INTO outbox(job_id,bot_id,scope,chat_id,state,payload,created_at,"
                        "access_version) VALUES (?,?,?,?,'pending',?,?,?)",
                        (
                            work["job_id"],
                            work["bot_id"],
                            work["scope"],
                            work["chat_id"],
                            json.dumps(
                                {
                                    "text": service_text,
                                    "thread_id": work["thread_id"],
                                    "reply_id": work["reply_id"],
                                },
                                ensure_ascii=False,
                            ),
                            stamp(),
                            work["access_version"],
                        ),
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
                    if work.get("batch_answers"):
                        pieces = [
                            (target, covers, part)
                            for target, covers, text in work["batch_answers"]
                            for part in delivery_chunks(text, work["snapshot"])
                        ]
                    else:
                        pieces = [
                            (work["reply_id"] if index == 0 else None, [], part)
                            for index, part in enumerate(
                                delivery_chunks(result.text, work["snapshot"])
                            )
                        ]
                    for index, (reply_to, covers, part) in enumerate(pieces):
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
                                        "reply_id": reply_to,
                                        **({"covers": covers} if covers else {}),
                                    },
                                    ensure_ascii=False,
                                ),
                                stamp(),
                                work["access_version"],
                                index,
                                time.time() + 0.8,
                            ),
                        )

    async def request_current(self, conn: Any, work: dict[str, Any]) -> bool:
        """Final guard before each network transmission, and after slow counting/preparation."""
        from zarya.memory_context import valid_snapshot

        persisted = await row(
            conn, "SELECT state,snapshot FROM dialogue_runs WHERE id=?", (work["run_id"],)
        )
        if (
            not persisted
            or persisted[0] != "generating"
            or json.loads(persisted[1]).get("invalidated")
        ):
            return False
        if not await valid_snapshot(conn, work["bot_id"], work["snapshot"]):
            return False
        access = await row(
            conn,
            "SELECT state,version,member_state FROM telegram_access "
            "WHERE bot_id=? AND subject_id=?",
            (work["bot_id"], work["chat_id"]),
        )
        if (
            not access
            or access[0] != "approved"
            or access[1] != work["snapshot"].get("source_access_version")
            or access[2] in {"left", "kicked", "migrated"}
        ):
            return False
        if self.memory and not await self.memory.valid(conn, work["bot_id"], work["snapshot"]):
            return False
        if not await source_valid(
            conn,
            work["bot_id"],
            work["chat_id"],
            work["snapshot"]["source_access_version"],
            work["snapshot"].get("source_manifest", []),
        ):
            return False
        if not await media_refs_valid(
            conn, work["snapshot"].get("media_refs", [])
        ) or not await avatar_valid(conn, work["snapshot"].get("avatar_id")):
            return False
        if (
            self.research
            and work["snapshot"].get("research_id")
            and not await self.research.valid(conn, work["snapshot"]["research_id"])
        ):
            return False
        if self.photos:
            for ref in work["snapshot"].get("photo_refs", []):
                if not await self.photos.valid(conn, ref["batch_id"]):
                    return False
        if work.get("mode", "live") == "live":
            job = await row(conn, "SELECT state FROM jobs WHERE id=?", (work["job_id"],))
            if not job or job[0] != "running":
                return False
            current = Settings.model_validate_json(
                (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            if not current.dialogue_enabled:
                return False
            b = work["snapshot"].get("behavior")
            if b and not await behavior.valid(conn, work["bot_id"], work["chat_id"], b):
                return False
        return True

    async def save_budget(self, work: dict[str, Any], budget: dict[str, Any]) -> bool:
        async with self.store.gate, self.db.transaction() as conn:
            if not await self.request_current(conn, work):
                return False
            work["snapshot"]["context_budget"] = budget
            await conn.execute(
                "UPDATE dialogue_runs SET snapshot=json_set(snapshot,'$.context_budget',json(?)) "
                "WHERE id=?",
                (json.dumps(budget), work["run_id"]),
            )
            if budget["provider_attempted"]:
                await conn.execute(
                    "UPDATE model_calls SET operation='dialogue' "
                    "WHERE run_id=? AND state='started'",
                    (work["run_id"],),
                )
            elif budget["api_count"]["state"] == "started":
                await conn.execute(
                    "INSERT OR IGNORE INTO model_calls(provider,model,state,created_at,"
                    "context_count_run_id,settings_version,operation) "
                    "VALUES ('openai',?,'started',?,?,?,'context_count')",
                    (
                        work["request"]["model"],
                        stamp(),
                        work["run_id"],
                        work["snapshot"]["settings_version"],
                    ),
                )
            return True

    async def finish_count(self, work: dict[str, Any], count: Any, latency: float) -> None:
        # Accounting/status only, never private count-payloads or fabricated billed usage.
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state=?,error_code=?,latency_ms=?,finished_at=? "
                "WHERE context_count_run_id=? AND state='started'",
                (count.state, count.error, int(latency), stamp(), work["run_id"]),
            )

    async def preflight(self, work: dict[str, Any], request: dict[str, Any]) -> str | None:
        budget = context_budget.inspect(
            request,
            expected_images=len(work["snapshot"].get("photo_refs", [])) if self.photos else 0,
        )
        budget["stage"] = "final"
        if not await self.save_budget(work, budget):
            return "context_changed"
        error = None
        if budget["state"] == "unverified":
            error = "budget_unverified"
        elif budget["state"] == "overflow":
            error = "context_overflow"
        elif budget["state"] == "needs_count":
            counter = getattr(self.adapter, "count_input", None)
            if counter is None:
                error = context_budget.resolve(
                    budget, tokens=None, state="unavailable", error="counter_unavailable"
                )
            else:
                budget["api_count"].update(state="started", attempted=True)
                if not await self.save_budget(work, budget):
                    return "context_changed"
                started = time.monotonic()
                from zarya.openai_adapter import TokenCountResult

                try:
                    async with asyncio.timeout(15):
                        count = await counter(request)
                except asyncio.CancelledError:
                    budget["api_count"].update(state="unknown", error="interrupted")
                    budget.update(state="unverified", decision="blocked")
                    await self.save_budget(work, budget)
                    await self.finish_count(
                        work,
                        TokenCountResult("unknown", error="interrupted"),
                        timings.elapsed(started),
                    )
                    raise
                except Exception:
                    count = TokenCountResult("unknown", error="count_uncertain")
                budget["api_count"]["latency_ms"] = timings.elapsed(started)
                await self.finish_count(work, count, budget["api_count"]["latency_ms"])
                error = context_budget.resolve(
                    budget, tokens=count.tokens, state=count.state, error=count.error
                )
        budget["decision"] = "blocked" if error else "ready"
        if context_budget.fingerprint(request) != budget["request_hash"]:
            budget.update(state="unverified", decision="blocked")
            error = "budget_unverified"
        # Write the generation boundary before the API; restart must never retry it.
        budget["provider_attempted"] = error is None
        if not await self.save_budget(work, budget):
            return "context_changed"
        return error

    async def execute(self, work: dict[str, Any]) -> None:
        if self.adapter is None:
            return
        started = time.monotonic()
        measured = work.setdefault("timings", {})
        post_start = None

        def capture() -> None:
            if "prepare_ms" not in measured:
                measured["prepare_ms"] = timings.elapsed(started)
            if post_start is not None:
                measured["postprocess_ms"] = timings.elapsed(post_start)

        try:
            from zarya.memory_context import valid_snapshot

            async with self.store.gate, self.db.transaction() as conn:
                context_current = await valid_snapshot(conn, work["bot_id"], work["snapshot"])
            if not context_current:
                capture()
                await self.finish(work, ModelResult(state="cancelled", error="context_changed"), 0)
                return
            if work["snapshot"].get("behavior") and work.get("mode", "live") == "live":
                async with self.db.transaction() as conn:
                    current_behavior = await behavior.valid(
                        conn, work["bot_id"], work["chat_id"], work["snapshot"]["behavior"]
                    )
                if not current_behavior:
                    capture()
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
                capture()
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
                    capture()
                    await self.finish(
                        work, ModelResult(state="cancelled", error="source_changed"), 0
                    )
                    return
            request = work["request"]
            refs = work.get("snapshot", {}).get("photo_refs", [])
            if self.photos and refs:
                request = await self.photos.attach(request, refs)
            request = context_budget.pack(request)
            error = await self.preflight(work, request)
            if error:
                capture()
                if error in context_budget.SERVICES:
                    work["service_error"] = error
                    await self.finish(work, ModelResult(state="rejected", error=error), 0)
                else:
                    await self.finish(work, ModelResult(state="cancelled", error=error), 0)
                return
            measured["prepare_ms"] = timings.elapsed(started)
            model_start = time.monotonic()
            try:
                result = await self.adapter.generate(request)
            finally:
                measured["model_ms"] = timings.elapsed(model_start)
            if result.error == "context_overflow":
                budget = work["snapshot"]["context_budget"]
                budget.update(state="provider_overflow", decision="provider_rejected")
                await self.save_budget(work, budget)
                work["service_error"] = "context_overflow"
            post_start = time.monotonic()
            if work["snapshot"].get("current_messages") and result.state == "completed":
                try:
                    parts = [
                        (mid, covers, chat_dashes(display_text(text, work["snapshot"])))
                        for mid, covers, text in message_batches.answers(
                            result.text, work["snapshot"]["current_messages"]
                        )
                    ]
                    if any(not text.strip() for _, _, text in parts):
                        raise ValueError("empty batch answer")
                    work["batch_answers"] = parts
                    result.text = "\n\n".join(text for _, _, text in parts)
                except ValueError:
                    result.text, result.state, result.error = "", "invalid", "invalid_batch_reply"
                    work["service_error"] = "invalid_batch_reply"
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
                    result.text = research_answer_text(
                        result.text, research, show_links=requested(work["snapshot"])
                    )
                    if len(result.text.encode("utf-16-le")) // 2 > 8000:
                        result.text, result.state, result.error = "", "invalid", "answer_too_long"
                except ValueError:
                    result.text, result.state, result.error = "", "invalid", "unknown_source"
            if (
                "message_link_replacements" in work["snapshot"]
                and result.state == "completed"
                and (not work.get("behavior_plan") or work["behavior_plan"].action == "text")
            ):
                result.text = message_links.render(
                    result.text, work["snapshot"]["message_link_replacements"]
                )
        except asyncio.CancelledError:
            capture()
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
        capture()
        await self.finish(work, result, int((time.monotonic() - started) * 1000))

    async def details(self, run_id: int) -> dict[str, Any] | None:
        value = await self.db.one(
            "SELECT id,bot_id,chat_id,thread_id,trigger,state,snapshot,response,"
            "error_code,created_at,mode,replay_of,job_id,timings_json FROM dialogue_runs "
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
                    "timings_json",
                ),
                value,
                strict=True,
            )
        )
        data["snapshot"] = json.loads(data["snapshot"])
        if data["snapshot"].get("invalidated"):
            # Source/research purges may retain only {"invalidated": true}.
            # Keep the read API shape usable without restoring erased content.
            data["snapshot"] = {**json.loads(empty_snapshot()), **data["snapshot"]}
        diagnostic = data["snapshot"].get("retrieval")
        if diagnostic:
            epoch_row = await self.db.one(
                "SELECT version FROM memory_epochs WHERE bot_id=?", (data["bot_id"],)
            )
            if (
                not epoch_row
                or epoch_row[0] != diagnostic.get("epoch")
                or diagnostic.get("at", 0) <= time.time() - 90 * 86400
            ):
                data["snapshot"].pop("retrieval", None)
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
            "SELECT part_index,state,payload,message_id,error_code,timings_json,attempts "
            "FROM outbox "
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
                "reply_id": json.loads(p[2]).get("reply_id"),
                "message_id": p[3],
                "error": p[4],
            }
            for p in parts
        ]
        first = next((p for p in parts if p[0] == 0), None)
        data["timing"] = timings.diagnostic(
            data.pop("timings_json"),
            first[5] if first else None,
            state="generating"
            if any(p[1] in {"pending", "sending"} for p in parts)
            else data["state"],
            mode=data["mode"],
            attempts=first[6] if first else 0,
        )
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
            "json_extract(snapshot,'$.incoming.text'),"
            "COALESCE(json_extract(snapshot,'$.invalidated'),0) FROM dialogue_runs"
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
                            "invalidated",
                        ),
                        p[:-1] + (bool(p[-1]),),
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
        if original["snapshot"].get("verbatim_quote"):
            raise ValueError(
                "Дословная цитата копируется напрямую. Для проверки отправь новый реплай."
            )
        if mode == "paid" and original["snapshot"].get("post_unreadable"):
            raise ValueError("Текст пересланного поста недоступен. Пришли его новым сообщением.")
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
            prompts = await self.store.prompts_for(settings.version)
            if expected_settings_version != settings.version:
                raise ValueError("Настройки изменились. Откройте параметры платного теста заново")
            question = saved.get("question_text", saved["incoming"]["text"])
            reply_mode = answer_mode(question)
            if saved.get("research"):
                reply_mode = research_answer_mode(
                    speaker_text({"text": question}), reply_mode, saved["research"]
                )
            style = (
                "relaxed"
                if saved.get("research") and reply_mode != "brief"
                else writing_style(random.random())
            )
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
                "prompt_bundle_sha256": prompts.sha256,
                "request": {
                    **saved["request"],
                    "model": settings.settings.model,
                    "reasoning": {"effort": settings.settings.reasoning},
                    "text": {
                        "verbosity": "medium"
                        if reply_mode == "detailed"
                        or (reply_mode == "analysis" and saved.get("research"))
                        else "low"
                    },
                    "max_output_tokens": settings.settings.max_output_tokens,
                    "instructions": instructions(
                        settings.settings,
                        saved["owner"],
                        reply_mode,
                        bool(self.photos and settings.settings.photo_enabled),
                        style,
                        sources_requested=requested(saved),
                        prompts=prompts,
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
                    behavior.configure(saved["request"], saved["behavior"], prompts)
                else:
                    saved.pop("behavior")
        if mode == "paid" and saved.get("avatar_id"):
            saved["request"]["instructions"] += prompts.text("dialogue.avatar")
        if mode == "paid":
            if saved.get("research"):
                saved["request"]["instructions"] += research_answer_policy(saved["research"])
            if saved.get("forwarded_post"):
                saved["request"]["instructions"] += FORWARD_POLICY
            if "message_link_replacements" in saved:
                saved["request"]["instructions"] += message_links.POLICY
            if saved.get("topic_context"):
                saved["request"]["instructions"] += TOPIC_CONTEXT_POLICY
            if saved.get("task_state"):
                from zarya.task_state import USE

                saved["request"]["instructions"] += USE
            if saved.get("current_messages"):
                message_batches.configure(saved["request"], saved["current_messages"])
            saved["request"] = context_budget.pack(saved["request"])
            saved["context_budget"] = context_budget.inspect(saved["request"])
            saved["context_budget"]["stage"] = "assembled"
        async with self.capacity:
            async with self.db.transaction() as conn:
                if (
                    mode == "paid"
                    and (await row(conn, "SELECT version FROM settings WHERE id=1", ()))[0]
                    != saved["settings_version"]
                ):
                    raise ValueError("Настройки изменились. Откройте платный тест заново")
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
                from zarya.memory_context import valid_snapshot

                if not await valid_snapshot(conn, original["bot_id"], saved):
                    raise ValueError("Контекст изменился; повторение недоступно")
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
                assert new_id is not None
                from zarya.participant_context import register

                await register(conn, new_id, saved)
                if mode == "paid":
                    await conn.execute(
                        "INSERT INTO model_calls(provider,model,state,created_at,run_id,"
                        "settings_version,request_json,pricing_profile,operation) VALUES "
                        "('openai',?,'started',?,?,?,?,?,'preflight')",
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
