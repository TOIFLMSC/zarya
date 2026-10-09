"""Conversation provenance regressions: unrelated media must never replace a post."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from test_photos import PhotoTransport, VisionModel, analyze, photo, ready
from test_research import Fetch
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.photos import PhotoEngine
from zarya.research import ResearchEngine
from zarya.source_material import resolve


async def answer(dialogue, research, number):
    for _ in range(20):
        work = await dialogue.claim("999")
        if work and not work.get("skip"):
            await dialogue.execute(work)
            if work["reply_id"] == number:
                return work
        pending = await research.claim("999") if research else None
        if pending and not pending.get("skip"):
            await research.execute(pending)
    pytest.fail("Addressed dialogue did not complete")


async def deliver(store, work, message_id):
    item = await store.db.one(
        "SELECT id FROM outbox WHERE job_id=? ORDER BY id LIMIT 1", (work["job_id"],)
    )
    assert item
    async with store.db.transaction() as conn:
        await conn.execute("UPDATE outbox SET state='sending' WHERE id=?", (item[0],))
    await store.delivery_result(item[0], "sent", message_id=message_id)


@pytest.mark.parametrize("with_research", [False, True])
async def test_reply_chain_selects_original_album_not_latest_photo(tmp_path, with_research):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos
        research = ResearchEngine(store, model, Fetch()) if with_research else None
        dialogue.research = research
        first = photo(2, -100, caption="Пост об инфляции", album="first", file="cinema")
        first["message"]["forward_origin"] = {"type": "channel"}
        await store.ingest(
            "999", "zarya_test", [first, photo(3, -100, album="first", file="other")]
        )
        await analyze(photos, PhotoTransport())
        await store.ingest(
            "999",
            "zarya_test",
            [event(4, -100, "Заря, про что этот пост?", reply_to_message={"message_id": 2})],
        )
        initial = await answer(dialogue, research, 4)
        await deliver(store, initial, 1000)
        # A different forwarded image arrives between explanation and follow-up.
        unrelated = photo(5, -100, caption="README другого проекта", file="readme")
        unrelated["message"]["forward_origin"] = {"type": "channel"}
        await store.ingest("999", "zarya_test", [unrelated])
        await analyze(photos, PhotoTransport())
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    6,
                    -100,
                    "А на фото че там?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        followup = await answer(dialogue, research, 6)
        assert {r["message_id"] for r in followup["snapshot"]["photo_refs"]} == {2, 3}
        selected = json.loads(followup["request"]["input"])["selected_material"]
        assert {m["message_id"] for m in selected["messages"]} == {2, 3}
        assert selected["unavailable"] is False
        assert "README" not in json.dumps(selected, ensure_ascii=False)
        # Further reply on another part/answer retains the same original album.
        await deliver(store, followup, 1001)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    7,
                    -100,
                    "А мем что означает?",
                    reply_to_message={"message_id": 1001, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        third = await answer(dialogue, research, 7)
        assert {r["message_id"] for r in third["snapshot"]["photo_refs"]} == {2, 3}


async def test_caption_hidden_and_direct_urls_use_same_post_on_followup(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        post = event(
            2,
            -100,
            "✨ статья и https://example.org/direct",
            forward_origin={"type": "channel"},
            entities=[
                {"type": "text_link", "offset": 3, "length": 6, "url": "https://example.org/hidden"}
            ],
        )
        await store.ingest("999", "zarya_test", [post])
        model, fetch = VisionModel(), Fetch()
        dialogue = DialogueEngine(store, model)
        research = ResearchEngine(store, model, fetch)
        dialogue.research = research
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    -100,
                    "Заря, поясни пост",
                    reply_to_message={"message_id": 2, "text": "Подменённый источник"},
                )
            ],
        )
        initial = await answer(dialogue, research, 3)
        assert set(fetch.calls) == {"https://example.org/direct", "https://example.org/hidden"}
        await deliver(store, initial, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    4,
                    -100,
                    "А что в самой статье?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        followup = await answer(dialogue, research, 4)
        selected = json.loads(followup["request"]["input"])["selected_material"]
        assert set(selected["messages"][0]["urls"]) == set(fetch.calls)
        assert "Подменённый" not in json.dumps(selected, ensure_ascii=False)


@pytest.mark.parametrize("kind", ["video", "animation"])
async def test_video_gif_preview_bound_to_post_without_downloading_video(tmp_path, kind):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        settings = await store.db.settings()
        await store.db.update_settings(
            settings.version, settings.settings.model_copy(update={"media_enabled": False})
        )
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos
        attachment = {
            "file_id": "full-video",
            "duration": 9000,
            "thumbnail": {
                "file_id": "preview",
                "file_unique_id": "preview",
                "width": 320,
                "height": 240,
            },
        }
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    2,
                    -100,
                    "",
                    caption="Пост с роликом",
                    forward_origin={"type": "channel"},
                    **{kind: attachment},
                )
            ],
        )
        transport = PhotoTransport()
        await analyze(photos, transport)
        assert transport.downloads == ["preview"]
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, поясни пост", reply_to_message={"message_id": 2})],
        )
        work = await answer(dialogue, None, 3)
        selected = json.loads(work["request"]["input"])["selected_material"]
        assert selected["messages"][0]["attachments"] == [
            {"kind": kind, "coverage": "thumbnail_only"}
        ]
        assert "движение и звук не анализировались" in selected["messages"][0]["text"]
        assert len(work["snapshot"]["photo_refs"]) == 1


@pytest.mark.parametrize("invalidate", ["edit", "forget", "expire", "other_chat", "grant"])
async def test_lost_source_never_falls_back_to_other_photo(tmp_path, invalidate):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos
        await store.ingest("999", "zarya_test", [photo(2, -100, caption="Исходный пост")])
        await analyze(photos, PhotoTransport())
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, поясни пост", reply_to_message={"message_id": 2})],
        )
        initial = await answer(dialogue, None, 3)
        await deliver(store, initial, 1000)
        async with store.db.transaction() as conn:
            if invalidate == "forget":
                await conn.execute(
                    "UPDATE events SET payload='{}' WHERE id IN "
                    "(SELECT event_id FROM research_messages WHERE message_id=2)"
                )
            elif invalidate == "expire":
                await conn.execute(
                    "UPDATE recent_messages SET received_at=? WHERE role='assistant'",
                    (time.time() - 91 * 86400,),
                )
            elif invalidate == "grant":
                await conn.execute("UPDATE outbox SET access_version=1")
        if invalidate == "edit":
            edited = photo(4, -100, caption="Исправленный пост", file="new")
            edited["edited_message"] = edited.pop("message")
            edited["edited_message"]["message_id"] = 2
            await store.ingest("999", "zarya_test", [edited])
        async with store.db.transaction() as conn:
            selected = await resolve(
                conn,
                "999",
                "-200" if invalidate == "other_chat" else "-100",
                2,
                {"message_id": 5, "reply_to_message": {"message_id": 1000}},
            )
        assert selected["unavailable"] and not selected["messages"]


async def test_reply_without_thumbnail_reports_unavailable_attachment(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        await store.ingest(
            "999",
            "zarya_test",
            [event(2, -100, "", caption="Ролик", video={"file_id": "video", "duration": 120})],
        )
        async with store.db.transaction() as conn:
            selected = await resolve(
                conn, "999", "-100", 2, {"message_id": 3, "reply_to_message": {"message_id": 2}}
            )
        assert selected["messages"][0]["attachments"] == [
            {"kind": "video", "coverage": "unavailable"}
        ]


async def test_chain_waits_for_album_tail_without_blocking_other_chat_and_types(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        await store.ingest("999", "zarya_test", [event(10)])
        from test_telegram import approve

        await approve(store)
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos
        await store.ingest("999", "zarya_test", [photo(2, -100, album="a")])
        await analyze(photos, PhotoTransport())
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, поясни пост", reply_to_message={"message_id": 2})],
        )
        initial = await answer(dialogue, None, 3)
        await deliver(store, initial, 1000)
        await store.ingest("999", "zarya_test", [photo(4, -100, album="a", file="tail")])
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    5,
                    -100,
                    "А что на второй?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                ),
                event(11, text="Привет"),
            ],
        )
        waiting = await dialogue.claim("999")
        assert waiting == {"skip": True}
        typing = await store.claim_typing("999", media_enabled=True)
        assert typing and typing["chat_id"] == "-100"
        other = await dialogue.claim("999")
        assert other and not other["skip"] and other["chat_id"] == "42"
        await dialogue.execute(other)
        await analyze(photos, PhotoTransport())
        followup = await answer(dialogue, None, 5)
        assert {r["message_id"] for r in followup["snapshot"]["photo_refs"]} == {2, 4}


@pytest.mark.parametrize("expired", [False, True])
async def test_source_edit_or_retention_erases_fresh_dependent_answers_and_replays(
    tmp_path, expired
):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        model = VisionModel()
        dialogue = DialogueEngine(store, model)
        await store.ingest(
            "999",
            "zarya_test",
            [event(2, -100, "Исходный текст поста", forward_origin={"type": "channel"})],
        )
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, поясни пост", reply_to_message={"message_id": 2})],
        )
        initial = await answer(dialogue, None, 3)
        await deliver(store, initial, 1000)
        recorded = await dialogue.replay(initial["run_id"], "recorded")
        # Replay-of-replay remains supported with source guards and no live job_id.
        paid = await dialogue.replay(recorded["id"], "paid", (await store.db.settings()).version)
        assert paid["state"] == "completed"
        if expired:
            async with store.db.transaction() as conn:
                await conn.execute(
                    "UPDATE events SET received_at='2020-01-01T00:00:00+00:00' WHERE update_id=2"
                )
            await MemoryEngine(store, None, tmp_path).cleanup("999", force=True)
        else:
            edited = event(4, -100, "Новый текст", forward_origin={"type": "channel"})
            edited["edited_message"] = edited.pop("message")
            edited["edited_message"]["message_id"] = 2
            await store.ingest("999", "zarya_test", [edited])
        for run_id in (initial["run_id"], recorded["id"], paid["id"]):
            run = await dialogue.details(run_id)
            assert run["snapshot"].get("invalidated") and run["response"] is None
            with pytest.raises(ValueError):
                await dialogue.replay(run_id, "recorded")
        assert not await store.db.one("SELECT 1 FROM recent_messages WHERE role='assistant'")
        assert all(r[0] is None for r in await store.db.all("SELECT request_json FROM model_calls"))


async def test_source_guard_cancels_pending_delivery_even_without_engines(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue = DialogueEngine(store, VisionModel())
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(2, -100, "Исходный пост", forward_origin={"type": "channel"}),
                event(3, -100, "Заря, поясни пост", reply_to_message={"message_id": 2}),
            ],
        )
        initial = await answer(dialogue, None, 3)
        async with store.db.transaction() as conn:
            # Simulate a source being erased after generation but before delivery.
            await conn.execute("UPDATE events SET payload='{}' WHERE update_id=2")
            await conn.execute("UPDATE outbox SET next_attempt=0")
        assert await store.claim_delivery("999") is None
        states = await store.db.all(
            "SELECT state,error_code FROM outbox WHERE job_id=?", (initial["job_id"],)
        )
        assert states and all(s == ("cancelled", "source_changed") for s in states)


async def test_ordinary_reply_keeps_brief_mode_without_research_operation(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue = DialogueEngine(store, VisionModel())
        fetch = Fetch()
        research = ResearchEngine(store, dialogue.adapter, fetch)
        dialogue.research = research
        await store.ingest("999", "zarya_test", [event(2, -100, "Заря, привет")])
        initial = await answer(dialogue, research, 2)
        await deliver(store, initial, 1000)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    -100,
                    "Как дела?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        followup = await answer(dialogue, research, 3)
        assert followup["snapshot"]["answer_mode"] == "brief"
        assert not fetch.calls
        assert not await store.db.one("SELECT 1 FROM research_runs")


async def test_old_post_without_photo_registration_is_loaded_on_followup_once(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        post = photo(2, -100, caption="Старый пост", file="cinema")
        post["message"]["forward_origin"] = {"type": "channel"}
        await store.ingest("999", "zarya_test", [post])
        async with store.db.transaction() as conn:
            # Accepted before photo admission existed (or while it was disabled).
            await conn.execute("DELETE FROM photo_items")
            await conn.execute("DELETE FROM photo_batches")
            await conn.execute("DELETE FROM photo_messages")
        dialogue = DialogueEngine(store, VisionModel())
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, поясни этот пост", reply_to_message={"message_id": 2})],
        )
        initial = await answer(dialogue, None, 3)
        await deliver(store, initial, 1000)
        photos = PhotoEngine(store, dialogue.adapter, tmp_path)
        dialogue.photos = photos
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    4,
                    -100,
                    "А на фото че?",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        assert await dialogue.claim("999") == {"skip": True}
        assert (await store.db.one("SELECT COUNT(*) FROM photo_batches"))[0] == 1
        # Repeated claim while waiting must not create more photo work.
        assert await dialogue.claim("999") is None
        transport = PhotoTransport()
        await analyze(photos, transport)
        work = await answer(dialogue, None, 4)
        assert transport.downloads == ["cinema"]
        assert {r["message_id"] for r in work["snapshot"]["photo_refs"]} == {2}
        assert (await store.db.one("SELECT COUNT(*) FROM jobs"))[0] == 3


@pytest.mark.parametrize("phase", ["queued", "completed", "analyzing"])
async def test_lazy_photo_retention_follows_old_source_not_new_batch(tmp_path, phase):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        await store.ingest("999", "zarya_test", [photo(2, -100, caption="Старая подпись")])
        async with store.db.transaction() as conn:
            await conn.execute("DELETE FROM photo_items")
            await conn.execute("DELETE FROM photo_batches")
            await conn.execute("DELETE FROM photo_messages")
            await conn.execute(
                "UPDATE events SET received_at=? WHERE update_id=2",
                ((datetime.now(UTC) - timedelta(days=89)).isoformat(),),
            )
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        settings = (await store.db.settings()).settings
        async with store.db.transaction() as conn:
            await photos.prepare_sources(conn, "999", "-100", "group", 2, [2], settings)
        task = None
        if phase == "completed":
            await analyze(photos, PhotoTransport())
        elif phase == "analyzing":
            model.release = asyncio.Event()
            task = asyncio.create_task(analyze(photos, PhotoTransport()))
            await model.entered.wait()
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE events SET received_at=? WHERE update_id=2",
                ((datetime.now(UTC) - timedelta(days=91)).isoformat(),),
            )
        memory = MemoryEngine(store, None, tmp_path)
        await memory.cleanup("999", force=True)
        if task:
            model.release.set()
            await task
        batch = await store.db.one("SELECT id,state,result,manifest FROM photo_batches")
        assert batch[1:] == ("cancelled", None, None)
        async with store.db.transaction() as conn:
            assert not await photos.valid(conn, batch[0])
        assert not list((tmp_path / "photos").glob("*"))
        assert all(
            r == ("", None, None)
            for r in await store.db.all("SELECT caption,raw_path,image_path FROM photo_items")
        )
        assert all(r[0] is None for r in await store.db.all("SELECT request_json FROM model_calls"))


async def test_erased_source_during_download_does_not_restore_asset_or_start_paid_call(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        await store.ingest("999", "zarya_test", [photo(2, -100)])
        model = VisionModel()
        photos = PhotoEngine(store, model, tmp_path)
        transport = PhotoTransport()
        transport.release = asyncio.Event()
        task = asyncio.create_task(analyze(photos, transport))
        await transport.entered.wait()
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE events SET payload='{}' WHERE update_id=2")
        memory = MemoryEngine(store, None, tmp_path)
        await memory.cleanup("999", force=True)
        transport.release.set()
        await task
        assert not model.calls
        assert all(
            r == (None, None)
            for r in await store.db.all("SELECT raw_path,image_path FROM photo_items")
        )
        assert (await store.db.one("SELECT COUNT(*) FROM memory_file_cleanup"))[0] > 0
        await memory.cleanup("999", force=True)
        assert not list((tmp_path / "photos").glob("*"))
