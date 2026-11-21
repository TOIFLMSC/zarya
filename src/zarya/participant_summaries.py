"""Immutable, demand-admitted personal blocks; no summary-of-summary or paid retry."""

import asyncio
import hashlib
import json
import re
import time
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zarya.models import Settings
from zarya.openai_adapter import ModelResult, estimate_cost, pricing_profile
from zarya.source_material import one
from zarya.telegram_store import stamp

BLOCK_SIZE = 5
FRESH = 2
QUEUE_LIMIT = 32
RETENTION = 90 * 86400


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1800)
    source_ids: list[int] = Field(min_length=5, max_length=5)


def enabled(settings: Settings) -> bool:
    return settings.memory_enabled and settings.participant_summaries_enabled


async def valid_sources(c: aiosqlite.Connection, block: dict[str, Any]) -> bool:
    """Batch-check exact revisions, author, grant and original source age, never text cache."""
    refs = block["manifest"]
    if len(refs) != BLOCK_SIZE or len({r["message_id"] for r in refs}) != BLOCK_SIZE:
        return False
    cutoff = time.time() - RETENTION
    row = await one(
        c,
        "SELECT COUNT(*) FROM json_each(?) ref JOIN participant_messages p "
        "ON p.message_id=json_extract(ref.value,'$.message_id') "
        "AND p.event_id=json_extract(ref.value,'$.event_id') "
        "AND p.received_at=json_extract(ref.value,'$.received_at') "
        "JOIN research_messages m ON m.bot_id=p.bot_id AND m.chat_id=p.chat_id "
        "AND m.message_id=p.message_id AND m.event_id=p.event_id "
        "AND m.access_version=p.access_version JOIN events e ON e.id=p.event_id "
        "JOIN telegram_access a ON a.bot_id=p.bot_id AND a.subject_id=p.chat_id "
        "AND a.version=p.access_version AND a.state='approved' "
        "AND a.member_state NOT IN ('left','kicked','migrated') "
        "WHERE p.bot_id=? AND p.chat_id=? AND p.access_version=? AND p.sender_id=? "
        "AND p.received_at>? AND e.bot_id=p.bot_id AND e.chat_id=p.chat_id "
        "AND e.disposition='accepted' AND e.payload!='{}' "
        "AND CAST(strftime('%s',e.received_at) AS REAL)>?",
        (
            json.dumps(refs),
            block["bot_id"],
            block["chat_id"],
            block["access_version"],
            block["sender_id"],
            cutoff,
            cutoff,
        ),
    )
    from zarya.media_logic import refs_valid

    return row[0] == BLOCK_SIZE and await refs_valid(c, block["media_refs"])


async def blocks(
    c: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    sender: str,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Only blocks touching this window, with fresh validation even after direct erasure."""
    async with c.execute(
        "SELECT DISTINCT b.id,b.state,b.result,b.error_code,b.manifest,b.media_refs,"
        "b.created_at,b.settings_version,m.model,m.state,m.latency_ms,m.cost_usd,m.usage_json "
        "FROM participant_summary_blocks b JOIN participant_summary_sources s ON s.block_id=b.id "
        "LEFT JOIN model_calls m ON m.participant_summary_id=b.id "
        "WHERE b.bot_id=? AND b.chat_id=? AND b.access_version=? AND b.sender_id=? "
        "AND s.event_id IN (SELECT json_extract(value,'$.event_id') FROM json_each(?)) "
        "ORDER BY b.id",
        (bot, chat, grant, sender, json.dumps(sources)),
    ) as cur:
        rows = await cur.fetchall()
    result = []
    for (
        bid,
        state,
        text,
        error,
        manifest,
        media,
        at,
        version,
        model,
        call,
        latency,
        cost,
        usage,
    ) in rows:
        block = dict(
            id=bid,
            bot_id=bot,
            chat_id=chat,
            access_version=grant,
            sender_id=sender,
            manifest=json.loads(manifest),
            media_refs=json.loads(media),
        )
        ok = await valid_sources(c, block)
        block.update(
            state=state if ok else "cancelled",
            summary=text if ok else None,
            error_code=error if ok else "source_changed",
            created_at=at,
            settings_version=version,
            model=model,
            call_state=call,
            latency_ms=latency,
            cost_usd=cost,
            usage=json.loads(usage) if usage else None,
        )
        result.append(block)
    return result


async def admit(
    c: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    sender: str,
    history: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    media: list[dict[str, Any]],
) -> None:
    """Called in the dialogue transaction. Reserve each original revision at most once.

    All states reserve membership, including unknown/error/cancelled: settings changes
    or a sliding window must not disguise a retry as a new block. No eager 90-day backfill.
    """
    queued = (
        await one(
            c,
            "SELECT COUNT(*) FROM participant_summary_blocks WHERE state IN ('queued','started')",
            (),
        )
    )[0]
    if queued >= QUEUE_LIMIT:
        return
    async with c.execute(
        "SELECT s.event_id FROM participant_summary_sources s JOIN participant_summary_blocks b "
        "ON b.id=s.block_id WHERE b.bot_id=? AND b.chat_id=? AND b.access_version=? "
        "AND b.sender_id=? AND s.event_id IN "
        "(SELECT json_extract(value,'$.event_id') FROM json_each(?))",
        (bot, chat, grant, sender, json.dumps(sources)),
    ) as cur:
        occupied = {r[0] for r in await cur.fetchall()}
    by_mid = {r["message_id"]: r for r in sources}
    tail: list[dict[str, Any]] = []
    for message in history[:-FRESH]:
        ref = by_mid[message["message_id"]]
        if ref["event_id"] in occupied:
            tail = []
            continue
        tail.append(ref)
        if len(tail) < BLOCK_SIZE:
            continue
        if queued >= QUEUE_LIMIT:
            break
        # Only cached transcripts belonging to the immutable block; never ambient decode.
        mids = {r["message_id"] for r in tail}
        block_media = [
            r for m in history if m["message_id"] in mids for r in m.get("media_refs", [])
        ]
        cur = await c.execute(
            "INSERT INTO participant_summary_blocks(bot_id,chat_id,access_version,sender_id,"
            "manifest,media_refs,created_at) VALUES (?,?,?,?,?,?,?)",
            (bot, chat, grant, sender, json.dumps(tail), json.dumps(block_media), stamp()),
        )
        for source in tail:
            await c.execute(
                "INSERT INTO participant_summary_sources VALUES (?,?,?)",
                (cur.lastrowid, source["message_id"], source["event_id"]),
            )
        queued += 1
        tail = []


def protected(message: dict[str, Any]) -> bool:
    """Exact originals carry critical values, quotations, media and negative caveats."""
    return bool(
        message.get("forwarded")
        or message.get("content_kinds")
        or message.get("attachments")
        or message.get("urls")
        or re.search(r"\d|\b(?:не|нет|без|никогда|нельзя)\b", message["text"], re.IGNORECASE)
    )


async def compact(
    c: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    sender: str,
    history: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    settings: Settings,
    *,
    exact_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries = await blocks(c, bot, chat, grant, sender, sources)
    original = {m["message_id"]: m for m in history}
    current = {r["message_id"]: r for r in sources}
    fresh = {m["message_id"] for m in history[-FRESH:]}
    exact = (exact_ids or set()) | fresh
    used: set[int] = set()
    additions = []
    for block in entries:
        mids = {r["message_id"] for r in block["manifest"]}
        full = all(current.get(r["message_id"]) == r for r in block["manifest"])
        replace = {
            mid
            for mid in mids & original.keys()
            if mid not in exact and not protected(original[mid])
        }
        block["used_ids"] = []
        block["full_window"] = full
        if not full:
            block["summary"] = None
        if not (
            enabled(settings)
            and full
            and block["state"] == "completed"
            and block["summary"]
            and replace
            and not mids & used
        ):
            continue
        first = original[min(mids)]
        entry = dict(
            role="summary",
            sender_id=sender,
            name=first["name"],
            message_id=min(mids),
            received_at=first["received_at"],
            coverage="summary_not_verbatim",
            source_ids=sorted(mids),
            block_id=block["id"],
            text=block["summary"],
        )
        # Do not increase input or discard detail for a block that cannot compact.
        if len(json.dumps(entry, ensure_ascii=False)) >= sum(
            len(json.dumps(original[mid], ensure_ascii=False)) for mid in replace
        ):
            block["fallback"] = "not_smaller"
            continue
        used.update(replace)
        block["used_ids"] = sorted(replace)
        additions.append(entry)
    raw = [m for m in history if m["message_id"] not in used]
    reasons = {}
    for m in raw:
        mid = m["message_id"]
        touching = [b for b in entries if any(r["message_id"] == mid for r in b["manifest"])]
        reasons[str(mid)] = (
            "fresh"
            if mid in fresh
            else "reply"
            if mid in (exact_ids or set())
            else "protected"
            if protected(m)
            else "boundary"
            if any(not b["full_window"] for b in touching)
            else "failed"
            if any(b["state"] in {"unknown", "error", "cancelled"} for b in touching)
            else "pending"
        )
    return sorted(raw + additions, key=lambda m: (m["received_at"], m["message_id"])), {
        "enabled": enabled(settings),
        "block_size": BLOCK_SIZE,
        "covered_ids": sorted(used),
        "original_ids": [m["message_id"] for m in raw],
        "fresh_ids": sorted(fresh),
        "raw_reasons": reasons,
        "blocks": entries,
    }


async def purge(c: aiosqlite.Connection, where: str, args: tuple[object, ...]) -> None:
    ids = "SELECT id FROM participant_summary_blocks WHERE " + where
    from zarya.source_material import purge as purge_dialogues

    await purge_dialogues(
        c,
        "EXISTS (SELECT 1 FROM json_each(snapshot,'$.context_refs.summaries') "
        "s WHERE json_extract(s.value,'$.id') IN (" + ids + "))",
        args,
    )
    await c.execute(
        "UPDATE model_calls SET request_json=NULL WHERE participant_summary_id IN (" + ids + ")",
        args,
    )
    await c.execute(
        "UPDATE participant_summary_blocks SET result=NULL,state='cancelled',"
        "error_code='source_changed' WHERE " + where,
        args,
    )


class SummaryEngine:
    """One work kind owned by MemoryEngine's supervised background loop."""

    def __init__(self, store: Any, adapter: Any):
        self.store, self.db, self.adapter = store, store.db, adapter

    async def recover(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as c:
            await c.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted',"
                "finished_at=? WHERE state='started' AND participant_summary_id IN "
                "(SELECT id FROM participant_summary_blocks WHERE bot_id=?)",
                (stamp(), bot),
            )
            await c.execute(
                "UPDATE participant_summary_blocks SET state='unknown',"
                "error_code='interrupted',finished_at=? WHERE bot_id=? AND state='started'",
                (stamp(), bot),
            )

    async def claim(self, bot: str) -> dict[str, Any] | None:
        if self.adapter is None:
            return None
        from zarya.participant_context import window

        async with self.store.gate, self.db.transaction() as c:
            version, raw = await one(c, "SELECT version,document FROM settings WHERE id=1", ())
            settings = Settings.model_validate_json(raw)
            if not enabled(settings):
                return None
            row = await one(
                c,
                "SELECT id,chat_id,access_version,sender_id,manifest,media_refs "
                "FROM participant_summary_blocks WHERE bot_id=? AND state='queued' "
                "ORDER BY id LIMIT 1",
                (bot,),
            )
            if not row:
                return None
            bid, chat, grant, sender, manifest, media = row
            work = dict(
                id=bid,
                bot_id=bot,
                chat_id=chat,
                access_version=grant,
                sender_id=sender,
                manifest=json.loads(manifest),
                media_refs=json.loads(media),
            )
            messages, _, _, _ = await window(
                c, bot, chat, grant, sender, settings.participant_context_messages
            )
            ids = {r["message_id"] for r in work["manifest"]}
            if not await valid_sources(c, work) or not ids <= {m["message_id"] for m in messages}:
                await purge(c, "id=?", (bid,))
                return {"skip": True}
            originals = [m for m in messages if m["message_id"] in ids]
            request: dict[str, Any] = dict(
                model=settings.memory_model,
                reasoning={"effort": "low"},
                store=False,
                max_output_tokens=1800,
                instructions=settings.participant_summary_prompt,
                input=json.dumps(
                    {"sender_id": sender, "original_messages": originals}, ensure_ascii=False
                ),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "participant_summary",
                        "strict": True,
                        "schema": Summary.model_json_schema(),
                    }
                },
            )
            await c.execute(
                "UPDATE participant_summary_blocks SET state='started',started_at=?,"
                "settings_version=?,instruction_hash=? WHERE id=?",
                (
                    stamp(),
                    version,
                    hashlib.sha256(request["instructions"].encode()).hexdigest(),
                    bid,
                ),
            )
            await c.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,settings_version,"
                "request_json,pricing_profile,participant_summary_id,operation) "
                "VALUES ('openai',?,'started',?,?,?,?,?,'participant_summary')",
                (
                    settings.memory_model,
                    stamp(),
                    version,
                    json.dumps(request, ensure_ascii=False),
                    pricing_profile(settings.memory_model),
                    bid,
                ),
            )
            work.update(request=request, originals=originals)
            return work

    async def execute(self, work: dict[str, Any]) -> None:
        if work.get("skip"):
            return
        started = time.monotonic()
        # Fresh permission/source gate immediately before paying.
        async with self.store.gate, self.db.transaction() as c:
            row = await one(
                c, "SELECT state FROM participant_summary_blocks WHERE id=?", (work["id"],)
            )
            settings = Settings.model_validate_json(
                (await one(c, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            allowed = row[0] == "started" and enabled(settings) and await valid_sources(c, work)
            if not allowed:
                await purge(c, "id=?", (work["id"],))
                await c.execute(
                    "UPDATE model_calls SET state='cancelled',error_code='source_changed',"
                    "finished_at=? WHERE participant_summary_id=? AND state='started'",
                    (stamp(), work["id"]),
                )
                return
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
        summary = None
        if result.state == "completed":
            try:
                parsed = Summary.model_validate_json(result.text)
                if set(parsed.source_ids) != {r["message_id"] for r in work["manifest"]}:
                    raise ValueError
                numbers = set(re.findall(r"\d+(?:[.,]\d+)*", parsed.summary))
                source_numbers = set(
                    re.findall(r"\d+(?:[.,]\d+)*", " ".join(m["text"] for m in work["originals"]))
                )
                if not numbers <= source_numbers:
                    raise ValueError
                summary = parsed.summary
            except (ValidationError, ValueError):
                pass
        async with self.store.gate, self.db.transaction() as c:
            changed = await c.execute(
                "UPDATE model_calls SET state=?,model=COALESCE(?,model),"
                "usage_json=?,response_id=?,request_id=?,latency_ms=?,cost_usd=?,"
                "pricing_profile=?,error_code=?,finished_at=? "
                "WHERE participant_summary_id=? AND state='started'",
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
            state = (
                await one(
                    c, "SELECT state FROM participant_summary_blocks WHERE id=?", (work["id"],)
                )
            )[0]
            settings = Settings.model_validate_json(
                (await one(c, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            if state != "started" or not enabled(settings) or not await valid_sources(c, work):
                await purge(c, "id=?", (work["id"],))
                return
            await c.execute(
                "UPDATE participant_summary_blocks SET state=?,result=?,error_code=?,"
                "finished_at=? WHERE id=?",
                (
                    "completed" if summary else "unknown" if result.state == "unknown" else "error",
                    summary,
                    result.error or ("invalid_summary" if not summary else None),
                    stamp(),
                    work["id"],
                ),
            )
