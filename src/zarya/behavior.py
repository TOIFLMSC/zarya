"""Scoped character state and durable, probability-based behavior decisions."""

import json
import random
import time
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zarya.database import ConflictError
from zarya.models import GroupBehaviorUpdate, Settings
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle

EMOJI = ["👍", "❤", "🔥", "🥰", "😁", "🤔", "😢", "😡", "🤝", "👀", "🤡"]
Tone = Literal["neutral", "curious", "warm", "grateful", "tender", "angry", "confident"]


class BehaviorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["text", "reaction", "silent"]
    text: str = Field(max_length=6000)
    reaction: str | None
    tone: Tone
    intensity: float = Field(ge=0, le=1)
    public_reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def coherent(self) -> "BehaviorPlan":
        if self.action == "text" and (not self.text.strip() or self.reaction is not None):
            raise ValueError("text_action")
        if self.action == "reaction" and (self.text.strip() or self.reaction not in EMOJI):
            raise ValueError("reaction_action")
        if self.action == "silent" and (self.text.strip() or self.reaction is not None):
            raise ValueError("silent_action")
        return self


def plan_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "behavior_plan",
        "strict": True,
        "schema": BehaviorPlan.model_json_schema(),
    }


async def one(conn: aiosqlite.Connection, sql: str, args: tuple[object, ...] = ()) -> Any:
    async with conn.execute(sql, args) as cursor:
        return await cursor.fetchone()


def decayed(
    tone: str, intensity: float, updated: float, now: float | None = None
) -> dict[str, Any]:
    # Half-life 30 minutes; UI reads never refresh the timestamp.
    value = round(intensity * 0.5 ** (max(0, (now or time.time()) - updated) / 1800), 3)
    return {
        "tone": tone if value >= 0.05 else "neutral",
        "intensity": value if value >= 0.05 else 0,
    }


async def mood(conn: aiosqlite.Connection, bot: str, chat: str, thread: int) -> dict[str, Any]:
    await conn.execute(
        "INSERT OR IGNORE INTO conversation_moods(bot_id,chat_id,thread_id,updated_at) "
        "VALUES (?,?,?,?)",
        (bot, chat, thread, time.time()),
    )
    state = await one(
        conn,
        "SELECT tone,intensity,updated_at,version,source_run_id,epoch FROM "
        "conversation_moods WHERE bot_id=? AND chat_id=? AND thread_id=?",
        (bot, chat, thread),
    )
    source = await one(conn, "SELECT state,snapshot FROM dialogue_runs WHERE id=?", (state[4],))
    value = decayed(state[0], state[1], state[2])
    if state[4] and (
        not source or source[0] == "cancelled" or json.loads(source[1]).get("invalidated")
    ):
        value = {"tone": "neutral", "intensity": 0}
    return {**value, "version": state[3], "epoch": state[5], "updated_at": state[2]}


async def policy(conn: aiosqlite.Connection, bot: str, chat: str) -> dict[str, Any]:
    value = await one(
        conn,
        "SELECT mode,chance_percent,version FROM group_behavior WHERE bot_id=? AND chat_id=?",
        (bot, chat),
    )
    return dict(zip(("mode", "chance_percent", "version"), value or ("off", 5, 0), strict=True))


def eligible(message: dict[str, Any]) -> bool:
    return bool(
        message.get("text", "").strip()
        and not message["text"].startswith("/")
        and not (
            message.get("from", {}).get("is_bot")
            or message.get("sender_chat")
            or message.get("forward_origin")
            or message.get("forward_date")
        )
        and not any(
            message.get(key)
            for key in ("photo", "video", "video_note", "voice", "animation", "document", "sticker")
        )
    )


async def prepare(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    thread: int,
    job: int,
    payload: dict[str, Any],
    message: dict[str, Any],
    settings: Settings,
    addressed: bool,
) -> dict[str, Any] | None:
    if not settings.behavior_enabled or not settings.dialogue_enabled:
        return None
    mode, rules = "addressed", None
    if not addressed:
        if (
            payload.get("trigger") != "ambient"
            or payload["scope"] != "group"
            or not eligible(message)
        ):
            return None
        rules = await policy(conn, bot, chat)
        if rules["mode"] == "off":
            return None
        if "initiative_draw" not in payload:
            payload["initiative_draw"] = random.random() * 100
            payload["initiative_policy_version"] = rules["version"]
            payload["initiative_mode"] = rules["mode"]
            payload["initiative_chance"] = rules["chance_percent"]
            await conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job))
        if (
            payload["initiative_policy_version"] != rules["version"]
            or payload["initiative_draw"] >= rules["chance_percent"]
        ):
            return None
        mode = rules["mode"]
    state = await mood(conn, bot, chat, thread)
    return {
        "schema_version": 1,
        "mode": mode,
        "mood_before": state,
        "policy_version": rules["version"] if rules else None,
        "chance_draw": payload.get("initiative_draw"),
        "thread_id": thread,
        "reactions_enabled": settings.reactions_enabled,
        "expressiveness": settings.expressiveness,
        "emotional_max_parts": settings.emotional_max_parts,
        "source_message_id": message["message_id"],
        "started_at": time.time(),
    }


def configure(
    request: dict[str, Any], state: dict[str, Any], prompts: PromptBundle = DEFAULT_PROMPTS
) -> None:
    data = json.loads(request["input"])
    data["behavior"] = {**state, "available_reactions": EMOJI if state["reactions_enabled"] else []}
    request["input"] = json.dumps(data, ensure_ascii=False)
    request["text"] = {**request["text"], "format": plan_format()}
    request["instructions"] += prompts.text("behavior.plan")


async def valid(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    state: dict[str, Any],
    *,
    delivery: bool = False,
) -> bool:
    settings = Settings.model_validate_json((await one(conn, "SELECT document FROM settings"))[0])
    if not settings.behavior_enabled or not settings.dialogue_enabled:
        return False
    current = await one(
        conn,
        "SELECT version,epoch FROM conversation_moods WHERE bot_id=? AND chat_id=? AND thread_id=?",
        (bot, chat, state["thread_id"]),
    )
    # Ordinary mood evolution must not cancel already prepared response parts.
    # Reset/forget invalidates all old plans, including late callbacks and delivery.
    if not current or current[1] != state["mood_before"].get("epoch", 1):
        return False
    if not delivery and current[0] != state["mood_before"]["version"]:
        return False
    if state["mode"] in {"shadow", "live"}:
        rules = await policy(conn, bot, chat)
        if rules["version"] != state["policy_version"] or rules["mode"] != state["mode"]:
            return False
        if delivery and state["mode"] != "live":
            return False
        latest = await one(
            conn,
            "SELECT message_id FROM recent_messages WHERE bot_id=? "
            "AND chat_id=? AND COALESCE(thread_id,0)=? AND role='user' "
            "ORDER BY received_at DESC,message_id DESC LIMIT 1",
            (bot, chat, state["thread_id"]),
        )
        if (
            time.time() - state["started_at"] > 90
            or not latest
            or latest[0] != state["source_message_id"]
        ):
            return False
    return True


async def commit(conn: aiosqlite.Connection, work: dict[str, Any], plan: BehaviorPlan) -> None:
    state = work["snapshot"]["behavior"]
    applied = work.get("mode", "live") == "live" and state["mode"] != "shadow"
    # Keep actual state changes gradual even if a model proposes an extreme jump.
    previous = state["mood_before"]["intensity"]
    intensity = min(plan.intensity, previous + 0.35)
    if state["expressiveness"] == "restrained":
        intensity = min(intensity, 0.5)
    plan.intensity = round(intensity, 3)
    state["plan"] = plan.model_dump()
    state["applied"] = applied
    if applied:
        await conn.execute(
            "UPDATE conversation_moods SET tone=?,intensity=?,updated_at=?,version=version+1,"
            "source_run_id=? WHERE bot_id=? AND chat_id=? AND thread_id=?",
            (
                plan.tone,
                plan.intensity,
                time.time(),
                work["run_id"],
                work["bot_id"],
                work["chat_id"],
                state["thread_id"],
            ),
        )
        state["committed_version"] = state["mood_before"]["version"] + 1
    await conn.execute(
        "INSERT INTO behavior_decisions VALUES (?,?,?,?,?,?,?)",
        (
            work["run_id"],
            state["mode"],
            plan.action,
            plan.model_dump_json(),
            plan.public_reason,
            int(applied),
            time.time(),
        ),
    )
    await conn.execute(
        "UPDATE dialogue_runs SET snapshot=? WHERE id=?",
        (json.dumps(work["snapshot"], ensure_ascii=False), work["run_id"]),
    )


class BehaviorEngine:
    def __init__(self, store: Any):
        self.store, self.db = store, store.db

    async def update_policy(self, bot: str, chat: str, body: GroupBehaviorUpdate) -> dict[str, Any]:
        async with self.store.gate, self.db.transaction() as conn:
            access = await one(
                conn,
                "SELECT scope,state FROM telegram_access WHERE bot_id=? AND subject_id=?",
                (bot, chat),
            )
            if not access or access[0] != "group" or access[1] != "approved":
                raise ValueError("Настройки инициативы доступны только разрешённым группам")
            old = await policy(conn, bot, chat)
            if old["version"] != body.expected_version:
                raise ConflictError
            await conn.execute(
                "INSERT INTO group_behavior VALUES (?,?,?,?,?) ON CONFLICT(bot_id,"
                "chat_id) DO UPDATE SET mode=excluded.mode,chance_percent="
                "excluded.chance_percent,version=excluded.version",
                (bot, chat, body.mode, body.chance_percent, old["version"] + 1),
            )
            await cancel_pending(conn, bot, chat, initiative_only=True)
            return await policy(conn, bot, chat)

    async def reset(self, bot: str, chat: str, thread: int, expected: int) -> None:
        async with self.store.gate, self.db.transaction() as conn:
            changed = await conn.execute(
                "UPDATE conversation_moods SET tone='neutral',intensity=0,updated_at=?,"
                "version=version+1,epoch=epoch+1,source_run_id=NULL WHERE bot_id=? AND chat_id=? "
                "AND thread_id=? AND version=?",
                (time.time(), bot, chat, thread, expected),
            )
            if changed.rowcount != 1:
                raise ConflictError
            await cancel_pending(conn, bot, chat, thread=thread)

    async def overview(
        self, bot: str, scope: str = "all", chat: str | None = None, page: int = 0
    ) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            async with conn.execute(
                "SELECT subject_id,title,scope,state FROM telegram_access "
                "WHERE bot_id=? AND (?='all' OR scope=?) ORDER BY updated_at DESC",
                (bot, scope, scope),
            ) as cur:
                peers = list(await cur.fetchall())
            groups = [
                {
                    "chat_id": p[0],
                    "title": p[1],
                    "scope": p[2],
                    "access": p[3],
                    "policy": await policy(conn, bot, p[0]),
                }
                for p in peers
            ]
            moods = []
            for peer in peers:
                if chat and peer[0] != chat:
                    continue
                async with conn.execute(
                    "SELECT thread_id FROM conversation_moods WHERE bot_id=? AND chat_id=?",
                    (bot, peer[0]),
                ) as cur:
                    threads = [r[0] for r in await cur.fetchall()]
                for thread in threads:
                    state = await mood(conn, bot, peer[0], thread)
                    reason = await one(
                        conn,
                        "SELECT b.public_reason FROM behavior_decisions b "
                        "JOIN conversation_moods m ON m.source_run_id=b.run_id "
                        "JOIN dialogue_runs r ON r.id=b.run_id WHERE m.bot_id=? "
                        "AND m.chat_id=? AND m.thread_id=? AND r.state!='cancelled' "
                        "AND NOT COALESCE(json_extract(r.snapshot,'$.invalidated'),0)",
                        (bot, peer[0], thread),
                    )
                    moods.append(
                        {
                            "chat_id": peer[0],
                            "thread_id": thread,
                            "title": peer[1],
                            "scope": peer[2],
                            **state,
                            "reason": reason[0] if reason else "",
                        }
                    )
            async with conn.execute(
                "SELECT b.run_id,r.chat_id,r.thread_id,b.mode,b.action,b.plan_json,b.public_reason,"
                "b.applied,b.created_at,r.state,c.cost_usd,c.state,"
                "(SELECT group_concat(state,',') FROM outbox o WHERE o.job_id=r.job_id) "
                "FROM behavior_decisions b "
                "JOIN dialogue_runs r ON r.id=b.run_id LEFT JOIN model_calls c ON c.run_id=r.id "
                "JOIN telegram_access a ON a.bot_id=r.bot_id AND a.subject_id=r.chat_id "
                "WHERE r.bot_id=? AND (?='all' OR a.scope=?) AND (? IS NULL OR r.chat_id=?) "
                "ORDER BY b.run_id DESC LIMIT 20 OFFSET ?",
                (bot, scope, scope, chat, chat, page * 20),
            ) as cur:
                decisions = [
                    dict(
                        zip(
                            (
                                "run_id",
                                "chat_id",
                                "thread_id",
                                "mode",
                                "action",
                                "plan",
                                "reason",
                                "applied",
                                "created_at",
                                "state",
                                "cost_usd",
                                "call_state",
                                "delivery",
                            ),
                            r,
                            strict=True,
                        )
                    )
                    for r in await cur.fetchall()
                ]
            for decision in decisions:
                decision["plan"] = json.loads(decision["plan"])
            total = await one(
                conn,
                "SELECT COUNT(*) FROM behavior_decisions b JOIN dialogue_runs r ON r.id=b.run_id "
                "JOIN telegram_access a ON a.bot_id=r.bot_id AND a.subject_id=r.chat_id "
                "WHERE r.bot_id=? AND (?='all' OR a.scope=?) AND (? IS NULL OR r.chat_id=?)",
                (bot, scope, scope, chat, chat),
            )
            return {
                "peers": groups,
                "moods": moods,
                "decisions": decisions,
                "total": total[0],
                "page": page,
            }


async def cancel_pending(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str | None = None,
    *,
    initiative_only: bool = False,
    thread: int | None = None,
) -> None:
    where = "bot_id=? AND json_type(snapshot,'$.behavior')='object'"
    args: list[object] = [bot]
    if chat is not None:
        where += " AND chat_id=?"
        args.append(chat)
    if thread is not None:
        where += " AND COALESCE(thread_id,0)=?"
        args.append(thread)
    if initiative_only:
        where += " AND json_extract(snapshot,'$.behavior.mode') IN ('shadow','live')"
    await conn.execute(
        "UPDATE jobs SET state='cancelled' WHERE state IN ('pending','running') "
        "AND id IN (SELECT job_id FROM dialogue_runs WHERE " + where + ")",
        args,
    )
    await conn.execute(
        "UPDATE outbox SET state='cancelled',error_code='behavior_changed' WHERE "
        "state='pending' AND job_id IN (SELECT job_id FROM dialogue_runs WHERE " + where + ")",
        args,
    )


async def erase_chat(conn: aiosqlite.Connection, bot: str, chat: str) -> None:
    await conn.execute("DELETE FROM conversation_moods WHERE bot_id=? AND chat_id=?", (bot, chat))
    await conn.execute(
        "DELETE FROM behavior_decisions WHERE run_id IN "
        "(SELECT id FROM dialogue_runs WHERE bot_id=? AND chat_id=?)",
        (bot, chat),
    )
    await conn.execute(
        "UPDATE group_behavior SET mode='off',version=version+1 WHERE bot_id=? AND chat_id=?",
        (bot, chat),
    )


async def cleanup(conn: aiosqlite.Connection, bot: str, cutoff: float) -> None:
    await conn.execute(
        "DELETE FROM behavior_decisions WHERE run_id IN (SELECT id FROM "
        "dialogue_runs WHERE bot_id=?) AND (created_at<? OR run_id IN "
        "(SELECT id FROM dialogue_runs WHERE state='cancelled' OR "
        "COALESCE(json_extract(snapshot,'$.invalidated'),0)))",
        (bot, cutoff),
    )
    await conn.execute(
        "UPDATE conversation_moods SET tone='neutral',intensity=0,source_run_id=NULL,"
        "version=version+1,epoch=epoch+1 WHERE bot_id=? AND ((updated_at<? AND "
        "(source_run_id IS NOT NULL OR intensity>0 OR tone!='neutral')) OR source_run_id IN "
        "(SELECT id FROM dialogue_runs WHERE state='cancelled' OR "
        "COALESCE(json_extract(snapshot,'$.invalidated'),0)))",
        (bot, cutoff),
    )
