"""Owner-only, bounded read projection. Never used as dialogue instructions."""

import argparse
import asyncio
import html
import io
import json
import re
import sqlite3
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import aiosqlite

from zarya.memory_context import RETENTION, valid_dependencies

LIMITS = {
    "facts": 200, "episodes": 50, "sources": 1000,
    "participant_summaries": 50, "participant_sources": 250,
}
MAX_TEXT = 2_000_000


class GraphUnavailable(Exception):
    pass


def identity(bot: str, chat: str, kind: str, key: object) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps(["zarya.graph.v1", bot, chat, kind, key])))


class GraphRepository:
    def __init__(self, path: Path):
        self.path = path
        self.capacity = asyncio.Semaphore(1)

    async def snapshot(self, bot: str, chat: str) -> dict[str, Any] | None:
        if self.capacity.locked():
            raise GraphUnavailable("Экспорт уже выполняется. Повтори запрос позже.")
        async with self.capacity:
            async with aiosqlite.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as c:
                c.row_factory = aiosqlite.Row
                await c.execute("PRAGMA query_only=ON")
                deadline = time.monotonic() + 5
                await c.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
                await c.execute("BEGIN")
                try:
                    async with asyncio.timeout(6):
                        return await self._read(c, bot, chat)
                except (sqlite3.OperationalError, TimeoutError) as exc:
                    raise GraphUnavailable(
                        "Граф временно недоступен. Повтори запрос позже."
                    ) from exc
                finally:
                    await c.set_progress_handler(lambda: 0, 0)
                    await c.rollback()

    async def _read(self, c: aiosqlite.Connection, bot: str, chat: str) -> dict[str, Any] | None:
        async def rows(sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
            async with c.execute(sql, args) as cursor:
                return list(await cursor.fetchall())

        access = await rows(
            "SELECT scope,title,version FROM telegram_access WHERE bot_id=? AND subject_id=? "
            "AND state='approved' AND member_state NOT IN ('left','kicked','migrated')",
            (bot, chat),
        )
        if not access:
            return None
        scope, title, grant = access[0]
        cutoff = time.time() - RETENTION
        epoch = await rows("SELECT version FROM memory_epochs WHERE bot_id=?", (bot,))
        nodes: dict[str, dict[str, Any]] = {}
        edges: set[tuple[str, str, str]] = set()
        coverage: dict[str, Any] = {"limits": LIMITS.copy(), "truncated": {}, "excluded": {}}
        text_size = 0

        def link(a: str, relation: str, b: str) -> None:
            edges.add((a, relation, b))
            if len(edges) > 10000:
                raise GraphUnavailable("Снимок превышает лимит 10000 связей.")

        def node(kind: str, key: object, label: str, text: str = "", **meta: Any) -> str:
            nonlocal text_size
            uid = identity(bot, chat, kind, key)
            if uid not in nodes:
                text_size += len(text.encode("utf-8")) + len(label.encode("utf-8"))
                if text_size > MAX_TEXT:
                    raise GraphUnavailable("Снимок превышает лимит текста. Нужен меньший срез.")
                nodes[uid] = {
                    "id": uid,
                    "kind": kind,
                    "key": key,
                    "label": label,
                    "text": text,
                    **meta,
                }
            return uid

        chat_node = node("chat", chat, title, scope=scope, version=grant)

        def person(sender: str) -> str:
            uid = node("profile", sender, f"Участник {sender}")
            link(uid, "belongs_to", chat_node)
            return uid

        async def bounded(kind: str, sql: str, args: tuple[Any, ...]) -> list[Any]:
            found = await rows(sql + " LIMIT ?", (*args, LIMITS[kind] + 1))
            coverage["truncated"][kind] = len(found) > LIMITS[kind]
            coverage["excluded"][kind] = 0
            return found[: LIMITS[kind]]

        sources = await bounded(
            "sources",
            "SELECT s.* FROM memory_sources s JOIN events e ON e.id=s.event_id "
            "JOIN research_messages r ON r.bot_id=s.bot_id AND r.chat_id=s.chat_id "
            "AND r.message_id=s.message_id AND r.event_id=s.event_id "
            "AND r.access_version=s.access_version WHERE s.bot_id=? AND s.chat_id=? "
            "AND s.scope=? AND s.access_version=? AND s.valid=1 AND s.received_at>? "
            "AND e.bot_id=s.bot_id AND e.chat_id=s.chat_id AND e.payload!='{}' "
            "AND e.disposition='accepted' ORDER BY s.id DESC",
            (bot, chat, scope, grant, cutoff),
        )
        source_map = {s["id"]: s for s in sources}

        def source(ref: dict[str, Any]) -> str | None:
            s = source_map.get(ref.get("id"))
            if s is None or s["event_id"] != ref.get("event_id"):
                return None
            uid = node(
                "source",
                s["id"],
                f"Сообщение {s['message_id']}",
                s["text"],
                version=s["event_id"],
                message_id=s["message_id"],
                thread_id=s["thread_id"],
                received_at=s["received_at"],
            )
            link(uid, "authored_by", person(s["sender_id"]))
            return uid

        async def valid(value: Any) -> bool:
            return value is None or await valid_dependencies(c, bot, chat, grant, value)

        facts = await bounded(
            "facts",
            "SELECT * FROM memory_facts WHERE bot_id=? AND chat_id=? AND scope=? "
            "AND access_version=? AND state IN ('active','proposed','disputed') "
            "ORDER BY id DESC",
            (bot, chat, scope, grant),
        )
        fact_map: dict[int, str] = {}
        for f in facts:
            if (f["state"] == "active" and f["reviewed_at"] <= cutoff) or not await valid(
                f["context_dependencies"]
            ):
                coverage["excluded"]["facts"] += 1
                continue
            uid = node(
                "fact",
                f["id"],
                f"Утверждение {f['id']}",
                f["text"],
                version=f["version"],
                state=f["state"],
                category=f["category"],
                provenance=f["provenance"],
                curated=bool(f["curated"]),
                observation_kind=f["observation_kind"],
            )
            fact_map[f["id"]] = uid
            link(uid, "about", person(f["sender_id"]))
            evidence = await rows(
                "SELECT * FROM memory_evidence WHERE fact_id=? ORDER BY source_id DESC LIMIT 1001",
                (f["id"],),
            )
            if len(evidence) > 1000:
                raise GraphUnavailable("Слишком много подтверждений для одного утверждения.")
            for e in evidence:
                if not await valid(e["context_dependencies"]):
                    continue
                witness = source({"id": e["source_id"], "event_id": e["event_id"]})
                if witness:
                    link(witness, "supports" if e["supporting"] else "evidence_for", uid)

        batches = await bounded(
            "episodes",
            "SELECT * FROM memory_batches WHERE bot_id=? AND chat_id=? AND scope=? "
            "AND access_version=? AND state='completed' AND summary IS NOT NULL "
            "AND created_at>? ORDER BY id DESC",
            (bot, chat, scope, grant, datetime.fromtimestamp(cutoff, UTC).isoformat()),
        )
        from zarya.memory_retrieval import valid_relation

        relations = await rows(
            "SELECT id,from_id,to_id FROM memory_relations WHERE bot_id=? AND chat_id=? "
            "AND access_version=? ORDER BY id DESC LIMIT 201",
            (bot, chat, grant),
        )
        if len(relations) > 200:
            raise GraphUnavailable("Слишком много явных отношений утверждений.")
        for relation in relations:
            if (
                relation["from_id"] in fact_map
                and relation["to_id"] in fact_map
                and await valid_relation(c, bot, chat, grant, {"id": relation["id"]})
            ):
                link(fact_map[relation["from_id"]], "contradicts", fact_map[relation["to_id"]])
        for b in batches:
            if not await valid(b["dependencies"]):
                coverage["excluded"]["episodes"] += 1
                continue
            manifest = json.loads(b["manifest"])
            if not isinstance(manifest, list) or any(
                not isinstance(ref, dict)
                or ref.get("id") not in source_map
                or source_map[ref["id"]]["event_id"] != ref.get("event_id")
                for ref in manifest
            ):
                coverage["excluded"]["episodes"] += 1
                continue
            uid = node(
                "episode",
                b["id"],
                f"Эпизод {b['id']}",
                b["summary"],
                version=b["id"],
                created_at=b["created_at"],
            )
            link(uid, "belongs_to", chat_node)
            for ref in manifest:
                witness = source(ref)
                if witness:
                    link(uid, "derived_from", witness)
            if b["dependencies"]:
                for ref in json.loads(b["dependencies"])["facts"]:
                    if ref["id"] in fact_map:
                        link(uid, "uses_fact", fact_map[ref["id"]])
        from zarya.participant_graph import projection

        summaries = await bounded(
            "participant_summaries",
            "SELECT id FROM participant_summary_blocks WHERE bot_id=? AND chat_id=? "
            "AND access_version=? AND state='completed' AND result IS NOT NULL ORDER BY id DESC",
            (bot, chat, grant),
        )
        coverage["included"] = {"participant_summaries": 0, "participant_sources": 0}
        coverage["truncated"]["participant_sources"] = False
        coverage["excluded"]["participant_sources"] = 0
        events: set[int] = set()
        for summary in summaries:
            block = await projection(c, bot, chat, grant, summary["id"])
            if not block:
                coverage["excluded"]["participant_summaries"] += 1
                continue
            added = {s["event_id"] for s in block["sources"]} - events
            if len(events | added) > LIMITS["participant_sources"]:
                coverage["excluded"]["participant_summaries"] += 1
                coverage["excluded"]["participant_sources"] += len(added)
                coverage["truncated"]["participant_sources"] = True
                continue
            events.update(added)
            uid = node(
                "participant_summary", block["id"], f"Персональная сводка {block['id']}",
                block["result"], version=block["id"], result_hash=block["result_hash"],
                sender_id=block["sender_id"], source_count=len(block["sources"]),
                created_at=block["created_at"],
                first_received_at=min(s["received_at"] for s in block["sources"]),
                last_received_at=max(s["received_at"] for s in block["sources"]),
                evidence_kind="compressed_conversation", read_only=True,
            )
            link(uid, "belongs_to", chat_node)
            link(uid, "about", person(block["sender_id"]))
            for s in block["sources"]:
                witness = node(
                    "participant_source", s["event_id"], f"Реплика {s['message_id']}", s["text"],
                    version=s["event_id"], message_id=s["message_id"], thread_id=s["thread_id"],
                    received_at=s["received_at"], forwarded=s["forwarded"],
                    attachment_kinds=s["attachment_kinds"], urls=s["urls"],
                    text_hash=s["text_hash"],
                )
                link(uid, "derived_from", witness)
                link(witness, "sent_by", person(s["sender_id"]))
                link(witness, "belongs_to", chat_node)
            coverage["included"]["participant_summaries"] += 1
        coverage["included"]["participant_sources"] = len(events)
        return {
            "schema_version": 1,
            "mode": "read_only_snapshot",
            "generated_at": datetime.now(UTC).isoformat(),
            "bot_id": bot,
            "chat_id": chat,
            "scope": scope,
            "access_version": grant,
            "memory_epoch": epoch[0][0] if epoch else 1,
            "coverage": coverage,
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": [{"source": a, "type": rel, "target": b} for a, rel, b in sorted(edges)],
        }


def plain(text: str) -> str:
    # Imported chat text must not become remote images, links, HTML or wikilinks.
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(text))


def vault_files(snapshot: dict[str, Any]) -> dict[str, str]:
    files = {"graph.json": json.dumps(snapshot, ensure_ascii=False, indent=2)}
    kinds = {
        "chat": "Чат",
        "profile": "Участник",
        "fact": "Утверждение",
        "source": "Сообщение",
        "episode": "Эпизод",
        "participant_summary": "Персональная сводка",
        "participant_source": "Реплика",
    }
    names = {n["id"]: f"{kinds[n['kind']]} {n['id']}" for n in snapshot["nodes"]}
    links: dict[str, list[str]] = {n["id"]: [] for n in snapshot["nodes"]}
    for edge in snapshot["edges"]:
        links[edge["source"]].append(f"- {edge['type']}: [[{names[edge['target']]}]]")
    for n in snapshot["nodes"]:
        metadata = {k: v for k, v in n.items() if k not in {"text", "label"}}
        metadata.update(
            schema_version=1,
            bot_id=snapshot["bot_id"],
            chat_id=snapshot["chat_id"],
            access_version=snapshot["access_version"],
            read_only=True,
        )
        front = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in metadata.items())
        note = ""
        if n["kind"] == "participant_summary":
            note = "Автоматическая сводка пяти реплик, только для чтения. "
            note += "Пересказ беседы, не подтверждённый факт о человеке.\n\n"
        files[f"nodes/{names[n['id']]}.md"] = (
            f"---\n{front}\n---\n\n# {plain(n['label'])}\n\n{note}{plain(n['text'])}\n\n"
            + "\n".join(links[n["id"]])
            + "\n"
        )
    files["Начало.md"] = (
        "# Граф памяти Зари\n\nПриватный снимок для владельца, не полная история переписки.\n"
        "Открой эту папку как хранилище Obsidian, затем «Граф».\n\n"
        "Редактирование файлов пока не меняет память бота. Новая выгрузка отражает удаления; "
        "этот архив, его распакованная копия, корзина и резервные копии "
        "автоматически не очищаются.\n\n"
        "Активное утверждение не означает доказанную истину. proposed — предположение; "
        "disputed — оспаривается. Подробности покрытия и типы связей — в graph.json.\n\n"
        f"Дата снимка: {snapshot['generated_at']}\n\n"
        "Персональные сводки — автоматический пересказ пяти исходных реплик; "
        "они и их источники доступны только для чтения, остаются в своём чате.\n\n"
        "Пределы: 200 утверждений, 50 эпизодов, 1000 кандидатов источников памяти, "
        "50 персональных сводок и 250 исходных реплик этих сводок. "
        "В граф входят только источники выбранных записей.\n\n"
        f"Покрытие: `{json.dumps(snapshot['coverage'], ensure_ascii=False)}`\n\n"
        + "\n".join(f"- [[{names[n['id']]}]]" for n in snapshot["nodes"] if n["kind"] == "chat")
    )
    return files


def vault_zip(snapshot: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in vault_files(snapshot).items():
            archive.writestr(name, text.encode("utf-8"))
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Приватный read-only снимок памяти для Obsidian")
    parser.add_argument("--database", type=Path, default=Path("data/zarya.sqlite3"))
    parser.add_argument("--bot", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Новый приватный каталог")
    args = parser.parse_args()
    snapshot = asyncio.run(GraphRepository(args.database).snapshot(args.bot, args.chat))
    if snapshot is None:
        parser.error("Чат недоступен")
    files = vault_files(snapshot)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = args.output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(
        f"Создан приватный снимок: {len(snapshot['nodes'])} узлов, {len(snapshot['edges'])} связей"
    )


if __name__ == "__main__":
    main()
