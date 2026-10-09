import io
import json

import pytest
from PIL import Image
from test_photos import PhotoTransport, VisionModel, analyze, photo, ready
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.photos import PhotoEngine, normalize
from zarya.source_material import describe, visual_sizes


def sticker(number, chat=-100, kind="static", thumbnail=True, **extra):
    item = {
        "file_id": "sticker-original",
        "file_unique_id": "unique-sticker",
        "width": 512,
        "height": 512,
        "is_animated": kind == "animated",
        "is_video": kind == "video",
    }
    if thumbnail:
        item["thumbnail"] = {
            "file_id": "sticker-preview",
            "file_unique_id": "preview",
            "width": 128,
            "height": 128,
        }
    return event(number, chat, "", sticker=item, **extra)


def transparent_webp():
    picture = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    picture.paste((0, 0, 0, 255), (16, 16, 48, 48))
    result = io.BytesIO()
    picture.save(result, "WEBP", lossless=True)
    return result.getvalue()


def test_webp_transparency_is_composited_before_jpeg():
    data, mime, width, height = normalize(transparent_webp())
    image = Image.open(io.BytesIO(data))
    assert mime == "image/webp" and (width, height) == (64, 64)
    assert image.getpixel((2, 2)) == (255, 255, 255)
    assert max(image.getpixel((32, 32))) < 10


@pytest.mark.parametrize(
    "kind,thumbnail,coverage,file",
    [
        ("static", True, "image", "sticker-original"),
        ("animated", True, "thumbnail_only", "sticker-preview"),
        ("video", True, "thumbnail_only", "sticker-preview"),
        ("animated", False, "unavailable", None),
    ],
)
async def test_sticker_addressed_loading_and_truthful_coverage(
    tmp_path, kind, thumbnail, coverage, file
):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        source = sticker(2, kind=kind, thumbnail=thumbnail)
        assert describe(source["message"])["attachments"][0]["coverage"] == coverage
        assert bool(visual_sizes(source["message"])) == bool(file)
        await store.ingest("999", "zarya_test", [source])
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos
        await dialogue.claim("999")
        assert not await store.db.one("SELECT 1 FROM photo_batches")  # No ambient paid analysis.
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, что на стикере?", reply_to_message={"message_id": 2})],
        )
        if file:
            assert await dialogue.claim("999") == {"skip": True}
            assert await dialogue.claim("999") is None
            transport = PhotoTransport(transparent_webp())
            await analyze(photos, transport)
            assert transport.downloads == [file]
        result = await answer(dialogue, None, 3)
        selected = json.loads(result["request"]["input"])["selected_material"]
        assert selected["messages"][0]["attachments"][0]["coverage"] == coverage
        assert bool(result["snapshot"]["photo_refs"]) == bool(file)
        if coverage == "thumbnail_only":
            assert "движение и звук не анализировались" in result["request"]["input"]
        await deliver(store, result, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    4,
                    -100,
                    "А что написано?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        follow = await answer(dialogue, None, 4)
        assert follow["snapshot"]["photo_refs"] == result["snapshot"]["photo_refs"]


async def test_private_sticker_loads_on_demand_and_edit_cancels_observation(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        photos = PhotoEngine(store, VisionModel(), tmp_path)
        dialogue = DialogueEngine(store, photos.adapter)
        dialogue.photos = photos
        await store.ingest("999", "zarya_test", [sticker(2, chat=42)])
        assert await dialogue.claim("999") == {"skip": True}
        await analyze(photos, PhotoTransport(transparent_webp()))
        result = await answer(dialogue, None, 2)
        assert result["snapshot"]["photo_refs"]
        change = event(3, 42, "Исправленное сообщение")
        change["edited_message"] = change.pop("message")
        change["edited_message"]["message_id"] = 2
        await store.ingest("999", "zarya_test", [change])
        state = await store.db.one("SELECT state,result FROM photo_batches")
        assert state == ("cancelled", None)


async def test_incoming_sticker_without_preview_never_substitutes_reply_photo(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        photos = PhotoEngine(store, VisionModel(), tmp_path)
        dialogue = DialogueEngine(store, photos.adapter)
        dialogue.photos = photos
        await store.ingest("999", "zarya_test", [photo(2)])
        await analyze(photos, PhotoTransport())
        old = await answer(dialogue, None, 2)
        await deliver(store, old, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [
                sticker(
                    3,
                    chat=42,
                    kind="animated",
                    thumbnail=False,
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        current = await answer(dialogue, None, 3)
        selected = json.loads(current["request"]["input"])["selected_material"]
        assert [item["message_id"] for item in selected["messages"]] == [3]
        assert selected["messages"][0]["attachments"] == [
            {"kind": "sticker", "coverage": "unavailable"}
        ]
        assert current["snapshot"]["photo_refs"] == []
