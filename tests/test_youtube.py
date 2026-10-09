import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_telegram import store_at

from zarya.research_fetch import SafeFetcher
from zarya.youtube import YouTubeFetcher, caption_url, chapters, transcript, video_id

IDENTIFIER = "eXcLD3We2tw"
URL = f"https://www.youtube.com/watch?v={IDENTIFIER}"


def watch(description="", tracks=None, initial=None, identifier=IDENTIFIER):
    player = {
        "videoDetails": {
            "videoId": identifier,
            "title": "Тестовый ролик",
            "author": "Автор",
            "shortDescription": description,
            "lengthSeconds": "123",
        },
        "playabilityStatus": {"status": "OK"},
        "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": tracks or []}},
    }
    return (
        "<script>var ytInitialPlayerResponse = " + json.dumps(player) + ";</script>"
        '<script>window["ytInitialData"] = ' + json.dumps(initial or {}) + ";</script>"
    )


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"https://youtu.be/{IDENTIFIER}?si=tracking",
        f"https://m.youtube.com/shorts/{IDENTIFIER}",
        f"https://youtube.com/live/{IDENTIFIER}",
    ],
)
def test_video_identity(url):
    assert video_id(url) == IDENTIFIER


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/bad",
        f"https://youtube.com.evil/watch?v={IDENTIFIER}",
        f"https://user:secret@youtube.com/watch?v={IDENTIFIER}",
        f"http://127.0.0.1/watch?v={IDENTIFIER}",
    ],
)
def test_invalid_video_identity(url):
    assert video_id(url) is None


async def test_full_description_chapters_and_no_false_watching(monkeypatch):
    description = "Длинное описание. " * 130 + "\n00:00 Начало\n01:10 Обсуждение"
    get = AsyncMock(return_value=watch(description))
    monkeypatch.setattr(YouTubeFetcher, "get", get)
    source = await SafeFetcher().fetch(f"https://youtu.be/{IDENTIFIER}?si=tracking")
    assert source["url"] == URL and source["coverage"] == "youtube_metadata"
    assert source["youtube"]["description"] == description
    assert source["youtube"]["chapters"][1] == {"seconds": 70, "title": "Обсуждение"}
    assert source["youtube"]["video_watched"] is False
    assert source["youtube"]["fallback_reason"] is None
    assert get.await_count == 1


async def test_caption_language_priority_signed_url_not_persisted(monkeypatch):
    tracks = [
        {
            "languageCode": "en",
            "baseUrl": f"https://www.youtube.com/api/timedtext?v={IDENTIFIER}&sig=PRIVATE_SIGNATURE",
        },
        {
            "languageCode": "ru",
            "kind": "asr",
            "baseUrl": f"https://www.youtube.com/api/timedtext?v={IDENTIFIER}&sig=PRIVATE_SIGNATURE",
        },
    ]
    captions = json.dumps({"events": [{"tStartMs": 1200, "segs": [{"utf8": "Привет &amp; мир"}]}]})
    get = AsyncMock(side_effect=[watch(tracks=tracks), captions])
    monkeypatch.setattr(YouTubeFetcher, "get", get)
    source = await SafeFetcher().fetch(URL)
    assert source["coverage"] == "youtube_transcript"
    assert source["youtube"]["transcript_language"] == "ru"
    assert source["youtube"]["transcript_automatic"] is True
    assert "[00:01] Привет & мир" in source["text"]
    assert "PRIVATE_SIGNATURE" not in json.dumps(source)
    assert "fmt=json3" in get.call_args.args[0]


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.org/api/timedtext?v={IDENTIFIER}",
        f"https://www.youtube.com/watch?v={IDENTIFIER}",
        "https://www.youtube.com/api/timedtext?v=aaaaaaaaaaa",
        f"http://www.youtube.com/api/timedtext?v={IDENTIFIER}",
    ],
)
def test_caption_target_restricted(url):
    with pytest.raises(ValueError):
        caption_url(url, IDENTIFIER)


def test_transcript_bounded_and_deduplicated():
    text, truncated = transcript(
        json.dumps(
            {
                "events": [
                    {"tStartMs": i * 1000, "segs": [{"utf8": str(i) + "а" * 100}]}
                    for i in range(400)
                ]
            }
        )
    )
    assert truncated and len(text) <= 18000 and text.startswith("[00:00]")
    assert len(chapters("0:00 Intro\n99:99 invalid", {})) == 1


async def test_failed_watch_falls_back_to_oembed_only_preview(monkeypatch):
    get = AsyncMock(
        side_effect=[TimeoutError(), json.dumps({"title": "Ролик", "author_name": "Автор"})]
    )
    monkeypatch.setattr(YouTubeFetcher, "get", get)
    source = await SafeFetcher().fetch(URL)
    assert source["coverage"] == "youtube_metadata"
    assert source["youtube"]["fallback_reason"] == "insufficient_youtube_text"
    assert source["youtube"]["diagnostics"] == [{"step": "watch_page", "code": "timeout"}]
    assert source["youtube"]["transcript_status"] == "unavailable"


async def test_wrong_video_and_consent_not_accepted_as_content(monkeypatch):
    for document in (
        watch(identifier="aaaaaaaaaaa"),
        "<title>Consent</title><meta name='description' content='Allow cookies'>",
    ):
        get = AsyncMock(side_effect=[document, ValueError("http_403")])
        monkeypatch.setattr(YouTubeFetcher, "get", get)
        source = await SafeFetcher().fetch(URL)
        assert source["coverage"] == "unavailable" and not source["text"]
        assert source["youtube"]["fallback_reason"]


class Response:
    def __init__(self, status=200, headers=None, data=b"ok"):
        self.status, self.headers, self.data = (
            status,
            headers or {"Content-Type": "text/html"},
            data,
        )
        self.content, self.charset = self, "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.data


@pytest.mark.parametrize(
    "response,expected",
    [
        (Response(302, {"Location": "http://127.0.0.1/"}), "private_address"),
        (Response(302, {"Location": "https://example.org/"}), "youtube_redirect_restricted"),
        (
            Response(200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}),
            "unsupported_compression",
        ),
        (Response(data=b"abcd"), "size_limit"),
    ],
)
async def test_network_bounds(monkeypatch, response, expected):
    class Session(Response):
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        def get(self, *args, **kwargs):
            return response

        async def __aexit__(self, *args):
            await self.connector.close()

    monkeypatch.setattr("zarya.youtube.aiohttp.ClientSession", Session)
    with pytest.raises(ValueError, match=expected):
        await YouTubeFetcher().get(URL, limit=3)


async def test_automatic_search_only_canonical_public_identity(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(
            store, f"Наш секрет ABC123, о чём видео? {URL}&si=PRIVATE_TRACKING"
        )
        fetch.fetch = AsyncMock(
            return_value={
                "requested_url": URL + "&si=PRIVATE_TRACKING",
                "url": URL,
                "title": "Публичное название",
                "text": "",
                "coverage": "unavailable",
                "error": "timeout",
                "youtube": {"fallback_reason": "insufficient_youtube_text"},
            }
        )
        await research.execute(work)
        assert len(model.calls) == 1
        request = model.calls[0]["input"]
        assert URL in request and "ABC123" not in request and "PRIVATE_TRACKING" not in request
        assert json.loads(request)["provided_material"] == ""
        details = await research.details(work["id"], "999")
        assert details["state"] == "completed"
        youtube = next(source for source in details["sources"] if source["url"] == URL)
        assert youtube["youtube"]["fallback_used"]
        result = await run_one(dialogue)
        await dialogue.replay(result["run_id"], "recorded")
        assert fetch.fetch.await_count == 1 and len(model.calls) == 2


@pytest.mark.parametrize(
    "url,location,expected",
    [
        (
            f"https://www.youtube.com/oembed?url={URL}&format=json",
            "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "youtube_identity_mismatch",
        ),
        (
            f"https://www.youtube.com/api/timedtext?v={IDENTIFIER}",
            "https://www.youtube.com/api/timedtext?v=aaaaaaaaaaa",
            "unsafe_caption_url",
        ),
    ],
)
async def test_same_host_redirect_cannot_change_video(monkeypatch, url, location, expected):
    class Session(Response):
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            assert self.calls == 1, "Must reject changed identity before second HTTP request"
            return Response(302, {"Location": location})

        async def __aexit__(self, *args):
            await self.connector.close()

    monkeypatch.setattr("zarya.youtube.aiohttp.ClientSession", Session)
    with pytest.raises(ValueError, match=expected):
        await YouTubeFetcher().get(url)


async def test_initial_data_description_and_macro_chapters(monkeypatch):
    initial = {
        "videoDescriptionBodyRenderer": {"description": {"runs": [{"text": "Подробное описание"}]}},
        "macroMarkersListItemRenderer": {
            "title": {"simpleText": "Обсуждение"},
            "timeDescription": {"simpleText": "1:30"},
        },
    }
    monkeypatch.setattr(YouTubeFetcher, "get", AsyncMock(return_value=watch(initial=initial)))
    source = await SafeFetcher().fetch(URL)
    assert source["youtube"]["description"] == "Подробное описание"
    assert source["youtube"]["chapters"] == [{"seconds": 90, "title": "Обсуждение"}]


async def test_metadata_sufficient_no_paid_fallback(tmp_path, monkeypatch):
    async with store_at(tmp_path) as store:
        _, research, model, _, work = await research_ready(store, "О чём видео? " + URL)
        monkeypatch.setattr(
            YouTubeFetcher, "get", AsyncMock(return_value=watch("0:00 Вступление\n1:00 Итоги"))
        )
        research.fetcher = SafeFetcher()
        await research.execute(work)
        assert not model.calls
        assert (await research.details(work["id"], "999"))["state"] == "completed"


async def test_revoked_during_youtube_fetch_never_searches(tmp_path):
    async with store_at(tmp_path) as store:
        _, research, model, fetch, work = await research_ready(store, "О чём видео? " + URL)
        fetch.coverage = "unavailable"
        fetch.block = asyncio.Event()
        task = asyncio.create_task(research.execute(work))
        await fetch.entered.wait()
        access = await store.db.one("SELECT version FROM telegram_access WHERE subject_id='42'")
        await store.decide("999", "private", "42", "revoked", access[0])
        fetch.block.set()
        await task
        assert not model.calls
