import asyncio
import hmac
import logging
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from zarya import __version__
from zarya.avatars import AvatarEngine
from zarya.backup import MARKER
from zarya.behavior import BehaviorEngine
from zarya.config import Config
from zarya.configuration_bridge import ConfigurationBridge
from zarya.database import ConflictError, Database
from zarya.dialogue import DialogueEngine
from zarya.graph import GraphRepository, GraphUnavailable, vault_zip
from zarya.media import MediaEngine
from zarya.media_library import listing as media_library_listing
from zarya.media_processing import MediaProcessor
from zarya.memory import MemoryEngine
from zarya.models import (
    AccessDecision,
    ConfigurationDecision,
    ConfigurationRollback,
    DeliveryDismiss,
    GroupBehaviorUpdate,
    LoginInput,
    MemoryMutation,
    MoodReset,
    ObsidianDecision,
    ObsidianUndo,
    PilotPreferences,
    PilotReview,
    ReplayInput,
    SettingsSnapshot,
    SettingsUpdate,
    SetupInput,
)
from zarya.obsidian import ObsidianBridge
from zarya.openai_adapter import ModelAdapter, OpenAIAdapter, SpeechAdapter
from zarya.operations import Operations
from zarya.photos import PhotoEngine
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle
from zarya.research import ResearchEngine
from zarya.research_fetch import Fetcher
from zarya.security import Sessions, bootstrap_token, hash_password, verify_password
from zarya.telegram_runtime import TelegramRuntime, Transport
from zarya.telegram_store import TelegramStore

COOKIE = "zarya_session"
logger = logging.getLogger(__name__)


class LocalBoundary(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, config: Config):
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "")
        if f"http://{host}" not in self.config.origins:
            return JSONResponse({"detail": "Недопустимый адрес панели"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed_origins = {f"http://{host}"}
            if self.config.dev_ui:
                allowed_origins.add("http://127.0.0.1:5173")
            if request.headers.get("origin") not in allowed_origins:
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Ожидается JSON"}, status_code=415)
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > 32768:
                    return JSONResponse({"detail": "Запрос слишком большой"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response


def create_app(
    config: Config | None = None,
    telegram_transport: Transport | None = None,
    model_adapter: ModelAdapter | None = None,
    research_fetcher: Fetcher | None = None,
    speech_adapter: SpeechAdapter | None = None,
) -> FastAPI:
    config = config or Config.from_env()
    if (config.data_dir / MARKER).exists():
        raise RuntimeError(
            "Восстановленная копия в карантине. Не запускайте её параллельно рабочему боту; "
            "см. docs/operations.md"
        )
    db = Database(config.data_dir / "zarya.sqlite3")
    prompts = DEFAULT_PROMPTS
    if db.path.is_file():
        with sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True) as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='configuration_releases'"
            ).fetchone()
            saved = (
                c.execute(
                    "SELECT prompts_json FROM configuration_releases ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if exists
                else None
            )
        if saved:
            prompts = PromptBundle.parse(saved[0].encode(), set(DEFAULT_PROMPTS.fragments))
        else:
            prompts = (
                PromptBundle.load(config.prompts_file) if config.prompts_file else DEFAULT_PROMPTS
            )
    else:
        prompts = PromptBundle.load(config.prompts_file) if config.prompts_file else DEFAULT_PROMPTS
    telegram_store = TelegramStore(db, prompts)
    behavior = BehaviorEngine(telegram_store)
    operations = Operations(db)
    dialogue = DialogueEngine(
        telegram_store,
        model_adapter
        or (
            OpenAIAdapter(config.openai_api_key)
            if config.openai_api_key and not config.dev_ui
            else None
        ),
    )
    photos = PhotoEngine(telegram_store, dialogue.adapter, config.data_dir)
    dialogue.photos = photos
    avatars = AvatarEngine(telegram_store, dialogue.adapter)
    dialogue.avatars = avatars
    memory = MemoryEngine(telegram_store, dialogue.adapter, config.data_dir)
    dialogue.memory = memory
    research = ResearchEngine(telegram_store, dialogue.adapter, research_fetcher)
    dialogue.research = research
    media = MediaEngine(
        telegram_store,
        dialogue.adapter,
        speech_adapter
        or (dialogue.adapter if isinstance(dialogue.adapter, OpenAIAdapter) else None),
        config.data_dir,
        MediaProcessor(config.ffmpeg_path, config.ffprobe_path),
    )
    dialogue.media = media
    telegram = TelegramRuntime(
        telegram_store,
        config.telegram_token,
        config.dev_ui,
        telegram_transport,
        dialogue,
        photos,
        memory,
        research,
        media,
        avatars,
    )
    obsidian = ObsidianBridge(
        memory,
        config.obsidian_file or config.data_dir / "obsidian.local.json",
        lambda: str(telegram.bot["id"]) if telegram.bot else None,
    )
    configuration_bridge = ConfigurationBridge(
        telegram_store, config.obsidian_file or config.data_dir / "obsidian.local.json"
    )
    sessions = Sessions(config.session_seconds)
    auth_lock = asyncio.Lock()
    attempts: deque[float] = deque()
    setup_path = config.data_dir / "bootstrap-token.txt"
    started = time.monotonic()
    ready = False
    setup_secret = ""

    def remove_bootstrap_file() -> None:
        try:
            setup_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Administrator configured; remove the obsolete bootstrap file locally.")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal ready, setup_secret, started
        config.data_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(config.data_dir / "instance.lock", timeout=0)
        with lock:
            await db.open()
            try:
                await db.initialize_configuration(prompts)
                if await db.admin_hash() is None:
                    setup_secret = bootstrap_token(setup_path)
                else:
                    remove_bootstrap_file()
                started = time.monotonic()
                ready = True
                telegram.start()
                obsidian.start()
                configuration_bridge.start()
                yield
            finally:
                ready = False
                sessions.values.clear()
                await configuration_bridge.stop()
                await obsidian.stop()
                await telegram.stop()
                await dialogue.close()
                await db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalBoundary, config=config)
    app.state.db = db
    app.state.sessions = sessions
    app.state.telegram = telegram
    app.state.telegram_store = telegram_store
    app.state.configuration_bridge = configuration_bridge
    app.state.dialogue = dialogue
    app.state.photos = photos
    app.state.avatars = avatars
    app.state.memory = memory
    app.state.research = research
    app.state.media = media
    app.state.behavior = behavior
    app.state.obsidian = obsidian

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors include raw inputs, potentially passwords. Return only field locations.
        fields = [".".join(str(part) for part in error["loc"][1:]) for error in exc.errors()]
        return JSONResponse(
            {"detail": "Проверьте заполненные поля", "fields": fields}, status_code=422
        )

    async def authenticated(request: Request) -> None:
        session = sessions.get(request.cookies.get(COOKIE, ""))
        if session is None:
            raise HTTPException(401, "Войдите в панель")
        if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()
        ):
            raise HTTPException(403, "Недействительный защитный токен")

    def throttle() -> None:
        now = time.monotonic()
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 8:
            raise HTTPException(429, "Слишком много попыток. Подождите минуту.")
        attempts.append(now)

    def enter(response: Response) -> dict[str, object]:
        token, session = sessions.create()
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=config.session_seconds,
            secure=False,
            path="/",
        )
        return {"authenticated": True, "setup_required": False, "csrf": session.csrf}

    @app.get("/healthz")
    async def health() -> Response:
        try:
            await db.one("SELECT 1")
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        healthy = ready and telegram.healthy
        return JSONResponse(
            {"status": "ok"}
            if healthy
            else {
                "status": "unavailable",
                "worker_state": telegram.connection,
                "worker_error": telegram.error,
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/api/session")
    async def session_info(request: Request) -> dict[str, object]:
        session = sessions.get(request.cookies.get(COOKIE, ""))
        return {
            "authenticated": session is not None,
            "setup_required": await db.admin_hash() is None,
            "csrf": session.csrf if session else None,
        }

    @app.post("/api/setup")
    async def setup(body: SetupInput, response: Response) -> dict[str, object]:
        nonlocal setup_secret
        throttle()
        async with auth_lock:
            if await db.admin_hash() is not None:
                raise HTTPException(409, "Администратор уже настроен")
            if not setup_secret or not hmac.compare_digest(
                body.token.get_secret_value().encode(), setup_secret.encode()
            ):
                raise HTTPException(403, "Неверный одноразовый ключ")
            hashed = await asyncio.to_thread(hash_password, body.password.get_secret_value())
            if not await db.create_admin(hashed):
                raise HTTPException(409, "Администратор уже настроен")
            setup_secret = ""
            remove_bootstrap_file()
            telegram.admin_ready.set()
            return enter(response)

    @app.post("/api/login")
    async def login(body: LoginInput, response: Response) -> dict[str, object]:
        throttle()
        async with auth_lock:
            stored = await db.admin_hash()
            if stored is None:
                raise HTTPException(409, "Сначала создайте администратора")
            valid = await asyncio.to_thread(
                verify_password, body.password.get_secret_value(), stored
            )
            if not valid:
                raise HTTPException(401, "Неверный пароль")
            return enter(response)

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        sessions.remove(request.cookies.get(COOKIE, ""))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(authenticated)])
    async def get_settings() -> SettingsSnapshot:
        return await db.settings()

    @app.get("/api/configuration", dependencies=[Depends(authenticated)])
    async def configuration_status(page: int = Query(default=0, ge=0, le=10000)) -> dict[str, Any]:
        return await configuration_bridge.listing(page)

    @app.get("/api/configuration/drafts/{draft_id}", dependencies=[Depends(authenticated)])
    async def configuration_details(draft_id: str) -> dict[str, Any]:
        try:
            return await configuration_bridge.details(draft_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/api/configuration/scan", dependencies=[Depends(authenticated)])
    async def configuration_scan() -> dict[str, bool]:
        try:
            await configuration_bridge.scan()
            return {"ok": True}
        except (ValueError, OSError, ConflictError):
            raise HTTPException(
                409, "Проверка конфигурации прервана. Проверь файлы и повтори"
            ) from None

    @app.post("/api/configuration/drafts/{draft_id}", dependencies=[Depends(authenticated)])
    async def configuration_decide(draft_id: str, body: ConfigurationDecision) -> dict[str, int]:
        try:
            version = await configuration_bridge.decide(
                draft_id, body.action, body.expected_version, body.hash
            )
            return {"version": version}
        except (ValueError, OSError, ConflictError):
            raise HTTPException(
                409, "Конфигурация или файлы изменились. Обнови сравнение перед активацией"
            ) from None

    @app.post("/api/configuration/rollback", dependencies=[Depends(authenticated)])
    async def configuration_rollback(body: ConfigurationRollback) -> dict[str, str]:
        try:
            return {"id": await configuration_bridge.rollback(body.version, body.expected_version)}
        except (ValueError, ConflictError):
            raise HTTPException(409, "Конфигурация изменилась или выпуск недоступен") from None

    @app.put("/api/settings", dependencies=[Depends(authenticated)])
    async def save_settings(body: SettingsUpdate) -> SettingsSnapshot:
        try:
            async with telegram_store.gate:
                result = await db.update_settings(body.expected_version, body.settings)
                return result
        except ConflictError as exc:
            raise HTTPException(
                409, "Настройки изменены в другой вкладке. Загрузите их заново."
            ) from exc

    def behavior_bot() -> str:
        return str(telegram.bot["id"]) if telegram.bot else ""

    @app.get("/api/behavior", dependencies=[Depends(authenticated)])
    async def get_behavior(
        scope: Literal["all", "private", "group"] = "all",
        chat_id: str | None = None,
        page: int = Query(default=0, ge=0, le=100000),
    ) -> dict[str, Any]:
        snapshot = await db.settings()
        return {
            **await behavior.overview(behavior_bot(), scope, chat_id, page),
            "settings": snapshot.settings.model_dump(),
            "settings_version": snapshot.version,
        }

    @app.put("/api/behavior/groups/{chat_id}", dependencies=[Depends(authenticated)])
    async def save_group_behavior(chat_id: str, body: GroupBehaviorUpdate) -> dict[str, Any]:
        try:
            return await behavior.update_policy(behavior_bot(), chat_id, body)
        except ConflictError as exc:
            raise HTTPException(
                409, "Настройка группы изменена. Черновик сохранён; обновите данные."
            ) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/behavior/moods/{chat_id}/reset", dependencies=[Depends(authenticated)])
    async def reset_mood(chat_id: str, body: MoodReset) -> dict[str, Any]:
        try:
            await behavior.reset(behavior_bot(), chat_id, body.thread_id, body.expected_version)
        except ConflictError as exc:
            raise HTTPException(
                409, "Настроение изменилось. Обновите состояние и повторите сброс."
            ) from exc
        return {"ok": True}

    @app.post("/api/behavior/stop", dependencies=[Depends(authenticated)])
    async def stop_behavior() -> dict[str, Any]:
        async with telegram_store.gate:
            current = await db.settings()
            saved = await db.update_settings(
                current.version, current.settings.model_copy(update={"dialogue_enabled": False})
            )
            async with db.transaction() as conn:
                await conn.execute(
                    "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','running') "
                    "AND kind='telegram' AND json_extract(payload,'$.service_reply')=0"
                )
                await conn.execute(
                    "UPDATE outbox SET state='cancelled',error_code='disabled' "
                    "WHERE state='pending' AND job_id IN (SELECT job_id FROM model_calls)"
                )
                await conn.execute("DELETE FROM active_dialogues")
        return {"settings": saved.settings.model_dump(), "version": saved.version}

    @app.get("/api/operations", dependencies=[Depends(authenticated)])
    async def operations_overview(
        days: int = Query(default=7, ge=7, le=90),
        scope: Literal["all", "private", "group"] = "all",
        chat: str = Query(default="", max_length=30),
        page: int = Query(default=0, ge=0, le=100000),
        problems: bool = False,
    ) -> dict[str, Any]:
        if days not in {7, 30, 90}:
            raise HTTPException(422, "Период: 7, 30 или 90 дней")
        return await operations.overview(
            days=days,
            bot=str(telegram.bot.get("id", "")) if telegram.bot else "",
            scope=scope,
            chat=chat,
            page=page,
            problems=problems,
        )

    @app.put("/api/operations/preferences", dependencies=[Depends(authenticated)])
    async def pilot_preferences(payload: PilotPreferences) -> dict[str, bool]:
        try:
            await operations.preferences(payload.expected_version, payload.warning_usd)
        except ConflictError:
            raise HTTPException(409, "Порог изменён в другой вкладке. Обнови данные.") from None
        return {"saved": True}

    @app.put("/api/operations/reviews/{case}", dependencies=[Depends(authenticated)])
    async def pilot_review(case: str, payload: PilotReview) -> dict[str, bool]:
        try:
            await operations.review(case, payload.status, payload.note, payload.expected_version)
        except ConflictError:
            raise HTTPException(409, "Проверка изменена в другой вкладке. Обнови данные.") from None
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"saved": True}

    @app.get("/api/status", dependencies=[Depends(authenticated)])
    async def status() -> dict[str, object]:
        row = await db.one("SELECT COUNT(*) FROM schema_migrations")
        settings = await db.settings()
        return {
            "version": __version__,
            "mode": "local",
            "stage": 9,
            "uptime_seconds": int(time.monotonic() - started),
            "database": {
                "status": "connected",
                "engine": "SQLite WAL",
                "schema_version": row[0],
                "sqlite_version": sqlite3.sqlite_version,
            },
            "settings_version": settings.version,
            "integrations": {
                "telegram": telegram.connection,
                "openai": "configured" if dialogue.adapter else "not_configured",
            },
            "workers": telegram.status()["workers"],
            "external_calls": telegram.api_calls,
        }

    def current_bot(expected: str | None = None) -> str:
        if telegram.bot is None:
            raise HTTPException(409, "Сначала дождитесь подключения Telegram")
        bot_id = str(telegram.bot["id"])
        if expected is not None and bot_id != expected:
            raise HTTPException(409, "Подключён другой бот. Обновите страницу")
        return bot_id

    @app.get("/api/telegram", dependencies=[Depends(authenticated)])
    async def telegram_status() -> dict[str, object]:
        return {
            **telegram.status(),
            "diagnostics": await telegram_store.diagnostics(current_bot())
            if telegram.bot
            else None,
        }

    @app.get("/api/telegram/access", dependencies=[Depends(authenticated)])
    async def telegram_access(
        scope: Literal["group", "private"], page: int = Query(default=0, ge=0, le=100000)
    ) -> dict[str, object]:
        if telegram.bot is None:
            return {"items": [], "total": 0, "page": page, "bot_id": None}
        bot_id = current_bot()
        return {**await telegram_store.peers(bot_id, scope, page), "bot_id": bot_id}

    @app.post("/api/telegram/access", dependencies=[Depends(authenticated)])
    async def decide_access(body: AccessDecision) -> dict[str, bool]:
        try:
            await telegram_store.decide(
                current_bot(body.bot_id),
                body.scope,
                body.subject_id,
                body.state,
                body.expected_version,
            )
        except ConflictError as exc:
            raise HTTPException(
                409, "Запись изменилась. Обновите список и повторите решение"
            ) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/dialogues", dependencies=[Depends(authenticated)])
    async def dialogues(
        page: int = Query(default=0, ge=0, le=100000),
        scope: Literal["all", "group", "private"] = "all",
    ) -> dict[str, object]:
        return await dialogue.listing(
            page, scope, str(telegram.bot["id"]) if telegram.bot else None
        )

    @app.get("/api/memory", dependencies=[Depends(authenticated)])
    async def memory_list(scope: Literal["all", "group", "private"] = "all") -> dict[str, object]:
        return await memory.listing(str(telegram.bot["id"]) if telegram.bot else None, scope)

    @app.get("/api/memory/chats/{chat_id}", dependencies=[Depends(authenticated)])
    async def memory_profile(chat_id: str) -> dict[str, object]:
        result = await memory.profile(current_bot(), chat_id)
        if result is None:
            raise HTTPException(404, "Чат недоступен")
        return result

    @app.get("/api/memory/chats/{chat_id}/context", dependencies=[Depends(authenticated)])
    async def participant_context(
        chat_id: str,
        sender_id: str = Query(pattern=r"^[1-9][0-9]{0,15}$"),
    ) -> dict[str, Any]:
        result = await memory.participant_window(current_bot(), chat_id, sender_id)
        if result is None:
            raise HTTPException(404, "Чат недоступен")
        return result

    graph = GraphRepository(db.path)

    async def graph_snapshot(chat_id: str) -> dict[str, Any]:
        try:
            result = await graph.snapshot(current_bot(), chat_id)
        except GraphUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "Чат недоступен")
        return result

    @app.get("/api/memory/chats/{chat_id}/graph", dependencies=[Depends(authenticated)])
    async def memory_graph(chat_id: str) -> dict[str, Any]:
        return await graph_snapshot(chat_id)

    @app.get("/api/memory/chats/{chat_id}/graph/vault", dependencies=[Depends(authenticated)])
    async def memory_graph_vault(chat_id: str) -> Response:
        snapshot = await graph_snapshot(chat_id)
        archive = await asyncio.to_thread(vault_zip, snapshot)
        return Response(
            archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="zarya-memory-vault.zip"',
            },
        )

    @app.post("/api/memory/facts/{fact_id}", dependencies=[Depends(authenticated)])
    async def memory_mutation(fact_id: int, body: MemoryMutation) -> dict[str, bool]:
        try:
            await memory.mutate(current_bot(body.bot_id), fact_id, body)
        except ConflictError as exc:
            raise HTTPException(409, "Запись изменилась. Обновите её перед сохранением") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/obsidian", dependencies=[Depends(authenticated)])
    async def obsidian_status(
        page: int = Query(default=0, ge=0, le=100000),
        history_page: int = Query(default=0, ge=0, le=100000),
    ) -> dict[str, Any]:
        return await obsidian.listing(
            str(telegram.bot["id"]) if telegram.bot else None,
            page,
            history_page,
        )

    @app.post("/api/obsidian/scan", dependencies=[Depends(authenticated)])
    async def obsidian_scan() -> dict[str, bool]:
        try:
            await obsidian.scan()
        except (ValueError, OSError, GraphUnavailable) as exc:
            obsidian.error = (
                str(exc) if not isinstance(exc, OSError) else "Папка занята или недоступна"
            )
            raise HTTPException(409, obsidian.error) from exc
        return {"ok": True}

    @app.post("/api/obsidian/changes/{change_id}", dependencies=[Depends(authenticated)])
    async def obsidian_decision(change_id: str, body: ObsidianDecision) -> dict[str, bool]:
        try:
            await obsidian.decide(current_bot(), change_id, body.action)
        except (ConflictError, ValueError, OSError) as exc:
            raise HTTPException(
                409, "Правка изменилась или недоступна. Обнови синхронизацию"
            ) from exc
        return {"ok": True}

    @app.post("/api/obsidian/history/{command_id}/undo", dependencies=[Depends(authenticated)])
    async def obsidian_undo(command_id: str, body: ObsidianUndo) -> dict[str, bool]:
        try:
            await obsidian.undo(current_bot(), command_id, body.operation_id)
        except (ConflictError, ValueError) as exc:
            raise HTTPException(
                409, "Отмена недоступна: запись изменилась или сведения отозваны"
            ) from exc
        return {"ok": True}

    @app.get("/api/photos", dependencies=[Depends(authenticated)])
    async def photo_list(
        page: int = Query(default=0, ge=0, le=100000),
        scope: Literal["all", "group", "private"] = "all",
    ) -> dict[str, object]:
        settings = await db.settings()
        return {
            **await photos.listing(str(telegram.bot["id"]) if telegram.bot else None, scope, page),
            "enabled": settings.settings.photo_enabled and settings.settings.dialogue_enabled,
            "model": settings.settings.photo_model,
            "bot": telegram.bot,
        }

    @app.get("/api/media-library", dependencies=[Depends(authenticated)])
    async def media_library(
        scope: Literal["all", "private", "group"] = "all",
        chat_id: str | None = Query(default=None, max_length=20),
        kind: Literal["all", "photo", "voice", "video", "video_note", "animation"] = "all",
        page: int = Query(default=0, ge=0, le=100000),
    ) -> dict[str, object]:
        settings = (await db.settings()).settings
        return {
            **await media_library_listing(
                db, str(telegram.bot["id"]) if telegram.bot else None, scope, chat_id, kind, page
            ),
            "photo_enabled": settings.photo_enabled and settings.dialogue_enabled,
            "photo_model": settings.photo_model,
            "photo_configured": photos.adapter is not None,
            "enabled": settings.media_enabled and settings.dialogue_enabled,
            "configured": bool(media.adapter and media.speech),
            "tools_available": media.processor.available,
            "model": settings.media_model,
            "asr_model": settings.asr_model,
            "limits": {
                "voice": settings.voice_max_seconds,
                "video": settings.video_max_seconds,
                "frames": settings.video_max_frames,
                "items": 3,
            },
        }

    @app.get("/api/media", dependencies=[Depends(authenticated)])
    async def media_list(
        scope: Literal["all", "private", "group"] = "all",
        chat_id: str | None = Query(default=None, max_length=20),
        page: int = Query(default=0, ge=0, le=10000),
    ) -> dict[str, object]:
        return await media.listing(current_bot(), scope, chat_id, page)

    @app.get("/api/media/{run_id}/frames/{index}", dependencies=[Depends(authenticated)])
    async def media_frame(run_id: int, index: int) -> FileResponse:
        path = await media.asset(run_id, index, current_bot())
        if path is None:
            raise HTTPException(404, "Кадр недоступен")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/media/{run_id}", dependencies=[Depends(authenticated)])
    async def media_detail(run_id: int) -> dict[str, object]:
        result = await media.details(run_id, current_bot())
        if result is None:
            raise HTTPException(404, "Операция не найдена")
        return result

    @app.get("/api/research", dependencies=[Depends(authenticated)])
    async def research_list(
        page: int = Query(default=0, ge=0, le=100000),
        scope: Literal["all", "group", "private"] = "all",
        chat: str = Query(default="", max_length=20),
    ) -> dict[str, object]:
        return await research.listing(
            str(telegram.bot["id"]) if telegram.bot else None, scope, chat, page
        )

    @app.get("/api/research/{research_id}", dependencies=[Depends(authenticated)])
    async def research_details(research_id: int) -> dict[str, object]:
        result = await research.details(research_id, current_bot())
        if not result:
            raise HTTPException(404, "Операция не найдена")
        return result

    @app.get("/api/photos/images/{item_id}", dependencies=[Depends(authenticated)])
    async def photo_image(item_id: int) -> Response:
        asset = await photos.asset(item_id, current_bot())
        if not asset:
            raise HTTPException(
                403, "Фото недоступно: проверьте разрешение чата и версию сообщения"
            )
        if not asset[0].is_file():
            raise HTTPException(404, "Исходник фото отсутствует")
        return FileResponse(asset[0], media_type=asset[1])

    @app.get("/api/photos/{batch_id}", dependencies=[Depends(authenticated)])
    async def photo_detail(batch_id: int) -> dict[str, object]:
        result = await photos.details(batch_id, current_bot())
        if not result:
            raise HTTPException(404, "Операция не найдена")
        return result

    @app.get("/api/dialogues/{run_id}", dependencies=[Depends(authenticated)])
    async def dialogue_details(run_id: int) -> dict[str, object]:
        result = await dialogue.details(run_id)
        if not result or result["bot_id"] != current_bot():
            raise HTTPException(404, "Операция не найдена")
        return result

    @app.post("/api/dialogues/replay", dependencies=[Depends(authenticated)])
    async def replay_dialogue(body: ReplayInput) -> dict[str, object]:
        original = await dialogue.details(body.run_id)
        if not original or original["bot_id"] != current_bot():
            raise HTTPException(404, "Операция не найдена")
        try:
            return await dialogue.replay(body.run_id, body.mode, body.expected_settings_version)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/telegram/dismiss", dependencies=[Depends(authenticated)])
    async def dismiss_delivery(body: DeliveryDismiss) -> dict[str, bool]:
        try:
            await telegram_store.dismiss(current_bot(body.bot_id), body.id)
        except ConflictError as exc:
            raise HTTPException(409, "Состояние отправки изменилось. Обновите список") from exc
        return {"ok": True}

    if (config.web_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=config.web_dir / "assets"), name="assets")

    @app.get("/")
    async def index() -> Response:
        if not (config.web_dir / "index.html").is_file():
            return JSONResponse(
                {"detail": "Сначала соберите Web UI. См. README.md"}, status_code=503
            )
        return FileResponse(config.web_dir / "index.html")

    return app
