"""Durable bounded vision queue, local assets, and scope-bound observations."""

import asyncio
import base64
import hashlib
import io
import json
import time
import warnings
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zarya.models import Settings
from zarya.openai_adapter import ModelAdapter, ModelResult, estimate_cost, pricing_profile
from zarya.photo_logic import PHOTO_REASONING, admit
from zarya.source_material import accepted, visual_sizes
from zarya.telegram_store import TelegramStore, row, stamp

MAX_BYTES = 5 * 1024 * 1024
MAX_BATCH_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16_000_000


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: str = Field(max_length=1500)
    visible_text: str = Field(max_length=3000)
    interpretation: str = Field(max_length=1000)
    uncertainty: str = Field(max_length=600)


class Downloader(Protocol):
    async def download_photo(self, file_id: str) -> bytes: ...


def normalize(data: bytes) -> tuple[bytes, str, int, int]:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("file_size")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError("image_format")
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ValueError("image_pixels")
            mime = Image.MIME[image.format]
            image.load()
            picture = ImageOps.exif_transpose(image)
            if "A" in picture.getbands() or "transparency" in picture.info:
                rgba = picture.convert("RGBA")
                picture = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
            picture = picture.convert("RGB")
            picture.thumbnail((2048, 2048))
            destination = io.BytesIO()
            picture.save(destination, "JPEG", quality=90)
            return destination.getvalue(), mime, width, height


def image_part(data: bytes, mime: str) -> dict[str, str]:
    return {
        "type": "input_image",
        "image_url": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
        "detail": "auto",
    }


class PhotoEngine:
    def __init__(self, store: TelegramStore, adapter: ModelAdapter | None, directory: Path):
        self.store, self.db, self.adapter = store, store.db, adapter
        self.directory = directory / "photos"

    async def prepare_sources(
        self,
        conn: Any,
        bot: str,
        chat: str,
        scope: str,
        grant: int,
        message_ids: list[int],
        settings: Settings,
    ) -> None:
        """Admit retained legacy attachments only on an addressed source request."""
        for message_id in message_ids:
            source = await accepted(conn, bot, chat, grant, message_id)
            if not source or not visual_sizes(source[1]):
                continue
            if settings.media_enabled and not (source[1].get("photo") or source[1].get("sticker")):
                continue
            existing = await row(
                conn,
                "SELECT 1 FROM photo_items i JOIN photo_batches b ON b.id=i.batch_id "
                "WHERE i.bot_id=? AND i.chat_id=? AND i.message_id=? AND i.event_id=? "
                "AND b.access_version=? LIMIT 1",
                (bot, chat, message_id, source[0], grant),
            )
            if not existing:
                await admit(
                    conn,
                    bot,
                    chat,
                    scope,
                    grant,
                    source[0],
                    source[1],
                    settings,
                    False,
                    addressed_source=True,
                )

    async def recover(self, bot: str) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state='unknown',error_code='interrupted' WHERE "
                "state='started' AND photo_batch_id IN (SELECT id FROM "
                "photo_batches WHERE bot_id=?)",
                (bot,),
            )
            await conn.execute(
                "UPDATE photo_batches SET state='unknown',error_code='interrupted' "
                "WHERE bot_id=? AND state='analyzing'",
                (bot,),
            )
            await conn.execute(
                "UPDATE photo_batches SET state='queued' WHERE bot_id=? AND state='downloading'",
                (bot,),
            )

    async def valid(self, conn: Any, batch: int, require_enabled: bool = True) -> bool:
        value = await row(
            conn,
            "SELECT 1 FROM photo_batches b JOIN telegram_access a ON a.bot_id=b.bot_id "
            "AND a.scope=b.scope AND a.subject_id=b.chat_id WHERE b.id=? "
            "AND b.state!='cancelled' AND a.state='approved' AND a.version=b.access_version "
            "AND a.member_state NOT IN ('left','kicked','migrated') AND NOT EXISTS "
            "(SELECT 1 FROM photo_items i LEFT JOIN photo_messages p ON p.bot_id=i.bot_id "
            "AND p.chat_id=i.chat_id AND p.message_id=i.message_id "
            "LEFT JOIN events source ON source.id=i.event_id WHERE i.batch_id=b.id "
            "AND (p.event_id IS NULL OR p.event_id!=i.event_id "
            "OR p.access_version!=b.access_version OR source.id IS NULL OR source.payload='{}' "
            "OR CAST(strftime('%s',source.received_at) AS REAL)<?))",
            (batch, time.time() - 90 * 86400),
        )
        if require_enabled:
            settings = Settings.model_validate_json(
                (await row(conn, "SELECT document FROM settings WHERE id=1", ()))[0]
            )
            return bool(value and settings.photo_enabled and settings.dialogue_enabled)
        return bool(value)

    async def claim(self, bot: str) -> dict[str, Any] | None:
        async with self.store.gate, self.db.transaction() as conn:
            value = await row(
                conn,
                "SELECT id,model,bot_id,chat_id,access_version,processing_version "
                "FROM photo_batches "
                "WHERE bot_id=? AND state='queued' AND collect_until<=? ORDER BY id LIMIT 1",
                (bot, time.time()),
            )
            if not value:
                return None
            batch = value[0]
            if not await self.valid(conn, batch) or self.adapter is None:
                await conn.execute(
                    "UPDATE photo_batches SET state=?,error_code=? WHERE id=?",
                    (
                        "cancelled" if self.adapter else "error",
                        "not_configured" if self.adapter is None else "disabled_or_superseded",
                        batch,
                    ),
                )
                return {"skip": True}
            await conn.execute("UPDATE photo_batches SET state='downloading' WHERE id=?", (batch,))
            async with conn.execute(
                "SELECT id,event_id,file_id,caption FROM photo_items WHERE batch_id=? "
                "ORDER BY message_id,id LIMIT 10",
                (batch,),
            ) as c:
                items = list(await c.fetchall())
            return dict(
                zip(
                    ("id", "model", "bot", "chat", "version", "processing_version"),
                    value,
                    strict=True,
                ),
                items=items,
            )

    def write_asset(self, item: int, raw: bytes) -> dict[str, Any]:
        converted, mime, width, height = normalize(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        raw_name, image_name = f"{item}-{digest}.raw", f"{item}-{digest}.jpg"
        (self.directory / raw_name).write_bytes(raw)
        (self.directory / image_name).write_bytes(converted)
        return {
            "raw_path": raw_name,
            "image_path": image_name,
            "content_hash": digest,
            "mime": mime,
            "width": width,
            "height": height,
            "byte_count": len(raw),
        }

    def path(self, name: str) -> Path:
        value = (self.directory / name).resolve()
        if value.parent != self.directory.resolve():
            raise ValueError("invalid_asset")
        return value

    async def execute(self, work: dict[str, Any], transport: Downloader) -> None:
        batch = work["id"]
        started = time.monotonic()
        call_started = False
        try:
            manifest: list[dict[str, Any]] = []
            contents: list[dict[str, Any]] = []
            total = 0
            for item, event, file_id, caption in work["items"]:
                async with self.store.gate, self.db.transaction() as conn:
                    if not await self.valid(conn, batch):
                        await conn.execute(
                            "UPDATE photo_batches SET state='cancelled' WHERE id=?", (batch,)
                        )
                        return
                async with asyncio.timeout(40):
                    raw = await transport.download_photo(file_id)
                total += len(raw)
                if total > MAX_BATCH_BYTES:
                    raise ValueError("album_size")
                asset = await asyncio.to_thread(self.write_asset, item, raw)
                contents.extend(
                    [
                        {"type": "input_text", "text": f"Фото {len(manifest) + 1}. " + caption},
                        image_part(
                            await asyncio.to_thread(self.path(asset["image_path"]).read_bytes),
                            "image/jpeg",
                        ),
                    ]
                )
                manifest.append(
                    {
                        "id": item,
                        "event_id": event,
                        "hash": asset["content_hash"],
                        "caption": caption,
                    }
                )
                async with self.db.transaction() as conn:
                    if not await self.valid(conn, batch):
                        for name in (asset["raw_path"], asset["image_path"]):
                            await conn.execute(
                                "INSERT OR IGNORE INTO memory_file_cleanup VALUES (?,?)",
                                (work["bot"], name),
                            )
                        await conn.execute(
                            "UPDATE photo_batches SET state='cancelled' WHERE id=?", (batch,)
                        )
                        return
                    await conn.execute(
                        "UPDATE photo_items SET state='prepared',raw_path=?,image_path=?,"
                        "content_hash=?,mime=?,width=?,height=?,byte_count=? WHERE id=?",
                        tuple(
                            asset[key]
                            for key in (
                                "raw_path",
                                "image_path",
                                "content_hash",
                                "mime",
                                "width",
                                "height",
                                "byte_count",
                            )
                        )
                        + (item,),
                    )
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        work["processing_version"],
                        PHOTO_REASONING,
                        work["model"],
                        [(i["hash"], i["caption"]) for i in manifest],
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            request = {
                "model": work["model"],
                "reasoning": {"effort": PHOTO_REASONING},
                "store": False,
                "max_output_tokens": 6000,
                "instructions": (self.store.prompts.text("media.photo")),
                "text": {
                    "verbosity": "low",
                    "format": {
                        "type": "json_schema",
                        "name": "photo_observation",
                        "strict": True,
                        "schema": Observation.model_json_schema(),
                    },
                },
                "input": [{"role": "user", "content": contents}],
            }
            async with self.store.gate, self.db.transaction() as conn:
                if not await self.valid(conn, batch):
                    await conn.execute(
                        "UPDATE photo_batches SET state='cancelled' WHERE id=?", (batch,)
                    )
                    return
                cached = await row(
                    conn,
                    "SELECT id,result FROM photo_batches WHERE bot_id=? AND chat_id=? "
                    "AND access_version=? AND fingerprint=? AND state IN ('completed','cache') "
                    "AND id!=? ORDER BY id DESC LIMIT 1",
                    (work["bot"], work["chat"], work["version"], fingerprint, batch),
                )
                settings_version = (await row(conn, "SELECT version FROM settings WHERE id=1", ()))[
                    0
                ]
                await conn.execute(
                    "UPDATE photo_batches SET "
                    "manifest=?,fingerprint=?,settings_version=? WHERE id=?",
                    (json.dumps(manifest), fingerprint, settings_version, batch),
                )
                if cached and await self.valid(conn, cached[0]):
                    await conn.execute(
                        "UPDATE photo_batches SET "
                        "state='cache',result=?,cache_source=?,finished_at=? "
                        "WHERE id=?",
                        (cached[1], cached[0], stamp(), batch),
                    )
                    return
                # Persist references, never base64 or Telegram file URLs.
                metadata = {**request, "input": {"photo_manifest": manifest}}
                await conn.execute(
                    "INSERT INTO model_calls(provider,model,state,created_at,photo_batch_id,"
                    "settings_version,request_json,pricing_profile) VALUES "
                    "('openai',?,'started',?,?,?,?,?)",
                    (
                        work["model"],
                        stamp(),
                        batch,
                        settings_version,
                        json.dumps(metadata),
                        pricing_profile(work["model"]),
                    ),
                )
                await conn.execute(
                    "UPDATE photo_batches SET state='analyzing' WHERE id=?", (batch,)
                )
                call_started = True
            assert self.adapter is not None
            result = await self.adapter.generate(request)
            await self.finish(work, result, int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            if call_started:
                await self.finish(work, ModelResult(state="unknown", error="interrupted"), 0)
            raise
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc) in {"file_size", "image_format", "image_pixels", "album_size"}
                else "download_or_decode"
            )
            if call_started:
                await self.finish(work, ModelResult(state="unknown", error="provider_uncertain"), 0)
            else:
                async with self.store.gate, self.db.transaction() as conn:
                    await conn.execute(
                        "UPDATE photo_batches SET state='error',error_code=?,finished_at=? "
                        "WHERE id=? AND state='downloading'",
                        (code, stamp(), batch),
                    )

    async def finish(self, work: dict[str, Any], result: ModelResult, latency: int) -> None:
        observation = None
        if result.state == "completed":
            try:
                observation = Observation.model_validate_json(result.text).model_dump_json()
            except ValidationError:
                result.state, result.error = "invalid", "invalid_observation"
        async with self.store.gate, self.db.transaction() as conn:
            await conn.execute(
                "UPDATE model_calls SET state=?,model=COALESCE(?,model),usage_json=?,response_id=?,"
                "request_id=?,latency_ms=?,cost_usd=?,error_code=?,finished_at=?,pricing_profile=? "
                "WHERE photo_batch_id=? AND state='started'",
                (
                    result.state,
                    result.model,
                    json.dumps(result.usage) if result.usage else None,
                    result.response_id,
                    result.request_id,
                    latency,
                    estimate_cost(result, work["model"]),
                    result.error,
                    stamp(),
                    pricing_profile(result.model or work["model"]),
                    work["id"],
                ),
            )
            allowed = await self.valid(conn, work["id"])
            await conn.execute(
                "UPDATE photo_batches SET state=?,result=?,error_code=?,finished_at=? WHERE id=?",
                (
                    result.state if allowed else "cancelled",
                    observation if allowed else None,
                    result.error,
                    stamp(),
                    work["id"],
                ),
            )

    async def enrich(
        self, conn: Any, bot: str, chat: str, version: int, messages: list[dict[str, Any]]
    ) -> None:
        seen: set[int] = set()
        budget = 10000
        for message in messages:
            value = await row(
                conn,
                "SELECT b.id,b.state,b.result FROM photo_batches b JOIN photo_items i "
                "ON i.batch_id=b.id JOIN photo_messages r ON r.bot_id=i.bot_id "
                "AND r.chat_id=i.chat_id AND r.message_id=i.message_id AND r.event_id=i.event_id "
                "WHERE b.bot_id=? AND b.chat_id=? AND b.access_version=? AND i.message_id=? "
                "ORDER BY b.id DESC LIMIT 1",
                (bot, chat, version, message["message_id"]),
            )
            if value and await self.valid(conn, value[0]):
                if value[0] in seen:
                    note = "Другой кадр того же альбома; общий разбор уже приведён в контексте."
                elif value[1] in {"completed", "cache"}:
                    note = (
                        value[2][: min(1800, budget)]
                        if budget
                        else "Разбор не вошёл в лимит контекста."
                    )
                    budget -= len(note) if budget else 0
                else:
                    note = "Фото пока не распознано; состояние: " + value[1]
                seen.add(value[0])
                coverage = (
                    "Превью вложения (движение и звук не анализировались)"
                    if any(
                        a.get("coverage") == "thumbnail_only"
                        for a in message.get("attachments", [])
                    )
                    else "Наблюдения об изображении"
                )
                message["text"] = (
                    message["text"].replace(
                        "[Вложение: photo; содержимое пока не распознано]", "[Фото]"
                    )
                    + "\n"
                    + coverage
                    + ": "
                    + note
                )

    async def refs(
        self, conn: Any, bot: str, chat: str, version: int, message_ids: list[int]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message_id in message_ids:
            async with conn.execute(
                "SELECT i.id,i.event_id,i.content_hash,b.id,i.message_id FROM photo_items i "
                "JOIN photo_batches b ON b.id=i.batch_id WHERE b.bot_id=? AND b.chat_id=? "
                "AND b.access_version=? AND b.album_key IN (SELECT b2.album_key "
                "FROM photo_batches b2 "
                "JOIN photo_items i2 ON i2.batch_id=b2.id WHERE b2.bot_id=b.bot_id "
                "AND b2.chat_id=b.chat_id AND b2.access_version=b.access_version "
                "AND i2.message_id=?) "
                "AND b.state IN ('completed','cache') "
                "ORDER BY i.message_id,i.id",
                (bot, chat, version, message_id),
            ) as cursor:
                found = await cursor.fetchall()
            for item, event, digest, batch, source_message in found:
                if await self.valid(conn, batch):
                    if not any(ref["id"] == item for ref in result):
                        result.append(
                            {
                                "id": item,
                                "event_id": event,
                                "hash": digest,
                                "batch_id": batch,
                                "message_id": source_message,
                            }
                        )
        return result[:10]

    async def attach(self, request: dict[str, Any], refs: list[dict[str, Any]]) -> dict[str, Any]:
        if not refs:
            return request
        contents: list[Any] = [{"type": "input_text", "text": request["input"]}]
        total = 0
        for index, ref in enumerate(refs):
            async with self.store.gate, self.db.transaction() as conn:
                if not await self.valid(conn, ref["batch_id"]):
                    raise ValueError("Фото больше недоступно для анализа")
                asset = await row(
                    conn,
                    "SELECT raw_path,mime FROM photo_items WHERE id=? AND event_id=? "
                    "AND content_hash=?",
                    (ref["id"], ref["event_id"], ref["hash"]),
                )
                if not asset or not asset[0]:
                    raise ValueError("Исходник фото недоступен")
            raw = await asyncio.to_thread(self.path(asset[0]).read_bytes)
            total += len(raw)
            if total > MAX_BATCH_BYTES:
                contents.append(
                    {
                        "type": "input_text",
                        "text": "Оставшиеся фото не переданы из-за лимита размера. "
                        "Не утверждай, что видела их.",
                    }
                )
                break
            contents.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"Оригинал фото {index + 1}. "
                        f"Сообщение {ref.get('message_id', 'неизвестно')}.",
                    },
                    image_part(raw, asset[1]),
                ]
            )
        return {**request, "input": [{"role": "user", "content": contents}]}

    async def listing(self, bot: str | None, scope: str, page: int) -> dict[str, Any]:
        condition = " WHERE b.bot_id=?" + (" AND b.scope=?" if scope != "all" else "")
        args: tuple[Any, ...] = (bot, scope) if scope != "all" else (bot,)
        values = await self.db.all(
            "SELECT b.id,b.chat_id,b.scope,b.state,b.created_at,b.model,b.cache_source,"
            "(SELECT COUNT(*) FROM photo_items i WHERE i.batch_id=b.id),"
            "(SELECT caption FROM photo_items i WHERE i.batch_id=b.id ORDER "
            "BY i.message_id LIMIT 1) "
            "FROM photo_batches b" + condition + " ORDER BY b.id DESC LIMIT 20 OFFSET ?",
            args + (page * 20,),
        )
        total = await self.db.one("SELECT COUNT(*) FROM photo_batches b" + condition, args)
        cost = await self.db.one(
            "SELECT SUM(m.cost_usd),SUM(m.cost_usd IS NULL) FROM model_calls m "
            "JOIN photo_batches b ON b.id=m.photo_batch_id WHERE b.bot_id=?",
            (bot,),
        )
        return {
            "items": [
                dict(
                    zip(
                        (
                            "id",
                            "chat_id",
                            "scope",
                            "state",
                            "created_at",
                            "model",
                            "cache_source",
                            "count",
                            "caption",
                        ),
                        v,
                        strict=True,
                    )
                )
                for v in values
            ],
            "total": total[0],
            "page": page,
            "configured": self.adapter is not None,
            "known_cost_usd": cost[0],
            "unknown_cost_calls": cost[1] or 0,
        }

    async def details(self, batch: int, bot: str) -> dict[str, Any] | None:
        value = await self.db.one(
            "SELECT id,chat_id,scope,state,model,result,error_code,created_at,settings_version,"
            "cache_source FROM photo_batches WHERE id=? AND bot_id=?",
            (batch, bot),
        )
        if not value:
            return None
        async with self.store.gate, self.db.transaction() as conn:
            allowed = await self.valid(conn, batch, False)
        data = dict(
            zip(
                (
                    "id",
                    "chat_id",
                    "scope",
                    "state",
                    "model",
                    "result",
                    "error",
                    "created_at",
                    "settings_version",
                    "cache_source",
                ),
                value,
                strict=True,
            )
        )
        data["available"] = allowed
        data["result"] = json.loads(data["result"]) if allowed and data["result"] else None
        items = await self.db.all(
            "SELECT id,caption,width,height,byte_count,raw_path FROM photo_items "
            "WHERE batch_id=? ORDER BY message_id,id",
            (batch,),
        )
        data["items"] = [
            {
                "id": v[0],
                "caption": v[1] if allowed else "",
                "width": v[2],
                "height": v[3],
                "byte_count": v[4],
                "image_url": f"/api/photos/images/{v[0]}" if allowed and v[5] else None,
            }
            for v in items
        ]
        call = await self.db.one(
            "SELECT model,state,usage_json,latency_ms,cost_usd,pricing_profile FROM model_calls "
            "WHERE photo_batch_id=?",
            (batch,),
        )
        data["call"] = (
            dict(
                zip(
                    ("model", "state", "usage", "latency_ms", "cost_usd", "pricing"),
                    call,
                    strict=True,
                )
            )
            if call
            else None
        )
        if data["call"] and data["call"]["usage"]:
            data["call"]["usage"] = json.loads(data["call"]["usage"])
        return data

    async def asset(self, item: int, bot: str) -> tuple[Path, str] | None:
        async with self.store.gate, self.db.transaction() as conn:
            value = await row(
                conn,
                "SELECT batch_id,raw_path,mime FROM photo_items WHERE id=? AND bot_id=?",
                (item, bot),
            )
            if not value or not value[1] or not await self.valid(conn, value[0], False):
                return None
            return self.path(value[1]), value[2]
