"""Supervised, explicit Bot API polling; never logs provider exception messages."""

import asyncio
import io
import logging
import time
import traceback
from contextlib import suppress
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import ReactionTypeEmoji, ReplyParameters

from zarya.avatars import AvatarEngine
from zarya.dialogue import DialogueEngine
from zarya.media import MediaEngine
from zarya.media_processing import MAX_MEDIA_BYTES
from zarya.memory import MemoryEngine
from zarya.photos import MAX_BYTES, PhotoEngine
from zarya.research import ResearchEngine
from zarya.telegram_store import TelegramStore


class Transport(Protocol):
    async def profile_photos(self, user_id: str, offset: int, limit: int) -> dict[str, Any]: ...
    async def download_media(self, file_id: str) -> bytes: ...
    async def download_photo(self, file_id: str) -> bytes: ...
    async def identity(self) -> dict[str, Any]: ...
    async def updates(self, offset: int | None) -> list[dict[str, Any]]: ...
    async def send(
        self, chat_id: str, text: str, thread_id: int | None, reply_id: int | None = None
    ) -> int: ...
    async def typing(self, chat_id: str, thread_id: int | None) -> bool: ...
    async def react(self, chat_id: str, message_id: int, emoji: str) -> bool: ...
    async def close(self) -> None: ...


class LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int = MAX_BYTES):
        super().__init__()
        self.limit = limit

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.limit:
            raise ValueError("file_size")
        return super().write(data)


class AiogramTransport:
    def __init__(self, token: str):
        self.bot = Bot(token=token)

    async def identity(self) -> dict[str, Any]:
        me = await self.bot.get_me(request_timeout=10)
        webhook = await self.bot.get_webhook_info(request_timeout=10)
        return {
            "id": str(me.id),
            "username": me.username or "",
            "name": me.first_name,
            "privacy_disabled": bool(me.can_read_all_group_messages),
            "webhook": bool(webhook.url),
        }

    async def updates(self, offset: int | None) -> list[dict[str, Any]]:
        values = await self.bot.get_updates(
            offset=offset,
            limit=100,
            timeout=25,
            request_timeout=35,
            allowed_updates=["message", "edited_message", "my_chat_member"],
        )
        # Incoming options may instantiate aiogram Default sentinels not present on the wire
        # (e.g. LinkPreviewOptions). Serialize only fields received from Telegram.
        return [
            value.model_dump(mode="json", exclude_none=True, exclude_unset=True, by_alias=True)
            for value in values
        ]

    async def send(
        self, chat_id: str, text: str, thread_id: int | None, reply_id: int | None = None
    ) -> int:
        result = await self.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            message_thread_id=thread_id,
            request_timeout=10,
            reply_parameters=ReplyParameters(message_id=reply_id) if reply_id else None,
        )
        return result.message_id

    async def typing(self, chat_id: str, thread_id: int | None) -> bool:
        return await self.bot.send_chat_action(
            chat_id=int(chat_id), action="typing", message_thread_id=thread_id, request_timeout=4
        )

    async def react(self, chat_id: str, message_id: int, emoji: str) -> bool:
        chat = await self.bot.get_chat(chat_id=int(chat_id), request_timeout=4)
        available = chat.available_reactions
        if available is not None and not any(
            getattr(reaction, "emoji", None) == emoji for reaction in available
        ):
            return False
        return await self.bot.set_message_reaction(
            chat_id=int(chat_id),
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=False,
            request_timeout=4,
        )

    async def close(self) -> None:
        await self.bot.session.close()

    async def download_photo(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id, request_timeout=10)
        if file.file_size and file.file_size > MAX_BYTES:
            raise ValueError("file_size")
        if not file.file_path:
            raise ValueError("missing_file")
        destination = LimitedBuffer()
        await self.bot.download_file(file.file_path, destination=destination, timeout=30)
        return destination.getvalue()

    async def profile_photos(self, user_id: str, offset: int, limit: int) -> dict[str, Any]:
        photos = await self.bot.get_user_profile_photos(
            user_id=int(user_id),
            offset=offset,
            limit=limit,
            request_timeout=10,
        )
        return photos.model_dump(exclude_none=True)

    async def download_media(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id, request_timeout=10)
        if file.file_size and file.file_size > MAX_MEDIA_BYTES:
            raise ValueError("file_size")
        if not file.file_path:
            raise ValueError("missing_file")
        destination = LimitedBuffer(MAX_MEDIA_BYTES)
        await self.bot.download_file(file.file_path, destination=destination, timeout=30)
        return destination.getvalue()


class TelegramRuntime:
    def __init__(
        self,
        store: TelegramStore,
        token: str = "",
        disabled: bool = False,
        transport: Transport | None = None,
        dialogue: DialogueEngine | None = None,
        photos: PhotoEngine | None = None,
        memory: MemoryEngine | None = None,
        research: ResearchEngine | None = None,
        media: MediaEngine | None = None,
        avatars: AvatarEngine | None = None,
    ):
        self.store = store
        self.dialogue = dialogue
        self.photos = photos
        self.memory = memory
        self.research = research
        self.media = media
        self.avatars = avatars
        self._token = token
        self.transport = transport
        self.enabled = bool(token or transport) and not disabled
        self.connection = "not_configured" if not self.enabled else "waiting_admin"
        if disabled:
            self.connection = "dev_disabled"
        self.error: str | None = None
        self.bot: dict[str, Any] | None = None
        self.tasks: list[asyncio.Task[None]] = []
        self.last_poll: float | None = None
        self.api_calls = 0
        self.admin_ready = asyncio.Event()

    def start(self) -> None:
        if self.enabled and not self.tasks:
            self.tasks = [asyncio.create_task(self.run(), name="telegram-receiver")]

    @property
    def healthy(self) -> bool:
        return not self.enabled or (
            self.connection not in {"failed", "blocked", "retrying"}
            and bool(self.tasks)
            and all(not task.done() for task in self.tasks)
        )

    def status(self) -> dict[str, Any]:
        return {
            "state": self.connection,
            "error": self.error,
            "bot": self.bot,
            "last_poll": self.last_poll,
            "healthy": self.healthy,
            "workers": "running"
            if len(self.tasks) > 1 and all(not t.done() for t in self.tasks)
            else "not_running",
            "api_calls": self.api_calls,
        }

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        if self.transport:
            await self.transport.close()
        self.tasks.clear()

    async def photo_worker(self, bot_id: str) -> None:
        assert self.photos and self.transport
        try:
            while self.connection not in {"failed", "blocked"}:
                work = await self.photos.claim(bot_id)
                if work and not work.get("skip"):
                    await self.photos.execute(work, self.transport)
                if not work:
                    await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "photo_worker_failed"

    async def memory_worker(self, bot_id: str) -> None:
        assert self.memory
        try:
            while self.connection not in {"failed", "blocked"}:
                await self.memory.cleanup(bot_id)
                work = await self.memory.claim(bot_id)
                if work:
                    await self.memory.execute(work)
                else:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "memory_worker_failed"

    async def research_worker(self, bot_id: str) -> None:
        assert self.research
        try:
            while self.connection not in {"failed", "blocked"}:
                work = await self.research.claim(bot_id)
                if work and not work.get("skip"):
                    await self.research.execute(work)
                if not work:
                    await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "research_worker_failed"

    async def run(self) -> None:
        try:
            if await self.store.db.admin_hash() is None:
                await self.admin_ready.wait()
            self.connection = "connecting"
            try:
                self.transport = self.transport or AiogramTransport(self._token)
            except Exception:
                self.connection, self.error = "blocked", "invalid_token"
                return
            while self.bot is None:
                try:
                    self.api_calls += 2
                    identity = await self.transport.identity()
                    if identity["webhook"]:
                        self.connection, self.error = "blocked", "webhook_configured"
                        return
                    self.bot = identity
                except TelegramUnauthorizedError:
                    self.connection, self.error = "blocked", "unauthorized"
                    return
                except (TelegramNetworkError, TelegramServerError, TimeoutError):
                    self.connection, self.error = "retrying", "network"
                    await asyncio.sleep(5)
                except TelegramRetryAfter as exc:
                    self.connection, self.error = "retrying", "rate_limit"
                    await asyncio.sleep(max(1, exc.retry_after))
            bot_id = str(self.bot["id"])
            await self.store.recover(bot_id)
            if self.avatars:
                await self.avatars.recover(bot_id)
                self.tasks.append(asyncio.create_task(self.avatar_worker(bot_id), name="avatars"))
            if self.media:
                await self.media.recover(bot_id)
                self.tasks.append(asyncio.create_task(self.media_worker(bot_id), name="media"))
            if self.research:
                await self.research.recover(bot_id)
                self.tasks.append(
                    asyncio.create_task(self.research_worker(bot_id), name="research")
                )
            if self.memory:
                await self.memory.recover(bot_id)
                self.tasks.append(asyncio.create_task(self.memory_worker(bot_id), name="memory"))
            if self.photos:
                await self.photos.recover(bot_id)
                self.tasks.append(asyncio.create_task(self.photo_worker(bot_id), name="photos"))
            self.tasks.extend([asyncio.create_task(self.work(bot_id), name="telegram-worker")])
            if self.dialogue:
                self.tasks.append(
                    asyncio.create_task(self.typing_worker(bot_id), name="telegram-typing")
                )
                self.tasks.extend(
                    asyncio.create_task(self.generate(bot_id), name=f"dialogue-{i}")
                    for i in range(2)
                )
            while True:
                try:
                    offset = await self.store.offset(bot_id)
                    self.api_calls += 1
                    updates = await self.transport.updates(offset)
                    await self.store.ingest(bot_id, str(self.bot["username"]), updates)
                    self.last_poll = time.time()
                    if self.connection != "failed":
                        self.connection, self.error = "connected", None
                except (TelegramUnauthorizedError, TelegramConflictError):
                    self.connection, self.error = "blocked", "unauthorized_or_other_receiver"
                    return
                except TelegramRetryAfter as exc:
                    self.connection, self.error = "retrying", "rate_limit"
                    await asyncio.sleep(max(1, exc.retry_after))
                except (TelegramNetworkError, TelegramServerError, TimeoutError):
                    self.connection, self.error = "retrying", "network"
                    await asyncio.sleep(3)
                # SQLite/validation failures stop the receiver before it can acknowledge.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connection, self.error = "failed", "receiver_failed"
            logging.getLogger(__name__).error("receiver_failed: %s", type(exc).__name__)
            logging.getLogger(__name__).error(
                "receiver_frames: %s",
                [(f.name, f.lineno) for f in traceback.extract_tb(exc.__traceback__)],
            )

    async def avatar_worker(self, bot_id: str) -> None:
        assert self.avatars and self.transport
        try:
            while self.connection not in {"failed", "blocked"}:
                work = await self.avatars.claim(bot_id)
                if work and not work.get("skip"):
                    await self.avatars.execute(work, self.transport)
                if not work:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "avatar_worker_failed"

    async def media_worker(self, bot_id: str) -> None:
        assert self.media and self.transport
        try:
            while self.connection not in {"failed", "blocked"}:
                await self.media.cleanup(bot_id)
                work = await self.media.claim(bot_id)
                if work and not work.get("skip"):
                    await self.media.execute(work, self.transport)
                if not work:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "media_worker_failed"

    async def work(self, bot_id: str) -> None:
        try:
            while True:
                if self.connection in {"failed", "blocked"}:
                    return
                if not self.dialogue:
                    for _ in range(25):
                        if not await self.store.process_one(bot_id):
                            break
                await self.deliver_one(bot_id)
                # Service messages are infrequent; one sender, <=1 send/sec globally.
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "worker_failed"

    async def generate(self, bot_id: str) -> None:
        assert self.dialogue is not None
        try:
            while self.connection not in {"failed", "blocked"}:
                async with self.dialogue.capacity:
                    work = await self.dialogue.claim(bot_id)
                    if work and not work["skip"]:
                        await self.dialogue.execute(work)
                await asyncio.sleep(0.25 if work else 0.7)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "dialogue_worker_failed"

    async def typing_worker(self, bot_id: str) -> None:
        try:
            while self.connection not in {"failed", "blocked"}:
                await self.type_one(bot_id)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.connection, self.error = "failed", "typing_worker_failed"

    async def type_one(self, bot_id: str) -> None:
        if not self.transport:
            return
        async with self.store.gate:
            item = await self.store.claim_typing(
                bot_id, bool(self.dialogue and self.dialogue.adapter)
            )
            if not item:
                return
            try:
                async with asyncio.timeout(5):
                    self.api_calls += 1
                    accepted = await self.transport.typing(item["chat_id"], item["thread_id"])
            except TelegramRetryAfter as exc:
                await self.store.typing_result(
                    item, "rate_limited", "rate_limit", max(1, exc.retry_after)
                )
            except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError):
                await self.store.typing_result(item, "rejected", "rejected")
            except asyncio.CancelledError:
                await self.store.typing_result(item, "uncertain", "interrupted")
                raise
            except Exception:
                await self.store.typing_result(item, "uncertain", "network")
            else:
                await self.store.typing_result(
                    item,
                    "accepted" if accepted else "rejected",
                    None if accepted else "not_accepted",
                )

    async def deliver_one(self, bot_id: str) -> None:
        if not self.transport:
            return
        async with self.store.gate:
            item = await self.store.claim_delivery(bot_id)
            if not item:
                return
            try:
                self.api_calls += 1
                async with asyncio.timeout(12):
                    if item.get("action") == "reaction":
                        accepted = await self.transport.react(
                            item["chat_id"], item["target_message_id"], item["emoji"]
                        )
                        if not accepted:
                            await self.store.delivery_result(
                                item["id"], "failed", "reaction_unavailable"
                            )
                            return
                        message_id = None
                    elif item.get("reply_id"):
                        message_id = await self.transport.send(
                            item["chat_id"], item["text"], item.get("thread_id"), item["reply_id"]
                        )
                    else:
                        message_id = await self.transport.send(
                            item["chat_id"], item["text"], item.get("thread_id")
                        )
            except TelegramRetryAfter as exc:
                await self.store.delivery_result(
                    item["id"], "pending", "rate_limit", delay=max(1, exc.retry_after)
                )
            except TelegramMigrateToChat:
                await self.store.delivery_result(item["id"], "failed", "chat_migrated")
            except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError):
                await self.store.delivery_result(item["id"], "failed", "rejected")
            except asyncio.CancelledError:
                await self.store.delivery_result(item["id"], "unknown", "interrupted")
                raise
            except Exception:
                # Includes timeout, network and 5xx: Telegram may have accepted the send.
                await self.store.delivery_result(item["id"], "unknown", "delivery_uncertain")
            else:
                await self.store.delivery_result(item["id"], "sent", message_id=message_id)
