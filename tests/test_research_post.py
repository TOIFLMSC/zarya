import json
from unittest.mock import AsyncMock

import pytest
from test_dialogue import private_ready, run_one
from test_media import engines, media_event, process
from test_photos import PhotoTransport, VisionModel, analyze, photo
from test_research import Fetch, ResearchModel, research_ready
from test_source_material import deliver
from test_telegram import event, store_at

from zarya.openai_adapter import ModelResult
from zarya.photos import PhotoEngine
from zarya.research import ResearchEngine
from zarya.research_logic import evidence_text, research_answer_text

QUESTION = "Эта цитата Трампа правдива?"
CAPTION = "Трамп: Хуанг — лидер. Старые чипы Nvidia можно продавать в Китай."
URL = "https://example.org/public/transcript"


async def test_private_question_and_forwarded_video_caption_share_one_factcheck(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue, media, speech, _ = engines(store, tmp_path)
        model = ResearchModel()
        dialogue.adapter = model
        dialogue.research = research = ResearchEngine(store, model, Fetch())
        await store.ingest("999", "zarya_test", [event(2, text=QUESTION)])
        assert await dialogue.claim("999") is None
        post = media_event(3, "video", caption=CAPTION, forward_origin={"type": "channel"})
        await store.ingest("999", "zarya_test", [post])
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["question"] == QUESTION
        assert work["material"] == CAPTION
        assert await media.claim("999") is None
        await research.execute(work)
        assert await dialogue.claim("999") == {"skip": True}
        analyzed = await process(media)
        assert analyzed["analysis_context"]["question"] == QUESTION
        reply = await run_one(dialogue)
        assert reply["snapshot"]["incoming"]["message_id"] == 3
        assert reply["snapshot"]["incoming"]["text"] == QUESTION
        assert {r["message_id"] for r in reply["snapshot"]["source_manifest"]} == {2, 3}
        assert {"id": analyzed["id"]} in reply["snapshot"]["media_refs"]
        assert len(model.calls) == 2 and len(speech.calls) == 1


async def test_reply_to_zarya_still_checks_original_caption(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue, media, speech, _ = engines(store, tmp_path)
        model = ResearchModel()
        dialogue.adapter = model
        dialogue.research = research = ResearchEngine(store, model, Fetch())
        post = media_event(2, "video", caption=CAPTION, forward_origin={"type": "channel"})
        first_question = event(3, text=QUESTION, reply_to_message={"message_id": 2})
        await store.ingest("999", "zarya_test", [post, first_question])
        assert (await dialogue.claim("999"))["skip"]  # The post alone was superseded.
        assert (await dialogue.claim("999"))["skip"]
        await research.execute(await research.claim("999"))
        assert (await dialogue.claim("999"))["skip"]
        await process(media)
        first_reply = await run_one(dialogue)
        await deliver(store, first_reply, 1000)
        clarification = event(
            4, text="Я хотел проверить именно цитату",
            reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
        )
        await store.ingest("999", "zarya_test", [clarification])
        assert (await dialogue.claim("999"))["skip"]
        work = await research.claim("999")
        assert work["material"] == CAPTION and work["question"] == "Я хотел проверить именно цитату"
        assert await media.claim("999") is None and len(speech.calls) == 1


@pytest.mark.parametrize(
    ("text", "caption"),
    [
        ("Он правда озвучил эту цитату в видео?", CAPTION),
        ("Посмотри видео и проверь цитату", CAPTION),
        (QUESTION, "Подробности в видео."),
    ],
)
async def test_factcheck_still_uses_video_when_the_question_needs_it(tmp_path, text, caption):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue, media, _, model = engines(store, tmp_path)
        dialogue.research = research = ResearchEngine(store, model, Fetch())
        post = media_event(2, "video", caption=caption, forward_origin={"type": "channel"})
        ask = event(3, text=text, reply_to_message={"message_id": 2})
        await store.ingest("999", "zarya_test", [post, ask])
        assert (await dialogue.claim("999"))["skip"]
        assert (await dialogue.claim("999"))["skip"]
        assert (await media.claim("999"))["kind"] == "video"
        assert await research.claim("999") is None


async def test_captioned_album_keeps_one_factcheck_and_both_video_items(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue, media, _, _ = engines(store, tmp_path)
        dialogue.research = research = ResearchEngine(store, ResearchModel(), Fetch())
        forward = {"type": "channel"}
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(2, text=QUESTION),
                media_event(
                    3, "video", caption=CAPTION, forward_origin=forward, media_group_id="a"
                ),
                media_event(4, "video", forward_origin=forward, media_group_id="a"),
            ],
        )
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["question"] == QUESTION and CAPTION in work["material"]
        assert await media.claim("999") is None
        assert not await store.db.one(
            "SELECT 1 FROM jobs WHERE json_extract(payload,'$.message_id')=4"
        )
        await research.execute(work)
        assert await dialogue.claim("999") == {"skip": True}
        assert (await store.db.one("SELECT count(*) FROM media_runs"))[0] == 2


@pytest.mark.parametrize("paired", [False, True])
async def test_caption_search_runs_before_photo_processing(tmp_path, paired):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue, _, _, _ = engines(store, tmp_path)
        model = ResearchModel()
        dialogue.adapter = model
        dialogue.research = research = ResearchEngine(store, model, Fetch())
        dialogue.photos = photos = PhotoEngine(store, VisionModel(), tmp_path)
        post = photo(3 if paired else 2, caption=CAPTION)
        post["message"]["forward_origin"] = {"type": "channel"}
        if paired:
            updates = [event(2, text=QUESTION), post]
        else:
            updates = [post, event(3, text=QUESTION, reply_to_message={"message_id": 2})]
        await store.ingest("999", "zarya_test", updates)
        assert await photos.claim("999") is None
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["material"] == CAPTION
        assert not await store.db.one("SELECT 1 FROM model_calls WHERE photo_batch_id IS NOT NULL")
        await research.execute(work)
        assert await dialogue.claim("999") == {"skip": True}
        image = await analyze(photos, PhotoTransport())
        assert not image.get("skip")
        if not paired:
            assert (await dialogue.claim("999"))["skip"]
        output = await run_one(dialogue)
        assert output["snapshot"]["photo_refs"]
        assert output["snapshot"]["incoming"]["text"] == QUESTION


async def test_late_photo_is_admitted_without_creating_second_album_answer(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        first = photo(3, caption=CAPTION, album="late")
        first["message"]["forward_origin"] = {"type": "channel"}
        await store.ingest("999", "zarya_test", [event(2, text=QUESTION), first])
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE jobs SET state='done' WHERE json_extract(payload,'$.message_id')=3"
            )
        later = photo(4, album="late", file="late-item")
        await store.ingest("999", "zarya_test", [later])
        assert await store.db.one("SELECT 1 FROM photo_items WHERE message_id=4")
        assert not await store.db.one(
            "SELECT 1 FROM jobs WHERE json_extract(payload,'$.message_id')=4"
        )


async def test_adjacent_forward_with_old_telegram_timestamp_is_not_bound(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        question = event(2, text=QUESTION, date=1)
        post = media_event(3, "video", date=10, caption=CAPTION, forward_origin={"type": "channel"})
        await store.ingest("999", "zarya_test", [question, post])
        assert await store.db.one(
            "SELECT json_extract(payload,'$.post_question') FROM jobs "
            "WHERE json_extract(payload,'$.message_id')=3"
        ) == (None,)


@pytest.mark.parametrize(
    "rendered", ["Вывод [S1].", "Вывод ([S1]).", "Вывод (https://example.org/public/transcript)."]
)
def test_citation_uses_dash_instead_of_parentheses(rendered):
    assert evidence_text(rendered, [{"id": 1, "url": URL}]) == f"Вывод - {URL}."


def test_markdown_link_shows_label_then_dash_before_url():
    assert evidence_text(f"[Стенограмма]({URL})", [{"id": 1, "url": URL}]) == f"Стенограмма - {URL}"


@pytest.mark.parametrize("rendered", ["Вот [S1]", f"Вот {URL}"])
def test_unreadable_page_cannot_be_rendered_as_evidence(rendered):
    with pytest.raises(ValueError, match="unknown_source"):
        evidence_text(
            rendered,
            [{"id": 1, "url": URL, "coverage": "unavailable", "error": "no_readable_text"}],
        )


def test_partial_general_research_can_link_a_claim_bound_homepage():
    homepage = "https://example.org/"
    context = {
        "analysis": {
            "discarded_claims": 1,
            "claims": [{"verdict": "supported", "source_ids": [1]}],
        },
        "sources": [{"id": 1, "url": homepage}],
    }
    assert research_answer_text("Опубликовано [S1]", context, True) == f"Опубликовано - {homepage}"


@pytest.mark.parametrize("reference", ["https://unobserved.example/", "/press/transcript"])
async def test_invalid_reference_does_not_throw_away_a_sourced_claim(tmp_path, reference):
    async with store_at(tmp_path) as store:
        dialogue, research, model, _, work = await research_ready(
            store, "Найди ссылку на разъяснение ведомства"
        )
        claims = {
            "summary": "UNCHECKED_SENTINEL",
            "claims": [
                {"text": "Есть стенограмма", "kind": "fact", "verdict": "supported", "urls": [URL]},
                {
                    "text": "UNCHECKED_SENTINEL",
                    "kind": "fact",
                    "verdict": "supported",
                    "urls": [URL, reference],
                },
            ],
        }
        model.generate = AsyncMock(
            side_effect=[
                ModelResult(
                    text=json.dumps(claims),
                    search_actions=[
                        {"type": "search", "status": "completed", "sources": [{"url": URL}]}
                    ],
                ),
                ModelResult(text="Стенограмма подтверждает часть цитаты."),
            ]
        )
        await research.execute(work)
        result = await research.details(work["id"], "999")
        assert result["state"] == "completed"
        assert result["result"]["discarded_claims"] == 1
        assert [c["text"] for c in result["result"]["claims"]] == ["Есть стенограмма"]
        assert "UNCHECKED_SENTINEL" not in json.dumps(result)
        assert model.generate.await_count == 1
        reply = await run_one(dialogue)
        assert "UNCHECKED_SENTINEL" not in reply["request"]["input"]
