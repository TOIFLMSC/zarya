import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_telegram import store_at

from zarya.openai_adapter import ModelResult

URL = "https://example.org/regulator/topic/"


def analysis():
    return {
        "summary": "Нужное разъяснение находится в разделе вопросов и ответов.",
        "claims": [{"text": "Разъяснение", "kind": "fact", "verdict": "supported", "urls": [URL]}],
    }


def action(status="completed", url=URL):
    return {"type": "search", "status": status, "sources": [{"url": url}]}


async def test_blocked_article_multihop_primary_source_reaches_requested_reply(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(
            store, "Найди ссылку на разъяснение из https://example.org/news/123"
        )
        fetch.fetch = AsyncMock(
            return_value={
                "requested_url": "https://example.org/news/123",
                "url": None,
                "text": "",
                "coverage": "unavailable",
                "error": "http_403",
            }
        )
        model.generate = AsyncMock(
            return_value=ModelResult(
                text=json.dumps(analysis()),
                search_actions=[
                    action(url="https://example.org/news"),
                    {
                        "type": "open_page",
                        "status": "completed",
                        "url": "https://example.org/news/123",
                    },
                    action(),
                    {"type": "open_page", "status": "completed", "url": URL},
                ],
            )
        )
        await research.execute(work)
        request = model.generate.call_args.args[0]
        assert request["max_tool_calls"] == 6
        assert request["model"] == work["model"] and request["max_output_tokens"] == 3000
        assert "Не повторяй одинаковое неудачное открытие" in request["instructions"]
        assert "Цель исследования задаёт текущий question" in request["instructions"]
        assert "не трать поиск на прежнюю тему вместо нового вопроса" in request["instructions"]
        assert set(json.loads(request["input"])) == {"question", "provided_material", "sources"}
        detail = await research.details(work["id"], "999")
        assert detail["state"] == "completed"
        identifier = detail["result"]["claims"][0]["source_ids"][0]
        model.generate.return_value = ModelResult(text=f"Разъяснение в FAQ [S{identifier}]")
        reply = await run_one(dialogue)
        answer = await dialogue.details(reply["run_id"])
        assert answer["state"] == "completed" and URL in answer["response"]
        assert "проверку не удалось завершить" in reply["request"]["instructions"]
        assert (await research.listing("999", "all", "", 0))["max_tool_calls"] == 6
        assert model.generate.await_count == 2  # One search and one dialogue; no retry.


@pytest.mark.parametrize(
    "case,reason",
    [
        ("json", "json"),
        ("schema", "schema"),
        ("unknown", "unknown_source"),
        ("uncited", "uncited_claim"),
        ("unfinished_only", "no_completed_search"),
        ("unfinished_source", "unknown_source"),
        ("too_long", "output_limit"),
    ],
)
async def test_evidence_failure_categories_do_not_retain_rejected_text_or_retry(
    tmp_path, case, reason
):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(store)
        data = analysis()
        actions = [action()]
        if case == "schema":
            data["claims"][0]["kind"] = "unsupported_kind"
        if case in {"unknown", "uncited"}:
            data["claims"][0]["urls"] = ["https://unobserved.example/"] if case == "unknown" else []
        if case == "unfinished_only":
            actions = [action(status="searching")]
            data["claims"] = []
        if case == "unfinished_source":
            actions = [action(url="https://other.example/"), action(status="searching")]
        if case == "too_long":
            data["summary"] = "x" * 2501
        rejected_text = "NOT_JSON_PRIVATE_SENTINEL" if case == "json" else json.dumps(data)
        model.generate = AsyncMock(
            return_value=ModelResult(text=rejected_text, search_actions=actions)
        )
        await research.execute(work)
        detail = await research.details(work["id"], "999")
        assert detail["state"] == "invalid" and detail["error"] == "invalid_evidence_" + reason
        assert detail["result"] is None and "PRIVATE_SENTINEL" not in json.dumps(detail)
        if case == "unfinished_source":
            assert URL not in {s.get("url") for s in detail["sources"]}
        assert model.generate.await_count == 1 and await research.claim("999") is None


async def test_unused_unfinished_action_does_not_discard_verified_claim(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(store)
        model.generate = AsyncMock(
            return_value=ModelResult(
                text=json.dumps(analysis()),
                search_actions=[action(), action("searching", "https://unfinished.example/")],
            )
        )
        await research.execute(work)
        detail = await research.details(work["id"], "999")
        assert detail["state"] == "completed"
        assert "https://unfinished.example/" not in {s.get("url") for s in detail["sources"]}
