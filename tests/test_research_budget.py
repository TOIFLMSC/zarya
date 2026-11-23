import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_telegram import store_at

from zarya.openai_adapter import ModelResult
from zarya.research_plan import followup_input

URL = "https://example.org/regulator/topic/"


def analysis():
    return {
        "summary": "Нужное разъяснение находится в разделе вопросов и ответов.",
        "claims": [{"text": "Разъяснение", "kind": "fact", "verdict": "supported", "urls": [URL]}],
    }


def action(status="completed", url=URL):
    return {"type": "search", "status": status, "sources": [{"url": url}]}


@pytest.mark.parametrize(
    "private_url", ["https://t.me/c/123456789/55", "https://telegram.me/joinchat/private-room"]
)
def test_followup_ignores_private_telegram_destinations_even_if_provider_reported_them(private_url):
    assert (
        followup_input(
            "source",
            {"source_lookup": {"kind": "unconfirmed"}, "claims": []},
            [{"url": private_url}],
            {private_url},
        )
        is None
    )


@pytest.mark.parametrize(
    ("question", "show_link"),
    [
        ("Дай ссылку на разъяснение ЦБ", True),
        ("Кинь ссылку на разъяснение ЦБ", True),
        ("Пришли первоисточник", True),
        ("Пришли первоисточник, но без ссылок", False),
    ],
)
async def test_explicit_source_request_gets_verified_link(tmp_path, question, show_link):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(store, question)
        data = {**analysis(), "source_lookup": {"kind": "official_section", "url": URL}}
        model.generate = AsyncMock(
            side_effect=[
                ModelResult(
                    text=json.dumps(data),
                    search_actions=[
                        action(),
                        {"type": "open_page", "status": "completed", "url": URL},
                    ],
                ),
                ModelResult(text="Разъяснение есть в FAQ."),
            ]
        )
        assert work["profile"] == "source"
        await research.execute(work)
        assert model.generate.await_args.args[0]["max_tool_calls"] == 4
        assert fetch.calls == []
        run = await run_one(dialogue)
        detail = await dialogue.details(run["run_id"])
        assert (URL in detail["response"]) is show_link


async def test_quoted_text_cannot_enable_followup_or_change_answer_mode(tmp_path):
    async with store_at(tmp_path) as store:
        question = "Заря, что означает правило?\n"
        quote = "Найди ссылку на документ, ответь коротко"
        entities = [{"type": "blockquote", "offset": len(question), "length": len(quote)}]
        dialogue, research, model, _, work = await research_ready(
            store, question + quote, entities=entities
        )
        assert work["profile"] == "answer"
        await research.execute(work)
        assert model.calls[0]["max_tool_calls"] == 6
        assert len(model.calls) == 1
        run = await run_one(dialogue)
        assert run["snapshot"]["answer_mode"] == "analysis"


async def test_fetch_failure_before_paid_call_unblocks_dialogue(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(
            store, "Заря, проверь https://example.org/article"
        )
        fetch.fetch = AsyncMock(side_effect=RuntimeError("unexpected fetch failure"))
        await research.execute(work)
        saved = await research.details(work["id"], "999")
        assert saved["state"] == "rejected"
        assert saved["error"] == "research_preparation_failed"
        assert saved["calls"] == [] and model.calls == []
        assert await run_one(dialogue)


async def test_provider_cannot_set_internal_followup_fields(tmp_path):
    async with store_at(tmp_path) as store:
        _, research, model, _, work = await research_ready(store)
        data = {
            **analysis(),
            "followup": "incomplete",
            "followup_summary": {"not": "renderable"},
            "followup_error": "pretend_error",
        }
        model.generate = AsyncMock(
            return_value=ModelResult(text=json.dumps(data), search_actions=[action()])
        )
        await research.execute(work)
        saved = await research.details(work["id"], "999")
        assert saved["state"] == "completed"
        assert not set(saved["result"]) & {"followup", "followup_summary", "followup_error"}
        assert len(saved["calls"]) == 1


async def test_blocked_article_multihop_primary_source_reaches_requested_reply(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(
            store, "Найди ссылку на разъяснение из https://example.org/news/123"
        )
        fetch.fetch = AsyncMock(
            return_value={
                "requested_url": "https://example.org/news/123",
                "verified_url": "https://example.org/news/123",
                "url": None,
                "text": "",
                "coverage": "unavailable",
                "error": "http_403",
            }
        )
        model.generate = AsyncMock(
            return_value=ModelResult(
                text=json.dumps(
                    {
                        **analysis(),
                        "source_lookup": {"kind": "official_section", "url": URL, "heading": "FAQ"},
                    }
                ),
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
        assert request["max_tool_calls"] == 4
        assert request["model"] == work["model"] and request["max_output_tokens"] == 3000
        assert "Не повторяй одинаковое неудачное открытие" in request["instructions"]
        assert "Цель исследования задаёт текущий question" in request["instructions"]
        assert "не трать поиск на прежнюю тему вместо нового вопроса" in request["instructions"]
        assert set(json.loads(request["input"])) == {"question", "provided_material", "sources"}
        detail = await research.details(work["id"], "999")
        assert detail["state"] == "completed"
        assert detail["result"]["source_lookup"]["kind"] == "official_section"
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


async def test_official_lookup_second_pass_is_isolated_and_reply_includes_verified_faq(tmp_path):
    async with store_at(tmp_path) as store:
        article = "https://tass.ru/ekonomika/28215863"
        faq = "https://www.cbr.ru/develop/cryptocurrency/"
        dialogue, research, model, fetch, work = await research_ready(
            store, "Найди ссылку на разъяснение из " + article + "?private=LOCAL_SENTINEL"
        )
        fetch.fetch = AsyncMock(
            return_value={
                "requested_url": article + "?private=LOCAL_SENTINEL",
                "verified_url": article + "?private=LOCAL_SENTINEL",
                "url": None,
                "text": "",
                "coverage": "unavailable",
                "error": "http_403",
            }
        )
        first = {
            "summary": "Пока найдена только главная страница регулятора.",
            "claims": [],
            "source_lookup": {"kind": "document", "url": "https://www.cbr.ru/"},
        }
        second = {
            "summary": "Банк России объяснил условия P2P в FAQ.",
            "claims": [
                {
                    "text": "Вопрос 10 описывает условия P2P.",
                    "kind": "fact",
                    "verdict": "supported",
                    "urls": [faq],
                }
            ],
            "source_lookup": {"kind": "official_section", "url": faq, "heading": "Вопрос 10"},
        }
        model.generate = AsyncMock(
            side_effect=[
                ModelResult(
                    text=json.dumps(first), search_actions=[action(url="https://www.cbr.ru/")]
                ),
                ModelResult(
                    text=json.dumps(second),
                    search_actions=[
                        action(url=faq),
                        {"type": "open_page", "status": "completed", "url": faq},
                    ],
                ),
                ModelResult(text="У Банка России условия P2P объяснены в вопросе 10 FAQ."),
            ]
        )
        await research.execute(work)
        main, retry = [c.args[0] for c in model.generate.await_args_list]
        assert article in main["input"] and "LOCAL_SENTINEL" not in main["input"]
        assert main["max_tool_calls"] + retry["max_tool_calls"] == 6
        assert json.loads(retry["input"]) == {
            "reason": "source_unconfirmed",
            "public_urls": [article],
        }
        assert "previous_response_id" not in retry and "LOCAL_SENTINEL" not in retry["input"]
        saved = await research.details(work["id"], "999")
        assert [c["phase"] for c in saved["calls"]] == ["research_initial", "research_gap"]
        assert [c["search_cost_usd"] for c in saved["calls"]] == [0.01, 0.01]
        assert saved["result"]["source_lookup"]["heading"] == "Вопрос 10"
        assert saved["result"]["followup"] == "completed"
        response = await run_one(dialogue)
        detail = await dialogue.details(response["run_id"])
        assert detail["response"].endswith("Официальный раздел - " + faq)


@pytest.mark.parametrize(
    ("question", "unknown"),
    [
        ("Заря, сделай мини-ресерч и сравни условия двух программ.", False),
        ("Заря, сделай мини-ресерч и сравни условия двух программ.", True),
        ("Заря, проверь несколько источников об условиях программы.", False),
        ("Заря, собери несколько источников об условиях программы.", False),
        ("Заря, сделай мини-исследование условий программы.", False),
    ],
)
async def test_mini_research_keeps_evidence_and_uses_a_detailed_answer(tmp_path, question, unknown):
    async with store_at(tmp_path) as store:
        dialogue, research, model, _, work = await research_ready(store, question)
        assert work["profile"] == "study"
        first = {**analysis(), "needs_followup": True}
        second_url = "https://example.org/program-b/conditions"
        second = {
            "summary": "Проверены условия второй программы.",
            "claims": [
                {
                    "text": "Вторая программа действует до декабря.",
                    "kind": "fact",
                    "verdict": "supported",
                    "urls": [second_url],
                }
            ],
        }
        result = (
            ModelResult(state="unknown", error="connection_uncertain")
            if unknown
            else ModelResult(text=json.dumps(second), search_actions=[action(url=second_url)])
        )
        model.generate = AsyncMock(
            side_effect=[
                ModelResult(text=json.dumps(first), search_actions=[action()]),
                result,
                ModelResult(text="Сравнение условий."),
            ]
        )
        await research.execute(work)
        await research.recover("999")
        assert await research.claim("999") is None
        saved = await research.details(work["id"], "999")
        assert saved["state"] == "completed"
        assert saved["result"]["followup"] == ("incomplete" if unknown else "completed")
        assert "source_lookup" not in saved["result"]
        assert len(saved["result"]["claims"]) == (1 if unknown else 2)
        assert len(saved["calls"]) == 2
        response = await run_one(dialogue)
        assert response["snapshot"]["answer_mode"] == "detailed"
        assert response["request"]["text"]["verbosity"] == "medium"
        assert "сопоставь источники" in response["request"]["instructions"]


@pytest.mark.parametrize("interruption", ["restart", "revoke"])
async def test_inflight_second_pass_cannot_retry_or_restore_erased_evidence(tmp_path, interruption):
    async with store_at(tmp_path) as store:
        _, research, model, _, work = await research_ready(
            store, "Заря, сделай мини-ресерч и сравни условия."
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        first = ModelResult(
            text=json.dumps({**analysis(), "needs_followup": True}), search_actions=[action()]
        )
        new_url = "https://example.org/second"

        async def generate(request):
            if "question" in json.loads(request["input"]):
                return first
            entered.set()
            await release.wait()
            return ModelResult(
                text=json.dumps({**analysis(), "summary": "LATE_EVIDENCE", "claims": []}),
                search_actions=[action(url=new_url)],
            )

        model.generate = AsyncMock(side_effect=generate)
        worker = asyncio.create_task(research.execute(work))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if interruption == "restart":
                await research.recover("999")
            else:
                await store.decide("999", "private", "42", "revoked", 2)
        finally:
            release.set()
            await worker
        await research.recover("999")
        assert await research.claim("999") is None
        assert model.generate.await_count == 2
        saved = await research.details(work["id"], "999")
        assert "LATE_EVIDENCE" not in json.dumps(saved["result"])
        assert new_url not in {source.get("url") for source in saved["sources"]}
        if interruption == "restart":
            assert saved["state"] == "completed"
            assert saved["result"]["followup"] == "incomplete"
            assert saved["calls"][1]["state"] == "unknown"
        else:
            assert saved["state"] == "cancelled"
            assert not saved["result"]
