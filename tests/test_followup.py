"""Request/replay contracts; semantic quality additionally needs a real-model check."""

import json

from test_dialogue import FakeModel, private_ready, run_one
from test_prompts import bundle
from test_source_material import deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.dialogue_logic import followup_policy
from zarya.openai_adapter import ModelResult


async def test_followup_keeps_new_constraint_sent_advice_and_calls_model_again(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        store.prompts = bundle("CUSTOM_IDENTITY")
        previous = "Посмотри сохранённый конфиг и резервную копию."
        model = FakeModel(ModelResult(text=previous))
        engine = DialogueEngine(store, model)
        await store.ingest("999", "zarya_test", [event(2, text="Как восстановить настройки?")])
        first = await run_one(engine)
        await deliver(store, first, 100)
        question = "Нет ни конфига, ни резервной копии, как поступать?"
        await store.ingest(
            "999",
            "zarya_test",
            [event(101, text=question, reply_to_message={"message_id": 100, "from": {"id": 999}})],
        )
        model.result = ModelResult(text="Сначала определим, работает ли ещё приложение.")
        second = await run_one(engine)
        request = model.calls[-1]
        payload = json.loads(request["input"])
        assert payload["current_message"]["text"] == question
        assert previous in [item.get("text") for item in payload["recent_chat"]]
        assert "CUSTOM_IDENTITY" in request["instructions"]
        assert followup_policy() in request["instructions"]
        assert len(model.calls) == 2 and first["run_id"] != second["run_id"]
        detail = await engine.details(second["run_id"])
        assert detail["state"] == "completed" and detail["response"] == model.result.text
        assert detail["snapshot"]["context_budget"]["excluded"] == []

        recorded = await engine.replay(second["run_id"], "recorded")
        assert recorded["response"] == detail["response"] and len(model.calls) == 2
        paid = await engine.replay(second["run_id"], "paid", (await store.db.settings()).version)
        assert paid["state"] == "completed" and len(model.calls) == 3
        assert followup_policy() in model.calls[-1]["instructions"]
        assert json.loads(model.calls[-1]["input"])["current_message"]["text"] == question
