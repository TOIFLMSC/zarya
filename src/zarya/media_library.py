"""Read-only, jointly paginated index of photo batches and media operations."""

from typing import Any

from zarya.database import Database


async def listing(
    db: Database, bot: str | None, scope: str, chat: str | None, kind: str, page: int
) -> dict[str, Any]:
    # Keep processing, access checks and assets in their original engines. This
    # index contains metadata only; opening a result still validates its sources.
    cte = """WITH library AS (
        SELECT 'photo' AS source, b.id, b.bot_id, b.chat_id, b.scope, b.state,
            b.created_at, NULL AS duration,
            (SELECT COUNT(*) FROM photo_items i WHERE i.batch_id=b.id) AS count,
            'photo' AS kind
        FROM photo_batches b WHERE b.bot_id=?
        UNION ALL
        SELECT 'media', id, bot_id, chat_id, scope, state, created_at, duration,
            1, kind FROM media_runs WHERE bot_id=?
    ), filtered AS (SELECT * FROM library WHERE (?='all' OR scope=?)
        AND (?='' OR chat_id=?) AND (?='all' OR kind=?)) """
    args = (bot, bot, scope, scope, chat or "", chat or "", kind, kind)
    async with db.transaction() as conn:
        cursor = await conn.execute(cte + "SELECT COUNT(*) FROM filtered", args)
        count_row = await cursor.fetchone()
        assert count_row is not None  # An aggregate without GROUP BY always returns one row.
        total = count_row[0]
        page = min(page, max(0, (total - 1) // 20))
        cursor = await conn.execute(
            cte
            + """SELECT f.source,f.id,f.chat_id,f.scope,f.state,f.created_at,
                f.duration,f.count,f.kind,a.title FROM filtered f
                LEFT JOIN telegram_access a ON a.bot_id=f.bot_id AND a.scope=f.scope
                    AND a.subject_id=f.chat_id
                ORDER BY f.created_at DESC,f.source DESC,f.id DESC LIMIT 20 OFFSET ?""",
            args + (page * 20,),
        )
        keys = (
            "source",
            "id",
            "chat_id",
            "scope",
            "state",
            "created_at",
            "duration",
            "count",
            "kind",
            "chat_title",
        )
        items = [dict(zip(keys, row, strict=True)) for row in await cursor.fetchall()]
        cursor = await conn.execute(
            cte
            + """SELECT SUM(m.cost_usd), SUM(m.cost_usd IS NULL), COUNT(*)
                FROM model_calls m JOIN filtered f
                ON (f.source='photo' AND m.photo_batch_id=f.id)
                OR (f.source='media' AND m.media_run_id=f.id)""",
            args,
        )
        cost_row = await cursor.fetchone()
        assert cost_row is not None
        cost, unknown, calls = cost_row
        cursor = await conn.execute(
            cte
            + """SELECT DISTINCT l.chat_id,l.scope,a.title FROM library l
                LEFT JOIN telegram_access a ON a.bot_id=l.bot_id AND a.scope=l.scope
                    AND a.subject_id=l.chat_id
                WHERE (?='all' OR l.scope=?) ORDER BY a.title,l.chat_id""",
            args + (scope, scope),
        )
        chats = [
            dict(zip(("id", "scope", "title"), r, strict=True)) for r in await cursor.fetchall()
        ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": 20,
        "chats": chats,
        "known_cost_usd": cost if calls else 0,
        "unknown_cost_calls": unknown or 0,
    }
