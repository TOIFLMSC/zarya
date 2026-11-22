import json
import time

import pytest
from test_dialogue import FakeModel, drain, private_ready, run_one
from test_telegram import approve, event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_context import valid_snapshot
from zarya.models import Settings
from zarya.openai_adapter import ModelResult
from zarya.participant_context import assistants, select, topic_history, window
from zarya.source_material import purge


def human_event(number, sender=42, text="сообщение", chat=-100, **extra):
    return event(
        number,
        chat=chat,
        text=text,
        **{
            "from": {"id": sender, "first_name": f"Участник {sender}", "is_bot": False},
            **extra,
        },
    )


async def group_ready(store):
    await store.ingest("999", "zarya_test", [human_event(1)])
    await approve(store, "group", "-100")
    return (
        await store.db.one(
            "SELECT version FROM telegram_access WHERE subject_id='-100'",
        )
    )[0]


async def selected(store, update, limit=20):
    async with store.db.transaction() as c:
        grant = await c.execute(
            "SELECT version FROM telegram_access WHERE subject_id='-100'",
        )
        version = (await grant.fetchone())[0]
        return await select(
            c,
            "999",
            "-100",
            version,
            update["message"],
            Settings(participant_context_messages=limit),
        )


async def test_personal_window_survives_active_neighbour_and_old_char_cap(tmp_path):
    async with store_at(tmp_path) as store:
        await group_ready(store)
        await store.ingest(
            "999",
            "zarya_test",
            [human_event(i, text=f"редкий {i} " + "x" * 1400) for i in range(2, 28)]
            + [human_event(i, sender=84) for i in range(28, 570)],
        )
        question = human_event(570, text="Заря, продолжим")
        await store.ingest("999", "zarya_test", [question])
        context, diagnostic, refs = await selected(store, question)
        assert [m["message_id"] for m in context] == list(range(9, 28))
        assert sum(len(m["text"]) for m in context) > 22000
        assert all(m["sender_id"] == "42" for m in context)
        assert diagnostic["participants"][0]["human_messages"] == 20
        assert diagnostic["participants"][0]["available"] == 27
        assert len(refs["humans"]) == 20
        assert (await store.db.one("SELECT COUNT(*) FROM recent_messages"))[0] > 500


@pytest.mark.parametrize("limit", [1, 20, 100])
async def test_depth_and_connected_reply_people(tmp_path, limit):
    async with store_at(tmp_path) as store:
        await group_ready(store)
        sources = [human_event(i, sender=84, message_thread_id=7) for i in range(2, 107)]
        await store.ingest("999", "zarya_test", sources)
        question = human_event(
            107, text="Заря, уточни", message_thread_id=7, reply_to_message=sources[-1]["message"]
        )
        await store.ingest("999", "zarya_test", [question])
        context, diagnostic, _ = await selected(store, question, limit)
        assert len(context) == limit
        assert {p["sender_id"] for p in diagnostic["participants"]} == {"42", "84"}
        assert [m["message_id"] for m in context] == list(range(107 - limit, 107))
        assert all(m["thread_id"] == 7 for m in context)
        assert [p["human_messages"] for p in diagnostic["participants"]] == [1, limit]


async def test_edits_keep_order_and_age_and_never_duplicate(tmp_path):
    async with store_at(tmp_path) as store:
        grant = await group_ready(store)
        original = human_event(2, text="старое")
        await store.ingest("999", "zarya_test", [original, human_event(3), human_event(4)])
        at = (
            await store.db.one("SELECT received_at FROM participant_messages WHERE message_id=2")
        )[0]
        changed = human_event(2, text="новое")
        await store.ingest(
            "999", "zarya_test", [{"update_id": 5, "edited_message": changed["message"]}]
        )
        async with store.db.transaction() as c:
            messages, _, count, _ = await window(c, "999", "-100", grant, "42", 2)
            assert [m["message_id"] for m in messages] == [3, 4]
            messages, _, count, _ = await window(c, "999", "-100", grant, "42", 3)
            assert messages[0]["text"] == "новое" and count == 3
            assert messages[0]["received_at"] == at


@pytest.mark.parametrize("pilot", ["", "-100", "-200"])
async def test_forum_topic_chronology_without_reply_and_with_exact_source(tmp_path, pilot):
    async with store_at(tmp_path) as store:
        await group_ready(store)
        await store.db.update_settings(
            1, Settings(topic_context_chat_id=pilot, memory_enabled=False)
        )
        exact = human_event(2, text="Точная цитата для ТЗ", message_thread_id=7)
        await store.ingest(
            "999",
            "zarya_test",
            [
                exact,
                human_event(3, sender=84, text="Встреча в 19:00", message_thread_id=7),
                human_event(4, sender=85, text="Перенесли на 21:00", message_thread_id=7),
                human_event(5, text="Соседняя тема в 23:00", message_thread_id=8),
                human_event(6, sender=84, text="Соседняя тема в 22:00", message_thread_id=8),
            ],
        )
        changed = human_event(4, sender=85, text="Перенесли на 20:00", message_thread_id=7)
        await store.ingest(
            "999",
            "zarya_test",
            [
                {"update_id": 7, "edited_message": changed["message"]},
                human_event(8, text="Заря, во сколько встреча?", message_thread_id=7),
            ],
        )
        model = FakeModel()
        engine = DialogueEngine(store, model)
        await drain(engine)
        (run_id,) = await store.db.one("SELECT id FROM dialogue_runs WHERE message_id=8")
        snapshot = (await engine.details(run_id))["snapshot"]
        body = json.loads(model.calls[-1]["input"])
        if pilot != "-100":
            assert "topic_context" not in body
            assert [m["message_id"] for m in body["recent_chat"]] == [2, 5]
            return
        topic = body["topic_context"]
        assert [(m["message_id"], m["sender_id"], m["text"]) for m in topic] == [
            (3, "84", "Встреча в 19:00"),
            (4, "85", "Перенесли на 20:00"),
        ]
        assert [m["message_id"] for m in body["recent_chat"]] == [2]
        assert all(m["name"] and type(m["received_at"]) is float for m in topic)
        refs = snapshot["context_refs"]["humans"]
        assert [r["message_id"] for r in refs if r["message_id"] in {3, 4}] == [3, 4]
        for item in topic:
            assert item["event_id"] == next(
                ref["event_id"] for ref in refs if ref["message_id"] == item["message_id"]
            )
        assert (await engine.replay(run_id, "recorded"))["snapshot"]["topic_context"] == topic
        settings_version = (await store.db.settings()).version
        await engine.replay(run_id, "paid", settings_version)
        assert json.loads(model.calls[-1]["input"])["topic_context"] == topic
        assert "В topic_context собраны" in model.calls[-1]["instructions"]

        # A no-reply turn uses the chronology. An explicit cross-topic reply still
        # resolves the exact accepted source, regardless of Telegram's embedded copy.
        await store.ingest(
            "999",
            "zarya_test",
            [
                human_event(
                    9,
                    text="Заря, процитируй",
                    message_thread_id=8,
                    reply_to_message={**exact["message"], "text": "устаревший текст Telegram"},
                ),
            ],
        )
        await drain(engine)
        (run_id,) = await store.db.one("SELECT id FROM dialogue_runs WHERE message_id=9")
        later = (await engine.details(run_id))["snapshot"]
        body = json.loads(model.calls[-1]["input"])
        selected_source = body["selected_material"]["messages"][0]
        assert selected_source["text"] == "Точная цитата для ТЗ"
        assert selected_source["sender_id"] == "42"
        assert selected_source["name"] == "Участник 42"
        assert selected_source["received_at"] > 0
        assert "устаревший текст Telegram" not in model.calls[-1]["input"]
        assert [m["message_id"] for m in body["topic_context"]] == [6]
        assert 2 not in [m["message_id"] for m in body["recent_chat"]]
        async with store.db.transaction() as conn:
            assert await valid_snapshot(conn, "999", snapshot)
            assert await valid_snapshot(conn, "999", later)
            await conn.execute(
                "DELETE FROM research_messages WHERE chat_id='-100' AND message_id=4"
            )
            assert not await valid_snapshot(conn, "999", snapshot)
            assert await valid_snapshot(conn, "999", later)

        # The ordinary edit path erases and cancels dependent snapshots.
        changed["message"]["text"] = "Перенесли на 20:30"
        original_run = snapshot["context_run_id"]
        await store.ingest(
            "999", "zarya_test", [{"update_id": 10, "edited_message": changed["message"]}]
        )
        assert (await engine.details(original_run))["snapshot"]["invalidated"]
        for mode in ("recorded", "paid"):
            with pytest.raises(ValueError):
                await engine.replay(original_run, mode, settings_version)
        assert (
            await store.db.one(
                "SELECT state FROM outbox WHERE job_id=(SELECT job_id "
                "FROM dialogue_runs WHERE id=?)",
                (original_run,),
            )
        )[0] == "cancelled"


async def test_topic_pilot_keeps_general_forum_separate_from_named_topics(tmp_path):
    async with store_at(tmp_path) as store:
        await group_ready(store)
        await store.db.update_settings(
            1, Settings(topic_context_chat_id="-100", memory_enabled=False)
        )
        general = [
            human_event(3, text="Один автор в общей теме"),
            human_event(4, sender=84, text="Сосед по общей теме"),
            human_event(5, text="Заря, что решили?"),
        ]
        for update in general:
            update["message"]["chat"]["is_forum"] = True
        await store.ingest(
            "999",
            "zarya_test",
            [
                human_event(2, text="Один автор в соседней теме", message_thread_id=7),
                *general,
            ],
        )
        model = FakeModel()
        await drain(DialogueEngine(store, model))
        body = json.loads(model.calls[-1]["input"])
        assert [m["message_id"] for m in body["recent_chat"]] == [3]
        assert [m["message_id"] for m in body["topic_context"]] == [4]


@pytest.mark.parametrize(
    "text, expected", [("реплика", list(range(5, 13))), ("я" * 2000, [11, 12])]
)
async def test_topic_history_limits_and_original_age(tmp_path, text, expected):
    async with store_at(tmp_path) as store:
        grant = await group_ready(store)
        await store.ingest(
            "999",
            "zarya_test",
            [human_event(i, sender=84, text=text, message_thread_id=7) for i in range(2, 14)]
            + [human_event(14, sender=84, text="/memory", message_thread_id=7)],
        )
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE participant_messages SET received_at=? WHERE message_id=13",
                (time.time() - 86401,),
            )
            topic, refs = await topic_history(conn, "999", "-100", grant, 7, 15, set())
            assert [m["message_id"] for m in topic] == expected
            assert [r["message_id"] for r in refs] == expected
            assert sum(len(m["text"]) for m in topic) <= 2500
            if len(text) > 100:
                assert topic[0]["text"] == "я" * 499 + "…"
            await conn.execute("UPDATE telegram_access SET state='denied'")
            assert await topic_history(conn, "999", "-100", grant, 7, 15, set()) == ([], [])


async def test_no_bots_services_commands_or_foreign_grant_in_personal_window(tmp_path):
    async with store_at(tmp_path) as store:
        grant = await group_ready(store)
        await store.ingest(
            "999",
            "zarya_test",
            [
                human_event(2, text="переслано", forward_origin={"type": "channel"}),
                human_event(3, text="/memory"),
                human_event(4, **{"from": {"id": 77, "is_bot": True}}),
                human_event(5, text="", new_chat_title="новая группа"),
                human_event(6, text="", photo=[{"file_id": "photo", "width": 10, "height": 10}]),
            ],
        )
        async with store.db.transaction() as c:
            messages, _, _, _ = await window(c, "999", "-100", grant, "42", 20)
            assert [m["message_id"] for m in messages] == [2, 6]
            assert messages[0]["forwarded"] is True
            await c.execute("UPDATE telegram_access SET version=version+1")
            assert (await window(c, "999", "-100", grant, "42", 20))[0] == []
            assert (await window(c, "999", "-100", grant + 1, "42", 20))[0] == []


@pytest.mark.parametrize("turns", [30, 100])
async def test_long_sent_dialogue_and_transitive_source_erasure(tmp_path, turns):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        engine = DialogueEngine(store, FakeModel(ModelResult(text="ответ")))
        works = []
        for i in range(turns):
            mid = 10 * (i + 1)
            await store.ingest("999", "zarya_test", [event(mid, text=f"вопрос {i}")])
            work = await run_one(engine)
            async with store.db.transaction() as c:
                await c.execute("UPDATE delivery_limits SET last_attempt=0")
                await c.execute("UPDATE outbox SET next_attempt=0")
            outgoing = await store.claim_delivery("999")
            assert outgoing is not None
            await store.delivery_result(outgoing["id"], "sent", message_id=mid + 1)
            works.append(work)
        latest = works[-1]["snapshot"]
        assert latest["participant_context"]["participants"][0]["human_messages"] == 20
        assert len([m for m in latest["context"] if m["role"] == "user"]) == 19
        assert len([m for m in latest["context"] if m["role"] == "assistant"]) == 19
        async with store.db.transaction() as c:
            statements = []
            await c.set_trace_callback(statements.append)
            messages, _, _, _ = await window(c, "999", "42", 2, "42", 20)
            parts, _ = await assistants(c, "999", "42", 2, messages)
            await c.set_trace_callback(None)
            assert len(parts) == 20
            assert sum("WITH RECURSIVE ancestors" in q for q in statements) == 1
            assert await valid_snapshot(c, "999", latest)
            # A withdrawn early ancestor is detected even before cascade erases descendants.
            await c.execute(
                "UPDATE events SET payload='{}' WHERE id=?",
                (works[0]["snapshot"]["context_refs"]["humans"][0]["event_id"],),
            )
            assert not await valid_snapshot(c, "999", latest)
            await purge(c, "id=?", (works[0]["run_id"],))
        rows = await store.db.all("SELECT snapshot,response FROM dialogue_runs")
        assert all(json.loads(s[0]).get("invalidated") and s[1] is None for s in rows)
        assert all(r[0] is None for r in await store.db.all("SELECT request_json FROM model_calls"))


async def test_personal_retention_cannot_be_extended_by_edit(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="старый вопрос")])
        async with store.db.transaction() as c:
            await c.execute(
                "UPDATE participant_messages SET received_at=?", (time.time() - 91 * 86400,)
            )
        changed = event(2, text="правка старого вопроса")
        await store.ingest(
            "999", "zarya_test", [{"update_id": 3, "edited_message": changed["message"]}]
        )
        memory = MemoryEngine(store, None, tmp_path)
        result = await memory.participant_window("999", "42", "42")
        assert result["available"] == 0 and result["messages"] == []
        await memory.cleanup("999", force=True)
        assert (await store.db.one("SELECT COUNT(*) FROM participant_messages"))[0] == 0


async def test_partial_sent_answer_only_and_next_job_uses_new_depth(tmp_path, monkeypatch):
    monkeypatch.setattr("zarya.dialogue.random.random", lambda: 0.0)
    async with store_at(tmp_path) as store:
        await private_ready(store)
        answer = "первая мысль. " * 35 + "\n\n" + "вторая мысль. " * 35
        engine = DialogueEngine(store, FakeModel(ModelResult(text=answer)))
        await store.ingest("999", "zarya_test", [event(10, text="вопрос")])
        first = await run_one(engine)
        assert (await store.db.one("SELECT COUNT(*) FROM outbox"))[0] >= 2
        async with store.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0")
        sent = await store.claim_delivery("999")
        assert sent is not None
        await store.delivery_result(sent["id"], "sent", message_id=11)
        async with store.db.transaction() as c:
            await c.execute("UPDATE outbox SET state='unknown' WHERE state='pending'")
        assert (await store.db.one("SELECT COUNT(*) FROM outbox WHERE state='unknown'"))[0] >= 1
        await store.ingest("999", "zarya_test", [event(20, text="продолжим")])
        second = await run_one(engine)
        assert [m["text"] for m in second["snapshot"]["context"] if m["role"] == "assistant"] == [
            sent["text"]
        ]
        await store.db.update_settings(1, Settings(participant_context_messages=1))
        await store.ingest("999", "zarya_test", [event(30, text="новый вопрос")])
        third = await run_one(engine)
        assert third["snapshot"]["context"] == []
        assert third["snapshot"]["participant_context"]["limit"] == 1
        assert first["snapshot"]["participant_context"]["limit"] == 20


async def test_window_api_settings_validation_and_revoke(tmp_path):
    from test_app import running, setup

    async with running(tmp_path) as (app, client):
        route = "/api/memory/chats/42/context?sender_id=42"
        assert (await client.get(route)).status_code == 401
        await setup(client, tmp_path)
        app.state.telegram.bot = {"id": "999", "username": "zarya_test"}
        store = app.state.telegram_store
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(i, text=f"личное {i}") for i in range(2, 7)])
        current = (await client.get("/api/settings")).json()
        for bad in [0, 101, 2.5, "20", True]:
            current["settings"]["participant_context_messages"] = bad
            response = await client.put(
                "/api/settings",
                json={
                    "expected_version": current["version"],
                    "settings": current["settings"],
                },
            )
            assert response.status_code == 422
        current["settings"]["participant_context_messages"] = 3
        response = await client.put(
            "/api/settings",
            json={
                "expected_version": current["version"],
                "settings": current["settings"],
            },
        )
        assert response.status_code == 200
        data = (await client.get(route)).json()
        assert data["limit"] == 3 and data["available"] == 5 and data["human_messages"] == 3
        assert [m["message_id"] for m in data["messages"]] == [4, 5, 6]
        assert (await client.get("/api/memory/chats/42/context?sender_id=77")).json()[
            "messages"
        ] == []
        assert (await client.get("/api/memory/chats/-100/context?sender_id=42")).status_code == 404
        assert (await client.get("/api/memory/chats/42/context?sender_id=bad")).status_code == 422
        await store.decide("999", "private", "42", "revoked", 2)
        assert (await client.get(route)).status_code == 404


async def test_backfill_only_current_allowed_retained_sources(tmp_path):
    from zarya.database import Database

    async with store_at(tmp_path) as store:
        grant = await group_ready(store)
        await store.ingest(
            "999",
            "zarya_test",
            [
                human_event(2, text="сохранившийся пост", forward_origin={"type": "channel"}),
                human_event(3, text="удалено"),
                human_event(4, text="старый доступ"),
            ],
        )
        async with store.db.transaction() as c:
            # The old chat-wide recent cap lost metadata, but the current approved event survives.
            await c.execute("DELETE FROM recent_messages WHERE message_id=2")
            await c.execute("UPDATE events SET payload='{}' WHERE update_id=3")
            await c.execute("UPDATE research_messages SET access_version=1 WHERE message_id=4")
            await c.execute("DROP TABLE context_run_edges")
            await c.execute("DROP TABLE participant_messages")
            await c.execute(
                "DELETE FROM schema_migrations WHERE version='022_participant_context.sql'"
            )
    db = Database(tmp_path / "test.sqlite3")
    await db.open()
    try:
        async with db.transaction() as c:
            messages, _, count, _ = await window(c, "999", "-100", grant, "42", 20)
        assert count == 1 and [m["message_id"] for m in messages] == [2]
        assert messages[0]["forwarded"] is True
    finally:
        await db.close()
