"""Durable addressed-media pipeline, scoped evidence and separate ASR/vision ledger."""

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from zarya.media_analysis import VideoObservation
from zarya.media_logic import current_dependency, current_job, purge, purge_analysis, valid
from zarya.media_processing import MAX_MEDIA_BYTES, MediaProcessor
from zarya.memory_logic import admit as admit_memory
from zarya.models import Settings
from zarya.openai_adapter import (
    ModelAdapter,
    ModelResult,
    SpeechAdapter,
    estimate_cost,
    estimate_speech_cost,
    pricing_profile,
)
from zarya.photos import image_part
from zarya.source_material import accepted
from zarya.telegram_store import TelegramStore, row, stamp

VERSION = "telegram-media-v2"
KINDS = ("voice", "video", "video_note", "animation")


class MediaDownloader(Protocol):
    async def download_media(self, file_id: str) -> bytes: ...


class MediaEngine:
    def __init__(
        self,
        store: TelegramStore,
        adapter: ModelAdapter | None,
        speech: SpeechAdapter | None,
        directory: Path,
        processor: MediaProcessor | None = None,
    ):
        self.store, self.db, self.adapter, self.speech = store, store.db, adapter, speech
        self.directory = directory / "media"
        self.processor = processor or MediaProcessor()

    async def prepare(
        self,
        conn: Any,
        job: int,
        bot: str,
        chat: str,
        scope: str,
        grant: int,
        targets: list[int],
        settings: Settings,
        version: int,
    ) -> bool:
        if not settings.media_enabled:
            return False
        waiting = False
        for message_id in list(dict.fromkeys(targets))[:10]:
            source = await accepted(conn, bot, chat, grant, message_id)
            if not source:
                continue
            event, message = source
            kind = next((k for k in KINDS if message.get(k)), None)
            if not kind:
                continue
            attachment = message[kind]
            existing = await row(
                conn,
                "SELECT id,state FROM media_runs WHERE bot_id=? AND "
                "chat_id=? AND access_version=? AND event_id=? "
                "ORDER BY (processing_version=?) DESC,id DESC LIMIT 1",
                (bot, chat, grant, event, VERSION),
            )
            if not existing:
                # Up to three media files per addressed request; the rest are explicit omissions.
                count = await row(
                    conn,
                    "SELECT COUNT(*) FROM media_dependencies md JOIN media_runs mr "
                    "ON mr.id=md.media_run_id WHERE md.job_id=? "
                    "AND COALESCE(mr.error_code,'')!='request_media_limit'",
                    (job,),
                )
                limits = {
                    "voice": settings.voice_max_seconds,
                    "video": settings.video_max_seconds,
                    "frames": settings.video_max_frames,
                    "reasoning": settings.media_reasoning,
                }
                error = (
                    "request_media_limit"
                    if count[0] >= 3
                    else "file_size"
                    if attachment.get("file_size", 0) > MAX_MEDIA_BYTES
                    else "duration_limit"
                    if attachment.get("duration", 0)
                    > limits["voice" if kind == "voice" else "video"]
                    else "tools_unavailable"
                    if not self.processor.available
                    else "not_configured"
                    if not self.adapter or not self.speech
                    else None
                )
                cursor = await conn.execute(
                    "INSERT INTO media_runs(bot_id,chat_id,scope,access_version,message_id,"
                    "event_id,"
                    "kind,file_id,state,processing_version,asr_model,model,settings_version,"
                    "limits,error_code,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        bot,
                        chat,
                        scope,
                        grant,
                        message_id,
                        event,
                        kind,
                        str(attachment.get("file_id", "")),
                        "unavailable" if error else "queued",
                        VERSION,
                        settings.asr_model,
                        settings.media_model,
                        version,
                        json.dumps(limits),
                        error,
                        stamp(),
                    ),
                )
                existing = (cursor.lastrowid, "unavailable" if error else "queued")
            elif existing[1] == "unavailable":
                retry = await row(
                    conn, "SELECT error_code FROM media_runs WHERE id=?", (existing[0],)
                )
                count = await row(
                    conn,
                    "SELECT COUNT(*) FROM media_dependencies md JOIN media_runs mr "
                    "ON mr.id=md.media_run_id WHERE md.job_id=? AND md.media_run_id!=? "
                    "AND COALESCE(mr.error_code,'')!='request_media_limit'",
                    (job, existing[0]),
                )
                paid = await row(
                    conn, "SELECT 1 FROM model_calls WHERE media_run_id=?", (existing[0],)
                )
                if (
                    retry[0] in {"request_media_limit", "stale_request"}
                    and count[0] < 3
                    and not paid
                ):
                    await conn.execute(
                        "UPDATE media_runs SET state='queued',error_code=NULL,"
                        "analysis_context='{}' "
                        "WHERE id=?",
                        (existing[0],),
                    )
                    existing = (existing[0], "queued")
            await conn.execute(
                "INSERT OR IGNORE INTO media_dependencies VALUES (?,?)", (job, existing[0])
            )
            waiting |= existing[1] in {"queued", "preparing", "transcribing", "analyzing"}
        return waiting

    async def recover(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted' "
                "WHERE state='started' AND media_run_id IN "
                "(SELECT id FROM media_runs WHERE bot_id=?)",
                (bot,),
            )
            await conn.execute(
                "UPDATE media_runs SET state=CASE WHEN transcript IS NOT NULL THEN 'partial' "
                "ELSE 'unknown' END,error_code='interrupted' WHERE bot_id=? "
                "AND state IN ('transcribing','analyzing')",
                (bot,),
            )
            await conn.execute(
                "UPDATE media_runs SET state='queued' WHERE bot_id=? AND state='preparing'", (bot,)
            )

    async def claim(self, bot: str) -> dict[str, Any] | None:
        async with self.store.gate, self.db.transaction() as conn:
            item = await row(
                conn,
                "SELECT id,kind,file_id,limits,asr_model,model,settings_version "
                "FROM media_runs WHERE bot_id=? AND state='queued' ORDER BY id LIMIT 1",
                (bot,),
            )
            if not item:
                return None
            if not await valid(conn, item[0]):
                await purge(conn, "id=?", (item[0],))
                return {"skip": True}
            if not await current_dependency(conn, item[0]):
                await conn.execute(
                    "UPDATE media_runs SET state='unavailable',error_code='stale_request' "
                    "WHERE id=?",
                    (item[0],),
                )
                return {"skip": True}
            context: dict[str, Any] = {}
            if item[1] != "voice":
                dependency = await current_job(conn, item[0])
                assert dependency
                update = json.loads(dependency[2])
                question = update.get("edited_message") or update.get("message") or {}
                source = await row(
                    conn,
                    "SELECT bot_id,chat_id,access_version,message_id FROM media_runs WHERE id=?",
                    (item[0],),
                )
                original = await accepted(conn, source[0], source[1], source[2], source[3])
                assert original
                spoken = await row(
                    conn,
                    "SELECT transcript FROM media_runs WHERE bot_id=? AND chat_id=? "
                    "AND event_id=? AND kind='voice' AND state IN ('completed','partial')",
                    (source[0], source[1], dependency[1]),
                )
                context = {
                    "job_id": dependency[0],
                    "question": str(
                        question.get("text")
                        or question.get("caption")
                        or (spoken[0] if spoken else "")
                        or "Опиши смысл видео"
                    )[:4000],
                    "sender_id": str(question.get("from", {}).get("id", "")),
                    "caption": str(original[1].get("caption") or "")[:8000],
                    "manifest": [{"message_id": question["message_id"], "event_id": dependency[1]}],
                }
            await conn.execute(
                "UPDATE media_runs SET state='preparing',analysis_context=? WHERE id=?",
                (json.dumps(context, ensure_ascii=False), item[0]),
            )
            work = dict(
                zip(
                    ("id", "kind", "file_id", "limits", "asr_model", "model", "version"),
                    item,
                    strict=True,
                )
            )
            work["analysis_context"] = context
            return work

    async def check(self, run: int) -> bool:
        async with self.store.gate, self.db.transaction() as conn:
            return await valid(conn, run)

    async def call(
        self,
        work: dict[str, Any],
        operation: str,
        request: dict[str, Any],
        audio: bytes | None = None,
    ) -> ModelResult:
        run, model = work["id"], request["model"]
        async with self.store.gate, self.db.transaction() as conn:
            if not await valid(conn, run):
                return ModelResult(state="cancelled", error="source_changed")
            if not await current_dependency(conn, run):
                return ModelResult(state="cancelled", error="stale_request")
            if operation == "vision" and not await current_job(
                conn, run, work["analysis_context"].get("job_id")
            ):
                return ModelResult(state="cancelled", error="stale_request")
            if operation == "vision":
                saved = await row(
                    conn, "SELECT analysis_context FROM media_runs WHERE id=?", (run,)
                )
                if json.loads(saved[0]) != work["analysis_context"]:
                    return ModelResult(state="cancelled", error="source_changed")
            await conn.execute(
                "UPDATE media_runs SET state=? WHERE id=?",
                ("transcribing" if operation == "asr" else "analyzing", run),
            )
            await conn.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,"
                "operation,settings_version,request_json) VALUES ('openai',?,'started',?,?,?,?,?)",
                (
                    model,
                    stamp(),
                    run,
                    operation,
                    work["version"],
                    json.dumps(request, ensure_ascii=False),
                ),
            )
        started = time.monotonic()
        try:
            if operation == "asr":
                assert self.speech and audio
                result = await self.speech.transcribe(model, audio)
            else:
                assert self.adapter
                content = [{"type": "input_text", "text": request["input"]}]
                for frame in work["frames"]:
                    path = self.directory / frame["path"]
                    content.append(
                        image_part(await asyncio.to_thread(path.read_bytes), "image/jpeg")
                    )
                result = await self.adapter.generate(
                    {**request, "input": [{"role": "user", "content": content}]}
                )
        except asyncio.CancelledError:
            result = ModelResult(state="unknown", error="interrupted")
            await self.finish_call(work, operation, result, started)
            raise
        except Exception:
            result = ModelResult(state="unknown", error="provider_uncertain")
        await self.finish_call(work, operation, result, started)
        return result

    async def finish_call(
        self, work: dict[str, Any], operation: str, result: ModelResult, started: float
    ) -> None:
        cost = (
            estimate_speech_cost(result, work["asr_model"])
            if operation == "asr"
            else estimate_cost(result, work["model"])
        )
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state=?,usage_json=?,latency_ms=?,cost_usd=?,"
                "request_id=?,response_id=?,error_code=?,finished_at=?,pricing_profile=?,"
                "model=COALESCE(?,model) "
                "WHERE media_run_id=? AND operation=? AND state='started'",
                (
                    result.state,
                    json.dumps(result.usage) if result.usage else None,
                    int((time.monotonic() - started) * 1000),
                    cost,
                    result.request_id,
                    result.response_id,
                    result.error,
                    stamp(),
                    ("asr-token-2026-10-08" if cost is not None else None)
                    if operation == "asr"
                    else pricing_profile(result.model or work["model"]),
                    result.model,
                    work["id"],
                    operation,
                ),
            )
            if not await valid(conn, work["id"]):
                await conn.execute(
                    "UPDATE model_calls SET request_json=NULL WHERE media_run_id=?", (work["id"],)
                )
                return
            if operation == "asr" and result.state == "completed":
                await conn.execute(
                    "UPDATE media_runs SET transcript=?,state=CASE "
                    "WHEN error_code='analysis_context_changed' THEN 'partial' ELSE state END "
                    "WHERE id=?",
                    (result.text[:12000], work["id"]),
                )
            if operation == "vision":
                saved = await row(
                    conn, "SELECT analysis_context FROM media_runs WHERE id=?", (work["id"],)
                )
                if json.loads(saved[0]) != work["analysis_context"]:
                    await conn.execute(
                        "UPDATE model_calls SET request_json=NULL WHERE media_run_id=? "
                        "AND operation='vision'",
                        (work["id"],),
                    )

    async def execute(self, work: dict[str, Any], downloader: MediaDownloader) -> None:
        run = work["id"]
        folder = self.directory / str(run)
        try:
            async with asyncio.timeout(45):
                data = await downloader.download_media(work["file_id"])
            if not await self.check(run):
                return
            prepared = await self.processor.prepare(
                data, folder, work["kind"], json.loads(work["limits"])
            )
            work["frames"] = prepared["frames"]
            async with self.store.gate, self.db.transaction() as conn:
                if not await valid(conn, run):
                    return
                await conn.execute(
                    "UPDATE media_runs SET duration=?,frames=?,coverage=? WHERE id=?",
                    (
                        prepared["duration"],
                        json.dumps(work["frames"]),
                        json.dumps(prepared["coverage"]),
                        run,
                    ),
                )
            errors = []
            transcript = ""
            if prepared["audio"]:
                result = await self.call(
                    work, "asr", {"model": work["asr_model"], "format": "wav"}, prepared["audio"]
                )
                if result.state != "completed":
                    errors.append(
                        "stale_request"
                        if result.error == "stale_request"
                        else "asr_" + result.state
                    )
                else:
                    transcript = result.text[:12000]
                    prepared["coverage"]["audio_intervals"] = [
                        [0, prepared.get("audio_duration") or prepared["duration"]]
                    ]
                    prepared["coverage"]["speech"] = (
                        "transcribed" if result.text else "empty_transcript"
                    )
                    async with self.store.gate, self.db.transaction() as conn:
                        if await valid(conn, run):
                            await conn.execute(
                                "UPDATE media_runs SET coverage=? WHERE id=?",
                                (json.dumps(prepared["coverage"]), run),
                            )
            if work["frames"]:
                request = {
                    "model": work["model"],
                    "reasoning": {"effort": json.loads(work["limits"]).get("reasoning", "low")},
                    "store": False,
                    "max_output_tokens": 6000,
                    "instructions": self.store.prompts.text("media.video"),
                    "input": json.dumps(
                        {
                            "duration": prepared["duration"],
                            "frame_times": prepared["coverage"]["frame_times"],
                            "question": work["analysis_context"]["question"],
                            "caption": work["analysis_context"]["caption"],
                            "transcript": transcript,
                            "audio_coverage": prepared["coverage"],
                        }
                    ),
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "media_observation",
                            "strict": True,
                            "schema": VideoObservation.model_json_schema(),
                        }
                    },
                }
                result = await self.call(work, "vision", request)
                if result.state == "completed":
                    try:
                        observation_data = VideoObservation.model_validate_json(result.text)
                        frame_marks = prepared["coverage"]["frame_times"]
                        for event in observation_data.timeline:
                            nearest = min(frame_marks, key=lambda t: abs(t - event.seconds))
                            if abs(nearest - event.seconds) > 0.15:
                                raise ValueError("unobserved_timestamp")
                            event.seconds = nearest
                        observation_data.timeline.sort(key=lambda e: e.seconds)
                        observation = observation_data.model_dump_json()
                        async with self.store.gate, self.db.transaction() as conn:
                            saved = await row(
                                conn, "SELECT analysis_context FROM media_runs WHERE id=?", (run,)
                            )
                            if (
                                await valid(conn, run)
                                and json.loads(saved[0]) == work["analysis_context"]
                            ):
                                await conn.execute(
                                    "UPDATE media_runs SET result=? WHERE id=?", (observation, run)
                                )
                    except (ValueError, ValidationError):
                        errors.append("invalid_observation")
                else:
                    errors.append(
                        "stale_request"
                        if result.error == "stale_request"
                        else "vision_" + result.state
                    )
            if work["kind"] == "voice" and not prepared["audio"]:
                errors.append(
                    "low_audio_signal"
                    if prepared["coverage"]["audio_signal"] == "low_signal"
                    else "missing_audio"
                )
            async with self.store.gate, self.db.transaction() as conn:
                if not await valid(conn, run):
                    return
                paid = await row(conn, "SELECT 1 FROM model_calls WHERE media_run_id=?", (run,))
                saved = await row(
                    conn, "SELECT analysis_context FROM media_runs WHERE id=?", (run,)
                )
                if (
                    work.get("analysis_context")
                    and json.loads(saved[0]) != work["analysis_context"]
                ):
                    return
                stale = not paid and (
                    "stale_request" in errors or not await current_dependency(conn, run)
                )
                await conn.execute(
                    "UPDATE media_runs SET state=?,coverage=?,error_code=?,finished_at=? "
                    "WHERE id=?",
                    (
                        "unavailable" if stale else "partial" if errors else "completed",
                        json.dumps(prepared["coverage"]),
                        "stale_request" if stale else ",".join(errors) if errors else None,
                        stamp(),
                        run,
                    ),
                )
                item = await row(
                    conn,
                    "SELECT bot_id,chat_id,scope,access_version,message_id,event_id,transcript "
                    "FROM media_runs WHERE id=?",
                    (run,),
                )
                if work["kind"] == "voice" and item[6]:
                    source = await accepted(conn, item[0], item[1], item[3], item[4])
                    assert source
                    settings = Settings.model_validate_json(
                        (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
                    )
                    # Author's own voice is evidence, never a forwarded speaker's identity.
                    await admit_memory(
                        conn,
                        item[0],
                        item[1],
                        item[2],
                        item[3],
                        item[5],
                        {**source[1], "text": item[6]},
                        False,
                        settings.memory_enabled,
                    )
                    await conn.execute(
                        "UPDATE memory_sources SET received_at=CAST(strftime('%s',"
                        "(SELECT received_at FROM events WHERE id=?)) AS REAL) "
                        "WHERE event_id=?",
                        (item[5], item[5]),
                    )
                    await conn.execute(
                        "UPDATE recent_messages SET text=? WHERE event_id=? AND role='user'",
                        ("[Расшифровка голосового, возможны ошибки] " + item[6][:4000], item[5]),
                    )
        except asyncio.CancelledError:
            async with self.db.transaction() as conn:
                paid = await row(conn, "SELECT 1 FROM model_calls WHERE media_run_id=?", (run,))
                await conn.execute(
                    "UPDATE media_runs SET state=CASE WHEN ? THEN CASE WHEN transcript IS NULL "
                    "THEN 'unknown' ELSE 'partial' END ELSE 'queued' END,error_code='interrupted' "
                    "WHERE id=? AND state!='cancelled'",
                    (bool(paid), run),
                )
            raise
        except (ValueError, TimeoutError, OSError):
            async with self.store.gate, self.db.transaction() as conn:
                if await valid(conn, run):
                    await conn.execute(
                        "UPDATE media_runs SET state='unavailable',error_code='preparation_failed',"
                        "finished_at=? WHERE id=?",
                        (stamp(), run),
                    )
        except Exception:
            async with self.store.gate, self.db.transaction() as conn:
                if await valid(conn, run):
                    await conn.execute(
                        "UPDATE media_runs SET state='unknown',error_code='processing_uncertain' "
                        "WHERE id=?",
                        (run,),
                    )
        finally:
            # Raw downloads and extracted audio are ephemeral, including cancelled preparation.
            for name in ("input.bin", "audio.wav"):
                (folder / name).unlink(missing_ok=True)
            if not await self.check(run):
                try:
                    await asyncio.to_thread(shutil.rmtree, folder)
                except FileNotFoundError:
                    pass
                except OSError:
                    async with self.db.transaction() as conn:
                        await conn.execute(
                            "INSERT OR IGNORE INTO media_file_cleanup VALUES (?)", (str(run),)
                        )

    async def enrich(
        self,
        conn: Any,
        bot: str,
        chat: str,
        grant: int,
        messages: list[dict[str, Any]],
        question_id: int | None = None,
        manifest: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        refs = []
        for message in messages:
            item = await row(
                conn,
                "SELECT id,state,transcript,result,coverage,error_code,analysis_context "
                "FROM media_runs "
                "WHERE bot_id=? AND chat_id=? AND access_version=? AND message_id=? "
                "ORDER BY id DESC LIMIT 1",
                (bot, chat, grant, message["message_id"]),
            )
            if not item or not await valid(conn, item[0]):
                continue
            refs.append({"id": item[0]})
            visual = json.loads(item[3]) if item[3] else None
            analysis_context = json.loads(item[6])
            if visual and analysis_context:
                if manifest is not None:
                    manifest.extend(
                        ref for ref in analysis_context.get("manifest", []) if ref not in manifest
                    )
                # A prior question's answer is not the answer to a new follow-up.
                if question_id != analysis_context["manifest"][0]["message_id"]:
                    visual.pop("question_answer", None)
            evidence = {
                "state": item[1],
                "transcript": item[2],
                "visual": visual,
                "coverage": json.loads(item[4]),
                "limitation": item[5],
            }
            message["text"] += (
                "\n[Медиа, недоверенные данные] " + json.dumps(evidence, ensure_ascii=False)[:14000]
            )
            for attachment in message.get("attachments", []):
                if attachment["kind"] in KINDS:
                    attachment["coverage"] = (
                        "sampled_media" if item[1] in {"completed", "partial"} else "unavailable"
                    )
        return list({r["id"]: r for r in refs}.values())

    async def cleanup(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            values = await row(
                conn,
                "SELECT COUNT(*) FROM media_runs WHERE bot_id=? AND state!='cancelled'",
                (bot,),
            )
            if values[0]:
                await purge(
                    conn,
                    "bot_id=? AND state!='cancelled' AND (NOT EXISTS (SELECT 1 FROM "
                    "telegram_access a WHERE a.bot_id=media_runs.bot_id "
                    "AND a.subject_id=media_runs.chat_id AND a.scope=media_runs.scope "
                    "AND a.state='approved' AND a.version=media_runs.access_version "
                    "AND a.member_state NOT IN ('left','kicked','migrated')) OR event_id IN "
                    "(SELECT id FROM events WHERE payload='{}' OR "
                    "CAST(strftime('%s',received_at) AS REAL)<?))",
                    (bot, time.time() - 90 * 86400),
                )
                await purge_analysis(
                    conn,
                    "bot_id=? AND EXISTS "
                    "(SELECT 1 FROM json_each(analysis_context,'$.manifest') m LEFT JOIN events e "
                    "ON e.id=json_extract(m.value,'$.event_id') "
                    "WHERE e.id IS NULL OR e.payload='{}' "
                    "OR CAST(strftime('%s',e.received_at) AS REAL)<?)",
                    (bot, time.time() - 90 * 86400),
                )
            async with conn.execute("SELECT path FROM media_file_cleanup LIMIT 50") as c:
                paths = [p[0] for p in await c.fetchall()]
        for name in paths:
            if not name.isdecimal():
                continue
            folder = (self.directory / name).resolve()
            if not folder.is_relative_to(self.directory.resolve()):
                continue
            try:
                await asyncio.to_thread(shutil.rmtree, folder)
            except FileNotFoundError:
                pass
            except OSError:
                continue
            async with self.db.transaction() as conn:
                await conn.execute("DELETE FROM media_file_cleanup WHERE path=?", (name,))

    async def listing(
        self, bot: str | None, scope: str, chat: str | None, page: int
    ) -> dict[str, Any]:
        settings = (await self.db.settings()).settings
        condition, args = "bot_id=?", [bot]
        if scope != "all":
            condition += " AND scope=?"
            args.append(scope)
        if chat:
            condition += " AND chat_id=?"
            args.append(chat)
        rows = await self.db.all(
            "SELECT id,chat_id,scope,kind,state,duration,created_at,error_code "
            "FROM media_runs WHERE " + condition + " ORDER BY id DESC LIMIT 20 OFFSET ?",
            tuple(args + [page * 20]),
        )
        total = await self.db.one("SELECT COUNT(*) FROM media_runs WHERE " + condition, tuple(args))
        return {
            "items": [
                dict(
                    zip(
                        (
                            "id",
                            "chat_id",
                            "scope",
                            "kind",
                            "state",
                            "duration",
                            "created_at",
                            "error",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in rows
            ],
            "page": page,
            "total": total[0],
            "enabled": settings.media_enabled,
            "configured": bool(self.adapter and self.speech),
            "tools_available": self.processor.available,
            "asr_model": settings.asr_model,
            "model": settings.media_model,
            "limits": {
                "voice": settings.voice_max_seconds,
                "video": settings.video_max_seconds,
                "frames": settings.video_max_frames,
                "bytes": MAX_MEDIA_BYTES,
                "items": 3,
            },
        }

    async def details(self, run: int, bot: str | None) -> dict[str, Any] | None:
        async with self.db.transaction() as conn:
            item = await row(
                conn,
                "SELECT id,chat_id,scope,kind,state,duration,created_at,error_code,transcript,"
                "result,coverage,frames FROM media_runs WHERE id=? AND bot_id=?",
                (run, bot),
            )
            if not item:
                return None
            data = dict(
                zip(
                    (
                        "id",
                        "chat_id",
                        "scope",
                        "kind",
                        "state",
                        "duration",
                        "created_at",
                        "error",
                        "transcript",
                        "result",
                        "coverage",
                        "frames",
                    ),
                    item,
                    strict=True,
                )
            )
            available = await valid(conn, run)
            data.update(
                available=available,
                transcript=item[8] if available else None,
                result=json.loads(item[9]) if available and item[9] else None,
                coverage=json.loads(item[10]) if available else {},
                frames=[],
            )
            if available:
                data["frames"] = [
                    {"seconds": f["seconds"], "url": f"/api/media/{run}/frames/{i}"}
                    for i, f in enumerate(json.loads(item[11]))
                ]
        calls = await self.db.all(
            "SELECT operation,model,state,usage_json,latency_ms,cost_usd,error_code "
            "FROM model_calls WHERE media_run_id=? ORDER BY id",
            (run,),
        )
        data["calls"] = [
            dict(
                zip(
                    ("operation", "model", "state", "usage", "latency_ms", "cost_usd", "error"),
                    (c[0], c[1], c[2], json.loads(c[3]) if c[3] else None, *c[4:]),
                    strict=True,
                )
            )
            for c in calls
        ]
        return data

    async def asset(self, run: int, index: int, bot: str | None) -> Path | None:
        async with self.db.transaction() as conn:
            item = await row(
                conn, "SELECT frames FROM media_runs WHERE id=? AND bot_id=?", (run, bot)
            )
            if not item or not await valid(conn, run):
                return None
            frames = json.loads(item[0])
            if index < 0 or index >= len(frames):
                return None
            path = (self.directory / frames[index]["path"]).resolve()
            return (
                path
                if path.is_relative_to((self.directory / str(run)).resolve()) and path.is_file()
                else None
            )
