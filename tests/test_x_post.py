import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_research_news import CORRECTION, start_post
from test_telegram import event, store_at

from zarya.openai_adapter import ModelResult
from zarya.research_fetch import SafeFetcher
from zarya.research_logic import evidence_text, research_answer_text, source_failure_text
from zarya.research_news import news_followup_plan, public_search_sources
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


def test_x_unavailable_id_is_excluded_from_search_and_news_followup():
    public = "https://example.org/news"
    entries = [{"requested_url": post_url(POST)}, {"requested_url": public}]
    sources = [
        {
            "id": 1, "coverage": "unavailable", "error": "x_post_unavailable",
            "requested_url": post_url(POST), "x_post": {"post_id": POST},
        },
        {"id": 2, "coverage": "partial_text", "url": public, "title": "Тестовая новость"},
    ]
    for manifest in (sources, public_search_sources(entries, sources)):
        escaped = f"example.net/mirror/%31{POST[1:]}"
        request = hosted_search_input(f"Проверь {URL} ({POST})", escaped, manifest)
        assert POST not in json.dumps(request)
        assert escaped not in json.dumps(request)
        assert request["sources"][0]["url"] == public
    actions = [{"type": "search", "status": "completed", "sources": [{"url": URL}]}]
    followup = news_followup_plan({"needs_followup": True}, entries, actions, sources)
    assert followup == {"reason": "evidence_gap", "public_urls": [public]}


@pytest.mark.parametrize("recheck", [False, True])
async def test_x_unavailable_id_cannot_return_through_mirrors_in_search_requests(
    tmp_path, monkeypatch, recheck
):
    blocked = ValueError("x_post_unavailable")
    monkeypatch.setattr(XFetcher, "get", AsyncMock(side_effect=blocked))
    mirror = f"https://threadreaderapp.com/thread/{POST}.html"
    escaped = f"https://example.com/mirror/%31{POST[1:]}.html"
    independent = "https://example.org/independent-report"
    async with store_at(tmp_path) as store:
        if recheck:
            dialogue, research, model, _ = await start_post(store, post_url(POST), "metadata_only")
            await store.ingest("999", "zarya_test", [
                event(3, text=CORRECTION, reply_to_message={
                    "message_id": 1000, "from": {"id": 999, "is_bot": True},
                }),
            ])
            assert await dialogue.claim("999") == {"skip": True}
            work = await research.claim("999")
            assert work and work["news_recheck"]
        else:
            _, research, model, _, work = await research_ready(
                store, f"Заря, сделай мини-ресерч и найди источники для {URL}"
            )
            assert work["profile"] == "study"
        research.fetcher = SafeFetcher()
        search_actions = [{
            "type": "search", "status": "completed",
            "sources": [{"url": url} for url in (mirror, independent, escaped)],
        }]
        model.generate = AsyncMock(side_effect=[
            ModelResult(
                text=json.dumps({"summary": "Ищу источник", "claims": [], "needs_followup": True}),
                search_actions=search_actions,
            ),
            ModelResult(
                text=json.dumps({"summary": "Источник проверен", "claims": []}),
                search_actions=search_actions,
            ),
        ])
        await research.execute(work)
        first, second = [call.args[0]["input"] for call in model.generate.await_args_list]
        assert POST not in first and POST not in second and escaped not in second
        assert json.loads(second)["public_urls"] == [independent]


@pytest.mark.parametrize("requested", [URL, "https://example.org/redirect"])
def test_x_legacy_preview_and_redirected_x_preview_are_disclosed(requested):
    source = {"id": 1, "requested_url": requested, "url": URL, "coverage": "metadata_only"}
    context = {"state": "completed", "sources": [source]}
    assert METADATA_NOTICE in research_answer_text("Вот описание.", context, False)
    assert METADATA_NOTICE in source_failure_text(context, False)


def test_x_single_preview_with_search_alias_keeps_singular_notice():
    sources = [
        {"id": 1, "url": post_url(POST), "coverage": "metadata_only"},
        {"id": 2, "url": URL, "coverage": "search_source"},
    ]
    assert research_answer_text("Описание", {"sources": sources}, False) == (
        f"{METADATA_NOTICE}\n\nОписание"
    )


@pytest.mark.parametrize("fmt", ["Пост - {}", "Пост ({})", "[Пост]({})"])
def test_x_final_links_accept_alias_only_for_observed_post(fmt):
    sources = [{"id": 1, "url": post_url(POST), "coverage": "x_post_text"}]
    assert evidence_text(fmt.format(URL), sources) == f"Пост - {post_url(POST)}"
    with pytest.raises(ValueError, match="unknown_source"):
        evidence_text(fmt.format(URL.replace(POST, OTHER)), sources)


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


async def test_x_local_text_can_confirm_primary_source_without_extra_search(tmp_path, monkeypatch):
    monkeypatch.setattr(XFetcher, "get", AsyncMock(return_value=thread()))
    async with store_at(tmp_path) as store:
        _, research, model, _, work = await research_ready(
            store, f"Заря, найди ссылку на первоисточник из этого поста {URL}"
        )
        assert work["profile"] == "source"
        research.fetcher = SafeFetcher()
        model.generate = AsyncMock(return_value=ModelResult(
            text=json.dumps({
                "summary": "Найден первоисточник",
                "claims": [{
                    "text": "Найден исходный пост", "kind": "fact",
                    "verdict": "supported", "urls": [URL],
                }],
                "source_lookup": {"kind": "document", "url": URL, "heading": "Исходный пост"},
            }),
            search_actions=[{
                "type": "search", "status": "completed", "sources": [{"url": "https://example.org"}],
            }],
        ))
        await research.execute(work)
        async with store.db.transaction() as connection:
            context = await research.context(connection, work["id"])
        assert context["state"] == "completed"
        assert context["analysis"]["source_lookup"]["source_id"] == 1
        assert model.generate.await_count == 1


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
