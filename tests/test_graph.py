import io
import json
import time
import zipfile

import pytest
from test_app import running, setup
from test_dialogue import FakeModel
from test_memory import extract, mutation, ready
from test_telegram import event, store_at

from zarya.graph import GraphRepository, GraphUnavailable, identity, vault_zip
from zarya.memory import MemoryEngine


async def populated(store, path):
    await ready(store)
    await store.ingest("999", "zarya_test", [event(4, -100, "Я люблю Python")])
    engine = MemoryEngine(store, FakeModel(), path)
    await extract(engine)
    return engine


async def test_graph_snapshot_relations_identity_no_mutation(tmp_path):
    async with store_at(tmp_path) as store:
        await populated(store, tmp_path)
        graph = GraphRepository(store.db.path)
        before = await store.db.one("SELECT count(*) FROM model_calls")
        first = await graph.snapshot("999", "-100")
        second = await graph.snapshot("999", "-100")
        assert first["nodes"] == second["nodes"]
        assert first["edges"] == second["edges"]
        assert {n["kind"] for n in first["nodes"]} == {
            "chat",
            "profile",
            "fact",
            "episode",
            "source",
        }
        ids = {n["id"] for n in first["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in first["edges"])
        assert any(e["type"] == "supports" for e in first["edges"])
        assert await store.db.one("SELECT count(*) FROM model_calls") == before
        assert not any(n["kind"] == "fact" for n in (await graph.snapshot("999", "42"))["nodes"])
        assert await graph.snapshot("888", "-100") is None
        assert await graph.snapshot("999", "-300") is None
        assert identity("999", "42", "profile", "42") != identity("999", "-100", "profile", "42")


async def test_delete_and_revoke_absent_in_new_export(tmp_path):
    async with store_at(tmp_path) as store:
        engine = await populated(store, tmp_path)
        graph = GraphRepository(store.db.path)
        fact = (await engine.profile("999", "-100"))["facts"][0]
        await engine.mutate("999", fact["id"], mutation(fact, "delete"))
        result = await graph.snapshot("999", "-100")
        assert not any(n["kind"] in {"fact", "episode", "source"} for n in result["nodes"])
        async with store.db.transaction() as c:
            await c.execute("UPDATE telegram_access SET state='revoked' WHERE subject_id='-100'")
        assert await graph.snapshot("999", "-100") is None


@pytest.mark.parametrize("change", ["revision", "expired", "reply_dependency", "grant"])
async def test_evidence_requires_current_witness_and_dependencies(tmp_path, change):
    async with store_at(tmp_path) as store:
        await populated(store, tmp_path)
        async with store.db.transaction() as c:
            if change == "revision":
                await c.execute("UPDATE memory_evidence SET event_id=99999")
            elif change == "expired":
                await c.execute(
                    "UPDATE memory_sources SET received_at=?", (time.time() - 91 * 86400,)
                )
            elif change == "grant":
                await c.execute("UPDATE memory_sources SET access_version=99")
            else:
                deps = {
                    "version": 1,
                    "sources": [],
                    "facts": [],
                    "runs": [],
                    "replies": [{"message_id": 99999, "event_id": 99999}],
                }
                await c.execute(
                    "UPDATE memory_evidence SET context_dependencies=?", (json.dumps(deps),)
                )
        result = await GraphRepository(store.db.path).snapshot("999", "-100")
        assert not any(e["type"] in {"supports", "evidence_for"} for e in result["edges"])


async def test_coverage_limits_and_safe_markdown(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        await populated(store, tmp_path)
        monkeypatch.setitem(__import__("zarya.graph", fromlist=["LIMITS"]).LIMITS, "episodes", 0)
        async with store.db.transaction() as c:
            await c.execute(
                "UPDATE memory_facts SET text=?",
                ("![](https://example.org/tracker) [[evil]] <script>x</script>",),
            )
        result = await GraphRepository(store.db.path).snapshot("999", "-100")
        assert result["coverage"]["truncated"]["episodes"]
        archive = zipfile.ZipFile(io.BytesIO(vault_zip(result)))
        assert "Начало.md" in archive.namelist()
        assert all(
            not name.startswith(("/", ".obsidian")) and ".." not in name
            for name in archive.namelist()
        )
        notes = "\n".join(archive.read(n).decode() for n in archive.namelist() if n.endswith(".md"))
        assert "![](https://" not in notes and "[[evil]]" not in notes and "<script>" not in notes


async def test_graph_api_requires_admin(tmp_path):
    async with running(tmp_path) as (app, client):
        for suffix in ("", "/vault"):
            assert (await client.get("/api/memory/chats/-100/graph" + suffix)).status_code == 401
        await setup(client, tmp_path)
        app.state.telegram.bot = {"id": "999"}
        await populated(app.state.telegram.store, tmp_path)
        result = await client.get("/api/memory/chats/-100/graph")
        assert result.status_code == 200 and result.json()["schema_version"] == 1
        archive = await client.get("/api/memory/chats/-100/graph/vault")
        assert archive.status_code == 200 and archive.headers["cache-control"] == "no-store"
        assert zipfile.is_zipfile(io.BytesIO(archive.content))
        assert (await client.get("/api/memory/chats/-300/graph")).status_code == 404


async def test_concurrent_export_rejected(tmp_path):
    async with store_at(tmp_path) as store:
        graph = GraphRepository(store.db.path)
        async with graph.capacity:
            with pytest.raises(GraphUnavailable):
                await graph.snapshot("999", "-100")


async def test_readonly_legacy_reply_does_not_repair_live_database(tmp_path):
    async with store_at(tmp_path) as store:
        await populated(store, tmp_path)
        await store.ingest("999", "zarya_test", [event(5, -100, "Обсуждение")])
        async with store.db.transaction() as c:
            async with c.execute("SELECT id FROM events WHERE update_id=5") as cursor:
                event_id = (await cursor.fetchone())[0]
            deps = {
                "version": 1,
                "sources": [],
                "facts": [],
                "runs": [],
                "replies": [{"message_id": 5, "event_id": event_id}],
            }
            await c.execute("UPDATE memory_facts SET context_dependencies=?", (json.dumps(deps),))
            await c.execute("DELETE FROM research_messages WHERE message_id=5")
        result = await GraphRepository(store.db.path).snapshot("999", "-100")
        assert any(n["kind"] == "fact" for n in result["nodes"])
        assert await store.db.one("SELECT count(*) FROM research_messages WHERE message_id=5") == (
            0,
        )
