from test_behavior import ready_engine
from test_dialogue import run_one
from test_telegram import event, store_at


async def test_behavior_response_outbox_and_replays_preserve_model_punctuation(tmp_path):
    draft = "Ну да — бывает, 2–3 раза."
    async with store_at(tmp_path) as store:
        engine, model, _ = await ready_engine(store, text=draft)
        await store.ingest("999", "zarya_test", [event(2, text="Заря, ну бывает же")])
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert detail["response"] == draft
        assert detail["snapshot"]["behavior"]["plan"]["text"] == detail["response"]
        assert detail["parts"][0]["text"] == detail["response"]
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["response"] == detail["response"] and len(model.calls) == 1
        paid = await engine.replay(work["run_id"], "paid", (await store.db.settings()).version)
        assert paid["response"] == detail["response"] and len(model.calls) == 2
