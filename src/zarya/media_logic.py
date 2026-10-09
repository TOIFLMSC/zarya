"""Media source fences and erasure, including derived answers/replays."""

import json
import time
from typing import Any

import aiosqlite

from zarya.models import Settings
from zarya.source_material import valid as source_valid

MEDIA_WAIT = (
    "EXISTS (SELECT 1 FROM media_dependencies md JOIN media_runs mr "
    "ON mr.id=md.media_run_id WHERE md.job_id=j.id AND mr.state IN "
    "('queued','preparing','transcribing','analyzing')) "
)


async def valid(conn: aiosqlite.Connection, run: int) -> bool:
    async with conn.execute(
        "SELECT bot_id,chat_id,access_version,message_id,event_id,state,analysis_context "
        "FROM media_runs WHERE id=?",
        (run,),
    ) as c:
        item = await c.fetchone()
    if not item or item[5] == "cancelled":
        return False
    async with conn.execute("SELECT document FROM settings WHERE id=1") as c:
        settings_row = await c.fetchone()
    assert settings_row is not None
    settings = Settings.model_validate_json(settings_row[0])
    return (
        settings.media_enabled
        and settings.dialogue_enabled
        and await source_valid(
            conn,
            item[0],
            item[1],
            item[2],
            [{"message_id": item[3], "event_id": item[4]}]
            + json.loads(item[6]).get("manifest", []),
        )
    )


async def refs_valid(conn: aiosqlite.Connection, refs: list[dict[str, Any]]) -> bool:
    for ref in refs:
        if not await valid(conn, ref["id"]):
            return False
    return True


async def current_job(conn: aiosqlite.Connection, run: int, job: int | None = None) -> Any:
    async with conn.execute(
        "SELECT j.id,e.id,e.payload FROM media_dependencies md JOIN jobs j ON j.id=md.job_id "
        "JOIN events e ON e.id=j.event_id WHERE md.media_run_id=? "
        "AND j.state='pending' AND e.payload!='{}' "
        "AND (? IS NULL OR j.id=?) "
        "AND CAST(strftime('%s',j.created_at) AS REAL)>=? "
        "AND NOT EXISTS (SELECT 1 FROM jobs newer JOIN events ne ON ne.id=newer.event_id "
        "WHERE ne.bot_id=e.bot_id AND ne.chat_id=e.chat_id AND newer.id>j.id "
        "AND json_extract(newer.payload,'$.sender_id')=json_extract(j.payload,'$.sender_id') "
        "AND COALESCE(json_extract(newer.payload,'$.thread_id'),0)="
        "COALESCE(json_extract(j.payload,'$.thread_id'),0) "
        "AND json_extract(newer.payload,'$.trigger') IN "
        "('private','name','mention','reply','continuation')) ORDER BY j.id LIMIT 1",
        (run, job, job, time.time() - 600),
    ) as c:
        return await c.fetchone()


async def current_dependency(conn: aiosqlite.Connection, run: int) -> bool:
    return await current_job(conn, run) is not None


async def purge_analysis(conn: aiosqlite.Connection, where: str, args: tuple[object, ...]) -> None:
    """Forget a question-dependent analysis while retaining independent speech/frames."""
    from zarya.research_logic import purge as purge_research
    from zarya.source_material import purge as purge_material

    ids = "SELECT id FROM media_runs WHERE analysis_context!='{}' AND (" + where + ")"
    await purge_research(
        conn,
        "job_id IN (SELECT job_id FROM media_dependencies WHERE media_run_id IN (" + ids + "))",
        args,
    )
    await purge_material(
        conn,
        "EXISTS (SELECT 1 FROM json_each(snapshot,'$.media_refs') m "
        "WHERE json_extract(m.value,'$.id') IN (" + ids + "))",
        args,
    )
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE operation='vision' "
        "AND media_run_id IN (" + ids + ")",
        args,
    )
    await conn.execute(
        "UPDATE media_runs SET result=NULL,analysis_context='{}',"
        "state=CASE WHEN transcript IS NULL THEN 'unavailable' ELSE 'partial' END,"
        "error_code='analysis_context_changed' WHERE id IN (" + ids + ")",
        args,
    )


async def purge(conn: aiosqlite.Connection, where: str, args: tuple[object, ...]) -> None:
    from zarya.research_logic import purge as purge_research
    from zarya.source_material import purge as purge_material

    ids = "SELECT id FROM media_runs WHERE " + where
    await purge_research(
        conn,
        "job_id IN (SELECT job_id FROM media_dependencies WHERE media_run_id IN (" + ids + "))",
        args,
    )
    await conn.execute(
        "INSERT OR IGNORE INTO media_file_cleanup(path) "
        "SELECT CAST(id AS TEXT) FROM media_runs WHERE " + where,
        args,
    )
    await purge_material(
        conn,
        "EXISTS (SELECT 1 FROM json_each(snapshot,'$.media_refs') m "
        "WHERE json_extract(m.value,'$.id') IN (" + ids + "))",
        args,
    )
    await conn.execute(
        "UPDATE model_calls SET request_json=NULL WHERE media_run_id IN (" + ids + ")", args
    )
    await conn.execute(
        "UPDATE media_runs SET state='cancelled',transcript=NULL,result=NULL,"
        "frames='[]',coverage='{}',analysis_context='{}',file_id='',"
        "error_code='source_changed' WHERE " + where,
        args,
    )
