import asyncio
import json
import time

import pytest
from test_behavior import ready_engine
from test_dialogue import FakeModel, private_ready, run_one
from test_telegram import FakeTransport, event, store_at

from zarya import timings
from zarya.behavior import GroupBehaviorUpdate
from zarya.dialogue import DialogueEngine
from zarya.memory_logic import invalidate
from zarya.openai_adapter import ModelResult
from zarya.operations import Operations
from zarya.source_material import purge
from zarya.telegram_runtime import TelegramRuntime


def test_numeric_only_and_clock_discontinuity(monkeypatch):
    raw = (
        '{"version":1,"model_ms":0,"queue_ms":null,"claim_ms":true,"prepare_ms":NaN,'
        '"postprocess_ms":Infinity,"telegram_ms":-2,"prompt":"SECRET","user_id":42}'
    )
    assert timings.clean(raw) == {"model_ms": 0}
    assert timings.clean("broken") == {}
    assert timings.clean('{"version":2,"model_ms":5}') == {}
    clock = {"wall": 1000, "mono": 10}
    monkeypatch.setattr(timings.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(timings.time, "monotonic", lambda: clock["mono"])
    anchor = timings.Clock()
    clock.update(wall=1002, mono=12)
    assert anchor.since(1001) == 1000
    assert anchor.since(999) is None  # Another process: do not invent intervals.
    clock.update(wall=1062, mono=12)
    assert anchor.since(1001) is None
    clock.update(wall=1002, mono=12)
    assert anchor.since(1001) is None  # Once discontinuous, remain invalid.


class SlowModel(FakeModel):
    async def generate(self, request):
        await asyncio.sleep(0.04)
        return await super().generate(request)


class SlowPhotos:
    async def attach(self, request, refs):
        await asyncio.sleep(0.08)
        return request

    async def valid(self, conn, batch):
        return True


async def ready_delivery(store):
    async with store.db.transaction() as conn:
        await conn.execute("UPDATE outbox SET next_attempt=0")
        await conn.execute("UPDATE delivery_limits SET last_attempt=0,blocked_until=0")


async def test_api_separate_from_preparation_live_first_part_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("zarya.dialogue.random.random", lambda: 0)
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        engine = DialogueEngine(store, SlowModel(ModelResult(text="Одна мысль\n\nДругая мысль")))
        work = await engine.claim("999")
        engine.photos = SlowPhotos()
        work["snapshot"]["photo_refs"] = [{"batch_id": 0}]
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        stages = detail["timing"]["stages"]
        assert stages["prepare_ms"] >= 70 and stages["model_ms"] >= 30
        assert detail["call"]["latency_ms"] == int(stages["model_ms"])
        assert "total_first_reply_ms" not in stages
        assert detail["timing"]["status"] == "pending"
        assert "timings" not in json.dumps(work["request"])
        assert "timings" not in json.dumps(detail["snapshot"])
        await ready_delivery(store)
        runtime = TelegramRuntime(store, transport=FakeTransport())
        await runtime.deliver_one("999")
        detail = await engine.details(work["run_id"])
        assert detail["timing"]["status"] == "complete"
        stages = detail["timing"]["stages"]
        assert stages["total_first_reply_ms"] > stages["model_ms"]
        assert all(key in stages for key in ("outbox_wait_ms", "telegram_ms", "result_write_ms"))
        first_total = stages["total_first_reply_ms"]
        await ready_delivery(store)
        await runtime.deliver_one("999")
        assert (await engine.details(work["run_id"]))["timing"]["stages"][
            "total_first_reply_ms"
        ] == first_total
        parts = await store.db.all("SELECT part_index,timings_json FROM outbox ORDER BY id")
        assert "total_first_reply_ms" not in timings.clean(parts[1][1])
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["timing"] == {"status": "test", "attempts": 0, "stages": {}}
        stats = (await Operations(store.db).overview(bot="999"))["response_timings"]
        assert stats["stages"]["total_first_reply_ms"]["samples"] == 1


async def test_retry_measurements_idempotent_and_unknown_not_retried(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        engine = DialogueEngine(store, FakeModel(ModelResult(text="Привет")))
        work = await run_one(engine)
        await ready_delivery(store)
        item = await store.claim_delivery("999")
        measurement = {"delivery_check_ms": 11, "telegram_ms": 13, "outbox_wait_ms": 15}
        await store.delivery_result(
            item["id"], "pending", "rate_limit", measured=measurement, attempt=1
        )
        first = (await store.db.one("SELECT timings_json FROM outbox"))[0]
        await store.delivery_result(item["id"], "pending", measured=measurement, attempt=1)
        assert (await store.db.one("SELECT timings_json FROM outbox"))[0] == first
        await ready_delivery(store)
        await store.claim_delivery("999")
        await store.delivery_result(
            item["id"],
            "sent",
            message_id=9,
            measured=measurement,
            attempt=1,
            confirmed_at=time.time(),
        )
        assert (await store.db.one("SELECT state FROM outbox"))[0] == "sending"
        await store.delivery_result(item["id"], "unknown", measured=measurement, attempt=2)
        detail = await engine.details(work["run_id"])
        assert detail["timing"]["attempts"] == 2
        assert detail["timing"]["stages"]["telegram_ms"] == 26
        assert detail["timing"]["stages"]["delivery_check_ms"] == 22
        assert detail["timing"]["stages"]["outbox_wait_ms"] == 15
        assert "total_first_reply_ms" not in detail["timing"]["stages"]
        assert await store.claim_delivery("999") is None
        stats = (await Operations(store.db).overview(bot="999"))["response_timings"]
        assert stats["retried"] == stats["partial"] == 1
        assert stats["stages"]["total_first_reply_ms"]["avg_ms"] is None


async def test_unknown_model_preserves_measured_api_without_delivery(tmp_path):
    class Uncertain(FakeModel):
        async def generate(self, request):
            raise RuntimeError("SECRET provider error")

    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [event(2, text="Привет")])
        engine = DialogueEngine(store, Uncertain())
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert "model_ms" in detail["timing"]["stages"]
        assert "telegram_ms" not in detail["timing"]["stages"]
        assert "SECRET" not in json.dumps(detail)
        assert await engine.claim("999") is None


@pytest.mark.parametrize("route", ["text", "reaction", "silent", "shadow"])
@pytest.mark.parametrize("method", ["memory", "source"])
async def test_durable_text_classification_survives_privacy_purge(
    tmp_path, monkeypatch, route, method
):
    monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.01)
    async with store_at(tmp_path) as store:
        chat = -100 if route == "shadow" else 42
        engine, _, behavior = await ready_engine(
            store,
            chat,
            action="text" if route == "shadow" else route,
            reaction="❤" if route == "reaction" else None,
        )
        if route == "shadow":
            await behavior.update_policy(
                "999", str(chat), GroupBehaviorUpdate(expected_version=0, mode="shadow")
            )
        await store.ingest("999", "zarya_test", [event(2, chat, "Спасибо за ответ")])
        work = await run_one(engine)
        before = (await Operations(store.db).overview(bot="999"))["response_timings"]
        assert before["operations"] == int(route == "text")
        assert timings.clean(
            (
                await store.db.one(
                    "SELECT timings_json FROM dialogue_runs WHERE id=?", (work["run_id"],)
                )
            )[0]
        )["live_text"] == int(route == "text")
        async with store.db.transaction() as conn:
            if method == "memory":
                await invalidate(conn, "999", str(chat))
            else:
                await purge(conn, "id=?", (work["run_id"],))
        assert (await Operations(store.db).overview(bot="999"))["response_timings"] == before
