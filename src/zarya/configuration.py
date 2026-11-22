"""Private immutable SQL releases; settings and prompt package commit together."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from zarya.models import Settings
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle, _unique


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def package(prompts: PromptBundle) -> dict[str, Any]:
    return {"schema_version": 1, "fragments": dict(prompts.fragments)}


def document(settings: Settings, prompts: PromptBundle) -> dict[str, Any]:
    return {"settings": settings.model_dump(), "prompts": package(prompts)}


def validate(raw: str) -> tuple[Settings, PromptBundle]:
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
        # Older immutable releases omit settings introduced after their publication.
        optional = {
            "memory_retrieval",
            "participant_context_messages",
            "topic_context_chat_id",
            "participant_summaries_enabled",
            "participant_summary_prompt",
        }
        if set(data) != {"settings", "prompts"} or not (
            set(Settings.model_fields) - optional
            <= set(data["settings"])
            <= set(Settings.model_fields)
        ):
            raise ValueError
        settings = Settings.model_validate(data["settings"], strict=True)
        prompts = PromptBundle.parse(
            canonical(data["prompts"]).encode(), set(DEFAULT_PROMPTS.fragments)
        )
        return settings, prompts
    except (ValueError, TypeError, KeyError):
        raise ValueError("Некорректная конфигурация: проверь поля, типы и размеры") from None


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


async def prompts_at(c: aiosqlite.Connection, version: int, fallback: PromptBundle) -> PromptBundle:
    async with c.execute(
        "SELECT prompts_json FROM configuration_releases WHERE version<=? "
        "ORDER BY version DESC LIMIT 1",
        (version,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        # Legacy queued work predates the bootstrap release and used its installation package.
        async with c.execute(
            "SELECT prompts_json FROM configuration_releases ORDER BY version LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return PromptBundle.parse(row[0].encode(), set(DEFAULT_PROMPTS.fragments)) if row else fallback


async def record(
    c: aiosqlite.Connection,
    version: int,
    settings: Settings,
    prompts: PromptBundle,
    origin: str,
    parent: int | None,
) -> None:
    await c.execute(
        "INSERT INTO configuration_releases VALUES (?,?,?,?,?,?,?,?)",
        (
            version,
            settings.model_dump_json(),
            canonical(package(prompts)),
            digest(document(settings, prompts)),
            prompts.sha256,
            origin,
            parent,
            datetime.now(UTC).isoformat(),
        ),
    )


async def effects(conn: aiosqlite.Connection, settings: Settings) -> None:
    """The same disable effects for Web UI, graph activation and rollback."""
    from zarya.avatar_logic import purge as purge_avatars
    from zarya.behavior import cancel_pending
    from zarya.media_logic import purge as purge_media

    if not settings.behavior_enabled or not settings.dialogue_enabled:
        async with conn.execute("SELECT DISTINCT bot_id FROM telegram_access") as cursor:
            bots = [r[0] for r in await cursor.fetchall()]
        for bot in bots:
            await cancel_pending(conn, bot)
    if not settings.media_enabled or not settings.dialogue_enabled:
        await purge_media(conn, "state!='cancelled'", ())
    if not settings.photo_enabled or not settings.dialogue_enabled:
        await purge_avatars(conn, "state!='cancelled'", ())
        await conn.execute(
            "UPDATE photo_batches SET state='cancelled',error_code='disabled' "
            "WHERE state IN ('queued','downloading','analyzing')"
        )
        await conn.execute(
            "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','runn"
            "ing') AND id IN (SELECT job_id FROM dialogue_runs WHERE json_arra"
            "y_length(snapshot,'$.photo_refs')>0)"
        )
        await conn.execute(
            "UPDATE outbox SET state='cancelled',error_code='photos_disabled' "
            "WHERE state='pending' AND job_id IN (SELECT job_id FROM dialogue_"
            "runs WHERE json_array_length(snapshot,'$.photo_refs')>0)"
        )
    if not settings.research_enabled or not settings.dialogue_enabled:
        await conn.execute(
            "UPDATE jobs SET state='cancelled' WHERE id IN (SELECT job_id FROM"
            " research_runs WHERE state IN ('queued','fetching','search_starte"
            "d')) AND state IN ('pending','running')"
        )
        await conn.execute(
            "UPDATE research_runs SET state='cancelled',error_code='disabled' "
            "WHERE state IN ('queued','fetching','search_started')"
        )
    if not settings.memory_enabled:
        await conn.execute("UPDATE memory_epochs SET version=version+1")
        await conn.execute("UPDATE dialogue_runs SET snapshot=json_remove(snapshot,'$.retrieval')")
        await conn.execute(
            "UPDATE memory_batches SET state='cancelled',error_code='disabled'"
            " WHERE state='started'"
        )
    if not settings.dialogue_enabled:
        await conn.execute(
            "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','runn"
            "ing') AND kind='telegram' AND json_extract(payload,'$.service_rep"
            "ly')=0"
        )
        await conn.execute(
            "UPDATE outbox SET state='cancelled',error_code='disabled' WHERE s"
            "tate='pending' AND job_id IN (SELECT job_id FROM model_calls)"
        )
        await conn.execute("DELETE FROM active_dialogues")
