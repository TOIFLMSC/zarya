"""On-demand profile pictures: durable work, bounded vision, memory-only image bytes."""

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from zarya.avatar_logic import AVATAR_WORD, FOLLOWUP, previous, purge, valid
from zarya.models import Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.photos import MAX_BATCH_BYTES, image_part, normalize
from zarya.source_material import accepted
from zarya.telegram_store import TelegramStore, row, stamp

MAX_IMAGES = 8
MAX_LIST = 1000


class AvatarTransport(Protocol):
    async def profile_photos(self, user_id: str, offset: int, limit: int) -> dict[str, Any]: ...
    async def download_photo(self, file_id: str) -> bytes: ...


class Observations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    descriptions: list[str] = Field(min_length=1, max_length=MAX_IMAGES)
    uncertainty: str = Field(max_length=400)


class AvatarEngine:
    def __init__(self, store: TelegramStore, adapter: ModelAdapter | None):
        self.store, self.db, self.adapter = store, store.db, adapter
        self.last_cleanup = 0.0

    async def prepare(
        self,
        conn: Any,
        job: int,
        bot: str,
        chat: str,
        scope: str,
        grant: int,
        message: dict[str, Any],
        settings: Settings,
        version: int,
    ) -> dict[str, Any] | None:
        existing = await row(conn, "SELECT id,state FROM avatar_runs WHERE job_id=?", (job,))
        if existing:
            return {
                "id": existing[0],
                "waiting": existing[1] in {"queued", "fetching", "analyzing"},
            }
        text = str(message.get("text") or message.get("caption") or "")
        explicit = bool(AVATAR_WORD.search(text))
        if not explicit and not FOLLOWUP.search(text):
            return None
        prior = await previous(conn, bot, chat, grant, message)
        if not explicit and not prior:
            return None
        source = await row(conn, "SELECT event_id FROM jobs WHERE id=?", (job,))
        binding = [{"message_id": message["message_id"], "event_id": source[0]}]
        target = None
        reply = message.get("reply_to_message") or {}
        own = bool(re.search(r"\b(?:мо(?:я|ю|ей|и|их|его|е|ё)|у меня)\b", text, re.I))
        if own:
            target = message.get("from")
            prior = None
        elif reply and not (reply.get("from") or {}).get("is_bot") and not reply.get("sender_chat"):
            target = reply.get("from")
            prior = None
            current = await accepted(conn, bot, chat, grant, reply["message_id"])
            if current:
                target = current[1].get("from") if not current[1].get("sender_chat") else None
                binding.append({"message_id": reply["message_id"], "event_id": current[0]})
        elif prior:
            target = {"id": prior["user_id"], "first_name": prior["name"]}
            binding += prior["binding"]
        # Deduplicate provenance; very long/expired chains require a fresh explicit selection.
        binding = list({r["message_id"]: r for r in binding}.values())
        user = str((target or {}).get("id", ""))
        mode = "current"
        all_requested = bool(
            re.search(
                r"\b(?:все|всё|всех)\b|сравни|какая.*лучше|"
                r"\b(?:мои|его|е[её]|твои)\s+(?:авы|аватарки)\b|"
                r"(?:оцени|посмотри|как тебе)\s+(?:эти\s+)?(?:авы|аватарки)\b",
                text,
                re.I,
            )
        )
        older_requested = bool(re.search(r"предыдущ|прошл", text, re.I))
        if all_requested and older_requested:
            mode = "older"
        elif re.search(r"остальн|следующ|ещ[её]", text, re.I):
            mode = "next"
        elif older_requested:
            mode = "previous"
        elif all_requested:
            mode = "all"
        if prior and re.search(r"сравни|лучше", text, re.I) and not explicit:
            mode = "compare"
        if mode == "current":
            for index, ordinal in enumerate(("перв", "втор", "треть"), 1):
                if re.search(r"\b" + ordinal + r"\w*\b", text, re.I):
                    mode = f"index:{index}"
        error = "target_unavailable" if not user.isdigit() or int(user) <= 0 else None
        if len(binding) > 32:
            error = "select_user_again"
        if not settings.photo_enabled:
            error = "photos_disabled"
        if not self.adapter:
            error = "not_configured"
        cursor = await conn.execute(
            "INSERT INTO avatar_runs(job_id,bot_id,chat_id,scope,access_version,user_id,"
            "target_name,binding,mode,previous_id,state,model,reasoning,settings_version,"
            "error_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job,
                bot,
                chat,
                scope,
                grant,
                user,
                str((target or {}).get("first_name", ""))[:80],
                json.dumps(binding),
                mode,
                prior["id"] if prior else None,
                "unavailable" if error else "queued",
                settings.photo_model,
                "low",
                version,
                error,
                stamp(),
            ),
        )
        return {"id": cursor.lastrowid, "waiting": error is None}

    async def context(self, conn: Any, run: int) -> dict[str, Any]:
        r = await row(
            conn,
            "SELECT user_id,target_name,binding,selection,result,state,error_code "
            "FROM avatar_runs WHERE id=?",
            (run,),
        )
        selected = json.loads(r[3])
        return {
            "user_id": r[0],
            "name": r[1],
            "binding": json.loads(r[2]),
            "available_count": selected.get("total"),
            "analysed_indices": [i["index"] for i in selected.get("items", [])]
            if r[5] in {"completed", "cache"}
            else [],
            "has_more": selected.get("has_more", False),
            "list_changed": selected.get("changed", False),
            "listing_truncated": selected.get("truncated", False),
            "static_images_only": True,
            "observations": json.loads(r[4]) if r[4] else None,
            "state": r[5],
            "error": r[6],
        }

    async def active(self, conn: Any, run: int) -> bool:
        if not await valid(conn, run):
            return False
        job = await row(
            conn,
            "SELECT j.state,j.created_at FROM avatar_runs a "
            "JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
            (run,),
        )
        from datetime import datetime

        newer = await row(
            conn,
            "SELECT 1 FROM avatar_runs a JOIN jobs j ON j.id=a.job_id "
            "JOIN jobs n ON n.id>j.id JOIN events e ON e.id=n.event_id "
            "WHERE a.id=? AND e.bot_id=a.bot_id AND e.chat_id=a.chat_id "
            "AND json_extract(n.payload,'$.sender_id')=json_extract(j.payload,'$.sender_id') "
            "AND COALESCE(json_extract(n.payload,'$.thread_id'),0)="
            "COALESCE(json_extract(j.payload,'$.thread_id'),0) "
            "AND json_extract(n.payload,'$.trigger') IN "
            "('private','name','mention','reply','continuation') LIMIT 1",
            (run,),
        )

        return bool(
            job
            and not newer
            and job[0] == "pending"
            and time.time() - datetime.fromisoformat(job[1]).timestamp() <= 600
        )

    async def recover(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted' "
                "WHERE state='started' AND avatar_run_id IN "
                "(SELECT id FROM avatar_runs WHERE bot_id=?)",
                (bot,),
            )
            await conn.execute(
                "UPDATE avatar_runs SET state='unknown',error_code='interrupted' "
                "WHERE bot_id=? AND state='analyzing'",
                (bot,),
            )
            await conn.execute(
                "UPDATE avatar_runs SET state='queued' WHERE bot_id=? AND state='fetching'", (bot,)
            )

    async def claim(self, bot: str) -> dict[str, Any] | None:
        async with self.store.gate, self.db.transaction() as conn:
            # Clear invalid cached observations as well as pending work, without erasing costs.
            if time.monotonic() - self.last_cleanup > 60:
                self.last_cleanup = time.monotonic()
                async with conn.execute(
                    "SELECT id FROM avatar_runs WHERE bot_id=? AND state!='cancelled'", (bot,)
                ) as c:
                    ids = [r[0] for r in await c.fetchall()]
                for run in ids:
                    if not await valid(conn, run):
                        await purge(conn, "id=?", (run,))
            r = await row(
                conn,
                "SELECT id,user_id,mode,previous_id,model,reasoning,settings_version "
                "FROM avatar_runs WHERE bot_id=? AND state='queued' ORDER BY id LIMIT 1",
                (bot,),
            )
            if not r:
                return None
            if not await self.active(conn, r[0]):
                await purge(conn, "id=?", (r[0],))
                return {"skip": True}
            prior = await row(conn, "SELECT selection FROM avatar_runs WHERE id=?", (r[3],))
            await conn.execute("UPDATE avatar_runs SET state='fetching' WHERE id=?", (r[0],))
            return dict(
                zip(
                    ("id", "user", "mode", "previous", "model", "reasoning", "version"),
                    r,
                    strict=True,
                ),
                prior=json.loads(prior[0]) if prior else {},
            )

    async def execute(self, work: dict[str, Any], transport: AvatarTransport) -> None:
        started = time.monotonic()
        paid = False
        selection: dict[str, Any] = {}
        try:
            all_items: list[dict[str, Any]] = []
            async with asyncio.timeout(45):
                total = 0
                for offset in range(0, MAX_LIST, 100):
                    page = await transport.profile_photos(work["user"], offset, 100)
                    total = int(page["total_count"])
                    photos = page["photos"]
                    for sizes in photos:
                        if not sizes:
                            continue
                        photo = max(sizes, key=lambda p: p["width"] * p["height"])
                        all_items.append(
                            {
                                "id": photo["file_unique_id"],
                                "file_id": photo["file_id"],
                                "index": len(all_items) + 1,
                            }
                        )
                    if not photos or offset + len(photos) >= total:
                        break
            if len({p["id"] for p in all_items}) != len(all_items):
                raise ValueError("profile_changed_during_fetch")
            prior = work["prior"]
            old_ids = [i["id"] for i in prior.get("items", [])]
            ids = [i["id"] for i in all_items]
            changed = bool(prior and (prior.get("total") != total or prior.get("head") != ids[:8]))
            start, count = 0, 1
            if work["mode"].startswith("index:"):
                start = int(work["mode"].split(":")[1]) - 1
            if work["mode"] == "all":
                count = MAX_IMAGES
            elif work["mode"] == "older":
                start, count = 1, MAX_IMAGES
            elif work["mode"] in {"previous", "next"}:
                anchor = old_ids[-1] if old_ids else (ids[0] if ids else None)
                if anchor and anchor not in ids:
                    raise ValueError("selection_changed_select_again")
                start = ids.index(anchor) + 1 if anchor else 0
                count = 1 if work["mode"] == "previous" else MAX_IMAGES
            if work["mode"] == "compare" and old_ids:
                if any(i not in ids for i in old_ids):
                    raise ValueError("selection_changed_select_again")
                chosen = [all_items[ids.index(i)] for i in old_ids]
            else:
                chosen = all_items[start : start + count]
            selection = {
                "total": total,
                "items": chosen,
                "head": ids[:8],
                "changed": changed,
                "truncated": len(all_items) < total,
                "has_more": bool(chosen and chosen[-1]["index"] < total),
            }
            if not chosen:
                raise ValueError(
                    "no_visible_photos"
                    if not all_items
                    else "listing_limit"
                    if len(all_items) < total
                    else "no_more_photos"
                )
            content: list[dict[str, Any]] = []
            size = 0
            async with asyncio.timeout(60):
                for item in chosen:
                    raw = await transport.download_photo(item["file_id"])
                    size += len(raw)
                    if size > MAX_BATCH_BYTES:
                        raise ValueError("batch_size")
                    data, _, _, _ = await asyncio.to_thread(normalize, raw)
                    item["hash"] = hashlib.sha256(data).hexdigest()
                    content.extend(
                        [
                            {"type": "input_text", "text": "Фото " + str(item["index"])},
                            image_part(data, "image/jpeg"),
                        ]
                    )
            # Recheck the selected page after downloading, before spending money.
            for item in chosen:
                page = await transport.profile_photos(work["user"], item["index"] - 1, 1)
                if (
                    page["total_count"] != total
                    or not page["photos"]
                    or item["id"] not in {p["file_unique_id"] for p in page["photos"][0]}
                ):
                    raise ValueError("profile_changed_during_fetch")
            request = {
                "model": work["model"],
                "reasoning": {"effort": work["reasoning"]},
                "store": False,
                "max_output_tokens": 3500,
                "instructions": self.store.prompts.text("media.avatar"),
                "input": [{"role": "user", "content": content}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "avatar_observations",
                        "strict": True,
                        "schema": Observations.model_json_schema(),
                    }
                },
            }
            async with self.store.gate, self.db.transaction() as conn:
                if not await self.active(conn, work["id"]):
                    await purge(conn, "id=?", (work["id"],))
                    return
                current = await row(
                    conn,
                    "SELECT bot_id,chat_id,access_version,user_id FROM avatar_runs WHERE id=?",
                    (work["id"],),
                )
                async with conn.execute(
                    "SELECT id,selection,result FROM avatar_runs WHERE bot_id=? AND chat_id=? "
                    "AND access_version=? AND user_id=? AND model=? AND reasoning=? "
                    "AND state IN ('completed','cache') ORDER BY id DESC LIMIT 100",
                    (*current, work["model"], work["reasoning"]),
                ) as c:
                    cache = await c.fetchall()
                fingerprint = [(i["id"], i["hash"], i["index"]) for i in chosen]
                for cached in cache:
                    previous_items = json.loads(cached[1]).get("items", [])
                    if [
                        (i["id"], i["hash"], i["index"]) for i in previous_items
                    ] == fingerprint and await valid(conn, cached[0]):
                        await conn.execute(
                            "UPDATE avatar_runs SET state='cache',selection=?,"
                            "result=?,checked_at=? WHERE id=?",
                            (json.dumps(selection), cached[2], stamp(), work["id"]),
                        )
                        return
                await conn.execute(
                    "UPDATE avatar_runs SET state='analyzing',selection=?,checked_at=? WHERE id=?",
                    (json.dumps(selection), stamp(), work["id"]),
                )
                await conn.execute(
                    "INSERT INTO model_calls(provider,model,state,created_at,"
                    "avatar_run_id,settings_version,pricing_profile) "
                    "VALUES ('openai',?,'started',?,?,?,?)",
                    (
                        work["model"],
                        stamp(),
                        work["id"],
                        work["version"],
                        pricing_profile(work["model"]),
                    ),
                )
            paid = True
            started = time.monotonic()
            assert self.adapter is not None
            result = await self.adapter.generate(request)
            if result.state == "completed":
                try:
                    observations = Observations.model_validate_json(result.text)
                    if len(observations.descriptions) != len(chosen) or any(
                        len(s) > 900 for s in observations.descriptions
                    ):
                        raise ValueError("invalid_observations")
                    result.text = observations.model_dump_json()
                except ValueError:
                    result.state, result.text, result.error = "invalid", "", "invalid_observations"
        except asyncio.CancelledError:
            await self.finish(
                work,
                ModelResult(state="unknown" if paid else "unavailable", error="interrupted"),
                selection,
                started,
                paid,
            )
            raise
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc)
                in {
                    "selection_changed_select_again",
                    "profile_changed_during_fetch",
                    "no_visible_photos",
                    "no_more_photos",
                    "listing_limit",
                    "batch_size",
                    "file_size",
                    "image_format",
                    "image_pixels",
                }
                else "profile_fetch_failed"
                if not paid
                else "provider_uncertain"
            )
            result = ModelResult(state="unknown" if paid else "unavailable", error=code)
        await self.finish(work, result, selection, started, paid)

    async def finish(
        self,
        work: dict[str, Any],
        result: ModelResult,
        selection: dict[str, Any],
        started: float,
        paid: bool,
    ) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            if paid:
                await conn.execute(
                    "UPDATE model_calls SET state=?,model=COALESCE(?,model),usage_json=?,"
                    "latency_ms=?,cost_usd=?,error_code=?,finished_at=?,pricing_profile=? "
                    "WHERE avatar_run_id=? "
                    "AND state='started'",
                    (
                        result.state,
                        result.model,
                        json.dumps(result.usage),
                        int((time.monotonic() - started) * 1000),
                        estimate_cost(result, work["model"]),
                        result.error,
                        stamp(),
                        pricing_profile(result.model or work["model"]),
                        work["id"],
                    ),
                )
            if not await self.active(conn, work["id"]):
                await purge(conn, "id=?", (work["id"],))
                return
            await conn.execute(
                "UPDATE avatar_runs SET state=?,selection=?,result=?,error_code=?,"
                "checked_at=? WHERE id=?",
                (
                    result.state,
                    json.dumps(selection),
                    result.text if result.state == "completed" else None,
                    result.error,
                    stamp(),
                    work["id"],
                ),
            )
