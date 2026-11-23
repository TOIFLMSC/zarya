"""Durable, isolated research dependency; no private memory in search requests."""

import asyncio
import json
import time
from datetime import datetime
from typing import Any

import aiosqlite

from zarya.memory_logic import epoch
from zarya.models import Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.research_dates import claim_dates
from zarya.research_fetch import Fetcher, SafeFetcher, check_target
from zarya.research_intent import (
    blocks_external,
    local_post_request,
    research_profile,
    speaker_text,
)
from zarya.research_logic import (
    API_FEEDS,
    SEARCH_TOOL_LIMIT,
    search_policy,
    urls,
    wants_search,
)
from zarya.research_news import (
    RECHECK_QUESTION,
    fresh_news_sources,
    limit_unconfirmed_claims,
    news_followup_plan,
    public_search_sources,
)
from zarya.research_plan import (
    FOLLOWUP,
    FOLLOWUP_INSTRUCTIONS,
    FOLLOWUP_TOOLS,
    INITIAL,
    OUTPUT_CONTRACT,
    followup_input,
    hosted_search_input,
    merge_analysis,
    source_lookup,
)
from zarya.research_quote import resolve_market_question, resolve_quote
from zarya.source_material import resolve
from zarya.telegram_store import TelegramStore, row, stamp
from zarya.x_post import (
    X_POLICY,
    post_id,
    post_url,
    references_post,
    source_post_id,
    unavailable_post_ids,
)
from zarya.youtube import video_id


def evidence_url(value: str) -> str:
    checked = check_target(value)
    identifier = post_id(checked)
    return post_url(identifier) if identifier else checked


def source_has_evidence(source: dict[str, Any]) -> bool:
    return (
        bool(source.get("url"))
        and not source.get("error")
        and source.get("coverage") != "unavailable"
        and not (source_post_id(source) and source.get("coverage") == "metadata_only")
    )


class ResearchEngine:
    def __init__(
        self, store: TelegramStore, adapter: ModelAdapter | None, fetcher: Fetcher | None = None
    ):
        self.store, self.db, self.adapter = store, store.db, adapter
        self.fetcher = fetcher or SafeFetcher()

    async def prepare(
        self,
        conn: aiosqlite.Connection,
        job: int,
        bot: str,
        chat: str,
        scope: str,
        grant: int,
        event: int,
        message: dict[str, Any],
        settings: Settings,
        version: int,
        selected: dict[str, Any] | None = None,
        *,
        text_only: bool = False,
    ) -> tuple[bool, dict[str, Any] | None]:
        existing = await row(conn, "SELECT id,state FROM research_runs WHERE job_id=?", (job,))
        if existing:
            if existing[1] == "cancelled":
                await conn.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
                return True, None
            if existing[1] in {"queued", "fetching", "search_started"}:
                return True, None
            return False, await self.context(conn, existing[0])
        if not settings.research_enabled:
            return False, None
        if blocks_external(message):
            return False, None
        question = str(message.get("text") or message.get("caption") or "")[:4000]
        selected_urls = urls(message)
        material = ""
        manifest = [{"message_id": message["message_id"], "event_id": event}]
        selected = (
            selected if selected is not None else await resolve(conn, bot, chat, grant, message)
        )
        quote = await resolve_quote(conn, bot, chat, grant, message, selected)
        news = (
            await fresh_news_sources(conn, bot, chat, grant, message)
            if not quote and not text_only
            else None
        )
        market_question = (
            await resolve_market_question(conn, bot, chat, grant, message, selected)
            if not quote and not news
            else None
        )
        has_post = not selected.get("via_assistant") or any(
            source["forwarded"] or source["attachments"] or source["urls"]
            for source in selected["messages"]
        )
        if selected["messages"] and has_post and not news:
            parts = []
            for source in selected["messages"]:
                text = source["text"]
                for attachment in [] if text_only else source["attachments"]:
                    kind = {
                        "photo": "Фото",
                        "video": "Видео",
                        "animation": "GIF",
                        "video_note": "Кружочек",
                        "voice": "Голосовое",
                        "sticker": "Стикер",
                    }[attachment["kind"]]
                    coverage = {
                        "image": "изображение",
                        "thumbnail_only": "только превью, без движения и звука",
                        "unavailable": "содержимое не распознано",
                        "sampled_media": "выбранные кадры и/или расшифровка; "
                        "ограничения указаны в данных",
                    }[attachment["coverage"]]
                    text += f"\n[{kind}: {coverage}]"
                heading = (
                    f"Сообщение {source['message_id']}\n" if len(selected["messages"]) > 1 else ""
                )
                parts.append(heading + text)
            material = "\n\n".join(parts)[:24000]
            selected_urls = list(
                dict.fromkeys(
                    selected_urls
                    + [url for source in selected["messages"] for url in source["urls"]]
                )
            )[:3]
        manifest += [ref for ref in selected["manifest"] if ref not in manifest]
        forwarded = bool(message.get("forward_origin") or message.get("forward_date"))
        if forwarded:
            material = material or question
            question = "Кратко поясни переданный пост"
        if news:
            selected_urls, dependencies = news
            manifest += [ref for ref in dependencies if ref not in manifest]
            question, material = RECHECK_QUESTION, ""
        elif quote:
            question, material, selected_urls = quote.question(), "", []
        elif market_question:
            question, material, selected_urls = market_question, "", []
        speaker_question = question if quote or market_question else speaker_text(message)
        local_post = local_post_request(message) and any(
            source["forwarded"] for source in selected["messages"]
        )
        search = bool(news) or text_only or (not local_post and wants_search(speaker_question))
        if not selected_urls and not material and not search:
            return False, None
        source_requests = [
            {"requested_url": url, "news_recheck": bool(news)} for url in selected_urls
        ]
        cursor = await conn.execute(
            "INSERT INTO "
            "research_runs(job_id,bot_id,chat_id,scope,access_version,state,mode,qu"
            "estion,material,manifest,sources,settings_version,model,reasoning,crea"
            "ted_at,profile) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)",
            (
                job,
                bot,
                chat,
                scope,
                grant,
                "search" if search else "read",
                question,
                material,
                json.dumps(manifest),
                json.dumps(source_requests),
                version,
                settings.research_model,
                settings.research_reasoning,
                stamp(),
                "study" if news else research_profile(speaker_question),
            ),
        )
        assert cursor.lastrowid is not None
        return True, None

    async def valid(self, conn: aiosqlite.Connection, research_id: int) -> bool:
        run = await row(
            conn,
            "SELECT bot_id,chat_id,scope,access_version,state,manifest FROM "
            "research_runs WHERE id=?",
            (research_id,),
        )
        if not run or run[4] == "cancelled":
            return False
        access = await row(
            conn,
            "SELECT state,version,member_state FROM telegram_access WHERE bot_id=? "
            "AND scope=? AND subject_id=?",
            (run[0], run[2], run[1]),
        )
        if (
            not access
            or access[0] != "approved"
            or access[1] != run[3]
            or access[2] in {"left", "kicked", "migrated"}
        ):
            return False
        for item in json.loads(run[5]):
            current = await row(
                conn,
                "SELECT m.event_id,e.payload FROM research_messages m JOIN events e ON "
                "e.id=m.event_id WHERE m.bot_id=? AND m.chat_id=? AND m.message_id=? "
                "AND m.access_version=?",
                (run[0], run[1], item["message_id"], run[3]),
            )
            if not current or current[0] != item["event_id"] or current[1] == "{}":
                return False
        return True

    async def current_job(self, conn: aiosqlite.Connection, job: int) -> bool:
        value = await row(
            conn,
            "SELECT j.state FROM jobs j JOIN events e ON e.id=j.event_id "
            "WHERE j.id=? AND NOT EXISTS (SELECT 1 FROM jobs newer JOIN events ne "
            "ON ne.id=newer.event_id WHERE ne.bot_id=e.bot_id AND ne.chat_id=e.chat_id "
            "AND newer.id>j.id AND json_extract(newer.payload,'$.sender_id')="
            "json_extract(j.payload,'$.sender_id') AND "
            "COALESCE(json_extract(newer.payload,'$.thread_id'),0)="
            "COALESCE(json_extract(j.payload,'$.thread_id'),0) AND "
            "json_extract(newer.payload,'$.trigger') IN "
            "('private','name','mention','reply','continuation'))",
            (job,),
        )
        return bool(value and value[0] == "pending")

    async def context(self, conn: aiosqlite.Connection, research_id: int) -> dict[str, Any]:
        run = await row(
            conn,
            "SELECT state,mode,material,sources,result,error_code FROM research_runs WHERE id=?",
            (research_id,),
        )
        if not run:
            return {"id": research_id, "state": "unavailable"}
        return {
            "id": research_id,
            "state": run[0],
            "mode": run[1],
            "provided_post": run[2],
            "sources": json.loads(run[3]),
            "analysis": json.loads(run[4]) if run[4] else None,
            "error": run[5],
            "video_watched": False,
        }

    async def claim(self, bot: str) -> dict[str, Any] | None:
        async with self.store.gate, self.db.transaction() as conn:
            value = await row(
                conn,
                "SELECT "
                "r.id,r.job_id,r.bot_id,r.chat_id,r.scope,r.question,r.material,r.sourc"
                "es,r.model,r.reasoning,r.settings_version,r.created_at,r.profile FROM "
                "research_runs r JOIN jobs j ON j.id=r.job_id WHERE r.bot_id=? AND "
                "r.state='queued' AND j.state='pending' ORDER BY r.id LIMIT 1",
                (bot,),
            )
            if not value:
                return None
            work = dict(
                zip(
                    (
                        "id",
                        "job_id",
                        "bot_id",
                        "chat_id",
                        "scope",
                        "question",
                        "material",
                        "sources",
                        "model",
                        "reasoning",
                        "settings_version",
                        "created_at",
                        "profile",
                    ),
                    value,
                    strict=True,
                )
            )
            settings = Settings.model_validate_json(
                (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            if (
                not await self.valid(conn, work["id"])
                or not await self.current_job(conn, work["job_id"])
                or not settings.research_enabled
                or not settings.dialogue_enabled
                or time.time() - datetime.fromisoformat(work["created_at"]).timestamp() > 600
            ):
                await conn.execute(
                    "UPDATE research_runs SET "
                    "state='cancelled',error_code='access_or_settings_changed' WHERE id=?",
                    (work["id"],),
                )
                return {"skip": True}
            work["sources"] = json.loads(work["sources"])
            work["news_recheck"] = bool(work["sources"]) and all(
                entry.get("news_recheck") is True for entry in work["sources"]
            )
            # Jobs from before this flag was persisted cannot establish news provenance.
            if work["question"] == RECHECK_QUESTION and any(
                "news_recheck" not in entry for entry in work["sources"]
            ):
                await conn.execute(
                    "UPDATE research_runs SET state='rejected',"
                    "error_code='legacy_news_recheck',finished_at=? WHERE id=?",
                    (stamp(), work["id"]),
                )
                return {"skip": True}
            work["epoch"] = await epoch(conn, bot)
            await conn.execute(
                "UPDATE research_runs SET state='fetching' WHERE id=?", (work["id"],)
            )
            return work

    async def execute(self, work: dict[str, Any]) -> None:
        started = time.monotonic()
        work["phase"] = INITIAL
        work["can_followup"] = False
        sources: list[dict[str, Any]] = []
        result = None
        try:
            for entry in work["sources"]:
                source = await self.fetcher.fetch(entry["requested_url"])
                source["id"] = len(sources) + 1
                sources.append(source)
            async with self.store.gate, self.db.transaction() as conn:
                run = await row(
                    conn, "SELECT mode,state FROM research_runs WHERE id=?", (work["id"],)
                )
                settings = Settings.model_validate_json(
                    (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
                )
                valid = (
                    await self.valid(conn, work["id"])
                    and await self.current_job(conn, work["job_id"])
                    and run[1] == "fetching"
                    and settings.research_enabled
                    and settings.dialogue_enabled
                    and await epoch(conn, work["bot_id"]) == work["epoch"]
                )
                job = await row(conn, "SELECT state FROM jobs WHERE id=?", (work["job_id"],))
                if not valid or not job or job[0] != "pending":
                    await conn.execute(
                        "UPDATE research_runs SET state='cancelled' WHERE id=?", (work["id"],)
                    )
                    return
                unsafe = any(
                    str(s.get("error") or "").startswith(("private", "unsafe")) for s in sources
                )
                fallback_sources = [
                    {
                        "id": s["id"],
                        "url": f"https://www.youtube.com/watch?v={identifier}",
                        "title": s.get("title", "")[:300],
                        "coverage": s.get("coverage"),
                    }
                    for s in sources
                    if (identifier := video_id(str(s.get("requested_url") or s.get("url") or "")))
                    and (
                        s.get("youtube", {}).get("fallback_reason")
                        or s.get("coverage") == "unavailable"
                    )
                ]
                fallback = bool(
                    run[0] == "read" and fallback_sources and self.adapter and not unsafe
                )
                if (run[0] == "read" and not fallback) or self.adapter is None or unsafe:
                    await conn.execute(
                        "UPDATE research_runs SET state=?,sources=?,error_code=?,finished_at=? "
                        "WHERE id=?",
                        (
                            "completed" if run[0] == "read" and not unsafe else "rejected",
                            json.dumps(sources, ensure_ascii=False),
                            "unsafe_source"
                            if unsafe
                            else None
                            if run[0] == "read"
                            else "not_configured",
                            stamp(),
                            work["id"],
                        ),
                    )
                    return
                if fallback:
                    for source in sources:
                        if any(entry["id"] == source["id"] for entry in fallback_sources):
                            source.setdefault("youtube", {})["fallback_used"] = True
                work["can_followup"] = run[0] == "search" and work["profile"] != "answer"
                limit = SEARCH_TOOL_LIMIT - (FOLLOWUP_TOOLS if work["can_followup"] else 0)
                search_input = (
                    hosted_search_input(
                        "Найди доступное описание и сведения о содержании именно этих "
                        "YouTube-роликов. Сверяй video ID; не подменяй их похожими видео. "
                        "Если сведений нет, честно скажи. Не утверждай, что посмотрел видеоряд.",
                        "",
                        fallback_sources,
                    )
                    if fallback
                    else hosted_search_input(
                        work["question"],
                        work["material"],
                        public_search_sources(work["sources"], sources)
                        if work.get("news_recheck")
                        else sources,
                    )
                )
                request = {
                    "model": work["model"],
                    "reasoning": {"effort": work["reasoning"]},
                    "tools": [{"type": "web_search", "search_context_size": "medium"}],
                    "tool_choice": "required",
                    "max_tool_calls": limit,
                    "include": ["web_search_call.action.sources"],
                    "max_output_tokens": 3000,
                    "text": {"verbosity": "low"},
                    "store": False,
                    "service_tier": "default",
                    "instructions": (
                        (await self.store.prompts_for(work["settings_version"])).text(
                            "research.analyse"
                        )
                        + search_policy(limit)
                        + OUTPUT_CONTRACT
                        + (X_POLICY if any(source_post_id(s) for s in sources) else "")
                    ),
                    "input": json.dumps(search_input, ensure_ascii=False),
                }
                await conn.execute(
                    "UPDATE research_runs SET state='search_started',sources=? WHERE id=?",
                    (json.dumps(sources, ensure_ascii=False), work["id"]),
                )
                await conn.execute(
                    "INSERT INTO "
                    "model_calls(job_id,provider,model,state,created_at,settings_version,re"
                    "quest_json,pricing_profile,research_run_id,operation) VALUES "
                    "(?,'openai',?,'started',?,?,?,?,?,?)",
                    (
                        work["job_id"],
                        work["model"],
                        stamp(),
                        work["settings_version"],
                        json.dumps(request, ensure_ascii=False),
                        pricing_profile(work["model"]),
                        work["id"],
                        INITIAL,
                    ),
                )
            assert self.adapter is not None
            result = await self.adapter.generate(request)
        except asyncio.CancelledError:
            await self.finish(
                work,
                sources,
                ModelResult(state="unknown", error="interrupted"),
                int((time.monotonic() - started) * 1000),
            )
            raise
        except Exception:
            result = ModelResult(state="unknown", error="research_uncertain")
        if result:
            followup = await self.finish(
                work, sources, result, int((time.monotonic() - started) * 1000)
            )
            if followup:
                await self.execute_followup(work, *followup)

    async def execute_followup(
        self, work: dict[str, Any], request: dict[str, Any], sources: list[dict[str, Any]]
    ) -> None:
        assert self.adapter is not None
        work = {**work, "phase": FOLLOWUP}
        async with self.store.gate, self.db.transaction() as conn:
            active = await self.search_current(conn, work) and await row(
                conn,
                "SELECT 1 FROM model_calls WHERE research_run_id=? AND operation=? "
                "AND state='started'",
                (work["id"], FOLLOWUP),
            )
        started = time.monotonic()
        try:
            result = (
                await self.adapter.generate(request)
                if active
                else ModelResult(state="cancelled", error="source_changed")
            )
        except asyncio.CancelledError:
            await self.finish(work, sources, ModelResult(state="unknown", error="interrupted"), 0)
            raise
        except Exception:
            result = ModelResult(state="unknown", error="research_uncertain")
        await self.finish(work, sources, result, int((time.monotonic() - started) * 1000))

    async def search_current(
        self,
        conn: aiosqlite.Connection,
        work: dict[str, Any],
        expected_state: str = "search_started",
    ) -> bool:
        run = await row(conn, "SELECT state FROM research_runs WHERE id=?", (work["id"],))
        settings = Settings.model_validate_json(
            (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
        )
        return bool(
            run
            and run[0] == expected_state
            and settings.research_enabled
            and settings.dialogue_enabled
            and await self.current_job(conn, work["job_id"])
            and await self.valid(conn, work["id"])
            and await epoch(conn, work["bot_id"]) == work["epoch"]
        )

    async def finish(
        self, work: dict[str, Any], sources: list[dict[str, Any]], result: ModelResult, latency: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        sources = list(sources)
        phase = work.get("phase", INITIAL)
        followup = phase == FOLLOWUP
        observed: set[str] = set()
        first_pass_urls: set[str] = work.get("date_source_urls", set())
        opened: set[str] = set()
        analysis = None
        state, error = result.state, result.error
        if state == "completed":
            try:
                raw = result.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                decoded = json.loads(raw)
                if (
                    not isinstance(decoded, dict)
                    or not isinstance(decoded.get("summary"), str)
                    or not isinstance(decoded.get("claims"), list)
                    or len(decoded["claims"]) > 5
                ):
                    raise ValueError("shape")
                analysis = {
                    "summary": decoded["summary"],
                    "claims": decoded["claims"],
                    "needs_followup": decoded.get("needs_followup") is True,
                }
                blocked_x = unavailable_post_ids(sources)
                available = {
                    evidence_url(s["url"]) for s in sources
                    if source_has_evidence(s) and not references_post(s["url"], blocked_x)
                }
                feeds: dict[str, int] = {}
                next_id = max((s["id"] for s in sources), default=0) + 1
                for action in result.search_actions or []:
                    if action.get("status") != "completed":
                        continue
                    action_sources = list(action.get("sources") or [])
                    if action.get("type") in {"open_page", "find_in_page"} and action.get("url"):
                        try:
                            opened.add(evidence_url(str(action["url"])))
                        except ValueError:
                            pass
                        else:
                            action_sources.append({"url": action["url"]})
                    for s in action_sources:
                        name = s.get("name")
                        if (
                            action.get("type") == "search"
                            and s.get("type") == "api"
                            and isinstance(name, str)
                            and name in API_FEEDS
                            and name not in feeds
                        ):
                            identifier = next_id
                            next_id += 1
                            feeds[name] = identifier
                            sources.append(
                                {
                                    "id": identifier,
                                    "url": None,
                                    "name": name,
                                    "title": name,
                                    "text": "",
                                    "coverage": "realtime_feed",
                                    "error": None,
                                    "retrieved_at": stamp(),
                                    "action_id": action.get("id"),
                                }
                            )
                            continue
                        try:
                            u = evidence_url(str(s.get("url", "")))
                        except ValueError:
                            continue
                        if references_post(u, blocked_x):
                            continue
                        observed.add(u)
                        if u not in available:
                            sources.append(
                                {
                                    "id": next_id,
                                    "url": u,
                                    "title": str(s.get("title") or u)[:300],
                                    "text": "",
                                    "coverage": "search_source",
                                    "error": None,
                                }
                            )
                            next_id += 1
                            available.add(u)
                for citation in result.citations or []:
                    try:
                        u = evidence_url(citation["url"])
                    except ValueError:
                        continue
                    if references_post(u, blocked_x):
                        continue
                    if u not in available:
                        sources.append(
                            {
                                "id": next_id,
                                "url": u,
                                "title": citation.get("title", u),
                                "text": "",
                                "coverage": "search_source",
                                "error": None,
                            }
                        )
                        next_id += 1
                        available.add(u)
                accepted_claims = []
                discarded = 0
                for claim in analysis["claims"]:
                    if (
                        not isinstance(claim, dict)
                        or not isinstance(claim.get("text"), str)
                        or len(claim["text"]) > 1000
                        or claim.get("kind") not in {"fact", "opinion", "uncertain"}
                        or claim.get("verdict") not in {"supported", "refuted", "uncertain"}
                        or not isinstance(claim.get("urls"), list)
                    ):
                        raise ValueError("claim")
                    if any(not isinstance(url, str) for url in claim["urls"]):
                        raise ValueError("claim")
                    try:
                        references = [evidence_url(url) for url in claim["urls"]]
                    except ValueError:
                        discarded += 1
                        continue
                    feed_refs = claim.get("feeds", [])
                    if not isinstance(feed_refs, list):
                        raise ValueError("unknown_feed")
                    if any(u not in available for u in references) or any(
                        not isinstance(name, str) or name not in feeds for name in feed_refs
                    ):
                        discarded += 1
                        continue
                    if claim["verdict"] != "uncertain" and not references and not feed_refs:
                        raise ValueError("uncited_claim")
                    source_ids = list(
                        dict.fromkeys(
                            [
                                s["id"]
                                for s in sources
                                if source_has_evidence(s) and evidence_url(s["url"]) in references
                            ]
                            + [feeds[name] for name in feed_refs]
                        )
                    )
                    accepted = {key: claim[key] for key in ("text", "kind", "verdict")} | {
                        "urls": references,
                        "feeds": feed_refs,
                        "source_ids": source_ids,
                    }
                    if claim["kind"] == "fact" and claim["verdict"] == "supported":
                        date_sources = {
                            s["url"]: s["id"]
                            for s in sources
                            if s.get("url") in references
                            and s["id"] in source_ids
                            and (
                                s.get("url") in observed
                                or s.get("url") in first_pass_urls
                                or s.get("coverage") == "partial_text"
                            )
                        }
                        if dates := claim_dates(claim.get("dates"), date_sources):
                            accepted["dates"] = dates
                    accepted_claims.append(accepted)
                if len(analysis["summary"]) > 2500:
                    raise ValueError("output_limit")
                if not any(
                    a.get("status") == "completed"
                    and (
                        a.get("type") == "search"
                        or (
                            followup and a.get("type") in {"open_page", "find_in_page"} and observed
                        )
                    )
                    for a in result.search_actions or []
                ):
                    raise ValueError("no_completed_search")
                if discarded:
                    if not any(c["verdict"] != "uncertain" for c in accepted_claims):
                        raise ValueError("unknown_source")
                    analysis["discarded_claims"] = discarded
                    analysis["summary"] = "Часть утверждений не удалось проверить по источникам."
                    analysis["needs_followup"] = False
                analysis["claims"] = accepted_claims
                if work.get("news_recheck"):
                    limit_unconfirmed_claims(
                        analysis, work["sources"][0]["requested_url"], observed
                    )
                # Preserve cited evidence when the provider returns more than our UI limit.
                used_ids = {
                    identifier for claim in analysis["claims"] for identifier in claim["source_ids"]
                }
                if len(used_ids) > 60:
                    raise ValueError("source_limit")
                if work.get("profile") == "source":
                    eligible = [
                        s
                        for s in sources
                        if s.get("url") in opened
                        or (
                            not followup and s.get("text")
                            and s.get("coverage") in {"partial_text", "x_post_text"}
                        )
                    ]
                    analysis["source_lookup"] = source_lookup(
                        decoded.get("source_lookup"), analysis["claims"], eligible
                    )
                    if discarded:
                        analysis["source_lookup"].pop("heading", None)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                # Fixed categories only: exception text may contain source/model content.
                reasons = {
                    "unknown_source": "unknown_source",
                    "unknown_feed": "unknown_source",
                    "uncited_claim": "uncited_claim",
                    "no_completed_search": "no_completed_search",
                    "output_limit": "output_limit",
                    "source_limit": "source_limit",
                }
                reason = "json" if isinstance(exc, json.JSONDecodeError) else "schema"
                if exc.args and isinstance(exc.args[0], str):
                    reason = reasons.get(exc.args[0], reason)
                analysis, state, error = None, "invalid", "invalid_evidence_" + reason
        async with self.store.gate, self.db.transaction() as conn:
            run = await row(
                conn,
                "SELECT state,result,sources,actions FROM research_runs WHERE id=?",
                (work["id"],),
            )
            if not followup and run and run[0] == "fetching":
                if result.error != "interrupted":
                    ready = await self.search_current(conn, work, "fetching")
                    await conn.execute(
                        "UPDATE research_runs SET state=?,error_code=?,finished_at=? WHERE id=?",
                        (
                            "rejected" if ready else "cancelled",
                            "research_preparation_failed" if ready else "source_changed",
                            stamp(),
                            work["id"],
                        ),
                    )
                return None
            valid = await self.search_current(conn, work)
            cost = estimate_cost(result, work["model"])
            search_cost = (
                0.01 * sum(a.get("type") == "search" for a in result.search_actions)
                if result.search_actions is not None
                else None
            )
            updated = await conn.execute(
                "UPDATE model_calls SET "
                "state=?,model=COALESCE(?,model),pricing_profile=?,usage_json=?,"
                "response_id=?,request_id=?,latency_ms=?,cost_usd="
                "?,search_cost_usd=?,error_code=?,finished_at=?,request_json=CASE WHEN "
                "? THEN request_json ELSE NULL END WHERE research_run_id=? AND "
                "COALESCE(operation,'research_initial')=? AND state='started'",
                (
                    result.state,
                    result.model,
                    pricing_profile(result.model or work["model"]),
                    json.dumps(result.usage) if result.usage else None,
                    result.response_id,
                    result.request_id,
                    latency,
                    cost,
                    search_cost,
                    result.error,
                    stamp(),
                    valid,
                    work["id"],
                    phase,
                ),
            )
            if updated.rowcount != 1:
                return None
            if not valid:
                if run and run[0] != "cancelled":
                    await conn.execute(
                        "UPDATE research_runs SET state='cancelled',error_code='source_changed' "
                        "WHERE id=?",
                        (work["id"],),
                    )
                return None
            actions = result.search_actions or []
            plan = None
            if followup:
                if analysis is None:
                    sources = json.loads(run[2])
                analysis = merge_analysis(json.loads(run[1]), analysis, error)
                actions = json.loads(run[3]) + actions
                state, error = "completed", None
            elif analysis and work.get("can_followup") and not analysis.get("discarded_claims"):
                plan = (
                    news_followup_plan(analysis, work["sources"], actions, sources)
                    if work.get("news_recheck")
                    else followup_input(work["profile"], analysis, sources, observed)
                )
                if plan:
                    analysis["followup"] = "started"
            used_ids = {
                identifier
                for claim in (analysis or {}).get("claims", [])
                for identifier in claim["source_ids"]
            }
            cited = [s for s in sources if s["id"] in used_ids]
            sources = cited + [s for s in sources if s["id"] not in used_ids][: 60 - len(cited)]
            await conn.execute(
                "UPDATE research_runs SET "
                "state=?,result=?,sources=?,actions=?,error_code=?,finished_at=? WHERE id=?",
                (
                    "search_started" if plan else state,
                    json.dumps(analysis, ensure_ascii=False) if analysis else None,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(actions, ensure_ascii=False),
                    error,
                    None if plan else stamp(),
                    work["id"],
                ),
            )
            if plan:
                work["date_source_urls"] = observed
                request = {
                    "model": work["model"],
                    "reasoning": {"effort": work["reasoning"]},
                    "tools": [{"type": "web_search", "search_context_size": "low"}],
                    "tool_choice": "required",
                    "max_tool_calls": FOLLOWUP_TOOLS,
                    "include": ["web_search_call.action.sources"],
                    "max_output_tokens": 1800,
                    "text": {"verbosity": "low"},
                    "store": False,
                    "service_tier": "default",
                    "instructions": FOLLOWUP_INSTRUCTIONS + OUTPUT_CONTRACT,
                    "input": json.dumps(plan, ensure_ascii=False),
                }
                await conn.execute(
                    "INSERT INTO model_calls(job_id,provider,model,state,created_at,"
                    "settings_version,request_json,pricing_profile,research_run_id,operation) "
                    "VALUES (?,'openai',?,'started',?,?,?,?,?,?)",
                    (
                        work["job_id"],
                        work["model"],
                        stamp(),
                        work["settings_version"],
                        json.dumps(request, ensure_ascii=False),
                        pricing_profile(work["model"]),
                        work["id"],
                        FOLLOWUP,
                    ),
                )
                return request, sources
        return None

    async def recover(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted',finished_at=? "
                "WHERE state='started' AND research_run_id IN (SELECT id FROM research_runs "
                "WHERE bot_id=?)",
                (stamp(), bot),
            )
            await conn.execute(
                "UPDATE research_runs SET "
                "state=CASE WHEN result IS NULL THEN 'unknown' ELSE 'completed' END,"
                "error_code=CASE WHEN result IS NULL THEN 'interrupted' ELSE NULL END,"
                "result=CASE WHEN result IS NULL THEN NULL ELSE "
                "json_set(result,'$.followup','incomplete','$.followup_error','interrupted') END,"
                "finished_at=? "
                "WHERE bot_id=? AND state='search_started'",
                (stamp(), bot),
            )
            await conn.execute(
                "UPDATE research_runs SET state='queued' WHERE bot_id=? AND state='fetching'",
                (bot,),
            )

    async def listing(self, bot: str | None, scope: str, chat: str, page: int) -> dict[str, Any]:
        where = "bot_id=?"
        args: tuple[object, ...] = (bot or "",)
        if scope != "all":
            where += " AND scope=?"
            args += (scope,)
        if chat:
            where += " AND chat_id=?"
            args += (chat,)
        values = await self.db.all(
            "SELECT "
            "id,chat_id,scope,state,mode,question,model,created_at,error_code FROM "
            "research_runs WHERE " + where + " ORDER BY id DESC LIMIT 20 OFFSET ?",
            args + (page * 20,),
        )
        total = await self.db.one("SELECT COUNT(*) FROM research_runs WHERE " + where, args)
        chats = await self.db.all(
            "SELECT DISTINCT chat_id,scope FROM research_runs WHERE bot_id=? ORDER BY chat_id",
            (bot or "",),
        )
        settings = await self.db.settings()
        return {
            "items": [
                dict(
                    zip(
                        (
                            "id",
                            "chat_id",
                            "scope",
                            "state",
                            "mode",
                            "question",
                            "model",
                            "created_at",
                            "error",
                        ),
                        v,
                        strict=True,
                    )
                )
                for v in values
            ],
            "total": total[0],
            "page": page,
            "chats": [{"id": c[0], "scope": c[1]} for c in chats],
            "enabled": settings.settings.research_enabled,
            "configured": self.adapter is not None,
            "max_tool_calls": SEARCH_TOOL_LIMIT,
            "model": settings.settings.research_model,
            "bot_id": bot,
        }

    async def details(self, research_id: int, bot: str) -> dict[str, Any] | None:
        run = await self.db.one(
            "SELECT "
            "id,chat_id,scope,state,mode,question,material,sources,result,actions,m"
            "odel,reasoning,created_at,error_code,job_id FROM research_runs WHERE "
            "id=? AND bot_id=?",
            (research_id, bot),
        )
        if not run:
            return None
        data = dict(
            zip(
                (
                    "id",
                    "chat_id",
                    "scope",
                    "state",
                    "mode",
                    "question",
                    "material",
                    "sources",
                    "result",
                    "actions",
                    "model",
                    "reasoning",
                    "created_at",
                    "error",
                    "job_id",
                ),
                run,
                strict=True,
            )
        )
        for field in ("sources", "result", "actions"):
            data[field] = json.loads(data[field]) if data[field] else None
        calls = await self.db.all(
            "SELECT "
            "model,pricing_profile,state,usage_json,latency_ms,cost_usd,"
            "search_cost_usd,error_code,COALESCE(operation,'research_initial') FROM "
            "model_calls WHERE research_run_id=? ORDER BY id",
            (research_id,),
        )
        data["calls"] = [
            dict(
                zip(
                    (
                        "model",
                        "pricing",
                        "state",
                        "usage",
                        "latency_ms",
                        "cost_usd",
                        "search_cost_usd",
                        "error",
                        "phase",
                    ),
                    call,
                    strict=True,
                )
            )
            for call in calls
        ]
        for call_data in data["calls"]:
            call_data["usage"] = json.loads(call_data["usage"]) if call_data["usage"] else None
        data["call"] = data["calls"][0] if data["calls"] else None
        return data
