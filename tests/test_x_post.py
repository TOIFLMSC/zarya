import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_telegram import store_at

from zarya.openai_adapter import ModelResult
from zarya.research_fetch import SafeFetcher
from zarya.research_logic import source_failure_text
from zarya.research_plan import hosted_search_input
from zarya.x_post import METADATA_NOTICE, XFetcher, parse_thread, post_id, post_url

POST, QUOTE, ROOT, PARENT, OTHER = [f"100000000000000000{i}" for i in range(1, 6)]
URL = f"https://twitter.com/TestAuthor/status/{POST}/photo/1?s=tracking"


def status(identifier, text, *, parent=None, quote=None, protected=False):
    return {
        "type": "status",
        "id": identifier,
        "url": f"https://x.com/TestAuthor/status/{identifier}",
        "author": {"screen_name": "TestAuthor", "protected": protected},
        "text": text,
        "quote": quote,
        "replying_to": {"status": parent, "url": post_url(parent)} if parent else None,
    }


def thread():
    return {
        "code": 200,
        "status": status(
            POST, "Выбранный текст", parent=PARENT, quote=status(QUOTE, "Текст цитаты")
        ),
        "thread": [
            status(OTHER, "Пост ниже", parent=POST),
            status(PARENT, "Ближайший предок", parent=ROOT),
            status(ROOT, "Начало ветки"),
        ],
    }


def test_x_identity_and_ancestors():
    assert post_id(URL) == POST
    assert post_id(f"https://x.com/i/web/status/{POST}") == POST
    assert post_id(f"https://other.example/TestAuthor/status/{POST}") is None
    assert post_id(f"https://x.com:8080/TestAuthor/status/{POST}") is None
    assert post_id(f"https://user:pass@x.com/TestAuthor/status/{POST}") is None
    text, details = parse_thread(thread(), POST)
    assert details["thread_status"] == "available"
    assert details["quote_id"] == QUOTE
    assert details["parent_ids"] == [ROOT, PARENT]
    assert "Цитируемый пост @TestAuthor" in text
    assert "Пост ниже" not in text
    assert text.index("Начало ветки") < text.index("Ближайший предок")


def test_x_thread_size_limit_and_cycle():
    payload = thread()
    payload["status"]["text"] *= 500
    payload["thread"][0] = status(OTHER, "Ранний предок", parent="1000000000000000006")
    payload["thread"][2]["replying_to"] = {"status": OTHER, "url": post_url(OTHER)}
    text, details = parse_thread(payload, POST)
    assert len(text) < 5000
    assert details["parent_ids"] == [OTHER, ROOT, PARENT]
    assert details["thread_status"] == "partial"
    assert details["text_truncated"]
    payload["thread"][0]["replying_to"] = {"status": POST, "url": post_url(POST)}
    _, details = parse_thread(payload, POST)
    assert details["thread_status"] == "partial" and details["parent_ids"] == []
    payload["thread"].append(payload["thread"][1])
    text, details = parse_thread(payload, POST)
    assert details["thread_status"] == "partial" and details["parent_ids"] == []


def test_x_wrong_identity_and_quote_coverage():
    payload = thread()
    payload["status"]["url"] = post_url(OTHER)
    with pytest.raises(ValueError, match="x_identity_mismatch"):
        parse_thread(payload, POST)
    payload["status"]["url"] = post_url(POST)
    payload["status"]["quote"]["author"]["protected"] = True
    text, details = parse_thread(payload, POST)
    assert "Текст цитаты" not in text
    assert details["quote_status"] == "unavailable" and details["quote_id"] is None
    payload["status"]["quote"] = {**status(QUOTE, ""), "media": {"videos": [{}]}}
    _, details = parse_thread(payload, POST)
    assert details["quote_status"] == "no_text" and details["has_media"]


@pytest.mark.parametrize(
    "unavailable", [{"type": "tombstone", "id": POST}, status(POST, "Закрыто", protected=True)]
)
async def test_x_restricted_status_stops_fallback(monkeypatch, unavailable):
    monkeypatch.setattr(
        XFetcher, "get", AsyncMock(return_value={"code": 200, "status": unavailable})
    )
    page = AsyncMock()
    monkeypatch.setattr(SafeFetcher, "fetch_page", page)
    source = await SafeFetcher().fetch(URL)
    assert source["error"] == "x_post_unavailable" and not source["text"]
    page.assert_not_awaited()


async def test_x_unrelated_preview_is_rejected(monkeypatch):
    monkeypatch.setattr(XFetcher, "get", AsyncMock(side_effect=TimeoutError))
    page = AsyncMock(return_value={
        "url": post_url(OTHER), "coverage": "metadata_only", "text": "Другое превью",
    })
    monkeypatch.setattr(SafeFetcher, "fetch_page", page)
    source = await SafeFetcher().fetch(URL)
    assert source["coverage"] == "unavailable" and not source["text"]
    page.assert_awaited_once_with(post_url(POST))


def test_x_search_only_receives_admitted_alias_without_parameters():
    result = hosted_search_input(
        f"Найди {URL} и {post_url(OTHER)}",
        "",
        [{"coverage": "x_post_text", "url": post_url(POST), "x_post": {"post_id": POST}}],
    )
    assert result["question"] == f"Найди {post_url(POST)} и [ссылка исключена]"


async def test_x_research_supplies_text_quote_and_thread_to_dialogue(tmp_path, monkeypatch):
    get = AsyncMock(return_value=thread())
    monkeypatch.setattr(XFetcher, "get", get)
    async with store_at(tmp_path) as store:
        dialogue, research, model, _, work = await research_ready(store, f"Заря, прочитай {URL}")
        research.fetcher = SafeFetcher()
        await research.execute(work)
        await run_one(dialogue)
        request = model.calls[-1]
        sources = json.loads(request["input"])["research"]["sources"]
        assert sources[0]["coverage"] == "x_post_text"
        assert sources[0]["x_post"]["parent_ids"] == [ROOT, PARENT]
        assert all(
            value in sources[0]["text"]
            for value in ("Выбранный текст", "Текст цитаты", "Начало ветки")
        )
        assert "Пост ниже" not in sources[0]["text"]
        assert "metadata_only" in request["instructions"]
    get.assert_awaited_once_with(POST)


async def test_x_metadata_limit_survives_dialogue_and_source_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(XFetcher, "get", AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(
        SafeFetcher, "fetch_page",
        AsyncMock(return_value={
            "url": URL, "coverage": "metadata_only", "text": "Публичное описание", "error": None,
        }),
    )
    async with store_at(tmp_path) as store:
        dialogue, research, model, _, work = await research_ready(store, f"Заря, прочитай {URL}")
        research.fetcher = SafeFetcher()
        await research.execute(work)
        model.generate = AsyncMock(return_value=ModelResult(text="Вот что удалось найти."))
        await run_one(dialogue)
        delivery = await store.db.one("SELECT payload FROM outbox ORDER BY part_index LIMIT 1")
        assert delivery and METADATA_NOTICE in json.loads(delivery[0])["text"]
        body = json.loads(model.generate.call_args.args[0]["input"])["research"]
        source = body["sources"][0]
        assert source["coverage"] == "metadata_only" and source["url"] == post_url(POST)
        assert source["text"] == "Публичное описание"
        assert METADATA_NOTICE in source_failure_text(body, False)
