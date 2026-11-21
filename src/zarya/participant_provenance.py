"""Validate reply ancestry as a DAG, independently of memory's depth bound."""

import hashlib
import json
import time
from typing import Any

import aiosqlite

from zarya.memory_read import MemoryRead
from zarya.source_material import one


async def valid(
    conn: aiosqlite.Connection,
    bot: str,
    snapshot: dict[str, Any],
    *,
    read: MemoryRead | None = None,
    depth: int = 0,
) -> bool:
    refs = snapshot.get("context_refs")
    if snapshot.get("invalidated"):
        return False
    if refs is None:
        return True
    if read is None:
        with MemoryRead(conn) as scoped:
            return await valid(conn, bot, snapshot, read=scoped, depth=depth)
    try:
        chat, grant = snapshot["retrieval_chat_id"], snapshot["source_access_version"]
        access = await read.one(
            conn,
            "SELECT state,version,member_state FROM telegram_access "
            "WHERE bot_id=? AND subject_id=?",
            (bot, chat),
            cache=False,
        )
        if (
            not access
            or access[0] != "approved"
            or access[1] != grant
            or access[2]
            in {
                "left",
                "kicked",
                "migrated",
            }
        ):
            return False
        roots = {r["run_id"] for r in refs["assistants"]}
        if roots and max(roots) >= snapshot.get("context_run_id", max(roots) + 1):
            return False
        async with conn.execute(
            "WITH RECURSIVE ancestors(id) AS (SELECT value FROM json_each(?) UNION "
            "SELECT e.parent_run_id FROM context_run_edges e JOIN ancestors a ON e.run_id=a.id) "
            "SELECT r.id,r.bot_id,r.chat_id,r.mode,r.state,json_remove(r.snapshot,"
            "'$.request','$.context','$.settings','$.research','$.retrieval',"
            "'$.participant_context','$.context_budget','$.question_text',"
            "'$.behavior','$.incoming') "
            "FROM dialogue_runs r "
            "JOIN ancestors a ON a.id=r.id ORDER BY r.id",
            (json.dumps(sorted(roots)),),
        ) as cursor:
            parents = await cursor.fetchall()
        if not roots <= {r[0] for r in parents}:
            return False
        async with conn.execute(
            "SELECT run_id,parent_run_id FROM context_run_edges WHERE run_id IN "
            "(SELECT value FROM json_each(?))",
            (json.dumps([r[0] for r in parents]),),
        ) as cursor:
            edges: dict[int, set[int]] = {}
            for child, parent_id in await cursor.fetchall():
                edges.setdefault(child, set()).add(parent_id)
        documents = [(None, snapshot)]
        for rid, parent_bot, parent_chat, mode, state, raw in parents:
            parent = json.loads(raw)
            if (
                parent_bot != bot
                or parent_chat != chat
                or mode != "live"
                or state != "completed"
                or parent.get("invalidated")
                or parent.get("source_access_version") != grant
            ):
                return False
            links = {r["run_id"] for r in parent.get("context_refs", {}).get("assistants", [])}
            stored = edges.get(rid, set())
            if links != stored or any(p >= rid for p in links):
                return False
            documents.append((rid, parent))
        humans: dict[int, dict[str, Any]] = {}
        parts: dict[int, dict[str, Any]] = {}
        media: dict[int, dict[str, Any]] = {}
        summaries: dict[int, str] = {}
        for _, document in documents:
            for r in document.get("context_refs", {}).get("humans", []):
                previous = humans.get(r["message_id"])
                if previous and previous != r:
                    return False
                humans[r["message_id"]] = r
            for r in document.get("context_refs", {}).get("assistants", []):
                parts[r["outbox_id"]] = r
            for r in document.get("context_refs", {}).get("media", []):
                media[r["id"]] = r
            for r in document.get("context_refs", {}).get("summaries", []):
                if r["id"] in summaries and summaries[r["id"]] != r["hash"]:
                    return False
                summaries[r["id"]] = r["hash"]
        cutoff = time.time() - 90 * 86400
        count = await one(
            conn,
            "SELECT COUNT(*) FROM json_each(?) ref JOIN participant_messages p "
            "ON p.message_id=json_extract(ref.value,'$.message_id') "
            "AND p.event_id=json_extract(ref.value,'$.event_id') "
            "AND p.received_at=json_extract(ref.value,'$.received_at') "
            "JOIN research_messages m ON m.bot_id=p.bot_id AND m.chat_id=p.chat_id "
            "AND m.message_id=p.message_id AND m.event_id=p.event_id "
            "AND m.access_version=p.access_version JOIN events e ON e.id=p.event_id "
            "WHERE p.bot_id=? AND p.chat_id=? AND p.access_version=? AND p.received_at>? "
            "AND e.bot_id=p.bot_id AND e.chat_id=p.chat_id AND e.disposition='accepted' "
            "AND e.payload!='{}' AND CAST(strftime('%s',e.received_at) AS REAL)>?",
            (json.dumps(list(humans.values())), bot, chat, grant, cutoff, cutoff),
        )
        if count[0] != len(humans):
            return False
        async with conn.execute(
            "SELECT id,result,manifest,media_refs FROM participant_summary_blocks "
            "WHERE id IN (SELECT value FROM json_each(?)) AND bot_id=? AND chat_id=? "
            "AND access_version=? AND state='completed' AND result IS NOT NULL",
            (json.dumps(list(summaries)), bot, chat, grant),
        ) as cursor:
            summary_rows = list(await cursor.fetchall())
        if len(summary_rows) != len(summaries):
            return False
        for bid, text, manifest, summary_media in summary_rows:
            if hashlib.sha256(text.encode()).hexdigest() != summaries[bid] or any(
                humans.get(r["message_id"]) != r for r in json.loads(manifest)
            ):
                return False
            for r in json.loads(summary_media):
                media[r["id"]] = r
        count = await one(
            conn,
            "SELECT COUNT(*) FROM json_each(?) ref JOIN outbox o "
            "ON o.id=json_extract(ref.value,'$.outbox_id') JOIN dialogue_runs r "
            "ON r.job_id=o.job_id AND r.id=json_extract(ref.value,'$.run_id') "
            "JOIN jobs j ON j.id=r.job_id JOIN research_messages source "
            "ON source.bot_id=r.bot_id AND source.chat_id=r.chat_id "
            "AND source.message_id=r.message_id AND source.event_id=j.event_id "
            "AND source.access_version=o.access_version JOIN recent_messages m "
            "ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id AND m.message_id=o.message_id "
            "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.state='sent' "
            "AND r.mode='live' AND r.state='completed' AND m.role='assistant' "
            "AND m.received_at>? AND json_extract(o.payload,'$.text')=m.text",
            (json.dumps(list(parts.values())), bot, chat, grant, cutoff),
        )
        if count[0] != len(parts):
            return False
        from zarya.avatar_logic import valid as avatar_valid
        from zarya.media_logic import refs_valid
        from zarya.memory_context import valid_snapshot
        from zarya.source_material import valid as source_valid

        source_refs: dict[int, dict[str, Any]] = {}
        inherited_media: dict[int, dict[str, Any]] = dict(media)
        avatars = set()
        # New reply ancestry does not increment old memory dependency depth.
        for _rid, parent in documents[1:]:
            shallow = {k: v for k, v in parent.items() if k != "context_refs"}
            if not await valid_snapshot(conn, bot, shallow, read=read, depth=depth):
                return False
            for ref in parent.get("source_manifest", []):
                previous_source = source_refs.get(ref["message_id"])
                if previous_source and previous_source != ref:
                    return False
                source_refs[ref["message_id"]] = ref
            for ref in parent.get("media_refs", []):
                inherited_media[ref["id"]] = ref
            if parent.get("avatar_id"):
                avatars.add(parent["avatar_id"])
        if not await source_valid(
            conn, bot, chat, grant, list(source_refs.values()), read=read
        ) or not await refs_valid(conn, list(inherited_media.values())):
            return False
        for avatar in avatars:
            if not await avatar_valid(conn, avatar):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


async def cascade_invalid(conn: aiosqlite.Connection) -> None:
    from zarya.source_material import purge

    await purge(
        conn,
        "id IN (SELECT e.run_id FROM context_run_edges e JOIN dialogue_runs p "
        "ON p.id=e.parent_run_id WHERE json_extract(p.snapshot,'$.invalidated')=1 "
        "OR p.state='cancelled')",
        (),
    )
