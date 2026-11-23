import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from test_app import running, setup
from test_telegram import store_at

from zarya.activity import overview_activity


async def request(
    conn,
    number,
    at,
    sender="42",
    *,
    trigger="private",
    state="completed",
    mode="live",
    bot="999",
    username="new_handle",
    chat="42",
):
    profile = {"id": int(sender), "username": username, "first_name": "Тест"}
    event = json.dumps({"message": {"from": profile, "text": "PRIVATE_TEXT"}})
    await conn.execute(
        "INSERT INTO events(id,bot_id,update_id,chat_id,payload,received_at) VALUES (?,?,?,?,?,?)",
        (number, bot, number, chat, event, at),
    )
    await conn.execute(
        "INSERT INTO jobs(id,kind,event_id,state,payload,created_at) "
        "VALUES (?,'telegram',?,'done','{}',?)",
        (number, number, at),
    )
    await conn.execute(
        "INSERT INTO dialogue_runs(id,job_id,bot_id,chat_id,sender_id,message_id,trigger,state,"
        "snapshot,created_at,mode) VALUES (?,?,?,?,?,?,?,?,'{}',?,?)",
        (number, number, bot, chat, sender, number, trigger, state, at, mode),
    )


async def test_activity_counts_retained_addressed_requests_and_prices_linked_calls(tmp_path):
    now = datetime(2026, 11, 23, 12, 4, 30, tzinfo=UTC)

    def at(hours=0, minutes=0, seconds=0, microseconds=0):
        return (
            now
            + timedelta(hours=hours, minutes=minutes, seconds=seconds, microseconds=microseconds)
        ).isoformat()

    async with store_at(tmp_path) as store:
        async with store.db.transaction() as conn:
            await request(conn, 1, at(hours=-24), username="old_handle")
            await request(conn, 2, at(minutes=-60, seconds=-30), chat="-100", trigger="reply")
            await request(conn, 3, at(minutes=-2, seconds=-31, microseconds=999999))
            await request(conn, 4, at(minutes=-1), sender="58")
            await request(conn, 5, at(seconds=-20))  # Current minute, in 24h but still open.
            await request(conn, 6, at(minutes=-3), mode="paid")
            await request(conn, 7, at(minutes=-3), trigger="initiative")
            await request(conn, 8, at(minutes=-3), state="service")
            await request(conn, 9, at(minutes=-3), bot="888")
            await request(conn, 10, at(hours=-24, microseconds=-1))
            await request(conn, 11, at(minutes=-3))
            await conn.execute("UPDATE events SET payload='{}' WHERE id=11")
            await conn.execute(
                "UPDATE dialogue_runs SET created_at=? WHERE id=3", (at(minutes=-1),)
            )
            await conn.executemany(
                "INSERT INTO model_calls(provider,model,state,created_at,run_id,cost_usd) "
                "VALUES ('test','test','completed',?,?,?)",
                [
                    (at(minutes=-2), 3, 0.01),
                    (at(minutes=-1), 4, 0.9),
                    (at(minutes=-2), 6, 0.8),
                    (at(minutes=-2), 11, 0.7),
                ],
            )
            await conn.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,context_count_run_id) "
                "VALUES ('test','test','completed',?,3)",
                (at(minutes=-2),),
            )
            await conn.execute(
                "INSERT INTO research_runs(id,job_id,bot_id,chat_id,scope,access_version,state,"
                "mode,question,material,manifest,settings_version,model,reasoning,created_at) "
                "VALUES (1,3,'999','42','private',1,'completed','search','','','[]',1,'test',"
                "'low',?)",
                (at(minutes=-2),),
            )
            await conn.executemany(
                "INSERT INTO model_calls(provider,model,state,created_at,research_run_id,"
                "operation,cost_usd,search_cost_usd) VALUES ('test','test','completed',?,1,?,?,?)",
                [
                    (at(minutes=-2), "research_initial", 0.02, 0.003),
                    (at(minutes=-2), "research_followup", 0.004, 0.002),
                ],
            )
            await conn.executemany(
                "INSERT INTO photo_batches(id,bot_id,chat_id,scope,access_version,album_key,"
                "generation,state,model,processing_version,created_at,collect_until,"
                "collect_deadline,leader_job_id,leader_message_id,result,finished_at) "
                "VALUES (?,'999','42','private',1,'album',?,?,'test','1',?,0,0,3,3,'{}',?)",
                [
                    (1, 1, "completed", at(minutes=-2), at(minutes=-2)),
                    (2, 2, "cache", at(minutes=-1), at(minutes=-1)),
                ],
            )
            await conn.executemany(
                "INSERT INTO photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,"
                "file_unique_id,caption,state) "
                "VALUES (?,'999','42',?,3,'file','key','','prepared')",
                [(1, 300), (1, 301), (2, 300)],
            )
            await conn.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,photo_batch_id,cost_usd) "
                "VALUES ('test','test','completed',?,1,.005)",
                (at(minutes=-2),),
            )
            await conn.executemany(
                "INSERT INTO photo_batches(id,bot_id,chat_id,scope,access_version,album_key,"
                "generation,state,model,processing_version,created_at,collect_until,"
                "collect_deadline,request_job_id,leader_message_id,finished_at) "
                "VALUES (?,'999','42','private',1,?,1,'invalid','test','1',?,0,0,?,3,?)",
                [
                    (3, "lazy", at(minutes=-2), 3, at(minutes=-2)),
                    (4, "legacy", at(minutes=-2), None, at(minutes=-2)),
                ],
            )
            await conn.execute(
                "INSERT INTO photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,"
                "file_unique_id,caption,state) "
                "VALUES (4,'999','42',304,3,'file','key','','prepared')",
            )
            await conn.executemany(
                "INSERT INTO model_calls(provider,model,state,created_at,photo_batch_id,cost_usd) "
                "VALUES ('test','test','invalid',?,?,?)",
                [(at(minutes=-2), 3, 0.007), (at(minutes=-2), 4, 0.3)],
            )
            await conn.executemany(
                "INSERT INTO media_runs(id,bot_id,chat_id,scope,access_version,message_id,event_id,"
                "kind,file_id,state,processing_version,asr_model,model,settings_version,limits,"
                "created_at,finished_at,transcript) VALUES (?,'999','42','private',1,?,?,'video',"
                "'file','partial','1','test','test',1,'{}',?,?,?)",
                [
                    (1, 310, 3, at(minutes=-2), None, "transcript"),
                    (2, 311, 4, at(minutes=-1), at(minutes=-1), ""),
                    (3, 312, 7, at(minutes=-3), at(minutes=-3), "ambient"),
                ],
            )
            await conn.executemany(
                "INSERT INTO media_dependencies VALUES (?,?)",
                [(3, 1), (3, 2), (4, 2), (7, 3)],
            )
            await conn.executemany(
                "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,operation,"
                "cost_usd) VALUES ('test','test','completed',?,?,?,?)",
                [
                    (at(minutes=-2), 1, "asr", 0.006),
                    (at(minutes=-2), 1, "vision", 0.008),
                    (at(minutes=-1), 2, "asr", 0.6),
                ],
            )
            await conn.execute(
                "UPDATE model_calls SET finished_at=? WHERE media_run_id=1 AND operation='asr'",
                (at(minutes=-1),),
            )
            await conn.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,operation,"
                "request_job_id,cost_usd) VALUES ('test','test','completed',?,2,'vision',3,.009)",
                (at(minutes=-1),),
            )

        data = await overview_activity(store.db, bot="999", now=now)
        assert len(data["minutes"]) == 60
        assert data["minutes"][0]["requests"] == 1
        assert data["minutes"][57]["requests"] == 1  # Fractional second stays in 12:01.
        assert data["minutes"][59]["attachments"] == 1
        assert data["minutes"][59]["attachment_types"] == {"video": 1}
        assert sum(row["requests"] for row in data["minutes"]) == 3
        assert sum(row["attachments"] for row in data["minutes"]) == 3
        assert data["leader"] == {
            "username": "new_handle",
            "name": "Тест",
            "requests": 4,
            "cost_usd": pytest.approx(0.074),
            "unknown_cost_calls": 1,
            "unassigned_calls": 2,
        }
        assert "PRIVATE_TEXT" not in json.dumps(data)
        assert (await overview_activity(store.db, bot="unknown", now=now))["leader"] is None


async def test_activity_api_requires_admin_and_returns_empty_minutes(tmp_path):
    async with running(tmp_path) as (_, client):
        path = "/api/overview/activity"
        assert (await client.get(path)).status_code == 401
        await setup(client, tmp_path)
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        data = response.json()
        assert data["leader"] is None and len(data["minutes"]) == 60
        assert all(row["requests"] == row["attachments"] == 0 for row in data["minutes"])
        for period, bucket in ((30, 1), (60, 1), (180, 5), (720, 15), (1440, 30)):
            response = await client.get(path, params={"period": period})
            assert response.status_code == 200
            data = response.json()
            assert data["period_minutes"] == period and data["bucket_minutes"] == bucket
            points = data["minutes"]
            assert points[-1]["end"] - points[0]["time"] == period * 60
            assert all(not row["attachment_types"] for row in points)
            assert all(left["end"] == right["time"] for left, right in pairwise(points))
        assert (await client.get(path, params={"period": 120})).status_code == 422


async def test_period_changes_chart_window_and_keeps_leader_window(tmp_path):
    now = datetime(2026, 11, 23, 12, 4, 30, tzinfo=UTC)
    async with store_at(tmp_path) as store:
        async with store.db.transaction() as conn:
            for number, age in enumerate((10, 40, 100, 500, 1000), 1):
                await request(conn, number, (now - timedelta(minutes=age)).isoformat())
            # This falls inside the first completed minute on the chart, outside the leader's day.
            await request(conn, 6, (now - timedelta(hours=24, seconds=10)).isoformat())
            await request(conn, 7, (now - timedelta(seconds=10)).isoformat())

        for period, expected in ((30, 1), (60, 2), (180, 3), (720, 4), (1440, 6)):
            data = await overview_activity(store.db, period_minutes=period, now=now)
            points = data["minutes"]
            assert sum(row["requests"] for row in points) == expected
            assert data["leader"]["requests"] == 6
            assert points[-1]["end"] - points[0]["time"] == period * 60
            assert all(
                row["time"] >= row["bucket_time"]
                and 0 < row["end"] - row["time"] <= data["bucket_minutes"] * 60
                for row in points
            )


async def test_attachment_types_use_original_revision_and_dedupe_video_preview(tmp_path):
    now = datetime(2026, 11, 23, 12, 4, 30, tzinfo=UTC)
    received = (now - timedelta(minutes=26)).isoformat()
    preview = (now - timedelta(minutes=19)).isoformat()
    processed = (now - timedelta(minutes=9)).isoformat()
    async with store_at(tmp_path) as store:
        async with store.db.transaction() as conn:
            for number in range(1, 5):
                await request(conn, number, received)
            await conn.executemany(
                "UPDATE events SET payload=? WHERE id=?",
                (
                    (json.dumps({"message": {"photo": [{"file_id": "p"}]}}), 1),
                    (json.dumps({"edited_message": {"sticker": {"file_id": "s"}}}), 2),
                    (json.dumps({"message": {"video": {"file_id": "v"}}}), 3),
                ),
            )
            await conn.execute(
                "INSERT INTO photo_batches(id,bot_id,chat_id,scope,access_version,album_key,"
                "generation,state,model,processing_version,created_at,collect_until,"
                "collect_deadline,leader_job_id,leader_message_id,result,finished_at) "
                "VALUES (1,'999','42','private',1,'album',1,'completed','test','1',?,"
                "0,0,1,1,'{}',?)",
                (received, preview),
            )
            await conn.executemany(
                "INSERT INTO photo_items(batch_id,bot_id,chat_id,message_id,event_id,file_id,"
                "file_unique_id,caption,state) VALUES (1,'999','42',?,?,'f','u','','prepared')",
                [(number, number) for number in range(1, 5)],
            )
            await conn.execute(
                "INSERT INTO media_runs(id,bot_id,chat_id,scope,access_version,message_id,event_id,"
                "kind,file_id,state,processing_version,asr_model,model,settings_version,limits,"
                "created_at,finished_at,result) VALUES (1,'999','42','private',1,3,3,'video',"
                "'v','completed','2','test','test',1,'{}',?,?,'{}')",
                (received, processed),
            )
            await conn.execute("INSERT INTO media_dependencies VALUES (3,1)")

        data = await overview_activity(store.db, period_minutes=30, now=now)
        nonempty = [row for row in data["minutes"] if row["attachments"]]
        assert len(nonempty) == 1
        assert nonempty[0]["time"] == int(datetime.fromisoformat(preview).timestamp()) // 60 * 60
        assert nonempty[0]["attachments"] == 4
        assert nonempty[0]["attachment_types"] == {"photo": 1, "sticker": 1, "video": 1, "other": 1}


async def test_attachment_completed_after_left_chart_boundary_is_counted(tmp_path):
    now = datetime(2026, 11, 23, 12, 4, 30, tzinfo=UTC)
    start = now.replace(second=0) - timedelta(hours=24)
    received = (start - timedelta(seconds=10)).isoformat()
    completed = (start + timedelta(seconds=10)).isoformat()
    async with store_at(tmp_path) as store:
        async with store.db.transaction() as conn:
            await request(conn, 1, received)
            await conn.execute("UPDATE dialogue_runs SET created_at=? WHERE id=1", (completed,))
            await conn.execute(
                "INSERT INTO media_runs(id,bot_id,chat_id,scope,access_version,message_id,event_id,"
                "kind,file_id,state,processing_version,asr_model,model,settings_version,limits,"
                "created_at,finished_at,transcript) VALUES (1,'999','42','private',1,1,1,'voice',"
                "'v','completed','1','test','test',1,'{}',?,?,'speech')",
                (received, completed),
            )
            await conn.execute("INSERT INTO media_dependencies VALUES (1,1)")

        data = await overview_activity(store.db, period_minutes=1440, now=now)
        assert data["leader"] is None
        assert data["minutes"][0]["attachment_types"] == {"voice": 1}
        assert all(row["requests"] == 0 for row in data["minutes"])
