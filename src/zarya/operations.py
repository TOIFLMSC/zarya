"""Read-only pilot telemetry. Never return prompts, message bodies or provider IDs."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from zarya.database import Database

CALLS = """
WITH calls AS (
 SELECT c.id,c.model,c.state,c.created_at,c.latency_ms,c.cost_usd,c.search_cost_usd,
 c.error_code,c.settings_version,
 CASE WHEN c.avatar_run_id IS NOT NULL THEN 'avatar'
 WHEN c.research_run_id IS NOT NULL THEN 'research'
 WHEN c.photo_batch_id IS NOT NULL THEN 'photo'
 WHEN c.memory_batch_id IS NOT NULL THEN 'memory'
 WHEN c.media_run_id IS NOT NULL THEN CASE WHEN c.operation='asr' THEN 'asr' ELSE 'video' END
 WHEN d.mode='paid' THEN 'replay' WHEN d.id IS NOT NULL THEN 'dialogue' ELSE 'other' END task,
 COALESCE(d.bot_id,p.bot_id,m.bot_id,r.bot_id,v.bot_id,a.bot_id,e.bot_id,'') bot_id,
 COALESCE(d.chat_id,p.chat_id,m.chat_id,r.chat_id,v.chat_id,a.chat_id,e.chat_id,'') chat_id,
 CASE WHEN d.id IS NOT NULL THEN CASE WHEN d.chat_id LIKE '-%' THEN 'group' ELSE 'private' END
 ELSE COALESCE(p.scope,m.scope,r.scope,v.scope,a.scope,'unknown') END scope,
 COALESCE(c.cost_usd,0)+COALESCE(c.search_cost_usd,0) known_cost_usd,
 CASE WHEN c.cost_usd IS NULL OR (c.research_run_id IS NOT NULL AND c.search_cost_usd IS NULL)
 THEN 1 ELSE 0 END unknown_cost
 FROM model_calls c LEFT JOIN dialogue_runs d ON d.id=c.run_id
 LEFT JOIN photo_batches p ON p.id=c.photo_batch_id
 LEFT JOIN memory_batches m ON m.id=c.memory_batch_id
 LEFT JOIN research_runs r ON r.id=c.research_run_id
 LEFT JOIN media_runs v ON v.id=c.media_run_id
 LEFT JOIN avatar_runs a ON a.id=c.avatar_run_id
 LEFT JOIN jobs j ON j.id=c.job_id LEFT JOIN events e ON e.id=j.event_id
), selected AS (SELECT * FROM calls WHERE created_at>=? AND created_at<=?
 AND (?='' OR bot_id=?) AND (?='all' OR scope=?) AND (?='' OR chat_id=?))
"""

PILOT_CASES = [
    (
        "conversation",
        "Характер и краткость",
        "Простой вопрос, шутка, спор и просьба объяснить подробнее. Оценить "
        "естественность, длину и отсутствие лишних встречных вопросов.",
    ),
    (
        "reply",
        "Обращения и цепочки реплаев",
        "Личка, имя в группе, реплай на ответ о старом посте. Ответ "
        "относится к выбранному посту, typing виден, реплай только у первой "
        "части.",
    ),
    (
        "memory",
        "Память и границы чатов",
        "Сообщить имя и интерес; спросить позже. Проверить общие факты в "
        "другой группе и отсутствие переноса личного без согласия. Исправить "
        "и удалить запись.",
    ),
    (
        "photo",
        "Фото и альбом",
        "Фото с надписью, альбом, два разных поста. Обычное фото в группе не "
        "вызывает ответа; вопрос выбирает правильное вложение.",
    ),
    (
        "media",
        "Голос и короткое видео",
        "Голосовое с шумом и короткий ролик со звуком. Сравнить смысл ответа "
        "с оригиналом, отметить пропущенные события и честность ограничений.",
    ),
    (
        "research",
        "Ссылки и фактчек",
        "Прямая и скрытая ссылка, пересланный пост и YouTube. Проверить "
        "основание вывода; ссылки в ответе только по просьбе, недоступность "
        "без выдумок.",
    ),
    (
        "initiative",
        "Реакции и инициатива",
        "Благодарность, шутка и обычная беседа. Оценить предпросмотр при 5%; "
        "затем отдельно включить live в выбранной группе и проверить "
        "уместность.",
    ),
    (
        "recovery",
        "Сбой и восстановление",
        "На тестовой установке: сеть, 429, остановка во время обработки и "
        "доставки. Unknown не повторяется автоматически. Создать, проверить "
        "и восстановить копию в новый каталог.",
    ),
]


class Operations:
    def __init__(self, db: Database):
        self.db = db

    async def overview(
        self,
        *,
        days: int = 7,
        bot: str = "",
        scope: str = "all",
        chat: str = "",
        page: int = 0,
        problems: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        params = (
            (now - timedelta(days=days)).isoformat(),
            now.isoformat(),
            bot,
            bot,
            scope,
            scope,
            chat,
            chat,
        )
        async with self.db.read_lock:
            # All totals/breakdowns/rows use one SQLite snapshot while writes continue.
            try:
                await self.db.reader.execute("BEGIN")

                async def rows(sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
                    async with self.db.reader.execute(sql, values) as cursor:
                        names = [c[0] for c in cursor.description or []]
                        return [
                            dict(zip(names, row, strict=True)) for row in await cursor.fetchall()
                        ]

                aggregate = (
                    "COUNT(*) calls,COALESCE(SUM(known_cost_usd),0) known_cost_usd,"
                    "COALESCE(SUM(unknown_cost),0) unknown_cost_calls,"
                    "AVG(latency_ms) avg_latency_ms,"
                    "COALESCE(SUM(state='unknown'),0) unknown_outcomes,"
                    "MIN(created_at) first_call_at"
                )
                summary = (await rows(CALLS + "SELECT " + aggregate + " FROM selected", params))[0]
                latency = await rows(
                    CALLS
                    + ", ranked AS (SELECT latency_ms,ROW_NUMBER() OVER (ORDER BY latency_ms) n,"
                    "COUNT(*) OVER () total FROM selected WHERE latency_ms IS NOT NULL "
                    "AND latency_ms>=0) SELECT latency_ms,total FROM ranked "
                    "WHERE n=(total*95+99)/100",
                    params,
                )
                summary["p95_latency_ms"] = latency[0]["latency_ms"] if latency else None
                summary["latency_samples"] = latency[0]["total"] if latency else 0
                breakdown = {}
                for dimension in ("model", "task", "chat_id"):
                    breakdown[dimension] = await rows(
                        CALLS
                        + f"SELECT {dimension} label,"
                        + aggregate
                        + f" FROM selected GROUP BY {dimension} ORDER BY known_cost_usd DESC,label",
                        params,
                    )
                condition = (
                    " WHERE state!='completed' OR error_code IS NOT NULL" if problems else ""
                )
                total = (await rows(CALLS + "SELECT COUNT(*) n FROM selected" + condition, params))[
                    0
                ]["n"]
                calls = await rows(
                    CALLS + "SELECT id,model,task,scope,chat_id,state,created_at,latency_ms,"
                    "known_cost_usd,unknown_cost,error_code,settings_version FROM selected"
                    + condition
                    + " ORDER BY id DESC LIMIT 20 OFFSET ?",
                    params + (page * 20,),
                )
                chats = await rows(
                    "SELECT bot_id,scope,subject_id chat_id,title FROM telegram_access "
                    "WHERE (?='' OR bot_id=?) ORDER BY title",
                    (bot, bot),
                )
                queue_union = """SELECT 'jobs' queue,j.state,e.bot_id,e.chat_id,
                    CASE WHEN e.chat_id LIKE '-%' THEN 'group' ELSE 'private' END scope,j.created_at
                    FROM jobs j JOIN events e ON e.id=j.event_id
                    UNION ALL SELECT 'delivery',state,bot_id,chat_id,scope,created_at FROM outbox
                    UNION ALL SELECT 'photo',state,bot_id,chat_id,scope,created_at
                    FROM photo_batches
                    UNION ALL SELECT 'memory',state,bot_id,chat_id,scope,created_at
                    FROM memory_batches
                    UNION ALL SELECT 'research',state,bot_id,chat_id,scope,created_at
                    FROM research_runs
                    UNION ALL SELECT 'media',state,bot_id,chat_id,scope,created_at
                    FROM media_runs
                    UNION ALL SELECT 'avatar',state,bot_id,chat_id,scope,created_at
                    FROM avatar_runs"""
                queues = await rows(
                    "SELECT queue,state,COUNT(*) count,MIN(created_at) oldest_at FROM ("
                    + queue_union
                    + ") WHERE state NOT IN ('completed','done','sent','cancelled','dismissed',"
                    "'skipped','cache','cached','rejected','invalid',"
                    "'failed','partial','unavailable') "
                    "AND (?='' OR bot_id=?) AND (?='all' OR scope=?) "
                    "AND (?='' OR chat_id=?) GROUP BY queue,state",
                    (bot, bot, scope, scope, chat, chat),
                )
                reviews = await rows("SELECT * FROM pilot_reviews ORDER BY case_id")
                preferences = (await rows("SELECT version,warning_usd FROM pilot_preferences"))[0]
                warning_summary = (
                    await rows(
                        CALLS + "SELECT " + aggregate + " FROM selected",
                        (
                            (now - timedelta(days=30)).isoformat(),
                            now.isoformat(),
                            bot,
                            bot,
                            "all",
                            "all",
                            "",
                            "",
                        ),
                    )
                )[0]
                return {
                    "generated_at": now.isoformat(),
                    "from_at": params[0],
                    "bot_id": bot,
                    "days": days,
                    "summary": summary,
                    "breakdown": breakdown,
                    "calls": calls,
                    "total": total,
                    "pages": math.ceil(total / 20),
                    "chats": chats,
                    "queues": queues,
                    "cases": [
                        {"id": key, "title": title, "scenario": scenario}
                        for key, title, scenario in PILOT_CASES
                    ],
                    "reviews": reviews,
                    "preferences": preferences,
                    "warning_summary": warning_summary,
                }
            finally:
                rollback = asyncio.create_task(self.db.reader.rollback())
                try:
                    await asyncio.shield(rollback)
                except asyncio.CancelledError:
                    await rollback
                    raise

    async def preferences(self, expected: int, warning: float | None) -> None:
        from zarya.database import ConflictError

        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE pilot_preferences SET warning_usd=?,version=version+1 WHERE version=?",
                (warning, expected),
            )
            if cursor.rowcount != 1:
                raise ConflictError

    async def review(self, case: str, status: str, note: str, expected: int) -> None:
        from zarya.database import ConflictError

        if case not in {c[0] for c in PILOT_CASES}:
            raise ValueError("Неизвестный сценарий")
        async with self.db.transaction() as conn:
            async with conn.execute(
                "SELECT version FROM pilot_reviews WHERE case_id=?", (case,)
            ) as cursor:
                current = await cursor.fetchone()
            if (current[0] if current else 0) != expected:
                raise ConflictError
            await conn.execute(
                "INSERT INTO pilot_reviews(case_id,status,note,version,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                "status=excluded.status,note=excluded.note,version=excluded.version,updated_at=excluded.updated_at",
                (case, status, note, expected + 1, datetime.now(UTC).isoformat()),
            )
