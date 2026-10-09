import asyncio
import io
import json

import httpx
import pytest
from PIL import Image
from test_telegram import FakeTransport, approve, event, store_at

from zarya.app import create_app
from zarya.config import Config
from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.photos import MAX_BYTES, PhotoEngine, normalize
from zarya.telegram_runtime import LimitedBuffer, TelegramRuntime


def picture(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(output, "PNG")
    return output.getvalue()


def photo(number, chat=42, caption="", album=None, file="one"):
    value = event(
        number,
        chat=chat,
        text="",
        caption=caption,
        photo=[{"file_id": file, "file_unique_id": file, "width": 320, "height": 240}],
    )
    if album:
        value["message"]["media_group_id"] = album
    return value


class PhotoTransport:
    def __init__(self, content=None):
        self.content = content or picture()
        self.downloads = []
        self.entered = asyncio.Event()
        self.release = None

    async def download_photo(self, file_id):
        self.downloads.append(file_id)
        self.entered.set()
        if self.release:
            await self.release.wait()
        return self.content


class VisionModel:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.result = None

    async def generate(self, request):
        self.calls.append(request)
        self.entered.set()
        if self.release:
            await self.release.wait()
        vision = "format" in request["text"]
        return self.result or ModelResult(
            text=json.dumps(
                {
                    "observations": "Красный фон и вывеска",
                    "visible_text": "Книжный клуб",
                    "interpretation": "Возможно, магазин",
                    "uncertainty": "Низ обрезан",
                },
                ensure_ascii=False,
            )
            if vision
            else "На вывеске — Книжный клуб.",
            usage={"input_tokens": 450, "output_tokens": 100},
            model=request["model"],
        )

    async def close(self):
        pass


async def ready(s, chat=42):
    await s.ingest("999", "zarya_test", [event(1, chat=chat)])
    await approve(s, "private" if chat > 0 else "group", str(chat))


async def analyze(engine, transport):
    async with engine.db.transaction() as c:
        await c.execute("UPDATE photo_batches SET collect_until=0")
    work = await engine.claim("999")
    assert work
    if not work.get("skip"):
        await engine.execute(work, transport)
    return work


async def test_private_album_one_answer_originals_and_no_base64_in_database(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        photos = PhotoEngine(s, model, tmp_path)
        dialogue = DialogueEngine(s, model)
        dialogue.photos = photos
        first, second = (
            photo(2, album="a"),
            photo(3, caption="Что на вывеске?", album="a", file="two"),
        )
        await s.ingest("999", "zarya_test", [first, first])
        assert await dialogue.claim("999") is None
        await s.ingest("999", "zarya_test", [second])
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 1
        await analyze(photos, PhotoTransport())
        assert len(model.calls) == 1
        work = await dialogue.claim("999")
        assert work and work["reply_id"] == 3
        assert len(work["snapshot"]["photo_refs"]) == 2
        assert "Книжный клуб" in work["request"]["input"]
        await dialogue.execute(work)
        assert (
            len([v for v in model.calls[-1]["input"][0]["content"] if v["type"] == "input_image"])
            == 2
        )
        image = next(
            v for v in model.calls[-1]["input"][0]["content"] if v["type"] == "input_image"
        )
        assert image["image_url"].startswith("data:image/png;")
        assert await dialogue.claim("999") is None
        for request in await s.db.all("SELECT request_json FROM model_calls"):
            assert "base64," not in request[0] and "api.telegram.org" not in request[0]
        recorded = await dialogue.replay(work["run_id"], "recorded")
        assert recorded["mode"] == "recorded" and len(model.calls) == 2
        version = (await s.db.settings()).version
        paid = await dialogue.replay(work["run_id"], "paid", version)
        assert paid["mode"] == "paid" and len(model.calls) == 3
        assert all(part["state"] == "test_only" for part in paid["parts"])
        await s.decide("999", "private", "42", "revoked", 2)
        with pytest.raises(ValueError, match="недоступно"):
            await dialogue.replay(work["run_id"], "paid", version)
        assert len(model.calls) == 3


async def test_group_photo_silent_reply_waits_other_chat_progresses(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s, -100)
        # Discover private using a fresh update.
        await s.ingest("999", "zarya_test", [event(10)])
        await approve(s)
        model = VisionModel()
        photos = PhotoEngine(s, model, tmp_path)
        dialogue = DialogueEngine(s, model)
        dialogue.photos = photos
        await s.ingest("999", "zarya_test", [photo(2, -100)])
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3, chat=-100, text="Заря, что тут написано?", reply_to_message={"message_id": 2}
                ),
                event(11, text="Привет"),
            ],
        )
        work = await dialogue.claim("999")
        assert work and work["chat_id"] == "42"
        await dialogue.execute(work)
        await analyze(photos, PhotoTransport())
        silent = await dialogue.claim("999")
        assert silent["skip"] and silent["snapshot"]["photo_refs"]
        question = await dialogue.claim("999")
        assert question and not question["skip"] and question["snapshot"]["photo_refs"]
        await dialogue.execute(question)
        assert (await s.db.one("SELECT COUNT(*) FROM model_calls WHERE run_id IS NOT NULL"))[0] == 2


async def test_late_album_tail_context_only(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        engine = PhotoEngine(s, model, tmp_path)
        dialogue = DialogueEngine(s, model)
        dialogue.photos = engine
        await s.ingest("999", "zarya_test", [photo(2, album="a", caption="Что тут?")])
        await analyze(engine, PhotoTransport())
        work = await dialogue.claim("999")
        await dialogue.execute(work)
        await s.ingest("999", "zarya_test", [photo(3, album="a", file="late")])
        await analyze(engine, PhotoTransport(picture("blue")))
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 1
        assert await dialogue.claim("999") is None
        assert (await s.db.one("SELECT COUNT(*) FROM photo_batches"))[0] == 2


@pytest.mark.parametrize("edited_id", [2, 3])
async def test_album_edit_preserves_unchanged_originals_and_cancels_dependent_answer(
    tmp_path, edited_id
):
    async with store_at(tmp_path) as s:
        await ready(s, -100)
        model = VisionModel()
        photos = PhotoEngine(s, model, tmp_path)
        dialogue = DialogueEngine(s, model)
        dialogue.photos = photos
        await s.ingest(
            "999",
            "zarya_test",
            [photo(2, -100, album="a", file="first"), photo(3, -100, album="a", file="second")],
        )
        await analyze(photos, PhotoTransport())
        silent = await dialogue.claim("999")
        assert silent["skip"]
        await s.ingest(
            "999",
            "zarya_test",
            [event(4, chat=-100, text="Заря, что на первом?", reply_to_message={"message_id": 2})],
        )
        work = await dialogue.claim("999")
        assert work and len(work["snapshot"]["photo_refs"]) == 2
        edited = photo(5, -100, album="a", file="changed", caption="новая подпись")
        edited["edited_message"] = edited.pop("message")
        edited["edited_message"]["message_id"] = edited_id
        await s.ingest("999", "zarya_test", [edited])
        await dialogue.execute(work)
        assert (await dialogue.details(work["run_id"]))["state"] == "cancelled"
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0
        transport = PhotoTransport()
        await analyze(photos, transport)
        assert transport.downloads == (
            ["changed", "second"] if edited_id == 2 else ["first", "changed"]
        )
        await s.ingest(
            "999",
            "zarya_test",
            [event(6, chat=-100, text="Заря, а теперь?", reply_to_message={"message_id": 2})],
        )
        fresh = await dialogue.claim("999")
        assert fresh and len(fresh["snapshot"]["photo_refs"]) == 2
        assert [ref["message_id"] for ref in fresh["snapshot"]["photo_refs"]] == [2, 3]
        await dialogue.execute(fresh)
        assert (await dialogue.details(fresh["run_id"]))["state"] == "completed"


async def test_cache_scope_caption_model_and_access_version(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        engine = PhotoEngine(s, model, tmp_path)
        for n, caption in [(2, "один"), (3, "один"), (4, "два")]:
            await s.ingest("999", "zarya_test", [photo(n, caption=caption)])
            await analyze(engine, PhotoTransport())
        assert len(model.calls) == 2
        assert (await engine.details(2, "999"))["cache_source"] == 1
        settings = await s.db.settings()
        await s.db.update_settings(
            settings.version, settings.settings.model_copy(update={"photo_model": "gpt-6.1-sol"})
        )
        await s.ingest("999", "zarya_test", [photo(5, caption="два")])
        await analyze(engine, PhotoTransport())
        assert len(model.calls) == 3
        await s.ingest("999", "zarya_test", [event(6, chat=-100)])
        await approve(s, "group", "-100")
        await s.ingest("999", "zarya_test", [photo(7, chat=-100, caption="два")])
        await analyze(engine, PhotoTransport())
        assert len(model.calls) == 4
        await approve(s)  # New grant version must not revive old cache.
        await s.ingest("999", "zarya_test", [photo(8, caption="два")])
        await analyze(engine, PhotoTransport())
        assert len(model.calls) == 5


async def test_recent_context_eviction_does_not_hide_history_or_invalidate_cache(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        engine = PhotoEngine(s, model, tmp_path)
        await s.ingest("999", "zarya_test", [photo(2)])
        await analyze(engine, PhotoTransport())
        async with s.db.transaction() as c:
            await c.execute(
                "DELETE FROM recent_messages"
            )  # Same effect as the bounded window aging out.
        assert await engine.asset(1, "999") is not None
        assert (await engine.details(1, "999"))["result"]
        await s.ingest("999", "zarya_test", [photo(3)])
        await analyze(engine, PhotoTransport())
        assert len(model.calls) == 1
        assert (await engine.details(2, "999"))["state"] == "cache"


async def test_album_edit_while_disabled_keeps_latest_caption_when_reassembled(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = PhotoEngine(s, VisionModel(), tmp_path)
        await s.ingest(
            "999",
            "zarya_test",
            [
                photo(2, album="a", file="first"),
                photo(3, album="a", caption="Старая подпись", file="second"),
            ],
        )
        await analyze(engine, PhotoTransport())
        settings = await s.db.settings()
        await s.db.update_settings(
            settings.version, settings.settings.model_copy(update={"photo_enabled": False})
        )
        edited = photo(4, album="a", caption="Новая подпись", file="new-second")
        edited["edited_message"] = edited.pop("message")
        edited["edited_message"]["message_id"] = 3
        await s.ingest("999", "zarya_test", [edited])
        async with s.db.transaction() as c:
            await c.execute("DELETE FROM recent_messages")
        settings = await s.db.settings()
        await s.db.update_settings(
            settings.version, settings.settings.model_copy(update={"photo_enabled": True})
        )
        edited_first = photo(5, album="a", file="new-first")
        edited_first["edited_message"] = edited_first.pop("message")
        edited_first["edited_message"]["message_id"] = 2
        await s.ingest("999", "zarya_test", [edited_first])
        transport = PhotoTransport()
        await analyze(engine, transport)
        assert transport.downloads == ["new-first", "new-second"]
        assert (await engine.details(2, "999"))["items"][1]["caption"] == "Новая подпись"
        assert (await engine.details(1, "999"))["result"] is None


async def test_revision_migration_backfills_unprocessed_edit_under_new_grant(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = PhotoEngine(s, VisionModel(), tmp_path)
        await s.ingest(
            "999",
            "zarya_test",
            [photo(2, album="a", file="first"), photo(3, album="a", file="second")],
        )
        await analyze(engine, PhotoTransport())
        await approve(s)
        settings = await s.db.settings()
        await s.db.update_settings(
            settings.version, settings.settings.model_copy(update={"photo_enabled": False})
        )
        edited = photo(4, album="a", caption="Новое разрешение и новая подпись", file="new-second")
        edited["edited_message"] = edited.pop("message")
        edited["edited_message"]["message_id"] = 3
        await s.ingest("999", "zarya_test", [edited])
        # Recreate a schema-5 checkpoint; retained events must repair the revision cursor.
        async with s.db.transaction() as c:
            await c.execute("DROP TABLE photo_messages")
            await c.execute("DELETE FROM schema_migrations WHERE version='006_photo_revisions.sql'")
        await s.db.close()
        await s.db.open()
        revision = await s.db.one(
            "SELECT event_id,access_version FROM photo_messages WHERE message_id=3"
        )
        assert revision[1] == 3
        settings = await s.db.settings()
        await s.db.update_settings(
            settings.version, settings.settings.model_copy(update={"photo_enabled": True})
        )
        edited_first = photo(5, album="a", file="new-first")
        edited_first["edited_message"] = edited_first.pop("message")
        edited_first["edited_message"]["message_id"] = 2
        await s.ingest("999", "zarya_test", [edited_first])
        transport = PhotoTransport()
        await analyze(engine, transport)
        assert transport.downloads == ["new-first", "new-second"]
        assert (await engine.details(2, "999"))["items"][1][
            "caption"
        ] == "Новое разрешение и новая подпись"


@pytest.mark.parametrize("phase", ["download", "model"])
async def test_revoke_during_work_suppresses_result_retains_call_cost(tmp_path, phase):
    async with store_at(tmp_path) as s:
        await ready(s)
        model, transport = VisionModel(), PhotoTransport()
        blocker = transport if phase == "download" else model
        blocker.release = asyncio.Event()
        engine = PhotoEngine(s, model, tmp_path)
        await s.ingest("999", "zarya_test", [photo(2)])
        task = asyncio.create_task(analyze(engine, transport))
        await blocker.entered.wait()
        await s.decide("999", "private", "42", "revoked", 2)
        blocker.release.set()
        await task
        detail = await engine.details(1, "999")
        assert detail["state"] == "cancelled" and detail["result"] is None
        assert await engine.asset(1, "999") is None
        assert len(model.calls) == (1 if phase == "model" else 0)
        if phase == "model":
            assert detail["call"]["cost_usd"] > 0


async def test_edit_during_call_never_publishes_old_analysis_or_answers_again(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        model.release = asyncio.Event()
        engine = PhotoEngine(s, model, tmp_path)
        await s.ingest("999", "zarya_test", [photo(2, caption="раньше")])
        task = asyncio.create_task(analyze(engine, PhotoTransport()))
        await model.entered.wait()
        edited = photo(3, caption="теперь")
        edited["edited_message"] = edited.pop("message")
        edited["edited_message"]["message_id"] = 2
        await s.ingest("999", "zarya_test", [edited])
        model.release.set()
        await task
        assert (await engine.details(1, "999"))["result"] is None
        await analyze(engine, PhotoTransport())
        assert (await engine.details(2, "999"))["result"]
        assert (await s.db.one("SELECT COUNT(*) FROM jobs WHERE state='pending'"))[0] == 0


@pytest.mark.parametrize("outcome", ["unknown", "invalid"])
async def test_terminal_failures_unblock_dialogue_without_paid_retry(tmp_path, outcome):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        model.result = (
            ModelResult(state="unknown", error="timeout")
            if outcome == "unknown"
            else ModelResult(text="bad json")
        )
        engine = PhotoEngine(s, model, tmp_path)
        await s.ingest("999", "zarya_test", [photo(2, caption="Что тут?")])
        await analyze(engine, PhotoTransport())
        await engine.recover("999")
        assert await engine.claim("999") is None and len(model.calls) == 1
        dialogue = DialogueEngine(s, model)
        dialogue.photos = engine
        work = await dialogue.claim("999")
        assert work and not work["skip"] and not work["snapshot"]["photo_refs"]
        assert "Фото пока не распознано" in work["request"]["input"]


async def test_restart_before_paid_call_requeues_after_paid_call_unknown(tmp_path):
    async with store_at(tmp_path) as s:
        await ready(s)
        engine = PhotoEngine(s, VisionModel(), tmp_path)
        await s.ingest("999", "zarya_test", [photo(2)])
        assert (await engine.claim("999"))["id"] == 1
        await engine.recover("999")
        assert (await engine.claim("999"))["id"] == 1
        async with s.db.transaction() as c:
            await c.execute("UPDATE photo_batches SET state='analyzing'")
            await c.execute(
                "INSERT INTO model_calls(provider,model,state,created_at,photo_batch_id) "
                "VALUES ('openai','gpt-6-luna','started','now',1)"
            )
        await engine.recover("999")
        assert (await engine.details(1, "999"))["state"] == "unknown"
        assert await engine.claim("999") is None


@pytest.mark.parametrize(
    "content", [b"not a photo", b"x" * (MAX_BYTES + 1)], ids=["invalid", "oversized"]
)
async def test_invalid_images_no_call_no_hanging_dependency(tmp_path, content):
    async with store_at(tmp_path) as s:
        await ready(s)
        model = VisionModel()
        engine = PhotoEngine(s, model, tmp_path)
        await s.ingest("999", "zarya_test", [photo(2)])
        await analyze(engine, PhotoTransport(content))
        assert not model.calls
        assert (await engine.details(1, "999"))["state"] == "error"


def test_actual_download_limit_and_decode_pixel_limit():
    stream = LimitedBuffer()
    stream.write(b"a" * MAX_BYTES)
    with pytest.raises(ValueError, match="file_size"):
        stream.write(b"x")
    output = io.BytesIO()
    Image.new("L", (4001, 4000)).save(output, "PNG")
    with pytest.raises(ValueError, match="image_pixels"):
        normalize(output.getvalue())


async def test_photo_api_authenticated_scope_bound_and_revocation(tmp_path):
    app = create_app(
        Config(data_dir=tmp_path, web_dir=tmp_path / "web", dev_ui=True),
        model_adapter=VisionModel(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
        ) as client:
            assert (await client.get("/api/photos/images/1")).status_code == 401
            token = (tmp_path / "bootstrap-token.txt").read_text().strip()
            response = await client.post(
                "/api/setup",
                json={"token": token, "password": "photo-test-password"},
                headers={"Origin": "http://127.0.0.1:8787"},
            )
            assert response.status_code == 200
            s, engine = app.state.telegram_store, app.state.photos
            app.state.telegram.bot = {"id": "999"}
            await ready(s)
            await s.ingest("999", "zarya_test", [photo(2)])
            await analyze(engine, PhotoTransport())
            detail = (await client.get("/api/photos/1")).json()
            assert detail["result"]["visible_text"] == "Книжный клуб"
            assert "file_id" not in json.dumps(detail) and "raw_path" not in json.dumps(detail)
            image = await client.get(detail["items"][0]["image_url"])
            assert image.status_code == 200 and image.headers["cache-control"] == "no-store"
            await s.decide("999", "private", "42", "revoked", 2)
            assert (await client.get("/api/photos/images/1")).status_code == 403
            assert (await client.get("/api/photos/1")).json()["result"] is None
            app.state.telegram.bot = {"id": "another"}
            assert (await client.get("/api/photos/1")).status_code == 404


@pytest.mark.parametrize("scenario", ["album", "reply"])
async def test_typing_spans_photo_wait_and_dialogue_without_duplicate_phase_pulse(
    tmp_path, scenario
):
    async with store_at(tmp_path) as s:
        await ready(s, -100)
        model = VisionModel()
        model.release = asyncio.Event()
        photos = PhotoEngine(s, model, tmp_path)
        dialogue = DialogueEngine(s, model)
        dialogue.photos = photos
        transport = FakeTransport()
        pulses = []

        async def typing(chat, thread):
            pulses.append((chat, thread))
            return True

        transport.typing = typing
        runtime = TelegramRuntime(s, transport=transport, dialogue=dialogue, photos=photos)
        await s.ingest(
            "999",
            "zarya_test",
            [
                photo(
                    2,
                    -100,
                    caption="Заря, что тут?" if scenario == "album" else "",
                    album="a" if scenario == "album" else None,
                )
            ],
        )
        if scenario == "album":
            await s.ingest("999", "zarya_test", [photo(3, -100, album="a", file="second")])
        task = asyncio.create_task(analyze(photos, PhotoTransport()))
        await model.entered.wait()
        if scenario == "reply":
            await runtime.type_one("999")
            assert pulses == []  # Ambient photo stays silent, including typing.
            await s.ingest(
                "999",
                "zarya_test",
                [
                    event(
                        3, chat=-100, text="Заря, что на фото?", reply_to_message={"message_id": 2}
                    )
                ],
            )
        await runtime.type_one("999")
        await runtime.type_one("999")
        assert pulses == [("-100", None)]
        async with s.db.transaction() as c:
            await c.execute("UPDATE jobs SET typing_due=0")
        await runtime.type_one("999")
        assert len(pulses) == 2
        model.release.set()
        await task
        work = await dialogue.claim("999")
        if work["skip"]:
            work = await dialogue.claim("999")
        assert work and not work["skip"]
        await runtime.type_one("999")
        assert len(pulses) == 2  # Media→dialogue keeps the same cooldown and history.
        await dialogue.execute(work)
        info = (await dialogue.details(work["run_id"]))["typing"]
        assert info["attempts"] == 2 and info["state"] == "accepted"
        async with s.db.transaction() as c:
            await c.execute("UPDATE dialogue_runs SET typing_error='legacy_network'")
        assert (await dialogue.details(work["run_id"]))["typing"]["error"] is None
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET state='sent'")
        await runtime.type_one("999")
        assert len(pulses) == 2


@pytest.mark.parametrize(
    "condition", ["revoke", "disabled", "expired", "superseded", "cooldown", "no_adapter"]
)
async def test_media_typing_suppression(tmp_path, condition):
    async with store_at(tmp_path) as s:
        await ready(s)
        await s.ingest("999", "zarya_test", [photo(2, album="a", caption="Вопрос")])
        if condition == "revoke":
            await s.decide("999", "private", "42", "revoked", 2)
        elif condition == "disabled":
            settings = await s.db.settings()
            await s.db.update_settings(
                settings.version, settings.settings.model_copy(update={"photo_enabled": False})
            )
        elif condition == "expired":
            async with s.db.transaction() as c:
                await c.execute("UPDATE jobs SET created_at='2000-01-01T00:00:00+00:00'")
        elif condition == "superseded":
            await s.ingest("999", "zarya_test", [event(3, text="Новый вопрос")])
        elif condition == "cooldown":
            async with s.db.transaction() as c:
                await c.execute(
                    "INSERT INTO delivery_limits(bot_id,chat_id,blocked_until) "
                    "VALUES ('999','42',9999999999)"
                )
        adapter = None if condition == "no_adapter" else VisionModel()
        runtime = TelegramRuntime(s, transport=FakeTransport(), dialogue=DialogueEngine(s, adapter))
        await runtime.type_one("999")
        assert (await s.db.one("SELECT SUM(typing_attempts) FROM jobs"))[0] == 0
