"""G6: bounded local graph ranking over one authorised, revision-checked snapshot."""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from zarya.memory_context import (
    RETENTION,
    dependencies,
    merge_dependencies,
    reply_context,
    select_episodes,
    valid_dependencies,
)
from zarya.memory_observations import strength
from zarya.memory_read import MemoryRead
from zarya.source_material import accepted

ALGORITHM = "local-graph-v1"
LIMITS = {
    "facts": 150,
    "episodes": 200,
    "edges": 600,
    "seeds": 8,
    "hops": 2,
    "neighbours": 3,
    "reply_messages": 6,
}


def terms(text: str) -> set[str]:
    # Keep tickers, numbers and short names; longer Russian words use a small lexical stem.
    return {w[:6] for w in re.findall(r"[\w]+", text.casefold()) if len(w) >= 2} - {
        "заря",
        "что",
        "это",
        "как",
        "там",
        "его",
        "она",
        "мне",
        "тут",
        "ещё",
        "еще",
        "этот",
        "этого",
        "какой",
        "помниш",
        "расска",
        "которы",
        "чтобы",
    }


async def conversation_chain(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    event_id: int | None,
    *,
    read: MemoryRead | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Only retained accepted messages or actually sent assistant answers, never nested text."""
    if read is None:
        with MemoryRead(conn) as read:
            return await conversation_chain(conn, bot, chat, grant, event_id, read=read)
    result: list[dict[str, Any]] = []
    deps = dependencies()
    seen: set[tuple[int, bool]] = set()
    size = 0
    include_message = False
    for _ in range(LIMITS["reply_messages"]):
        if event_id is None or (event_id, include_message) in seen:
            break
        seen.add((event_id, include_message))
        item, extra = await reply_context(
            conn,
            bot,
            chat,
            grant,
            {"event_id": event_id},
            include_message=include_message,
            read=read,
        )
        include_message = False
        if not item or size + len(item["text"]) > 6000:
            break
        combined = json.loads(json.dumps(deps))
        merge_dependencies(combined, extra)
        # Reserve the same nesting depth as final snapshot validation.
        if not await valid_dependencies(conn, bot, chat, grant, combined, depth=1, read=read):
            break
        result.append(item)
        deps = combined
        size += len(item["text"])
        if item["role"] == "user":
            event_id = extra["replies"][0]["event_id"]
        else:
            sent = await read.one(
                conn,
                "SELECT j.event_id FROM jobs j JOIN dialogue_runs r ON r.job_id=j.id "
                "WHERE r.id=? AND r.bot_id=? AND r.chat_id=?",
                (extra["runs"][0]["id"], bot, chat),
            )
            event_id = int(sent[0]) if sent else None
            include_message = True
    return list(reversed(result)), deps


async def valid_relation(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    ref: dict[str, Any],
    *,
    depth: int = 0,
    read: MemoryRead | None = None,
) -> dict[str, Any] | None:
    """An unresolved pair has separate guards; it is never an active memory fact."""
    if read is None:
        with MemoryRead(conn) as read:
            return await valid_relation(conn, bot, chat, grant, ref, depth=depth, read=read)
    if depth > 8:
        return None
    row = await read.one(
        conn,
        "SELECT from_id,from_version,to_id,to_version,proof,created_at "
        "FROM memory_relations WHERE id=? AND bot_id=? AND chat_id=? "
        "AND access_version=? AND kind='contradicts'",
        (ref["id"], bot, chat, grant),
    )
    if not row or row[5] <= time.time() - RETENTION:
        return None
    if ref.get("from_version", row[1]) != row[1] or ref.get("to_version", row[3]) != row[3]:
        return None
    if not await valid_dependencies(conn, bot, chat, grant, row[4], depth=depth + 1, read=read):
        return None
    endpoints = []
    for fact_id, version in ((row[0], row[1]), (row[2], row[3])):
        fact = await read.one(
            conn,
            "SELECT version,state,text,sender_id,context_dependencies FROM memory_facts "
            "WHERE id=? AND bot_id=? AND chat_id=? AND access_version=?",
            (fact_id, bot, chat, grant),
        )
        if not fact or fact[0] != version or fact[1] not in {"active", "disputed", "proposed"}:
            return None
        if fact[4] is not None and not await valid_dependencies(
            conn, bot, chat, grant, fact[4], depth=depth + 1, read=read
        ):
            return None
        endpoints.append(
            {
                "id": fact_id,
                "version": version,
                "state": fact[1],
                "text": fact[2],
                "sender_id": fact[3],
            }
        )
    return {
        "id": ref["id"],
        "from_version": row[1],
        "to_version": row[3],
        "kind": "contradicts",
        "status": "unresolved_not_established_fact",
        "statements": endpoints,
    }


async def candidates(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    participants: list[str],
    now: float,
    *,
    read: MemoryRead | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if read is None:
        with MemoryRead(conn) as read:
            return await candidates(conn, bot, chat, grant, participants, now, read=read)
    async with conn.execute(
        "SELECT f.id,f.version,f.sender_id,f.category,f.text,f.provenance,f.chat_id,"
        "f.access_version,f.context_dependencies,f.state,f.observation_kind "
        "FROM memory_facts f JOIN telegram_access a ON a.bot_id=f.bot_id AND "
        "a.subject_id=f.chat_id AND a.scope=f.scope AND a.version=f.access_version "
        "WHERE f.bot_id=? AND f.sender_id IN (SELECT value FROM json_each(?)) "
        "AND (f.state='active' OR (f.state='proposed' AND f.chat_id=? "
        "AND f.observation_kind!='none')) AND a.state='approved' "
        "AND a.member_state NOT IN ('left','kicked','migrated') AND (f.chat_id=? OR "
        "(? LIKE '-%' AND EXISTS (SELECT 1 FROM memory_shares s WHERE s.fact_id=f.id "
        "AND s.fact_version=f.version AND s.state='active'))) "
        "ORDER BY (f.chat_id=?) DESC,f.id DESC LIMIT 150",
        (bot, json.dumps(participants), chat, chat, chat, chat),
    ) as cur:
        rows = list(await cur.fetchall())
    facts = []
    for f in rows:
        if f[8] is not None and not await valid_dependencies(
            conn, bot, f[6], f[7], f[8], depth=1, read=read
        ):
            continue
        assessment = (
            await strength(conn, bot, f[0], depth=1, read=read) if f[9] == "proposed" else None
        )
        if assessment and not assessment["eligible"]:
            continue
        facts.append(
            {
                "id": f[0],
                "version": f[1],
                "sender_id": f[2],
                "category": f[3],
                "text": f[4],
                "provenance": f[5],
                "shared": f[6] != chat,
                "tentative": f[9] == "proposed",
                "repetition": assessment["level"] if assessment else None,
                "addressing_allowed": f[10] != "nickname",
            }
        )
    async with conn.execute(
        "SELECT id,summary,dependencies FROM memory_batches WHERE bot_id=? AND chat_id=? "
        "AND access_version=? AND state='completed' AND summary IS NOT NULL "
        "AND created_at>? ORDER BY id DESC LIMIT 200",
        (bot, chat, grant, datetime.fromtimestamp(now - RETENTION, UTC).isoformat()),
    ) as cur:
        rows = list(await cur.fetchall())
    episodes = []
    for r in rows:
        if r[2] is None or await valid_dependencies(
            conn, bot, chat, grant, r[2], depth=1, read=read
        ):
            episodes.append({"id": r[0], "text": r[1]})
    return facts, episodes


def take_facts(rows: list[dict[str, Any]], budget: int = 5000) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    tentative = 0
    for f in rows:
        if len(selected) >= 25 or len(f["text"]) > budget or (f["tentative"] and tentative >= 3):
            continue
        selected.append(f)
        budget -= len(f["text"])
        tentative += int(f["tentative"])
    return selected


async def links(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    facts: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    now: float,
    *,
    read: MemoryRead | None = None,
) -> tuple[list[tuple[str, str, str]], dict[str, set[int]], bool]:
    if read is None:
        with MemoryRead(conn) as read:
            return await links(conn, bot, chat, grant, facts, episodes, now, read=read)
    # Shared facts are terminal: never even load their origin evidence here.
    local = {f["id"] for f in facts if not f["shared"]}
    batches = {e["id"] for e in episodes}
    if not local:
        return [], {}, False
    async with conn.execute(
        "SELECT e.fact_id,e.batch_id,e.context_dependencies,s.id,s.event_id,s.message_id,"
        "s.thread_id FROM memory_evidence e JOIN memory_sources s ON s.id=e.source_id "
        "JOIN memory_facts f ON f.id=e.fact_id WHERE s.bot_id=? AND s.chat_id=? "
        "AND s.access_version=? AND s.valid=1 AND s.received_at>? AND e.event_id=s.event_id "
        "AND e.supporting=1 AND f.bot_id=s.bot_id AND f.chat_id=s.chat_id "
        "AND f.access_version=s.access_version ORDER BY e.source_id DESC,e.fact_id DESC LIMIT 601",
        (bot, chat, grant, now - RETENTION),
    ) as cur:
        rows = list(await cur.fetchall())
    if len(rows) > LIMITS["edges"]:
        return [], {}, True
    edges: list[tuple[str, str, str]] = []
    topics: dict[str, set[int]] = {}
    source_nodes: dict[int, int] = {}
    for fact, batch, deps, source, event, message, thread in rows:
        if fact not in local:
            continue
        original = await accepted(conn, bot, chat, grant, message)
        if not original or original[0] != event:
            continue
        if deps is not None and not await valid_dependencies(
            conn, bot, chat, grant, deps, depth=1, read=read
        ):
            continue
        # A confirmation through a bot reply is context, never independent support.
        parsed = json.loads(deps) if deps else dependencies()
        if parsed["runs"] or any(r["id"] == fact for r in parsed["facts"]):
            continue
        node = f"f:{fact}"
        source_nodes[source] = event
        edges.append((node, f"s:{source}", "support_edge"))
        if batch in batches:
            edges.append((f"s:{source}", f"e:{batch}", "related_episode"))
        if thread is not None:
            topics.setdefault(node, set()).add(thread)
            if batch in batches:
                topics.setdefault(f"e:{batch}", set()).add(thread)
    # Dependency edges are context connections, never independent support.
    for table, ids, prefix, column in (
        ("memory_facts", local, "f", "context_dependencies"),
        ("memory_batches", batches, "e", "dependencies"),
    ):
        if not ids:
            continue
        async with conn.execute(
            f"SELECT id,{column} FROM {table} WHERE bot_id=? AND chat_id=? "
            "AND access_version=? AND id IN (" + ",".join("?" for _ in ids) + ")",
            (bot, chat, grant, *sorted(ids)),
        ) as cur:
            derived = list(await cur.fetchall())
        for row_id, raw in derived:
            if raw is None:
                continue
            value = json.loads(raw)
            node = f"{prefix}:{row_id}"
            for ref in value["facts"]:
                if ref["id"] in local and node != f"f:{ref['id']}":
                    edges.append((node, f"f:{ref['id']}", "dependency_edge"))
            if prefix == "e":
                for ref in value["sources"]:
                    if source_nodes.get(ref["id"]) == ref["event_id"]:
                        edges.append((node, f"s:{ref['id']}", "related_episode"))
    edges = list(dict.fromkeys(edges))
    return edges[: LIMITS["edges"]], topics, len(edges) > LIMITS["edges"]


def rank(
    facts: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    query: str,
    edges: list[tuple[str, str, str]],
    topics: dict[str, set[int]],
    speaker: str,
    thread: int | None,
    reply_sources: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    words = terms(query)
    nodes = {**{f"f:{f['id']}": f for f in facts}, **{f"e:{e['id']}": e for e in episodes}}
    scores = {k: 10 * len(words & terms(v["text"])) for k, v in nodes.items()}
    reasons = {k: ["query_match"] if scores[k] else ["recent"] for k in nodes}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for a, b, why in edges:
        adjacency.setdefault(a, []).append((b, why))
        adjacency.setdefault(b, []).append((a, why))
    # Only relevant seeds can promote neighbours; evidence quantity adds no confidence.
    reply_seeds = [f"s:{i}" for i in sorted(reply_sources) if f"s:{i}" in adjacency]
    seeds = (reply_seeds + sorted((k for k in nodes if scores[k]), key=lambda k: (-scores[k], k)))[
        :8
    ]
    boosts: dict[str, str] = {}
    for seed in seeds:
        seen = {seed}
        frontier = [seed]
        for _ in range(2):
            following = []
            for current in frontier:
                for neighbour, why in sorted(adjacency.get(current, []))[:3]:
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    following.append(neighbour)
                    if neighbour in nodes:
                        boosts[neighbour] = "accepted_reply" if seed in reply_seeds else why
            frontier = following
    for node, row in nodes.items():
        if node in boosts:
            scores[node] += 12 if boosts[node] == "accepted_reply" else 4
            reasons[node].append(boosts[node])
        if thread is not None and thread in topics.get(node, set()):
            scores[node] += 2
            reasons[node].append("same_topic")
        if row.get("sender_id") == speaker:
            scores[node] += 1
            reasons[node].append("speaker")
    ordered_facts = sorted(facts, key=lambda f: (-scores[f"f:{f['id']}"], f["shared"], -f["id"]))
    # Preserve one fresh episode; four remaining slots compete for relevance.
    ranked = sorted(episodes, key=lambda e: (-scores[f"e:{e['id']}"], -e["id"]))
    matched = [e for e in ranked if e not in episodes[:1] and scores[f"e:{e['id']}"] > 0][:4]
    chosen = episodes[:1] + matched if matched else episodes[:2]
    selected_episodes, used = [], 0
    for e in chosen:
        if used + len(e["text"]) <= 5000:
            selected_episodes.append(e)
            used += len(e["text"])
    return ordered_facts, selected_episodes, reasons


def describe_selection(value: dict[str, Any], reasons: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "facts": [
            {
                "id": f["id"],
                "version": f["version"],
                "reasons": reasons.get(f"f:{f['id']}", ["recent"]),
            }
            for f in value["facts"]
        ],
        "episodes": [
            {"id": e["id"], "reasons": reasons.get(f"e:{e['id']}", ["recent"])}
            for e in value["summaries"]
        ],
        "conflicts": [c["id"] for c in value.get("conflicts", [])],
        "reply_messages": [m["message_id"] for m in value.get("conversation", [])],
        "chars": sum(len(f["text"]) for f in value["facts"])
        + sum(len(e["text"]) for e in value["summaries"])
        + sum(len(s["text"]) for c in value.get("conflicts", []) for s in c["statements"])
        + sum(len(m["text"]) for m in value.get("conversation", [])),
    }


async def select_context(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    participants: list[str],
    query: str,
    mode: str,
    epoch: int,
    diagnostics: dict[str, Any] | None,
    *,
    current_query: str,
    recent: list[dict[str, Any]],
    event_id: int | None,
    speaker: str,
    thread: int | None,
    read: MemoryRead | None = None,
) -> dict[str, Any]:
    if read is None:
        with MemoryRead(conn) as read:
            return await select_context(
                conn,
                bot,
                chat,
                grant,
                participants,
                query,
                mode,
                epoch,
                diagnostics,
                current_query=current_query,
                recent=recent,
                event_id=event_id,
                speaker=speaker,
                thread=thread,
                read=read,
            )
    now = time.time()
    started = time.perf_counter()
    facts, episodes = await candidates(conn, bot, chat, grant, participants, now, read=read)
    loaded = time.perf_counter()
    baseline: dict[str, Any] = {
        "facts": take_facts(facts),
        "summaries": select_episodes(episodes, query),
    }
    selected = time.perf_counter()
    diag: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "mode": mode,
        "epoch": epoch,
        "at": now,
        "limits": LIMITS.copy(),
        "candidates": {"facts": len(facts), "episodes": len(episodes)},
        "load_ms": round((loaded - started) * 1000, 2),
        "legacy_ms": round((selected - loaded) * 1000, 2),
        "legacy": describe_selection(baseline, {}),
        "fallback": None,
        "capped": False,
    }
    graph = baseline
    if mode != "legacy":
        try:
            chain, deps = await conversation_chain(conn, bot, chat, grant, event_id, read=read)
            # Short continuations inherit a bounded same-topic conversation seed.
            seed = current_query[:4000] + " " + " ".join(m["text"] for m in chain)[-4000:]
            if len(terms(current_query)) < 6:
                same_topic = [m for m in recent if thread is None or m.get("thread_id") == thread]
                seed += " " + " ".join(m["text"] for m in same_topic[-4:])[-2000:]
            edges, topics, capped = await links(
                conn, bot, chat, grant, facts, episodes, now, read=read
            )
            diag["capped"] = capped
            if capped:
                raise ValueError("edge_budget")
            ranked, summaries, reasons = rank(
                facts,
                episodes,
                seed,
                edges,
                topics,
                speaker,
                thread,
                {r["id"] for r in deps["sources"]},
            )
            async with conn.execute(
                "SELECT id FROM memory_relations WHERE bot_id=? AND chat_id=? "
                "AND access_version=? ORDER BY id DESC LIMIT 40",
                (bot, chat, grant),
            ) as cur:
                relations = list(await cur.fetchall())
            conflicts = []
            for (relation_id,) in relations:
                pair = await valid_relation(conn, bot, chat, grant, {"id": relation_id}, read=read)
                if pair and all(s["sender_id"] in participants for s in pair["statements"]):
                    score = len(
                        terms(seed) & terms(" ".join(s["text"] for s in pair["statements"]))
                    )
                    if score:
                        conflicts.append((score, pair))
            pairs = [p for _, p in sorted(conflicts, key=lambda v: (-v[0], -v[1]["id"]))[:2]]
            conflict_chars = sum(len(s["text"]) for p in pairs for s in p["statements"])
            graph = {"facts": take_facts(ranked, 5000 - conflict_chars), "summaries": summaries}
            if pairs:
                graph["conflicts"] = pairs
            if chain:
                graph["conversation"] = chain
                graph["conversation_dependencies"] = deps
            diag["edges"] = len(edges)
            diag["graph"] = describe_selection(graph, reasons)
        except Exception:
            # No partially validated graph reaches the model. Never retry a paid operation.
            graph = baseline
            diag["fallback"] = "graph_unavailable" if not diag["capped"] else "edge_budget"
        diag["graph_ms"] = round((time.perf_counter() - selected) * 1000, 2)
    if diagnostics is not None:
        diagnostics.update(diag)
    return graph if mode == "graph" else baseline
