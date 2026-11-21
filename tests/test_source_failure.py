import copy
import json
from unittest.mock import AsyncMock

import pytest
from test_behavior import enable, result
from test_dialogue import FakeModel
from test_research import research_ready
from test_telegram import FakeTransport, event, store_at

from zarya.openai_adapter import ModelResult
from zarya.research_logic import evidence_text, source_failure_text
from zarya.response_links import source_policy
from zarya.telegram_runtime import TelegramRuntime

URL = "https://www.cbr.ru/develop/cryptocurrency/"
SOURCES = [
    {"id": 33, "url": URL, "coverage": "search_source", "error": None},
    {"id": 1, "url": None, "coverage": "unavailable", "error": "http_403"},
    {"id": 2, "url": "https://example.org/unrelated", "error": None},
]
ANALYSIS = {
    "summary": "Do not copy https://unobserved.example/",
    "claims": [
        {"source_ids": [33], "urls": ["https://unobserved.example/"], "text": "Untrusted prose"}
    ],
}


async def prepared(store, *, ask=True, behavior=False):
    if behavior:
        await enable(store)
    dialogue, research, model, fetch, search = await research_ready(
        store, "Найди мне ссылку на разъяснения регулятора" if ask else "Проверь новость"
    )
    await research.execute(search)
    async with store.db.transaction() as conn:
        await conn.execute(
            "UPDATE research_runs SET sources=?,result=? WHERE id=?",
            (json.dumps(SOURCES), json.dumps(ANALYSIS), search["id"]),
        )
    work = await dialogue.claim("999")
    assert work and not work["skip"]
    return dialogue, work


@pytest.mark.parametrize("text", ["Вот [S1]", "Вот https://invented.example/", "Вот [S999]"])
@pytest.mark.parametrize("behavior", [False, True])
async def test_invalid_synthesis_delivers_verified_link_without_retry(tmp_path, text, behavior):
    async with store_at(tmp_path) as store:
        engine, work = await prepared(store, behavior=behavior)
        outcome = (
            result(text=text)
            if behavior
            else ModelResult(text=text, usage={"input_tokens": 100, "output_tokens": 10})
        )
        model = FakeModel(outcome)
        engine.adapter = model
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "invalid" and detail["error"] == "unknown_source"
        assert not detail["response"] and detail["call"]["cost_usd"] > 0
        assert len(detail["parts"]) == 1 and URL in detail["parts"][0]["text"]
        assert "invented" not in detail["parts"][0]["text"]
        assert "unobserved" not in detail["parts"][0]["text"]
        assert "unrelated" not in detail["parts"][0]["text"]
        if behavior:
            assert "plan" not in detail["snapshot"]["behavior"]
        await engine.finish(work, outcome, 0)
        assert len((await engine.details(work["run_id"]))["parts"]) == 1
        assert await engine.claim("999") is None and len(model.calls) == 1
        with pytest.raises(ValueError, match="нет сохранённого ответа"):
            await engine.replay(work["run_id"], "recorded")
        transport = FakeTransport()
        await TelegramRuntime(store, dialogue=engine, transport=transport).deliver_one("999")
        assert len(transport.sent) == 1 and URL in transport.sent[0][1]


async def test_correct_nonsequential_source_id_preserves_normal_answer(tmp_path):
    async with store_at(tmp_path) as store:
        engine, work = await prepared(store)
        engine.adapter = FakeModel(ModelResult(text="Вот разъяснения [S33]"))
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert detail["state"] == "completed" and not detail["error"]
        assert URL in detail["response"]
        assert "id=33" in source_policy(True)
        assert evidence_text("Вот [S33]", SOURCES) == "Вот  (" + URL + ")"


async def test_service_does_not_send_unrequested_links(tmp_path):
    async with store_at(tmp_path) as store:
        engine, work = await prepared(store, ask=False)
        engine.adapter = FakeModel(ModelResult(text="Вывод [S999]"))
        await engine.execute(work)
        detail = await engine.details(work["run_id"])
        assert len(detail["parts"]) == 1 and "http" not in detail["parts"][0]["text"]


@pytest.mark.parametrize("when", ["during_generation", "before_delivery"])
async def test_source_edit_cancels_service_reply(tmp_path, when):
    async with store_at(tmp_path) as store:
        engine, work = await prepared(store)

        async def edit():
            update = event(3, text="Исправленный вопрос")
            update["edited_message"] = update.pop("message")
            update["edited_message"]["message_id"] = 2
            await store.ingest("999", "zarya_test", [update])

        async def generate(request):
            if when == "during_generation":
                await edit()
            return ModelResult(text="Вот [S999]")

        engine.adapter = FakeModel()
        engine.adapter.generate = AsyncMock(side_effect=generate)
        await engine.execute(work)
        if when == "before_delivery":
            assert len((await engine.details(work["run_id"]))["parts"]) == 1
            await edit()
        transport = FakeTransport()
        await TelegramRuntime(store, dialogue=engine, transport=transport).deliver_one("999")
        assert not transport.sent
        assert not await store.db.all("SELECT id FROM outbox WHERE state='pending'")


@pytest.mark.parametrize("mode", ["paid", "shadow", "live"])
async def test_no_service_delivery_for_replay_or_initiative(tmp_path, monkeypatch, mode):
    async with store_at(tmp_path) as store:
        engine, work = await prepared(store, behavior=mode != "paid")
        if mode == "paid":
            work["mode"] = "paid"
        else:
            work["snapshot"]["behavior"]["mode"] = mode
            monkeypatch.setattr("zarya.behavior.valid", AsyncMock(return_value=True))
        outcome = ModelResult(state="invalid", error="unknown_source")
        await engine.finish(work, outcome, 0)
        assert not (await engine.details(work["run_id"]))["parts"]


def test_fallback_selects_only_claim_bound_public_urls_and_keeps_whole_links():
    research = {
        "state": "completed",
        "sources": copy.deepcopy(SOURCES),
        "analysis": copy.deepcopy(ANALYSIS),
    }
    research["sources"].extend(
        [
            {"id": 4, "url": "http://127.0.0.1/"},
            {"id": 5, "url": "https://example.org/error", "error": "http_403"},
            {"id": 6, "url": "https://example.org/" + "a" * 1900},
            {"id": 7, "url": "https://example.org/" + "b" * 1900},
            {"id": 8, "url": "https://example.org/short"},
            {"id": 9, "url": "https://example.org/fourth"},
        ]
    )
    research["analysis"]["claims"][0]["source_ids"] += [33, 999, 1, 4, 5, 6, 7, 8, 9]
    text = source_failure_text(research, True)
    assert text.count(URL) == 1 and len(text.encode("utf-16-le")) // 2 <= 3500
    assert text.count("https://") == 3 and "short" in text and "fourth" not in text
    assert "a" * 1900 in text and "b" * 100 not in text
    assert "127.0.0.1" not in text and "error" not in text and "unobserved" not in text
    assert "Untrusted" not in text and "unrelated" not in text
    research["state"] = "unknown"
    assert "http" not in source_failure_text(research, True)
    research["state"] = "completed"
    assert "http" not in source_failure_text(research, False)
