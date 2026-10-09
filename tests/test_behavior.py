import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SetMessageReaction
from test_app import running, setup
from test_dialogue import FakeModel, drain, run_one
from test_photos import ready
from test_telegram import FakeTransport, event, store_at

from zarya.behavior import BehaviorEngine, decayed, eligible, prepare
from zarya.dialogue import DialogueEngine
from zarya.models import GroupBehaviorUpdate
from zarya.openai_adapter import ModelResult
from zarya.telegram_runtime import TelegramRuntime


def result(action="text", *, text="О, спасибо!", tone="grateful", reaction=None):
    return ModelResult(
        text=json.dumps(
            {
                "action": action,
                "text": text if action == "text" else "",
                "reaction": reaction,
                "tone": tone,
                "intensity": 0.7,
                "public_reason": "Собеседник поблагодарил за помощь",
            },
            ensure_ascii=False,
        ),
        usage={"input_tokens": 100, "output_tokens": 100},
    )


async def enable(store):
    snapshot = await store.db.settings()
    await store.db.update_settings(
        snapshot.version, snapshot.settings.model_copy(update={"behavior_enabled": True})
    )


async def ready_engine(store, chat=42, action="text", **kwargs):
    await ready(store, chat)
    await enable(store)

    class PlanModel(FakeModel):
        async def generate(self, request):
            self.calls.append(request)
            return copy.deepcopy(self.result)

    model = PlanModel(result(action, **kwargs))
    engine = DialogueEngine(store, model)
    await drain(engine)  # service greeting is separate from behavior plans
    return engine, model, BehaviorEngine(store)


async def due(store):
    async with store.db.transaction() as conn:
        await conn.execute("UPDATE outbox SET next_attempt=0")
        await conn.execute("UPDATE delivery_limits SET last_attempt=0")


def test_mood_decay_is_time_based_and_returns_to_neutral():
    assert decayed("angry", 0.8, 100, 1900) == {"tone": "angry", "intensity": 0.4}
    assert decayed("angry", 0.8, 100, 20000) == {"tone": "neutral", "intensity": 0}


async def test_addressed_single_call_updates_scoped_mood_and_keeps_links_rule(tmp_path):
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, text="Спасибо! (https://example.org/)")
        await s.ingest("999", "zarya_test", [event(2, text="Спасибо, Заря")])
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert len(model.calls) == 1 and detail["response"] == "Спасибо!"
        assert detail["snapshot"]["behavior"]["plan"]["intensity"] == 0.35
        before = await behavior.overview("999")
        after = await behavior.overview("999")
        assert before["moods"][0]["updated_at"] == after["moods"][0]["updated_at"]
        assert before["moods"][0]["tone"] == "grateful"
        # Existing finish guard is exactly-once for mood as well.
        await engine.finish(work, result(), 0)
        assert (await behavior.overview("999"))["moods"][0]["version"] == 2


@pytest.mark.parametrize("action", ["reaction", "silent"])
async def test_nontext_actions_no_fake_assistant_context_and_replay_isolated(tmp_path, action):
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(
            s, action=action, reaction="❤" if action == "reaction" else None
        )
        async with s.db.transaction() as conn:
            await conn.execute("DELETE FROM outbox")
        await s.ingest("999", "zarya_test", [event(2, text="Спасибо, Заря")])
        work = await run_one(engine)
        await due(s)
        transport = FakeTransport()
        transport.react = AsyncMock(return_value=True)
        runtime = TelegramRuntime(s, transport=transport, dialogue=engine)
        await runtime.type_one("999")
        assert (await s.db.one("SELECT SUM(typing_attempts) FROM jobs"))[0] == 0
        await runtime.deliver_one("999")
        assert transport.react.await_count == int(action == "reaction")
        assert not await s.db.one("SELECT 1 FROM recent_messages WHERE role='assistant'")
        mood_version = (await behavior.overview("999"))["moods"][0]["version"]
        count = (await s.db.one("SELECT COUNT(*) FROM outbox"))[0]
        recorded = await engine.replay(work["run_id"], "recorded")
        paid = await engine.replay(work["run_id"], "paid", (await s.db.settings()).version)
        assert recorded["snapshot"]["behavior"]["plan"]["action"] == action
        assert paid["snapshot"]["behavior"]["plan"]["action"] == action
        assert not recorded["parts"] and not paid["parts"]
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == count
        assert (await behavior.overview("999"))["moods"][0]["version"] == mood_version


@pytest.mark.parametrize("mode", ["shadow", "live"])
async def test_initiative_chance_single_call_no_research_media_and_shadow_no_typing(
    tmp_path, mode, monkeypatch
):
    monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.01)
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, -100, text="Ну это вообще сюжетный поворот")
        await behavior.update_policy(
            "999", "-100", GroupBehaviorUpdate(expected_version=0, mode=mode)
        )
        engine.research = SimpleNamespace(
            prepare=AsyncMock(side_effect=AssertionError("research forbidden"))
        )
        engine.media = SimpleNamespace(
            prepare=AsyncMock(side_effect=AssertionError("media forbidden")),
            enrich=AsyncMock(return_value=[]),
        )
        async with s.db.transaction() as conn:
            await conn.execute("DELETE FROM outbox")
        await s.ingest("999", "zarya_test", [event(2, -100, "вот это сегодня поворот в истории")])
        work = await engine.claim("999")
        assert work and not work["skip"] and work["snapshot"]["behavior"]["mode"] == mode
        transport = FakeTransport()
        runtime = TelegramRuntime(s, transport=transport, dialogue=engine)
        await runtime.type_one("999")
        assert (await s.db.one("SELECT SUM(typing_attempts) FROM jobs"))[0] == 0
        await engine.execute(work)
        assert len(model.calls) == 1
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == int(mode == "live")
        assert (await behavior.overview("999"))["moods"][0]["version"] == (
            1 if mode == "shadow" else 2
        )
        if mode == "live":
            await due(s)
            await runtime.deliver_one("999")
            assert not await s.db.one("SELECT 1 FROM active_dialogues")


async def test_chance_boundary_durable_draw_no_reroll_and_policy_off(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, -100)
        await behavior.update_policy(
            "999", "-100", GroupBehaviorUpdate(expected_version=0, mode="shadow")
        )
        monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.05)
        await s.ingest("999", "zarya_test", [event(2, -100, "обычный интересный разговор")])
        work = await run_one(engine)
        assert work["skip"] and not model.calls
        stored = await s.db.one("SELECT payload FROM jobs WHERE id=?", (work["job_id"],))
        payload = json.loads(stored[0])
        assert payload["initiative_draw"] == 5
        monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.0)
        async with s.db.transaction() as conn:
            assert (
                await prepare(
                    conn,
                    "999",
                    "-100",
                    0,
                    work["job_id"],
                    payload,
                    event(2, -100, "обычный разговор")["message"],
                    (await s.db.settings()).settings,
                    False,
                )
                is None
            )
        assert (await behavior.overview("999"))["decisions"][0]["action"] == "not_selected"


@pytest.mark.parametrize("change", ["reset", "revoke", "edit", "disable", "policy", "newer"])
async def test_late_plan_cannot_override_reset_revoke_edit_or_stale_initiative(
    tmp_path, change, monkeypatch
):
    monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.0)
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, -100)
        initiative = change in {"policy", "newer"}
        if initiative:
            await behavior.update_policy(
                "999", "-100", GroupBehaviorUpdate(expected_version=0, mode="live")
            )
        await s.ingest(
            "999",
            "zarya_test",
            [event(2, -100, "обычный разговор" if initiative else "Заря, спасибо")],
        )
        work = await engine.claim("999")
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(request):
            entered.set()
            await release.wait()
            return result()

        model.generate = generate
        task = asyncio.create_task(engine.execute(work))
        await entered.wait()
        if change == "reset":
            await behavior.reset("999", "-100", 0, 1)
        elif change == "revoke":
            version = (
                await s.db.one("SELECT version FROM telegram_access WHERE subject_id='-100'")
            )[0]
            await s.decide("999", "group", "-100", "revoked", version)
        elif change == "edit":
            edited = event(3, -100, "исправленный вопрос")
            edited["edited_message"] = edited.pop("message")
            edited["edited_message"]["message_id"] = 2
            await s.ingest("999", "zarya_test", [edited])
        elif change == "disable":
            cfg = await s.db.settings()
            await s.db.update_settings(
                cfg.version, cfg.settings.model_copy(update={"behavior_enabled": False})
            )
        elif change == "policy":
            await behavior.update_policy(
                "999", "-100", GroupBehaviorUpdate(expected_version=1, mode="shadow")
            )
        else:
            await s.ingest("999", "zarya_test", [event(3, -100, "новый поворот темы")])
        release.set()
        await task
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "cancelled" and detail["call"]["usage"] is not None
        assert not await s.db.one("SELECT 1 FROM outbox WHERE job_id=?", (work["job_id"],))
        assert not await s.db.one(
            "SELECT 1 FROM behavior_decisions WHERE run_id=?", (work["run_id"],)
        )


async def test_reaction_rate_retry_and_unknown_restart_not_repeated(tmp_path):
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, action="reaction", reaction="👍")
        async with s.db.transaction() as conn:
            await conn.execute("DELETE FROM outbox")
        await s.ingest("999", "zarya_test", [event(2, text="спасибо")])
        await run_one(engine)
        transport = FakeTransport()
        transport.react = AsyncMock(
            side_effect=TelegramRetryAfter(
                method=SetMessageReaction(chat_id=42, message_id=2), message="retry", retry_after=2
            )
        )
        runtime = TelegramRuntime(s, transport=transport)
        await due(s)
        await runtime.deliver_one("999")
        assert (await s.db.one("SELECT state,error_code FROM outbox")) == ("pending", "rate_limit")
        async with s.db.transaction() as conn:
            await conn.execute("UPDATE delivery_limits SET blocked_until=0")
        transport.react = AsyncMock(side_effect=TimeoutError())
        await due(s)
        await runtime.deliver_one("999")
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "unknown"
        await s.recover("999")
        await runtime.deliver_one("999")
        assert transport.react.await_count == 1


async def test_behavior_api_auth_scope_conflict_stop_uses_current_settings(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/behavior")).status_code == 401
        await setup(client, tmp_path)
        app.state.telegram.bot = {"id": "999", "username": "test"}
        engine, model, behavior = await ready_engine(app.state.telegram_store, -100)
        assert (await client.get("/api/behavior")).json()["peers"][0]["chat_id"] == "-100"
        assert (
            await client.put(
                "/api/behavior/groups/-100",
                json={"expected_version": 0, "mode": "shadow", "chance_percent": 5},
            )
        ).status_code == 200
        assert (
            await client.put(
                "/api/behavior/groups/-100",
                json={"expected_version": 0, "mode": "live"},
            )
        ).status_code == 409
        assert (
            await client.put(
                "/api/behavior/groups/-100",
                json={"expected_version": 1, "mode": "live", "chance_percent": 101},
            )
        ).status_code == 422
        stop = await client.post("/api/behavior/stop", json={})
        assert stop.status_code == 200 and not stop.json()["settings"]["dialogue_enabled"]
        assert stop.json()["settings"]["behavior_enabled"]


async def test_new_mood_preserves_prepared_parts_but_reset_invalidates_them(tmp_path):
    from zarya.behavior import valid

    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s, text="Первая мысль.\n\nВторая мысль.")
        async with s.db.transaction() as conn:
            await conn.execute("DELETE FROM outbox")
        await s.ingest("999", "zarya_test", [event(2, text="Первый вопрос")])
        first = await run_one(engine)
        await s.ingest("999", "zarya_test", [event(3, text="Второй вопрос")])
        await run_one(engine)
        async with s.db.transaction() as conn:
            assert await valid(conn, "999", "42", first["snapshot"]["behavior"], delivery=True)
        mood = (await behavior.overview("999"))["moods"][0]
        assert mood["version"] == 3 and mood["epoch"] == 1
        await behavior.reset("999", "42", 0, mood["version"])
        async with s.db.transaction() as conn:
            assert not await valid(conn, "999", "42", first["snapshot"]["behavior"], delivery=True)
        assert (await behavior.overview("999"))["moods"][0]["epoch"] == 2


async def test_paid_replay_uses_current_expression_and_reaction_settings(tmp_path):
    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s)
        await s.ingest("999", "zarya_test", [event(2, text="Спасибо")])
        original = await run_one(engine)
        cfg = await s.db.settings()
        cfg = await s.db.update_settings(
            cfg.version,
            cfg.settings.model_copy(
                update={
                    "reactions_enabled": False,
                    "expressiveness": "restrained",
                    "emotional_max_parts": 2,
                }
            ),
        )
        replay = await engine.replay(original["run_id"], "paid", cfg.version)
        state = replay["snapshot"]["behavior"]
        assert not state["reactions_enabled"] and state["expressiveness"] == "restrained"
        assert state["emotional_max_parts"] == 2 and not state["applied"]
        assert not json.loads(model.calls[-1]["input"])["behavior"]["available_reactions"]


@pytest.mark.parametrize(
    "extra",
    [
        {"forward_origin": {"type": "channel"}},
        {"from": {"is_bot": True}},
        {"photo": [{}]},
        {"video": {}},
        {"voice": {"file_id": "x"}},
        {"text": "/start"},
        {"text": ""},
        {"sender_chat": {"id": -10}},
    ],
)
def test_initiative_excludes_service_forwarded_and_media(extra):
    message = {"text": "интересная беседа", **extra}
    # Empty video objects aren't valid Telegram media; test a real object.
    if extra == {"video": {}}:
        message["video"] = {"file_id": "x"}
    assert not eligible(message)


async def test_purge_removes_decision_reason_and_resets_mood_once(tmp_path):
    from zarya.behavior import cleanup
    from zarya.source_material import purge

    async with store_at(tmp_path) as s:
        engine, model, behavior = await ready_engine(s)
        await s.ingest("999", "zarya_test", [event(2, text="Спасибо")])
        work = await run_one(engine)
        async with s.db.transaction() as conn:
            await purge(conn, "id=?", (work["run_id"],))
            await cleanup(conn, "999", 10**12)
            await cleanup(conn, "999", 10**12)
        overview = await behavior.overview("999")
        assert not overview["decisions"]
        assert overview["moods"][0]["tone"] == "neutral"
        assert not overview["moods"][0]["reason"]
        assert overview["moods"][0]["epoch"] == 2
