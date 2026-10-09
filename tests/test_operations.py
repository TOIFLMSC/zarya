import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_app import running, setup
from test_telegram import store_at

from zarya.operations import Operations


async def seed(db):
    now = datetime.now(UTC).isoformat()
    async with db.transaction() as c:
        for n, chat, mode in [(1, "42", "live"), (2, "-100", "paid"), (3, "-100", "recorded")]:
            await c.execute(
                "INSERT INTO "
                "dialogue_runs(id,bot_id,chat_id,sender_id,message_id,trigger,state,s"
                "napshot,response,created_at,mode) VALUES "
                "(?,'999',?,'42',1,'private','completed','{}','PRIVATE_RESPONSE',?,?)",
                (n, chat, now, mode),
            )
        for run, cost, state, latency in [(1, 0.02, "completed", 100), (2, None, "unknown", None)]:
            await c.execute(
                "INSERT INTO "
                "model_calls(provider,model,state,created_at,run_id,cost_usd,latency_"
                "ms,request_json) VALUES ('openai','luna',?,?,?,?,?,'PRIVATE_PROMPT')",
                (state, now, run, cost, latency),
            )
        await c.execute(
            "INSERT INTO "
            "research_runs(id,job_id,bot_id,chat_id,scope,access_version,state,mo"
            "de,question,material,manifest,sources,settings_version,model,reasoni"
            "ng,created_at) VALUES "
            "(1,NULL,'999','-100','group',1,'completed','search','SECRET','','[]'"
            ",'[]',1,'luna','low',?)",
            (now,),
        )
        await c.execute(
            "INSERT INTO "
            "model_calls(provider,model,state,created_at,research_run_id,cost_usd"
            ",search_cost_usd,latency_ms) VALUES "
            "('openai','luna','completed',?,1,.03,.01,300)",
            (now,),
        )
        # Unattributed legacy call outside the period must not contaminate the totals.
        await c.execute(
            "INSERT INTO model_calls(provider,model,state,created_at,cost_usd) "
            "VALUES ('openai','old','completed',?,99)",
            ((datetime.now(UTC) - timedelta(days=100)).isoformat(),),
        )


async def test_costs_scope_replay_unknown_and_no_private_content(tmp_path):
    async with store_at(tmp_path) as store:
        await seed(store.db)
        ops = Operations(store.db)
        data = await ops.overview(bot="999")
        assert data["summary"]["calls"] == 3
        assert data["summary"]["known_cost_usd"] == pytest.approx(0.06)
        assert data["summary"]["unknown_cost_calls"] == 1
        assert data["summary"]["unknown_outcomes"] == 1
        assert data["summary"]["p95_latency_ms"] == 300
        assert data["summary"]["latency_samples"] == 2
        assert "PRIVATE" not in json.dumps(data) and "SECRET" not in json.dumps(data)
        assert {r["label"] for r in data["breakdown"]["task"]} == {"dialogue", "research", "replay"}
        group = await ops.overview(bot="999", scope="group", chat="-100", problems=True)
        assert group["summary"]["known_cost_usd"] == pytest.approx(0.04)
        assert group["total"] == 1 and group["calls"][0]["task"] == "replay"
        assert (await ops.overview(bot="other"))["summary"]["calls"] == 0


async def test_api_auth_filters_review_conflict_and_persistence(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/operations")).status_code == 401
        await setup(client, tmp_path)
        assert (await client.get("/api/operations?days=30")).status_code == 200
        assert (await client.get("/api/operations?days=8")).status_code == 422
        preference = {"expected_version": 1, "warning_usd": 2.5}
        assert (await client.put("/api/operations/preferences", json=preference)).status_code == 200
        assert (await client.put("/api/operations/preferences", json=preference)).status_code == 409
        assert (
            await client.put(
                "/api/operations/preferences",
                json={
                    "expected_version": 2,
                    "warning_usd": -1,
                },
            )
        ).status_code == 422
        assert (await client.get("/api/operations")).json()["preferences"]["warning_usd"] == 2.5
        body = {"expected_version": 0, "status": "passed", "note": "Синтетический тест"}
        assert (
            await client.put("/api/operations/reviews/conversation", json=body)
        ).status_code == 200
        assert (
            await client.put("/api/operations/reviews/conversation", json=body)
        ).status_code == 409
        assert (await client.put("/api/operations/reviews/invalid", json=body)).status_code == 404
        assert (
            await client.put("/api/operations/reviews/photo", json={**body, "status": "madeup"})
        ).status_code == 422
        client.headers.pop("X-CSRF-Token")
        assert (await client.put("/api/operations/reviews/photo", json=body)).status_code == 403
        assert (await client.get("/api/operations")).json()["reviews"][0]["status"] == "passed"
    async with running(tmp_path) as (app, _):
        assert (await app.state.db.one("SELECT note FROM pilot_reviews"))[0] == "Синтетический тест"
        assert (await app.state.db.one("SELECT warning_usd FROM pilot_preferences"))[0] == 2.5


async def test_cancelled_begin_releases_snapshot(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        execute = store.db.reader.execute

        async def cancelled_begin(sql, *args):
            await execute(sql, *args)
            raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            patch.setattr(store.db.reader, "execute", cancelled_begin)
            with pytest.raises(asyncio.CancelledError):
                await Operations(store.db).overview()
        assert not store.db.reader.in_transaction
        assert (await Operations(store.db).overview())["summary"]["calls"] == 0


async def test_media_costs_pagination_and_terminal_queues(tmp_path):
    from test_backup import source_at

    from zarya.database import Database

    path = await source_at(tmp_path / "source")
    db = Database(path / "zarya.sqlite3")
    await db.open()
    try:
        now = datetime.now(UTC).isoformat()
        async with db.transaction() as c:
            await c.execute("UPDATE photo_batches SET state='cache'")
            await c.execute(
                "INSERT INTO memory_batches(id,bot_id,chat_id,scope,access_version,epoch,"
                "manifest,state,created_at,settings_version) "
                "VALUES (1,'999','42','private',1,1,'[]','completed',?,1)",
                (now,),
            )
            for n, state in [(1, "unavailable"), (2, "queued")]:
                await c.execute(
                    "INSERT INTO media_runs(id,bot_id,chat_id,scope,access_version,message_id,"
                    "event_id,kind,file_id,state,processing_version,asr_model,model,"
                    "settings_version,limits,created_at) "
                    "VALUES (?,'999','42','private',1,1,?,'video','f',?,'1','asr','luna',1,'{}',?)",
                    (n, n, state, "2026-01-01T00:00:00+00:00"),
                )
            for operation, cost in [("asr", 0.01), ("vision", 0.02)]:
                await c.execute(
                    "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,"
                    "operation,cost_usd) VALUES ('openai','luna','completed',?,1,?,?)",
                    (now, operation, cost),
                )
            for column in ("photo_batch_id", "memory_batch_id"):
                await c.execute(
                    f"INSERT INTO model_calls(provider,model,state,created_at,{column},cost_usd) "
                    "VALUES ('openai','luna','completed',?,1,.04)",
                    (now,),
                )
            for _ in range(22):
                await c.execute(
                    "INSERT INTO model_calls(provider,model,state,created_at,cost_usd) "
                    "VALUES ('openai','legacy','completed',?,.01)",
                    (now,),
                )
        ops = Operations(db)
        data = await ops.overview(bot="999", scope="private")
        assert data["summary"]["calls"] == 4
        assert data["summary"]["known_cost_usd"] == pytest.approx(0.11)
        assert data["summary"]["avg_latency_ms"] is None
        assert data["summary"]["p95_latency_ms"] is None
        assert {r["label"] for r in data["breakdown"]["task"]} == {
            "asr",
            "video",
            "photo",
            "memory",
        }
        assert [(r["queue"], r["state"], r["count"]) for r in data["queues"]] == [
            ("media", "queued", 1),
        ]  # Queue is current even though its item was created outside the selected period.
        assert not (await ops.overview(scope="group"))["queues"]
        all_calls = await ops.overview()
        next_page = await ops.overview(page=1)
        assert all_calls["total"] == 26 and all_calls["pages"] == 2
        assert len(all_calls["calls"]) == 20 and len(next_page["calls"]) == 6
        assert not {c["id"] for c in all_calls["calls"]} & {c["id"] for c in next_page["calls"]}
        assert (await ops.overview(chat="missing"))["warning_summary"]["calls"] == 26
    finally:
        await db.close()


async def test_pilot_burst_200_updates_survives_restart_without_duplicates(tmp_path):
    from test_telegram import approve, event

    async with store_at(tmp_path) as store:
        await store.ingest("999", "zarya_test", [event(1)])
        await approve(store)
        batch = [event(i, text="Обычный вопрос") for i in range(2, 202)]
        await store.ingest("999", "zarya_test", batch)
        await store.ingest("999", "zarya_test", batch)
        assert (await store.db.one("SELECT COUNT(*) FROM jobs"))[0] == 200
    async with store_at(tmp_path) as store:
        data = await Operations(store.db).overview(bot="999")
        assert next(q for q in data["queues"] if q["queue"] == "jobs")["count"] == 200
        for _ in range(200):
            await store.process_one("999")
        assert (await store.db.one("SELECT COUNT(*) FROM jobs WHERE state='done'"))[0] == 200
        # This isolated store has no dialogue engine: it never makes paid calls or replies.
        assert (await store.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0
        assert not (await Operations(store.db).overview(bot="999"))["queues"]
