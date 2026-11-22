"""Bounded, durable participant memory. A model proposes evidence; it grants no rights."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zarya.database import ConflictError
from zarya.memory_context import (
    dependencies,
    merge_dependencies,
    reply_context,
    valid_dependencies,
    valid_snapshot,
)
from zarya.memory_e5 import LocalIndex, Ranking, query_text
from zarya.memory_logic import (
    RETENTION,
    SHAREABLE,
    epoch,
    erase_fact,
    invalidate,
    one,
    prune_dependencies,
    source_sharing,
    suppress_sources,
)
from zarya.memory_observations import KINDS, strength
from zarya.models import MemoryMutation, Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.telegram_store import TelegramStore, stamp


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender_id: str
    category: Literal["name", "interest", "preference", "topic", "joke", "observation"]
    key: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=500)
    provenance: Literal["self", "other", "inference"]
    source_ids: list[int] = Field(min_length=1, max_length=5)
    existing_fact_id: int | None = None
    relation: Literal["new", "supports", "contradicts", "uncertain"] = "new"
    observation_kind: Literal["none", "interest", "nickname", "habit"] = "none"


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1600)
    facts: list[Proposal] = Field(max_length=10)


def extraction_schema() -> dict[str, Any]:
    schema = Extraction.model_json_schema()
    proposal = schema["$defs"]["Proposal"]
    proposal["required"] = list(proposal["properties"])
    for prop in proposal["properties"].values():
        prop.pop("default", None)
    return schema


class MemoryEngine:
    def __init__(self, store: TelegramStore, adapter: ModelAdapter | None, data_dir: Path):
        from zarya.participant_summaries import SummaryEngine

        self.store, self.db, self.adapter, self.data_dir = store, store.db, adapter, data_dir
        self.summaries = SummaryEngine(store, adapter)
        self.last_cleanup = 0.0
        self.semantic_index = LocalIndex(self.db)

    async def recover(self, bot: str) -> None:
        await self.summaries.recover(bot)
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted',finished_at=? "
                "WHERE state='started' AND memory_batch_id IN (SELECT id FROM memory_batches "
                "WHERE bot_id=?)",
                (stamp(), bot),
            )
            await conn.execute(
                "UPDATE memory_batches SET state='unknown',error_code='interrupted',finished_at=? "
                "WHERE bot_id=? AND state='started'",
                (stamp(), bot),
            )

    async def claim(self, bot: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.adapter is None:
            return None
        async with self.store.gate, self.db.transaction() as conn:
            setting = await one(conn, "SELECT version,document FROM settings WHERE id=1")
            settings = Settings.model_validate_json(setting[1])
            if not settings.memory_enabled:
                return None
            candidate = await one(
                conn,
                "SELECT "
                "s.chat_id,s.scope,s.access_version,COUNT(*),MIN(s.received_at),MAX(s.received_at) "
                "FROM memory_sources s JOIN telegram_access a ON a.bot_id=s.bot_id AND "
                "a.subject_id=s.chat_id "
                "AND a.scope=s.scope AND a.version=s.access_version WHERE s.bot_id=? AND "
                "s.valid=1 AND s.processed=0 "
                "AND a.state='approved' AND a.member_state NOT IN ('left','kicked','migrated') "
                "AND s.received_at>? AND NOT EXISTS (SELECT 1 FROM memory_batches b WHERE "
                "b.bot_id=s.bot_id "
                "AND b.chat_id=s.chat_id AND (b.state='started' OR (b.state!='cancelled' AND "
                "b.created_at>?))) "
                "GROUP BY s.chat_id,s.scope,s.access_version ORDER BY MIN(s.received_at) LIMIT 1",
                (
                    bot,
                    time.time() - RETENTION,
                    datetime.fromtimestamp(time.time() - (0 if force else 120), UTC).isoformat(),
                ),
            )
            if not candidate:
                return None
            chat, scope, access, count, oldest, newest = candidate
            if (
                not force
                and count < 10
                and time.time() - newest < 60
                and time.time() - oldest < 180
            ):
                return None
            async with conn.execute(
                "SELECT id,event_id,message_id,sender_id,name,thread_id,text,received_at "
                "FROM memory_sources WHERE bot_id=? AND chat_id=? AND access_version=? "
                "AND valid=1 AND processed=0 AND received_at>? ORDER BY event_id LIMIT 20",
                (bot, chat, access, time.time() - RETENTION),
            ) as cursor:
                sources = [
                    dict(
                        zip(
                            (
                                "id",
                                "event_id",
                                "message_id",
                                "sender_id",
                                "name",
                                "thread_id",
                                "text",
                                "received_at",
                            ),
                            r,
                            strict=True,
                        )
                    )
                    for r in await cursor.fetchall()
                ]
            # Bound both turns and text, including unusually long Telegram captions.
            used, selected = 0, []
            for source in sources:
                if used + len(source["text"]) > 18000:
                    break
                selected.append(source)
                used += len(source["text"])
            sources = selected
            if not sources:
                return None
            deps = dependencies()
            contexts = {}
            for source in sources:
                deps["sources"].append({"id": source["id"], "event_id": source["event_id"]})
                reply, refs = await reply_context(conn, bot, chat, access, source)
                if reply and used + len(reply["text"]) <= 18000:
                    source["reply_context"] = reply
                    contexts[source["id"]] = refs
                    merge_dependencies(deps, refs)
                    used += len(reply["text"])
            people = list({s["sender_id"] for s in sources})
            speakers = people[:]
            async with conn.execute(
                "SELECT sender_id,MAX(name) FROM memory_sources WHERE bot_id=? AND chat_id=? "
                "AND access_version=? AND valid=1 AND received_at>? GROUP BY sender_id "
                "ORDER BY MAX(received_at) DESC LIMIT 50",
                (bot, chat, access, time.time() - RETENTION),
            ) as cursor:
                participants = [{"id": r[0], "name": r[1]} for r in await cursor.fetchall()]
            people = list(set(people) | {p["id"] for p in participants})
            async with conn.execute(
                "SELECT id,version,sender_id,category,fact_key,text,state,context_dependencies,"
                "observation_kind "
                "FROM memory_facts "
                "WHERE bot_id=? AND chat_id=? AND access_version=? AND state IN "
                "('active','disputed','proposed') AND (sender_id IN ("
                + ",".join("?" for _ in speakers)
                + ") OR (observation_kind='nickname' AND sender_id IN ("
                + ",".join("?" for _ in people)
                + "))) "
                "ORDER BY id DESC LIMIT 15",
                (bot, chat, access, *speakers, *people),
            ) as cursor:
                existing_rows = list(await cursor.fetchall())
            existing = []
            for r in existing_rows:
                if r[7] is None or await valid_dependencies(conn, bot, chat, access, r[7]):
                    existing.append(
                        dict(
                            zip(
                                ("id", "version", "sender_id", "category", "key", "text", "state"),
                                r[:7],
                                strict=True,
                            )
                        )
                    )
                    existing[-1]["observation_kind"] = r[8]
            deps["facts"] = [{"id": f["id"], "version": f["version"]} for f in existing]
            version = await epoch(conn, bot)
            request = {
                "model": settings.memory_model,
                "reasoning": {"effort": "low"},
                "store": False,
                "max_output_tokens": 2500,
                "instructions": (await self.store.prompts_for(setting[0], conn)).text(
                    "memory.extract"
                ),
                "input": json.dumps(
                    {"sources": sources, "existing_facts": existing, "participants": participants},
                    ensure_ascii=False,
                ),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "participant_memory",
                        "strict": True,
                        "schema": extraction_schema(),
                    }
                },
            }
            cursor = await conn.execute(
                "INSERT INTO memory_batches(bot_id,chat_id,scope,access_version,epoch,manifest,"
                "state,created_at,settings_version,dependencies) "
                "VALUES (?,?,?,?,?,?,'started',?,?,?)",
                (
                    bot,
                    chat,
                    scope,
                    access,
                    version,
                    json.dumps([{"id": s["id"], "event_id": s["event_id"]} for s in sources]),
                    stamp(),
                    setting[0],
                    json.dumps(deps),
                ),
            )
            batch = cursor.lastrowid
            for source in sources:
                await conn.execute(
                    "UPDATE memory_sources SET processed=1 WHERE id=?", (source["id"],)
                )
            await conn.execute(
                "INSERT INTO "
                "model_calls(provider,model,state,created_at,settings_version,request_json,"
                "pricing_profile,memory_batch_id) VALUES ('openai',?,'started',?,?,?,?,?)",
                (
                    settings.memory_model,
                    stamp(),
                    setting[0],
                    json.dumps(request, ensure_ascii=False),
                    pricing_profile(settings.memory_model),
                    batch,
                ),
            )
            return {
                "id": batch,
                "bot_id": bot,
                "chat_id": chat,
                "scope": scope,
                "access_version": access,
                "epoch": version,
                "request": request,
                "sources": sources,
                "existing": existing,
                "dependencies": deps,
                "contexts": contexts,
                "participants": people,
            }

    async def execute(self, work: dict[str, Any]) -> None:
        assert self.adapter is not None
        started = time.monotonic()
        try:
            result = await self.adapter.generate(work["request"])
        except asyncio.CancelledError:
            await self.finish(
                work,
                ModelResult(state="unknown", error="interrupted"),
                int((time.monotonic() - started) * 1000),
            )
            raise
        except Exception:
            result = ModelResult(state="unknown", error="provider_uncertain")
        await self.finish(work, result, int((time.monotonic() - started) * 1000))

    async def finish(self, work: dict[str, Any], result: ModelResult, latency: int) -> None:
        extracted = None
        if result.state == "completed":
            try:
                extracted = Extraction.model_validate_json(result.text)
                sources = {s["id"]: s for s in work["sources"]}
                participants = set(work.get("participants", [])) | {
                    s["sender_id"] for s in work["sources"]
                }
                for fact in extracted.facts:
                    if fact.observation_kind != "none":
                        if KINDS[fact.observation_kind] != fact.category:
                            raise ValueError("invalid_observation")
                        # Repeated actions imply a trait; they do not self-certify it.
                        if fact.provenance == "self":
                            fact.provenance = "inference"
                    if fact.sender_id not in participants or any(
                        i not in sources for i in fact.source_ids
                    ):
                        raise ValueError("foreign_source")
                    if fact.provenance == "self" and any(
                        sources[i]["sender_id"] != fact.sender_id for i in fact.source_ids
                    ):
                        raise ValueError("false_self")
                    if fact.existing_fact_id is not None:
                        previous = next(
                            (
                                f
                                for f in work.get("existing", [])
                                if f["id"] == fact.existing_fact_id
                            ),
                            None,
                        )
                        if (
                            not previous
                            or previous["sender_id"] != fact.sender_id
                            or previous["category"] != fact.category
                            or previous["observation_kind"] != fact.observation_kind
                        ):
                            raise ValueError("foreign_fact")
                        fact.key = previous["key"]
                    if (
                        fact.relation in {"supports", "contradicts"}
                        and fact.existing_fact_id is None
                    ):
                        raise ValueError("missing_fact")
            except (ValidationError, ValueError):
                extracted = None
        async with self.store.gate, self.db.transaction() as conn:
            changed = await conn.execute(
                "UPDATE model_calls SET state=?,model=COALESCE(?,model),usage_json=?,"
                "response_id=?,request_id=?,latency_ms=?,cost_usd=?,pricing_profile=?,"
                "error_code=?,finished_at=? "
                "WHERE memory_batch_id=? AND state='started'",
                (
                    result.state,
                    result.model,
                    json.dumps(result.usage) if result.usage else None,
                    result.response_id,
                    result.request_id,
                    latency,
                    estimate_cost(result, work["request"]["model"]),
                    pricing_profile(result.model or work["request"]["model"]),
                    result.error,
                    stamp(),
                    work["id"],
                ),
            )
            if changed.rowcount != 1:
                return
            access = await one(
                conn,
                "SELECT state,version,member_state FROM telegram_access WHERE bot_id=? AND "
                "scope=? AND subject_id=?",
                (work["bot_id"], work["scope"], work["chat_id"]),
            )
            batch = await one(conn, "SELECT state FROM memory_batches WHERE id=?", (work["id"],))
            settings = Settings.model_validate_json(
                (await one(conn, "SELECT document FROM settings WHERE id=1"))[0]
            )
            valid = bool(
                access
                and access[0] == "approved"
                and access[1] == work["access_version"]
                and access[2] not in {"left", "kicked", "migrated"}
                and batch[0] == "started"
                and settings.memory_enabled
                and await valid_dependencies(
                    conn,
                    work["bot_id"],
                    work["chat_id"],
                    work["access_version"],
                    work["dependencies"],
                )
            )
            for source in work["sources"]:
                current = await one(
                    conn,
                    "SELECT valid,event_id,received_at FROM memory_sources WHERE id=?",
                    (source["id"],),
                )
                valid = valid and bool(
                    current
                    and current[0]
                    and current[1] == source["event_id"]
                    and current[2] > time.time() - RETENTION
                )
            state = result.state if valid else "cancelled"
            if valid and result.state == "completed" and extracted is None:
                state = "invalid"
            await conn.execute(
                "UPDATE memory_batches SET state=?,summary=?,error_code=?,finished_at=? WHERE id=?",
                (
                    state,
                    extracted.summary if valid and extracted else None,
                    result.error
                    or (
                        "invalid_result"
                        if state == "invalid"
                        else "memory_changed"
                        if not valid
                        else None
                    ),
                    stamp(),
                    work["id"],
                ),
            )
            if not valid:
                await conn.execute(
                    "UPDATE model_calls SET request_json=NULL WHERE memory_batch_id=?",
                    (work["id"],),
                )
            if state != "completed" or extracted is None:
                return
            for proposal in extracted.facts:
                await self.apply_proposal(conn, work, proposal)
            await prune_dependencies(conn, work["bot_id"])

    async def apply_proposal(
        self, conn: aiosqlite.Connection, work: dict[str, Any], p: Proposal
    ) -> None:
        if p.existing_fact_id is not None:
            claimed = next(
                (f for f in work.get("existing", []) if f["id"] == p.existing_fact_id), None
            )
            current = await one(
                conn,
                "SELECT version FROM memory_facts WHERE id=? AND bot_id=? "
                "AND chat_id=? AND access_version=? AND state!='deleted'",
                (p.existing_fact_id, work["bot_id"], work["chat_id"], work["access_version"]),
            )
            if not claimed or not current or current[0] != claimed["version"]:
                return
        key = p.key.strip().casefold()
        latest = max(s["event_id"] for s in work["sources"] if s["id"] in p.source_ids)
        tomb = await one(
            conn,
            "SELECT MAX(through_event) FROM memory_tombstones WHERE bot_id=? AND chat_id=? "
            "AND sender_id=?",
            (work["bot_id"], work["chat_id"], p.sender_id),
        )
        if tomb and tomb[0] is not None and latest <= tomb[0]:
            return
        async with conn.execute(
            "SELECT id,text,curated,state FROM memory_facts WHERE bot_id=? AND chat_id=? "
            "AND access_version=? AND sender_id=? AND category=? AND fact_key=? AND "
            "state!='deleted'",
            (work["bot_id"], work["chat_id"], work["access_version"], p.sender_id, p.category, key),
        ) as cursor:
            previous = list(await cursor.fetchall())
        same = next(
            (f for f in previous if f[1].strip().casefold() == p.text.strip().casefold()), None
        )
        if p.relation == "supports":
            same = next((f for f in previous if f[0] == p.existing_fact_id), None)
        if same:
            fact_id = same[0]
            if same[3] == "outdated" and not same[2]:
                # New independent evidence replaces invalid context, rather than inheriting it.
                await conn.execute("DELETE FROM memory_evidence WHERE fact_id=?", (fact_id,))
                await conn.execute(
                    "UPDATE memory_facts SET context_dependencies=NULL,version=version+1 "
                    "WHERE id=?",
                    (fact_id,),
                )
            await conn.execute(
                "UPDATE memory_facts SET reviewed_at=?,updated_at=?,state=CASE "
                "WHEN state='outdated' AND curated=0 THEN ? ELSE state END WHERE id=?",
                (time.time(), stamp(), "active" if p.provenance == "self" else "proposed", fact_id),
            )
        else:
            state = "active" if p.provenance == "self" else "proposed"
            if previous and p.relation == "contradicts":
                state = "proposed" if any(f[2] for f in previous) else "disputed"
                for fact in previous:
                    if not fact[2]:
                        await conn.execute(
                            "UPDATE memory_facts SET state='disputed',version=version+1 WHERE id=?",
                            (fact[0],),
                        )
                        await conn.execute(
                            "UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fact[0],)
                        )
                # Conflict immediately invalidates already prepared context and old answers.
                await invalidate(conn, work["bot_id"], work["chat_id"], True)
            elif previous:
                # Wording alone is not proof of a contradiction.
                state = "proposed"
            cursor = await conn.execute(
                "INSERT INTO "
                "memory_facts(bot_id,chat_id,scope,access_version,sender_id,category,fact_key,text,"
                "provenance,state,created_at,updated_at,reviewed_at,observation_kind) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work["bot_id"],
                    work["chat_id"],
                    work["scope"],
                    work["access_version"],
                    p.sender_id,
                    p.category,
                    key,
                    p.text.strip(),
                    p.provenance,
                    state,
                    stamp(),
                    stamp(),
                    time.time(),
                    p.observation_kind,
                ),
            )
            fact_id = cursor.lastrowid
        if not same or (same[3] == "outdated" and not same[2]):
            context_deps = dependencies()
            if p.observation_kind == "none":
                for source_id in p.source_ids:
                    merge_dependencies(
                        context_deps, work.get("contexts", {}).get(source_id, dependencies())
                    )
            await conn.execute(
                "UPDATE memory_facts SET context_dependencies=? WHERE id=?",
                (json.dumps(context_deps), fact_id),
            )
        # A supported fact retains its original sufficient proof. A later confirmation
        # may quote a bot answer based on this very fact; requiring it would create a cycle.
        for source_id in p.source_ids:
            event_id = next(s["event_id"] for s in work["sources"] if s["id"] == source_id)
            await conn.execute(
                "INSERT INTO memory_evidence(fact_id,source_id,event_id,provenance,batch_id,"
                "context_dependencies,supporting) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(fact_id,source_id) DO NOTHING",
                (
                    fact_id,
                    source_id,
                    event_id,
                    p.provenance,
                    work["id"],
                    json.dumps(work.get("contexts", {}).get(source_id, dependencies())),
                    int(p.relation in {"new", "supports"}),
                ),
            )
        if p.relation == "contradicts" and fact_id != p.existing_fact_id:
            # Preserve only an explicit new relation. Equal keys/wording are not proof.
            proof = dependencies()
            for source_id in p.source_ids:
                source = next(s for s in work["sources"] if s["id"] == source_id)
                proof["sources"].append({"id": source_id, "event_id": source["event_id"]})
                merge_dependencies(proof, work.get("contexts", {}).get(source_id, dependencies()))
            origin = await one(conn, "SELECT version FROM memory_facts WHERE id=?", (fact_id,))
            target = await one(
                conn, "SELECT version FROM memory_facts WHERE id=?", (p.existing_fact_id,)
            )
            await conn.execute(
                "INSERT INTO memory_relations(bot_id,chat_id,access_version,kind,from_id,"
                "from_version,to_id,to_version,batch_id,proof,created_at) "
                "VALUES (?,?,?,'contradicts',?,?,?,?,?,?,?)",
                (
                    work["bot_id"],
                    work["chat_id"],
                    work["access_version"],
                    fact_id,
                    origin[0],
                    p.existing_fact_id,
                    target[0],
                    work["id"],
                    json.dumps(proof),
                    time.time(),
                ),
            )

    async def prepare_search(self, bot: str, pending_sql: str) -> tuple[Any, Ranking | None]:
        snapshot = await self.db.settings()
        settings = snapshot.settings
        if (
            not settings.dialogue_enabled
            or not settings.memory_enabled
            or settings.memory_retrieval != "e5"
        ):
            self.semantic_index.deactivate()
            return None, None
        self.semantic_index.schedule(bot)
        job = await self.db.one(pending_sql, (bot,))
        if job is None:
            return None, None
        _, payload_json, grant, update_json, *_ = job
        payload = json.loads(payload_json)
        access = await self.db.one(
            "SELECT 1 FROM telegram_access WHERE bot_id=? AND scope=? AND subject_id=? "
            "AND version=? AND state='approved' "
            "AND member_state NOT IN ('left','kicked','migrated')",
            (bot, payload["scope"], payload["chat_id"], grant),
        )
        if access is None:
            return None, None
        update = json.loads(update_json)
        message = update.get("message") or update.get("edited_message") or {}
        query = query_text(message)
        return (snapshot.version, job, query), await self.semantic_index.rank(bot, query)

    async def context(
        self,
        conn: aiosqlite.Connection,
        bot: str,
        chat: str,
        access: int,
        participants: list[str],
        query: str = "",
        *,
        diagnostics: dict[str, Any] | None = None,
        current_query: str | None = None,
        recent: list[dict[str, Any]] | None = None,
        event_id: int | None = None,
        speaker: str = "",
        thread: int | None = None,
        semantic: Ranking | None = None,
    ) -> tuple[dict[str, Any], int]:
        from zarya.memory_retrieval import select_context

        version = await epoch(conn, bot)
        settings = Settings.model_validate_json(
            (await one(conn, "SELECT document FROM settings WHERE id=1"))[0]
        )
        if not settings.memory_enabled:
            return {"facts": [], "summaries": []}, version
        result = await select_context(
            conn,
            bot,
            chat,
            access,
            participants,
            query,
            settings.memory_retrieval,
            version,
            diagnostics,
            current_query=current_query if current_query is not None else query,
            recent=recent or [],
            event_id=event_id,
            speaker=speaker,
            thread=thread,
            semantic=semantic,
        )
        return result, version

    async def valid(self, conn: aiosqlite.Connection, bot: str, snapshot: dict[str, Any]) -> bool:
        return (
            not snapshot.get("invalidated")
            and (
                "memory_epoch" not in snapshot or await epoch(conn, bot) == snapshot["memory_epoch"]
            )
            and await valid_snapshot(conn, bot, snapshot)
        )

    async def mutate(
        self,
        bot: str,
        fact_id: int,
        body: MemoryMutation,
        *,
        operation_id: str | None = None,
        change_id: str | None = None,
        undo_id: str | None = None,
    ) -> None:
        from zarya.memory_command_apply import apply_command

        async with self.store.gate, self.db.transaction() as conn:
            await apply_command(self, conn, bot, fact_id, body, operation_id, change_id, undo_id)

    async def _mutate(
        self,
        conn: aiosqlite.Connection,
        bot: str,
        fact_id: int,
        body: MemoryMutation,
    ) -> None:
        fact = await one(
            conn,
            "SELECT chat_id,scope,sender_id,category,version,state,text FROM memory_facts "
            "WHERE bot_id=? AND id=?",
            (bot, fact_id),
        )
        if not fact or fact[4] != body.expected_version or fact[5] == "deleted":
            raise ConflictError
        chat, scope, sender, category, version, state, text = fact
        access = await one(
            conn,
            "SELECT 1 FROM telegram_access a JOIN memory_facts f ON f.bot_id=a.bot_id "
            "AND f.chat_id=a.subject_id AND f.scope=a.scope AND f.access_version=a.version "
            "WHERE f.id=? AND a.state='approved' AND a.member_state NOT IN "
            "('left','kicked','migrated')",
            (fact_id,),
        )
        if not access:
            raise ValueError("Доступ к исходному чату отозван")
        sharing = await one(conn, "SELECT state FROM memory_shares WHERE fact_id=?", (fact_id,))
        if body.action == "delete":
            await erase_fact(conn, bot, fact_id)
            return
        if body.action == "share":
            if category not in SHAREABLE or state != "active":
                raise ValueError(
                    "Перенос доступны только активным именам, интересам и предпочтениям"
                )
            if scope == "private":
                raise ValueError(
                    "Разрешить перенос личного сведения может только собеседник: /memory в личке"
                )
            await conn.execute(
                "INSERT INTO memory_shares VALUES (?,?,'active','admin','admin',NULL,?) "
                "ON CONFLICT(fact_id) DO UPDATE SET "
                "fact_version=excluded.fact_version,state='active',"
                "authority='admin',actor_id='admin',consent_event_id=NULL,updated_at=excluded.updated_at",
                (fact_id, version, stamp()),
            )
        elif body.action == "unshare":
            await conn.execute(
                "UPDATE memory_shares SET state='revoked',updated_at=? WHERE fact_id=?",
                (stamp(), fact_id),
            )
        elif body.action in {"accept", "edit"}:
            if body.action == "edit" and body.text is None:
                raise ValueError("Введите текст записи")
            async with conn.execute(
                "SELECT source_id FROM memory_evidence WHERE fact_id=?", (fact_id,)
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
            if body.action == "edit":
                if await source_sharing(conn, ids):
                    sharing = ("active",)
                await suppress_sources(conn, bot, chat, ids, scrub=False)
                await conn.execute(
                    "UPDATE memory_facts SET context_dependencies=NULL WHERE id=?", (fact_id,)
                )
            # A selected resolution supersedes competing proposals with the same semantic key.
            competitor_share = await one(
                conn,
                "SELECT 1 FROM memory_shares s JOIN memory_facts f ON f.id=s.fact_id "
                "WHERE s.state='active' AND f.bot_id=? AND f.chat_id=? AND f.sender_id=? "
                "AND f.category=? AND f.fact_key=(SELECT fact_key FROM memory_facts "
                "WHERE id=?)",
                (bot, chat, sender, category, fact_id),
            )
            if competitor_share:
                sharing = ("active",)
            await conn.execute(
                "UPDATE memory_shares SET state='revoked' WHERE fact_id IN "
                "(SELECT id FROM memory_facts WHERE bot_id=? AND chat_id=? AND sender_id=? "
                "AND category=? AND fact_key=(SELECT fact_key FROM memory_facts WHERE id=?))",
                (bot, chat, sender, category, fact_id),
            )
            await conn.execute(
                "UPDATE memory_facts SET state='outdated',version=version+1 WHERE bot_id=? "
                "AND chat_id=? "
                "AND sender_id=? AND category=? AND fact_key=(SELECT fact_key FROM "
                "memory_facts WHERE id=?) "
                "AND id!=? AND state!='deleted'",
                (bot, chat, sender, category, fact_id, fact_id),
            )
            await conn.execute(
                "UPDATE memory_facts SET "
                "text=?,state='active',curated=1,version=version+1,updated_at=?,reviewed_at=? "
                "WHERE id=?",
                (body.text if body.action == "edit" else text, stamp(), time.time(), fact_id),
            )
            await conn.execute(
                "UPDATE memory_shares SET state='revoked' WHERE fact_id=?", (fact_id,)
            )
            await conn.execute("DELETE FROM memory_consent_requests WHERE fact_id=?", (fact_id,))
        await invalidate(
            conn,
            bot,
            chat,
            bool(sharing and sharing[0] == "active") or body.action in {"share", "unshare"},
        )

    async def listing(self, bot: str | None, scope: str) -> dict[str, Any]:
        chats = await self.db.all(
            "SELECT a.subject_id,a.scope,a.title,COUNT(DISTINCT s.sender_id),COUNT(DISTINCT f.id) "
            "FROM telegram_access a LEFT JOIN memory_sources s ON s.bot_id=a.bot_id AND "
            "s.chat_id=a.subject_id "
            "LEFT JOIN memory_facts f ON f.bot_id=a.bot_id AND f.chat_id=a.subject_id AND "
            "f.state!='deleted' "
            "WHERE a.bot_id=? AND a.state='approved' AND a.member_state NOT IN "
            "('left','kicked','migrated') "
            "AND (?='all' OR a.scope=?) GROUP BY a.subject_id,a.scope,a.title ORDER BY a.title",
            (bot, scope, scope),
        )
        calls = await self.db.one(
            "SELECT SUM(m.cost_usd),SUM(m.cost_usd IS NULL) FROM model_calls m JOIN "
            "memory_batches b ON b.id=m.memory_batch_id "
            "WHERE b.bot_id=?",
            (bot,),
        )
        settings = (await self.db.settings()).settings
        return {
            "chats": [
                dict(zip(("chat_id", "scope", "title", "participants", "facts"), r, strict=True))
                for r in chats
            ],
            "bot_id": bot,
            "enabled": settings.memory_enabled,
            "configured": self.adapter is not None,
            "model": settings.memory_model,
            "known_cost_usd": calls[0],
            "unknown_cost_calls": calls[1] or 0,
            "retention_days": 90,
        }

    async def profile(self, bot: str, chat: str) -> dict[str, Any] | None:
        peer = await self.db.one(
            "SELECT scope,title,version FROM telegram_access WHERE bot_id=? AND subject_id=? "
            "AND state='approved' AND member_state NOT IN ('left','kicked','migrated')",
            (bot, chat),
        )
        if not peer:
            return None
        facts = await self.db.all(
            "SELECT "
            "id,sender_id,category,text,provenance,state,version,curated,created_at,updated_at "
            "FROM memory_facts WHERE bot_id=? AND chat_id=? AND access_version=? AND "
            "state!='deleted' ORDER BY id DESC LIMIT 200",
            (bot, chat, peer[2]),
        )
        items = []
        for f in facts:
            item = dict(
                zip(
                    (
                        "id",
                        "sender_id",
                        "category",
                        "text",
                        "provenance",
                        "state",
                        "version",
                        "curated",
                        "created_at",
                        "updated_at",
                    ),
                    f,
                    strict=True,
                )
            )
            evidence = await self.db.all(
                "SELECT s.id,s.sender_id,s.name,s.message_id,s.thread_id,CASE WHEN s.valid=1 "
                "AND s.event_id=e.event_id "
                "THEN s.text ELSE '' END,s.received_at FROM memory_evidence e JOIN "
                "memory_sources s ON s.id=e.source_id "
                "WHERE e.fact_id=?",
                (f[0],),
            )
            item["sources"] = [
                dict(
                    zip(
                        (
                            "id",
                            "sender_id",
                            "name",
                            "message_id",
                            "thread_id",
                            "text",
                            "received_at",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in evidence
            ]
            share = await self.db.one(
                "SELECT state,authority,actor_id,consent_event_id,fact_version FROM "
                "memory_shares WHERE fact_id=?",
                (f[0],),
            )
            item["share"] = (
                dict(
                    zip(
                        ("state", "authority", "actor_id", "consent_event_id", "fact_version"),
                        share,
                        strict=True,
                    )
                )
                if share
                else None
            )
            items.append(item)
            async with self.db.transaction() as conn:
                item["confidence"] = await strength(conn, bot, f[0])
        summaries = await self.db.all(
            "SELECT "
            "b.id,b.state,b.summary,b.created_at,b.finished_at,b.error_code,m.model,"
            "m.usage_json,m.cost_usd,m.latency_ms "
            "FROM memory_batches b LEFT JOIN model_calls m ON m.memory_batch_id=b.id WHERE "
            "b.bot_id=? AND b.chat_id=? "
            "AND b.access_version=? ORDER BY b.id DESC LIMIT 30",
            (bot, chat, peer[2]),
        )
        participants = await self.db.all(
            "SELECT sender_id,MAX(name),MAX(received_at) FROM ("
            "SELECT sender_id,name,received_at FROM memory_sources WHERE bot_id=? AND chat_id=? "
            "AND access_version=? AND valid=1 AND received_at>? UNION ALL "
            "SELECT p.sender_id,COALESCE("
            "json_extract(e.payload,'$.edited_message.from.first_name'),"
            "json_extract(e.payload,'$.message.from.first_name'),'Участник'),p.received_at "
            "FROM participant_messages p JOIN research_messages m ON m.bot_id=p.bot_id "
            "AND m.chat_id=p.chat_id AND m.message_id=p.message_id AND m.event_id=p.event_id "
            "AND m.access_version=p.access_version JOIN events e ON e.id=p.event_id "
            "WHERE p.bot_id=? AND p.chat_id=? AND p.access_version=? AND p.received_at>? "
            "AND e.payload!='{}' AND e.disposition='accepted') "
            "GROUP BY sender_id ORDER BY MAX(received_at) DESC LIMIT 100",
            (
                bot,
                chat,
                peer[2],
                time.time() - RETENTION,
                bot,
                chat,
                peer[2],
                time.time() - RETENTION,
            ),
        )
        return {
            "bot_id": bot,
            "chat_id": chat,
            "scope": peer[0],
            "title": peer[1],
            "facts": items,
            "participants": [
                dict(zip(("sender_id", "name", "last_seen"), r, strict=True)) for r in participants
            ],
            "batches": [
                dict(
                    zip(
                        (
                            "id",
                            "state",
                            "summary",
                            "created_at",
                            "finished_at",
                            "error",
                            "model",
                            "usage",
                            "cost_usd",
                            "latency_ms",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in summaries
            ],
        }

    async def participant_window(self, bot: str, chat: str, sender: str) -> dict[str, Any] | None:
        from zarya.participant_context import assistants, window

        async with self.db.read_lock:
            conn = self.db.reader
            await conn.execute("BEGIN")
            try:
                peer = await one(
                    conn,
                    "SELECT version FROM telegram_access WHERE bot_id=? AND subject_id=? "
                    "AND state='approved' AND member_state NOT IN ('left','kicked','migrated')",
                    (bot, chat),
                )
                if not peer:
                    return None
                settings = Settings.model_validate_json(
                    (await one(conn, "SELECT document FROM settings WHERE id=1"))[0]
                )
                messages, sources, count, _ = await window(
                    conn,
                    bot,
                    chat,
                    peer[0],
                    sender,
                    settings.participant_context_messages,
                )
                parts, _ = await assistants(conn, bot, chat, peer[0], messages)
                from zarya.participant_summaries import compact

                _, summary_coverage = await compact(
                    conn, bot, chat, peer[0], sender, messages, sources, settings
                )
                return {
                    "sender_id": sender,
                    "limit": settings.participant_context_messages,
                    "available": count,
                    "human_messages": len(messages),
                    "assistant_messages": len(parts),
                    "summaries": summary_coverage,
                    "messages": sorted(
                        messages + parts,
                        key=lambda m: (m["received_at"], m["message_id"]),
                    ),
                }
            finally:
                await conn.rollback()

    async def cleanup(self, bot: str, *, force: bool = False) -> None:
        from zarya.behavior import cleanup as cleanup_behavior

        if not force and time.time() - self.last_cleanup < 3600:
            return
        self.last_cleanup = time.time()
        cutoff = time.time() - RETENTION
        cutoff_stamp = datetime.fromtimestamp(cutoff, UTC).isoformat()
        paths = []
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE dialogue_runs SET snapshot=json_remove(snapshot,'$.retrieval') "
                "WHERE bot_id=? AND (json_extract(snapshot,'$.retrieval.at')<? OR "
                "json_extract(snapshot,'$.retrieval.epoch')!=(SELECT version FROM memory_epochs "
                "WHERE bot_id=?))",
                (bot, cutoff, bot),
            )
            await conn.execute(
                "DELETE FROM memory_relations WHERE bot_id=? AND created_at<?",
                (bot, cutoff),
            )
            await conn.execute(
                "UPDATE memory_commands SET before_text=NULL,after_text=NULL "
                "WHERE bot_id=? AND created_at<?",
                (bot, cutoff_stamp),
            )
            await conn.execute(
                "UPDATE obsidian_changes SET before_text=NULL,proposed_text=NULL,"
                "state='invalidated' "
                "WHERE bot_id=? AND created_at<?",
                (bot, cutoff_stamp),
            )
            old = await one(
                conn,
                "SELECT 1 FROM memory_sources WHERE bot_id=? AND received_at<? AND text!='' "
                "LIMIT 1",
                (bot, cutoff),
            )
            if old:
                async with conn.execute(
                    "SELECT DISTINCT chat_id FROM memory_sources WHERE bot_id=? "
                    "AND received_at<? AND text!=''",
                    (bot, cutoff),
                ) as cursor:
                    expired_chats = [r[0] for r in await cursor.fetchall()]
                for chat in expired_chats:
                    from zarya.memory_commands import scrub_scope

                    await scrub_scope(conn, bot, chat)
                    await invalidate(conn, bot, chat)
            await conn.execute(
                "UPDATE memory_sources SET text='',valid=0,processed=1 WHERE bot_id=? AND "
                "received_at<?",
                (bot, cutoff),
            )
            await conn.execute(
                "DELETE FROM recent_messages WHERE bot_id=? AND received_at<?", (bot, cutoff)
            )
            await conn.execute(
                "UPDATE memory_batches SET summary=NULL WHERE bot_id=? AND created_at<?",
                (bot, cutoff_stamp),
            )
            await conn.execute(
                "UPDATE events SET payload='{}' WHERE bot_id=? AND received_at<?",
                (bot, cutoff_stamp),
            )
            from zarya.research_logic import purge as purge_research
            from zarya.source_material import purge as purge_material

            await purge_material(
                conn,
                "bot_id=? AND (EXISTS (SELECT 1 FROM "
                "json_each(snapshot,'$.source_manifest') m LEFT JOIN events e "
                "ON e.id=json_extract(m.value,'$.event_id') "
                "WHERE e.id IS NULL OR e.payload='{}' OR e.received_at<?) "
                "OR EXISTS (SELECT 1 FROM json_each(snapshot,'$.context_refs.humans') m "
                "LEFT JOIN events e ON e.id=json_extract(m.value,'$.event_id') "
                "WHERE e.id IS NULL OR e.payload='{}' OR e.received_at<? "
                "OR json_extract(m.value,'$.received_at')<?))",
                (bot, cutoff_stamp, cutoff_stamp, cutoff),
            )

            await purge_research(
                conn,
                "bot_id=? AND (created_at<? OR EXISTS "
                "(SELECT 1 FROM json_each(manifest) m JOIN events e "
                "ON e.id=json_extract(m.value,'$.event_id') WHERE e.payload='{}'))",
                (bot, cutoff_stamp),
            )
            await conn.execute(
                "DELETE FROM participant_messages WHERE bot_id=? AND (received_at<? OR "
                "NOT EXISTS (SELECT 1 FROM events e WHERE e.id=participant_messages.event_id "
                "AND e.payload!='{}'))",
                (bot, cutoff),
            )
            from zarya.participant_summaries import purge as purge_summaries

            await purge_summaries(
                conn,
                "bot_id=? AND state!='cancelled' AND EXISTS (SELECT 1 FROM json_each(manifest) r "
                "LEFT JOIN participant_messages p ON p.bot_id=participant_summary_blocks.bot_id "
                "AND p.chat_id=participant_summary_blocks.chat_id "
                "AND p.message_id=json_extract(r.value,'$.message_id') "
                "AND p.event_id=json_extract(r.value,'$.event_id') "
                "WHERE p.event_id IS NULL OR p.received_at<?)",
                (bot, cutoff),
            )
            from zarya.participant_provenance import cascade_invalid

            await cascade_invalid(conn)
            await conn.execute(
                "DELETE FROM research_messages WHERE bot_id=? AND event_id IN "
                "(SELECT id FROM events WHERE received_at<?)",
                (bot, cutoff_stamp),
            )
            await conn.execute(
                "UPDATE model_calls SET request_json=NULL WHERE created_at<? AND (run_id IN "
                "(SELECT id FROM dialogue_runs WHERE bot_id=?) "
                "OR memory_batch_id IN (SELECT id FROM memory_batches WHERE bot_id=?) OR "
                "photo_batch_id IN (SELECT id FROM photo_batches WHERE bot_id=?))",
                (cutoff_stamp, bot, bot, bot),
            )
            await conn.execute(
                "UPDATE dialogue_runs SET snapshot=?,response=NULL,error_code='retention' "
                "WHERE bot_id=? AND created_at<?",
                (
                    json.dumps(
                        {
                            "invalidated": True,
                            "incoming": {"text": "", "sender_id": "", "message_id": 0},
                            "context": [],
                            "settings": {},
                            "owner": False,
                            "settings_version": 0,
                        }
                    ),
                    bot,
                    cutoff_stamp,
                ),
            )
            await conn.execute(
                "UPDATE outbox SET payload='{\"text\":\"\"}',state=CASE WHEN state='pending' "
                "THEN 'cancelled' ELSE state END "
                "WHERE bot_id=? AND created_at<?",
                (bot, cutoff_stamp),
            )
            await cleanup_behavior(conn, bot, cutoff)
            from zarya.avatar_logic import purge as purge_avatars

            await purge_avatars(
                conn,
                "bot_id=? AND (created_at<? OR EXISTS "
                "(SELECT 1 FROM json_each(binding) m LEFT JOIN events e "
                "ON e.id=json_extract(m.value,'$.event_id') WHERE e.id IS NULL "
                "OR e.payload='{}' OR e.received_at<?))",
                (bot, cutoff_stamp, cutoff_stamp),
            )
            await conn.execute(
                "UPDATE jobs SET payload='{}',state=CASE WHEN state IN ('pending','running') "
                "THEN 'cancelled' ELSE state END "
                "WHERE event_id IN (SELECT id FROM events WHERE bot_id=?) AND created_at<?",
                (bot, cutoff_stamp),
            )
            expired_photos = (
                "SELECT b.id FROM photo_batches b WHERE b.bot_id=? AND (b.created_at<? "
                "OR EXISTS (SELECT 1 FROM photo_items i LEFT JOIN events e ON e.id=i.event_id "
                "WHERE i.batch_id=b.id AND (e.id IS NULL OR e.payload='{}' OR e.received_at<?)))"
            )
            photo_args = (bot, cutoff_stamp, cutoff_stamp)
            async with conn.execute(
                "SELECT raw_path,image_path FROM photo_items WHERE batch_id IN ("
                + expired_photos
                + ")",
                photo_args,
            ) as cursor:
                paths = [p for r in await cursor.fetchall() for p in r if p]
            for path in paths:
                await conn.execute(
                    "INSERT OR IGNORE INTO memory_file_cleanup VALUES (?,?)", (bot, path)
                )
            await conn.execute(
                "UPDATE model_calls SET request_json=NULL WHERE photo_batch_id IN ("
                + expired_photos
                + ")",
                photo_args,
            )
            await conn.execute(
                "UPDATE photo_items SET caption='',raw_path=NULL,image_path=NULL WHERE batch_id IN "
                "(" + expired_photos + ")",
                photo_args,
            )
            await conn.execute(
                "UPDATE photo_batches SET "
                "state='cancelled',result=NULL,manifest=NULL,error_code='retention' WHERE "
                "id IN (" + expired_photos + ")",
                photo_args,
            )
            await conn.execute(
                "DELETE FROM memory_consent_requests WHERE bot_id=? AND expires_at<?",
                (bot, time.time()),
            )
            # Facts are retained; old unsupported beliefs stop entering prompts until reviewed.
            stale = await one(
                conn,
                "SELECT 1 FROM memory_facts WHERE bot_id=? AND state='active' AND "
                "reviewed_at<? LIMIT 1",
                (bot, cutoff),
            )
            if stale:
                async with conn.execute(
                    "SELECT f.chat_id,MAX(CASE WHEN s.state='active' THEN 1 ELSE 0 END) "
                    "FROM memory_facts f LEFT JOIN memory_shares s ON s.fact_id=f.id "
                    "WHERE f.bot_id=? AND f.state='active' AND f.reviewed_at<? GROUP BY f.chat_id",
                    (bot, cutoff),
                ) as cursor:
                    stale_chats = list(await cursor.fetchall())
                await conn.execute(
                    "UPDATE memory_facts SET state='outdated',version=version+1 WHERE bot_id=? "
                    "AND state='active' AND reviewed_at<?",
                    (bot, cutoff),
                )
                for chat, shared in stale_chats:
                    await invalidate(conn, bot, chat, bool(shared))
                await conn.execute(
                    "UPDATE memory_shares SET state='revoked' WHERE fact_id IN (SELECT id FROM "
                    "memory_facts WHERE bot_id=? AND state='outdated')",
                    (bot,),
                )
                await epoch(conn, bot)
                await conn.execute(
                    "UPDATE memory_epochs SET version=version+1 WHERE bot_id=?", (bot,)
                )
        root = await asyncio.to_thread((self.data_dir / "photos").resolve)
        queued = await self.db.all("SELECT path FROM memory_file_cleanup WHERE bot_id=?", (bot,))
        for (value,) in queued:
            path = await asyncio.to_thread((root / value).resolve)
            if path.is_relative_to(root):
                try:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                except OSError:
                    continue
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "DELETE FROM memory_file_cleanup WHERE bot_id=? AND path=?", (bot, value)
                    )
