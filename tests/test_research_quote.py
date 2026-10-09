import json

import pytest
from test_photos import ready
from test_research import Fetch, ResearchModel
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.research import ResearchEngine
from zarya.research_quote import Quote, advance


@pytest.mark.parametrize(
    "text,previous,expected",
    [
        ("Заря, скок биток ща?", None, Quote("BTC", "USD")),
        ("Цена HYPE в тенге?", None, Quote("HYPE", "KZT")),
        ("а hype?", Quote("BTC", "USD"), Quote("HYPE", "USD")),
        ("а в тенге?", Quote("HYPE", "USD"), Quote("HYPE", "KZT")),
        ("а $pepe?", Quote("BTC", "KZT"), Quote("PEPE", "KZT")),
        ("а XYZ?", Quote("BTC", "USD"), Quote("XYZ", "USD")),
        ("Сколько стоит токен pepe?", None, Quote("PEPE", "USD")),
        ("а hype?", None, None),
        ("а python?", Quote("BTC", "USD"), None),
        ("да, он", Quote("BTC", "USD"), None),
        ("как дела?", Quote("BTC", "USD"), None),
        ("Цена BTC вчера", None, None),
        ("Почему растёт цена BTC?", None, None),
        ("Сколько будет стоить HYPE завтра?", None, None),
        ("Сколько биткоинов у Сатоши?", None, None),
        ("Цена HYPE в USD и KZT?", None, None),
    ],
)
def test_quote_intent(text, previous, expected):
    assert advance(text, previous) == expected


class Model(ResearchModel):
    clarification = False

    async def generate(self, request):
        if self.clarification and not request.get("tools"):
            return ModelResult(text="HYPE - токен Hyperliquid? свежую цену щас не вижу")
        return await super().generate(request)


def reply(number, text, target, **kwargs):
    return event(
        number,
        -100,
        text,
        reply_to_message={"message_id": target, "from": {"id": 999, "is_bot": True}},
        **kwargs,
    )


async def setup_chain(store):
    await ready(store, -100)
    model = Model()
    dialogue = DialogueEngine(store, model)
    research = ResearchEngine(store, model, Fetch())
    dialogue.research = research
    await store.ingest("999", "zarya_test", [event(2, -100, "Заря, скок биток ща?")])
    first = await answer(dialogue, research, 2)
    await deliver(store, first, 1000)
    return dialogue, research, model


async def test_exact_reply_switch_isolated_from_other_author_currency_and_private_material(
    tmp_path,
):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        other = event(3, -100, "Заря, скок биток ща в тенге?")
        other["message"]["from"]["id"] = 43
        await store.ingest("999", "zarya_test", [other])
        another = await answer(dialogue, research, 3)
        await deliver(store, another, 1001)
        await store.ingest("999", "zarya_test", [reply(4, "а hype?", 1000)])
        result = await answer(dialogue, research, 4)
        detail = await research.details(result["snapshot"]["research_id"], "999")
        assert "HYPE (Hyperliquid) в USD" in detail["question"]
        assert detail["material"] == ""
        request = next(r for r in reversed(model.calls) if r.get("tools"))
        assert json.loads(request["input"])["provided_material"] == ""
        assert "тенге" not in request["input"] and "recent_chat" not in request["input"]
        # The reply in the other branch uses that branch's currency.
        await store.ingest("999", "zarya_test", [reply(5, "а hype?", 1001)])
        result = await answer(dialogue, research, 5)
        detail = await research.details(result["snapshot"]["research_id"], "999")
        assert "HYPE (Hyperliquid) в KZT" in detail["question"]


@pytest.mark.parametrize("clarification", [True, False])
async def test_confirmation_only_after_actual_sent_instrument_clarification(
    tmp_path, clarification
):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        model.clarification = clarification
        await store.ingest("999", "zarya_test", [reply(3, "а hype?", 1000)])
        second = await answer(dialogue, research, 3)
        await deliver(store, second, 1001)
        model.clarification = False
        await store.ingest("999", "zarya_test", [reply(4, "да, он", 1001)])
        final = await answer(dialogue, research, 4)
        assert bool(final["snapshot"].get("research_id")) == clarification
        if clarification:
            details = await research.details(final["snapshot"]["research_id"], "999")
            assert "HYPE (Hyperliquid) в USD" in details["question"]


@pytest.mark.parametrize("barrier", ["topic", "unsent", "changed", "unrelated"])
async def test_quote_context_does_not_cross_invalid_or_changed_reply_chain(tmp_path, barrier):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        target = 1000
        if barrier == "unsent":
            async with store.db.transaction() as conn:
                await conn.execute("UPDATE outbox SET state='unknown' WHERE message_id=1000")
        elif barrier == "changed":
            edit = event(3, -100, "Заря, привет")
            edit["edited_message"] = edit.pop("message")
            edit["edited_message"]["message_id"] = 2
            await store.ingest("999", "zarya_test", [edit])
            await dialogue.claim("999")
        elif barrier == "unrelated":
            await store.ingest("999", "zarya_test", [reply(3, "Как дела?", 1000)])
            changed = await answer(dialogue, research, 3)
            await deliver(store, changed, 1001)
            target = 1001
        incoming = reply(4, "а hype?", target)
        if barrier == "topic":
            incoming["message"]["chat"]["is_forum"] = True
            incoming["message"]["message_thread_id"] = 33
        await store.ingest("999", "zarya_test", [incoming])
        result = await answer(dialogue, research, 4)
        assert not result["snapshot"].get("research_id")
