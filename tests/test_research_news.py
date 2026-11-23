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
from zarya.research_dates import claim_dates
from zarya.research_logic import research_answer_text

PUBLIC_POST = "https://x.com/i/status/1234567890123456789"
PUBLIC_ARTICLE = "https://www.reuters.com/business/example-board-reinstates-chief-executive-after-vote-2026-11-23/"
CORRECTION = "Подожди, его же сняли недавно и только сегодня вернули"
PRIVATE_NOTE = "Моя личная пометка ОЛЬХА-752"


@pytest.mark.parametrize(
    ("draft", "kept", "followup"),
    [
        ("Его восстановили 23 ноября", False, False),
        ("Его восстановили 23.11.2026. [RESEARCH_DATES]", False, False),
        ("Его восстановили сегодня. [RESEARCH_DATES]", False, False),
        ("Его восстановили 23-го. Да, вопрос с датами важен.", True, True),
        ("Да, вопрос с датами важен. [RESEARCH_DATES]", True, False),
    ],
)
async def test_news_reply_keeps_event_and_publication_dates_distinct(
    tmp_path, draft, kept, followup
):
    record = "https://example.org/board/decision"
    announcement = "https://example.com/news/announcement"
    async with store_at(tmp_path) as store:
        dialogue, research, model, _ = await start_post(store, PUBLIC_POST, "metadata_only")
        first_pass = ModelResult(
            text=json.dumps({"summary": "Ищу объявление", "claims": [], "needs_followup": True}),
            search_actions=[
                {"type": "search", "status": "completed", "sources": [{"url": record}]}
            ],
        )
        result = ModelResult(
            text=json.dumps(
                {
                    "summary": "Решение принято 22 ноября, сообщение вышло 23 ноября",
                    "claims": [
                        {
                            "text": (
                                "Совет восстановил директора 22 ноября, компания сообщила 23 ноября"
                            ),
                            "kind": "fact",
                            "verdict": "supported",
                            "urls": [record, announcement],
                            "dates": {
                                "subject": "Возвращение директора",
                                "event": {"date": "2026-11-22", "url": record},
                                "publication": {"date": "2026-11-23", "url": announcement},
                            },
                        }
                    ],
                }
            ),
            search_actions=[
                {
                    "type": "search",
                    "status": "completed",
                    "sources": [{"url": announcement}]
                    if followup
                    else [{"url": record}, {"url": announcement}],
                }
            ],
        )
        model.generate = AsyncMock(side_effect=[first_pass, result] if followup else [result])
        dialogue.adapter = FakeModel(ModelResult(text=draft))
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
        parts = await store.db.all(
            "SELECT payload FROM outbox WHERE job_id=? ORDER BY part_index", (work["job_id"],)
        )
        sent = "\n".join(json.loads(payload)["text"] for (payload,) in parts)
        assert "22 ноября 2026" in sent and "23 ноября 2026" in sent
        assert "Его восстановили" not in sent
        assert "[RESEARCH_DATES]" not in sent and "https://" not in sent
        assert ("Да, вопрос с датами важен." in sent) is kept
        assert "Вставь [RESEARCH_DATES]" in work["snapshot"]["request"]["instructions"]
        context = work["snapshot"]["research"]
        with_links = research_answer_text("[RESEARCH_DATES]", context, show_links=True)
        assert record in with_links and announcement in with_links
        claim = context["analysis"]["claims"][0]
        assert claim["dates"]["event"]["source_id"] != claim["dates"]["publication"]["source_id"]
        assert model.generate.await_count == (2 if followup else 1)


@pytest.mark.parametrize("invalid", ["bad_day", "unknown_url", "missing_event", "dated_subject"])
def test_dates_need_a_valid_source_for_each_role(invalid):
    record = "https://example.org/decision"
    release = "https://example.com/announcement"
    dates = {
        "subject": "Возвращение директора",
        "event": {"date": "2026-11-22", "url": record},
        "publication": {"date": "2026-11-23", "url": release},
    }
    if invalid == "bad_day":
        dates["event"]["date"] = "2026-11-31"
    elif invalid == "unknown_url":
        dates["event"]["url"] = "https://example.org/unproven"
    elif invalid == "missing_event":
        del dates["event"]
    else:
        dates["subject"] = "Возвращение директора 21 ноября"
    assert claim_dates(dates, {record: 1, release: 2}) is None


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
                                "dates": {
                                    "subject": "Вступление решения в силу",
                                    "event": {"date": "2026-11-22", "url": PUBLIC_POST},
                                    "publication": {"date": "2026-11-23", "url": PUBLIC_POST},
                                },
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
        assert "dates" not in result["claims"][0]
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
