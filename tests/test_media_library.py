import pytest
from test_app import running, setup


async def seed(db):
    async with db.transaction() as c:
        for n in range(1, 24):
            await c.execute(
                "INSERT INTO photo_batches(id,bot_id,chat_id,scope,access_version,album_key,"
                "generation,state,model,processing_version,created_at,collect_until,"
                "collect_deadline,leader_message_id) VALUES (?,'999','42','private',1,?,1,"
                "'completed','luna','1',?,0,0,?)",
                (n, str(n), f"2026-10-09T10:{n:02d}:00+00:00", n),
            )
        for n, bot, chat, scope in [(1, "999", "-100", "group"), (2, "other", "42", "private")]:
            await c.execute(
                "INSERT INTO media_runs(id,bot_id,chat_id,scope,access_version,message_id,"
                "event_id,kind,file_id,state,processing_version,asr_model,model,"
                "settings_version,limits,created_at) VALUES (?,?,?,?,1,1,1,'video','f',"
                "'completed','1','asr','luna',1,'{}','2026-10-09T10:15:30+00:00')",
                (n, bot, chat, scope),
            )
        await c.execute(
            "INSERT INTO telegram_access(bot_id,scope,subject_id,title,updated_at) "
            "VALUES ('999','group','-100','Тестовая группа','2026-10-09')"
        )
        await c.execute(
            "INSERT INTO model_calls(provider,model,state,created_at,photo_batch_id,cost_usd) "
            "VALUES ('openai','luna','completed','2026-10-09',1,.01)"
        )
        await c.execute(
            "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,operation,"
            "cost_usd) VALUES ('openai','luna','completed','2026-10-09',1,'vision',.02)"
        )
        await c.execute(
            "INSERT INTO model_calls(provider,model,state,created_at,media_run_id,operation) "
            "VALUES ('openai','asr','unknown','2026-10-09',1,'asr')"
        )


async def test_combined_order_pages_filters_cost_and_bot_boundary(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/media-library")).status_code == 401
        await setup(client, tmp_path)
        await seed(app.state.db)
        app.state.telegram.bot = {"id": "999"}
        data = (await client.get("/api/media-library")).json()
        assert data["total"] == 24 and len(data["items"]) == 20
        assert data["items"][0]["id"] == 23
        assert data["items"][8]["source"] == "media"
        assert data["items"][8]["chat_title"] == "Тестовая группа"
        assert data["known_cost_usd"] == pytest.approx(0.03)
        assert data["unknown_cost_calls"] == 1
        other = (await client.get("/api/media-library?page=1")).json()
        keys = {(i["source"], i["id"]) for i in data["items"] + other["items"]}
        assert len(keys) == 24 and ("photo", 1) in keys and ("media", 1) in keys
        assert (await client.get("/api/media-library?page=999")).json()["page"] == 1
        photos = (await client.get("/api/media-library?kind=photo")).json()
        assert photos["total"] == 23 and photos["known_cost_usd"] == 0.01
        assert photos["unknown_cost_calls"] == 0
        group = (await client.get("/api/media-library?scope=group&chat_id=-100")).json()
        assert group["total"] == 1 and group["known_cost_usd"] == 0.02
        assert group["chats"] == [{"id": "-100", "scope": "group", "title": "Тестовая группа"}]
        assert (await client.get("/api/media-library?scope=private&chat_id=-100")).json()[
            "total"
        ] == 0
        app.state.telegram.bot = {"id": "other"}
        assert (await client.get("/api/media-library")).json()["total"] == 1
        app.state.telegram.bot = None
        assert (await client.get("/api/media-library")).json()["total"] == 0
        assert (await client.get("/api/media-library?kind=invalid")).status_code == 422
