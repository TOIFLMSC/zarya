import json
import time
from unittest.mock import AsyncMock

import pytest
from test_dialogue import FakeModel, private_ready
from test_research import Fetch, ResearchModel
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.research import ResearchEngine

PUBLIC_POST = "https://x.com/i/status/1234567890123456789"
PUBLIC_ARTICLE = "https://www.reuters.com/business/example-board-reinstates-chief-executive-after-vote-2026-11-23/"
CORRECTION = "Подожди, его же сняли недавно и только сегодня вернули"
PRIVATE_NOTE = "Моя личная пометка ОЛЬХА-752"


async def start_post(store, url, coverage):
    await private_ready(store)
    dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Это пока только пост")))
    model = ResearchModel()
    dialogue.research = research = ResearchEngine(store, model, Fetch(coverage))
    posted = event(
        2,
        text=f"{url}?tracking=TOKEN-919\n{PRIVATE_NOTE}",
        forward_origin={"type": "hidden_user"},
    )
    await store.ingest("999", "zarya_test", [posted])
    first = await answer(dialogue, research, 2)
    assert first["snapshot"]["research"]["mode"] == "read"
    assert not model.calls
    await deliver(store, first, 1000)
    return dialogue, research, model, first


@pytest.mark.parametrize(
    ("text", "url", "coverage"),
    [
        (CORRECTION, PUBLIC_POST, "metadata_only"),
        ("А сегодня торги остановили по акциям или фьючерсам?", PUBLIC_POST, "metadata_only"),
        ("Стоп, речь про фьючерсы", PUBLIC_POST, "metadata_only"),
        ("Подожди, на самом деле вчера, а не сегодня", PUBLIC_POST, "metadata_only"),
        ("Стоп, он ушёл сам", PUBLIC_POST, "metadata_only"),
        ("Подожди, источник уже опроверг это", PUBLIC_POST, "metadata_only"),
        (
            CORRECTION + " https://example.org/docs/private-draft?key=PRIVATE-943",
            PUBLIC_POST,
            "metadata_only",
        ),
        (CORRECTION, PUBLIC_ARTICLE, "partial_text"),
    ],
)
async def test_fresh_news_reply_researches_public_post_again(tmp_path, text, url, coverage):
    async with store_at(tmp_path) as store:
        dialogue, research, model, first = await start_post(store, url, coverage)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    text=text + " " + PRIVATE_NOTE,
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        second = await answer(dialogue, research, 3)
        assert second["snapshot"]["research"]["mode"] == "search"
        assert first["snapshot"]["research_id"] != second["snapshot"]["research_id"]
        assert len(model.calls) == 1
        request = model.calls[0]
        payload = json.loads(request["input"])
        assert request["tools"][0]["type"] == "web_search" and request["tool_choice"] == "required"
        assert payload["provided_material"] == ""
        assert {row["url"] for row in payload["sources"]} == {url}
        assert "СМИ" in payload["question"] and "первоисточник" in payload["question"].lower()
        assert not any(
            value in request["input"]
            for value in (PRIVATE_NOTE, "TOKEN-919", "PRIVATE-943", "private-draft")
        )
        assert "recent_chat" not in payload and "memory" not in payload
        assert PRIVATE_NOTE in second["snapshot"]["incoming"]["text"]


@pytest.mark.parametrize(
    "barrier",
    ["opt_out", "casual", "old_reply", "wrong_message", "shared_document", "disabled"],
)
async def test_implicit_news_search_needs_recent_public_reply_and_permission(tmp_path, barrier):
    url = (
        "https://example.org/news/embargoed-board-draft"
        if barrier == "shared_document"
        else PUBLIC_POST
    )
    async with store_at(tmp_path) as store:
        dialogue, research, model, _ = await start_post(store, url, "partial_text")
        text, target = CORRECTION, 1000
        if barrier == "opt_out":
            text += ". Без интернета"
        if barrier == "casual":
            text = "Подожди, я сегодня ещё вернусь"
        if barrier == "wrong_message":
            target = 9876
        if barrier == "old_reply":
            async with store.db.transaction() as conn:
                await conn.execute(
                    "UPDATE recent_messages SET received_at=? WHERE message_id=?",
                    (time.time() - 49 * 3600, target),
                )
        if barrier == "disabled":
            current = await store.db.settings()
            await store.db.update_settings(
                current.version, current.settings.model_copy(update={"research_enabled": False})
            )
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    text=text,
                    reply_to_message={"message_id": target, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        second = await answer(dialogue, research, 3)
        assert second["snapshot"].get("research", {}).get("mode") != "search"
        assert not model.calls


async def test_queued_legacy_news_without_provenance_does_not_call_search(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, _ = await start_post(store, PUBLIC_POST, "metadata_only")
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    text=CORRECTION,
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        assert await dialogue.claim("999") == {"skip": True}
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE research_runs SET sources=? WHERE state='queued'",
                (json.dumps([{"requested_url": PUBLIC_POST}]),),
            )
        assert await research.claim("999") == {"skip": True}
        assert await store.db.one(
            "SELECT state,error_code FROM research_runs WHERE state='rejected'"
        ) == ("rejected", "legacy_news_recheck")
        assert not model.calls


async def test_origin_post_alone_does_not_confirm_the_event(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, _ = await start_post(store, PUBLIC_POST, "metadata_only")
        model.generate = AsyncMock(
            return_value=ModelResult(
                text=json.dumps(
                    {
                        "summary": "Решение точно вступило в силу",
                        "claims": [
                            {
                                "text": "Решение вступило в силу",
                                "kind": "fact",
                                "verdict": "supported",
                                "urls": [PUBLIC_POST],
                            }
                        ],
                    }
                ),
                search_actions=[
                    {
                        "type": "search",
                        "status": "completed",
                        "sources": [{"url": PUBLIC_POST}],
                    }
                ],
            )
        )
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    text=CORRECTION,
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        work = await answer(dialogue, research, 3)
        result = work["snapshot"]["research"]["analysis"]
        assert model.generate.await_count == 2
        assert result["followup"] == "completed"
        assert result["claims"][0]["verdict"] == "uncertain"
        assert "нет независимого подтверждения" in result["summary"]
        assert "точно" not in result["summary"] + result["followup_summary"]


async def test_fresh_news_second_search_does_not_send_local_redirect(tmp_path):
    redirect = "https://tass.ru/politika/1234567"
    independent = "https://example.org/company/statement"
    async with store_at(tmp_path) as store:
        dialogue, research, model, _ = await start_post(store, PUBLIC_ARTICLE, "partial_text")
        research.fetcher.fetch = AsyncMock(
            return_value={
                "url": redirect,
                "title": "Персональная метка для адресата",
                "text": PRIVATE_NOTE,
                "coverage": "partial_text",
                "error": None,
            }
        )
        model.generate = AsyncMock(
            return_value=ModelResult(
                text=json.dumps({"summary": "Нужны источники", "claims": []}),
                search_actions=[
                    {
                        "type": "search",
                        "status": "completed",
                        "sources": [{"url": independent}],
                    }
                ],
            )
        )
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    text=CORRECTION,
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        await answer(dialogue, research, 3)
        first, second = [json.loads(c.args[0]["input"]) for c in model.generate.await_args_list]
        assert first["sources"][0] == {"id": 1, "url": PUBLIC_ARTICLE, "title": ""}
        assert second["public_urls"] == [PUBLIC_ARTICLE, independent]
