import asyncio
import io
import json
import math
import sys
import time
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_photos import VisionModel, picture, ready
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.dialogue_logic import instructions
from zarya.media import MediaEngine
from zarya.media_processing import MAX_MEDIA_BYTES, MediaProcessor, frame_times
from zarya.models import Settings
from zarya.openai_adapter import ModelResult, OpenAIAdapter, estimate_speech_cost
from zarya.research import ResearchEngine
from zarya.telegram_runtime import LimitedBuffer, TelegramRuntime


def media_event(number, kind="voice", chat=42, **extra):
    return event(number, chat, "", **{kind: {"file_id": f"media-{number}", "duration": 4}}, **extra)


class Speech:
    def __init__(self, result=None):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.result = result or ModelResult(
            text="Расскажи, чем факт отличается от мнения",
            model="gpt-4o-mini-transcribe",
            usage={"type": "tokens", "input_tokens": 80, "output_tokens": 20},
        )

    async def transcribe(self, model, audio):
        self.calls.append((model, len(audio)))
        self.entered.set()
        if self.release:
            await self.release.wait()
        return self.result


class Processor:
    available = True

    async def prepare(self, data, directory, kind, limits):
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        if kind != "voice":
            (directory / "frame-0.jpg").write_bytes(picture())
            frames = [{"path": f"{directory.name}/frame-0.jpg", "seconds": 2.0}]
        return {
            "duration": 4.0,
            "frames": frames,
            "audio": b"wav" if kind != "animation" else None,
            "coverage": {
                "audio_signal": "missing" if kind == "animation" else "present",
                "audio_intervals": [],
                "frame_times": [2.0] if frames else [],
                "note": "sampled",
            },
        }


class Downloader:
    def __init__(self):
        self.calls = []

    async def download_media(self, file_id):
        self.calls.append(file_id)
        return b"synthetic"


def engines(store, tmp_path, speech=None, processor=None):
    model = VideoModel()
    speech = speech or Speech()
    media = MediaEngine(store, model, speech, tmp_path, processor or Processor())
    dialogue = DialogueEngine(store, model)
    dialogue.media = media
    return dialogue, media, speech, model


class VideoModel(VisionModel):
    async def generate(self, request):
        result = await super().generate(request)
        if self.result or "format" not in request["text"]:
            return result
        result.text = json.dumps(
            {
                "observations": "Красный фон и вывеска",
                "visible_text": "Книжный клуб",
                "interpretation": "Возможно, магазин",
                "timeline": [{"seconds": 2.0, "visual": "Красный фон"}],
                "speech_summary": "Просьба объяснить разницу факта и мнения",
                "question_answer": "Ответ на первоначальный вопрос",
                "uncertainty": "Промежутки не просмотрены",
                "evidence_basis": "sampled_frames_and_transcript",
            },
            ensure_ascii=False,
        )
        return result


async def process(media):
    work = await media.claim("999")
    assert work and not work.get("skip")
    downloader = Downloader()
    await media.execute(work, downloader)
    return work


@pytest.mark.parametrize("legacy", [False, True])
async def test_video_reasoning_pinned_when_queued_and_legacy_jobs_stay_low(tmp_path, legacy):
    async with store_at(tmp_path) as store:
        await ready(store)
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE settings SET document=json_set(document,'$.media_reasoning','high')"
            )
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, "video")])
        await dialogue.claim("999")
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE settings SET document=json_set(document,'$.media_reasoning','medium')"
            )
            if legacy:
                await conn.execute("UPDATE media_runs SET limits=json_remove(limits,'$.reasoning')")
        await process(media)
        vision = next(c for c in model.calls if "format" in c["text"])
        assert vision["reasoning"]["effort"] == ("low" if legacy else "high")
        answer = await dialogue.claim("999")
        assert answer["request"]["reasoning"]["effort"] == "low"


@pytest.mark.parametrize("ask", [False, True])
async def test_spoken_source_request_controls_links_without_research(tmp_path, ask):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(
            store,
            tmp_path,
            Speech(ModelResult(text="Поясни мем" + (" и скинь источники" if ask else ""))),
        )
        await store.ingest("999", "zarya_test", [media_event(2)])
        await dialogue.claim("999")
        await process(media)
        answer = await dialogue.claim("999")
        assert answer["snapshot"]["sources_requested"] is ask
        model.result = ModelResult(
            text="Мем опубликован тут https://example.org/. Забавная обработка."
        )
        await dialogue.execute(answer)
        detail = await dialogue.details(answer["run_id"])
        assert ("https://example.org/" in detail["response"]) is ask


@pytest.mark.parametrize("kind", ["voice", "video", "video_note", "animation"])
async def test_addressed_media_scoped_evidence_paid_ledger_and_no_binary_db(tmp_path, kind):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, kind)])
        assert (await dialogue.claim("999"))["skip"]
        typing = TelegramRuntime(
            store, transport=SimpleNamespace(typing=AsyncMock(return_value=True)), dialogue=dialogue
        )
        await typing.type_one("999")
        assert typing.transport.typing.await_count == 1
        work = await process(media)
        detail = await media.details(work["id"], "999")
        assert detail["state"] == "completed" and detail["available"]
        assert len(detail["frames"]) == (0 if kind == "voice" else 1)
        assert len(speech.calls) == (0 if kind == "animation" else 1)
        assert {c["operation"] for c in detail["calls"]} == (
            {"asr"} if kind == "voice" else {"vision"} if kind == "animation" else {"asr", "vision"}
        )
        answer = await dialogue.claim("999")
        assert not answer["skip"] and answer["snapshot"]["media_refs"] == [{"id": work["id"]}]
        assert (
            "Расскажи" in answer["request"]["input"]
            if kind != "animation"
            else "Красный фон" in answer["request"]["input"]
        )
        await dialogue.execute(answer)
        assert await media.claim("999") is None
        calls = await store.db.all(
            "SELECT request_json FROM model_calls WHERE media_run_id IS NOT NULL"
        )
        assert all("base64" not in c[0] for c in calls)
        assert not (tmp_path / "media" / str(work["id"]) / "audio.wav").exists()
        assert not (tmp_path / "media" / str(work["id"]) / "input.bin").exists()


async def test_ambient_video_not_downloaded_then_reply_chain_selects_original(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100, caption="Пост", forward_origin={"type": "channel"})
        await store.ingest("999", "zarya_test", [post])
        assert (await dialogue.claim("999"))["skip"]
        assert await media.claim("999") is None
        assert (await store.db.one("SELECT COUNT(*) FROM photo_items"))[0] == 0
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, объясни пост", reply_to_message=post["message"])],
        )
        assert (await dialogue.claim("999"))["skip"]
        work = await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
        delivery = await store.claim_delivery("999")
        assert delivery
        await store.delivery_result(delivery["id"], "sent", message_id=50)
        await store.ingest(
            "999",
            "zarya_test",
            [
                media_event(4, "video", -100),
                event(
                    5,
                    -100,
                    "А что видно?",
                    reply_to_message={
                        "message_id": 50,
                        "from": {"id": 999, "is_bot": True},
                        "text": "answer",
                    },
                ),
            ],
        )
        # Ambient job is silent; the selected source reuses its completed ASR.
        assert (await dialogue.claim("999"))["skip"]
        follow = await dialogue.claim("999")
        assert follow["snapshot"]["media_refs"] == [{"id": work["id"]}]
        assert len(speech.calls) == 1 and await media.claim("999") is None


async def test_revision_revoke_and_late_callback_preserve_usage_erase_content(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        speech.release = asyncio.Event()
        await store.ingest("999", "zarya_test", [media_event(2)])
        await dialogue.claim("999")
        work = await media.claim("999")
        task = asyncio.create_task(media.execute(work, Downloader()))
        await speech.entered.wait()
        version = (await store.db.one("SELECT version FROM telegram_access WHERE subject_id='42'"))[
            0
        ]
        await store.decide("999", "private", "42", "revoked", version)
        await media.cleanup("999")
        speech.release.set()
        await task
        detail = await media.details(work["id"], "999")
        assert not detail["available"] and detail["transcript"] is None
        assert detail["calls"][0]["cost_usd"] is not None
        assert (
            await store.db.one(
                "SELECT request_json FROM model_calls WHERE media_run_id=?", (work["id"],)
            )
        )[0] is None
        assert not (tmp_path / "media" / str(work["id"])).exists()


async def test_edit_invalidates_answer_replay_and_frames(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, "video")])
        await dialogue.claim("999")
        work = await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        edit = media_event(3, "video")
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 2
        await store.ingest("999", "zarya_test", [edit])
        assert (await media.details(work["id"], "999"))["state"] == "cancelled"
        assert await media.asset(work["id"], 0, "999") is None
        with pytest.raises(ValueError):
            await dialogue.replay(answer["run_id"], "recorded")


async def test_partial_video_keeps_asr_and_recovery_does_not_repeat_paid_work(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        model.result = ModelResult(state="unknown", error="timeout")
        await store.ingest("999", "zarya_test", [media_event(2, "video")])
        await dialogue.claim("999")
        work = await process(media)
        detail = await media.details(work["id"], "999")
        assert detail["state"] == "partial" and detail["transcript"]
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE media_runs SET state='analyzing' WHERE id=?", (work["id"],))
            await conn.execute("UPDATE model_calls SET state='started' WHERE operation='vision'")
        await media.recover("999")
        assert await media.claim("999") is None
        assert (await media.details(work["id"], "999"))["state"] == "partial"
        assert len(speech.calls) == 1


async def test_retention_uses_original_event_and_private_forwarded_not_self(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, forward_origin={"type": "user"})])
        await dialogue.claim("999")
        work = await process(media)
        assert (await store.db.one("SELECT COUNT(*) FROM memory_sources"))[0] == 0
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE events SET received_at='2020-01-01T00:00:00+00:00' "
                "WHERE id=(SELECT event_id FROM media_runs WHERE id=?)",
                (work["id"],),
            )
        await media.cleanup("999")
        detail = await media.details(work["id"], "999")
        assert detail["state"] == "cancelled" and detail["coverage"] == {}


async def test_metadata_limits_reject_before_download(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        item = media_event(2)
        item["message"]["voice"]["duration"] = 999
        await store.ingest("999", "zarya_test", [item])
        answer = await dialogue.claim("999")
        assert "duration_limit" in answer["request"]["input"]
        assert await media.claim("999") is None and not speech.calls


def pcm(silent=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(
            b"\0\0" * 32000
            if silent
            else b"".join(
                int(1000 * math.sin(i * 0.2)).to_bytes(2, "little", signed=True)
                for i in range(32000)
            )
        )
    return output.getvalue()


@pytest.mark.parametrize("silent", [False, True])
async def test_real_ffmpeg_wav_duration_silence_and_ephemeral_raw(tmp_path, silent):
    processor = MediaProcessor()
    if not processor.available:
        pytest.skip("FFmpeg not installed")
    result = await processor.prepare(
        pcm(silent), tmp_path / "1", "voice", {"voice": 300, "video": 120, "frames": 6}
    )
    assert 1.9 <= result["duration"] <= 2.1
    assert bool(result["audio"]) != silent
    assert not (tmp_path / "1" / "input.bin").exists()


@pytest.mark.parametrize("kind", ["video", "video_note", "animation"])
async def test_real_ffmpeg_sample_frames_and_audio_missing(tmp_path, kind):
    processor = MediaProcessor()
    if not processor.available:
        pytest.skip("FFmpeg not installed")
    source = tmp_path / ("clip.gif" if kind == "animation" else "clip.mp4")
    await processor.run(
        processor.ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=320x240:d=2",
        "-an",
        "-c:v",
        "gif" if kind == "animation" else "libx264",
        "-threads",
        "1",
        str(source),
    )
    result = await processor.prepare(
        source.read_bytes(), tmp_path / "1", kind, {"voice": 300, "video": 120, "frames": 6}
    )
    assert result["frames"] and result["audio"] is None
    assert result["coverage"]["audio_signal"] == "missing"
    assert result["frames"][0]["seconds"] == 0
    assert result["frames"][-1]["seconds"] >= 1.8


async def test_subprocess_cancel_waits_for_child_exit(tmp_path):
    processor = MediaProcessor()
    task = asyncio.create_task(processor.run(sys.executable, "-c", "import time; time.sleep(30)"))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5


async def test_real_animation_with_audio_tail_samples_within_video(tmp_path):
    processor = MediaProcessor()
    if not processor.available:
        pytest.skip("FFmpeg not installed")
    source = tmp_path / "animation.mp4"
    await processor.run(
        processor.ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=224x126:r=10:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-t",
        "1.024",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-threads",
        "1",
        str(source),
    )
    result = await processor.prepare(
        source.read_bytes(),
        tmp_path / "1",
        "animation",
        {"voice": 300, "video": 120, "frames": 24},
    )
    assert result["duration"] > 1.0  # Audio/container extends beyond the video stream.
    assert result["coverage"]["frame_times"] == [0.0, 0.9]
    assert all((tmp_path / frame["path"]).is_file() for frame in result["frames"])
    assert not (tmp_path / "1" / "input.bin").exists()


async def test_decoder_rejects_playlists_and_large_media(tmp_path):
    processor = MediaProcessor()
    if not processor.available:
        pytest.skip("FFmpeg not installed")
    with pytest.raises(ValueError):
        await processor.prepare(
            b"#EXTM3U\nhttp://127.0.0.1/secret",
            tmp_path / "1",
            "video",
            {"voice": 300, "video": 120, "frames": 6},
        )
    with pytest.raises(ValueError):
        await processor.prepare(
            b"x" * (MAX_MEDIA_BYTES + 1),
            tmp_path / "2",
            "voice",
            {"voice": 300, "video": 120, "frames": 6},
        )
    with pytest.raises(ValueError):
        LimitedBuffer(5).write(b"123456")


async def test_asr_official_sdk_request_has_no_responses_fields():
    adapter = OpenAIAdapter("test")
    response = SimpleNamespace(
        text="Привет",
        _request_id="asr1",
        model_dump=lambda: {
            "text": "Привет",
            "usage": {"type": "tokens", "input_tokens": 80, "output_tokens": 20},
        },
    )
    create = AsyncMock(return_value=response)
    adapter.client.audio.transcriptions.create = create
    result = await adapter.transcribe("gpt-4o-mini-transcribe", b"wav")
    kwargs = create.call_args.kwargs
    assert kwargs["response_format"] == "json" and kwargs["file"][0] == "audio.wav"
    assert not {"store", "reasoning", "max_output_tokens"}.intersection(kwargs)
    assert estimate_speech_cost(result, "gpt-4o-mini-transcribe") == pytest.approx(0.0002)
    assert estimate_speech_cost(ModelResult(), "gpt-4o-mini-transcribe") is None
    await adapter.close()


async def test_own_voice_is_brief_question_not_research_post(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(
            store, tmp_path, Speech(ModelResult(text="Привет, как дела?"))
        )
        dialogue.research = ResearchEngine(store, model)
        await store.ingest("999", "zarya_test", [media_event(2)])
        await dialogue.claim("999")
        await process(media)
        answer = await dialogue.claim("999")
        assert answer["snapshot"]["answer_mode"] == "brief"
        assert (await store.db.one("SELECT COUNT(*) FROM research_runs"))[0] == 0
        prompt = instructions(Settings(), False, "brief", False)
        assert "sampled_media" in prompt and "Аудио и видеоряд не анализировались" not in prompt


async def test_stale_question_does_not_pay_then_new_reply_can_prepare(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        original = media_event(2)
        await store.ingest("999", "zarya_test", [original])
        await dialogue.claim("999")
        await store.ingest("999", "zarya_test", [event(3, text="Другой вопрос")])
        assert (await media.claim("999"))["skip"]
        assert not speech.calls
        # Finish the superseded and replacement question without Telegram delivery.
        assert (await dialogue.claim("999"))["skip"]
        replacement = await dialogue.claim("999")
        await dialogue.execute(replacement)
        await store.ingest(
            "999",
            "zarya_test",
            [event(4, text="Разбери голосовое", reply_to_message=original["message"])],
        )
        assert (await dialogue.claim("999"))["skip"]
        await process(media)
        assert len(speech.calls) == 1


async def test_fourth_album_video_can_be_requested_separately(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        posts = [media_event(i, "video", -100, media_group_id="album") for i in range(2, 6)]
        await store.ingest(
            "999",
            "zarya_test",
            posts + [event(6, -100, "Заря, поясни альбом", reply_to_message=posts[0]["message"])],
        )
        for _ in range(5):
            await dialogue.claim("999")
        for _ in range(3):
            await process(media)
        fourth = await store.db.one("SELECT id,state,error_code FROM media_runs WHERE message_id=5")
        assert fourth[1:] == ("unavailable", "request_media_limit")
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        await store.ingest(
            "999",
            "zarya_test",
            [event(7, -100, "Заря, теперь этот ролик", reply_to_message=posts[3]["message"])],
        )
        assert (await dialogue.claim("999"))["skip"]
        work = await process(media)
        assert work["id"] == fourth[0] and len(speech.calls) == 4
        assert await media.claim("999") is None


async def test_locked_media_cleanup_is_retried(tmp_path, monkeypatch):
    import zarya.media as module

    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, "video")])
        await dialogue.claim("999")
        work = await process(media)
        from zarya.media_logic import purge

        async with store.db.transaction() as conn:
            await purge(conn, "id=?", (work["id"],))
        original = module.shutil.rmtree
        monkeypatch.setattr(
            module.shutil,
            "rmtree",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")),
        )
        await media.cleanup("999")
        assert (await store.db.one("SELECT COUNT(*) FROM media_file_cleanup"))[0] == 1
        monkeypatch.setattr(module.shutil, "rmtree", original)
        await media.cleanup("999")
        assert (await store.db.one("SELECT COUNT(*) FROM media_file_cleanup"))[0] == 0


@pytest.mark.parametrize("actual", ["gpt-6.1-sol", "unknown-model"])
async def test_media_ledger_records_actual_vision_model(tmp_path, actual):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        text = (await model.generate({"model": "gpt-6-luna", "text": {"format": True}})).text
        model.result = ModelResult(
            text=text, model=actual, usage={"input_tokens": 100, "output_tokens": 20}
        )
        await store.ingest("999", "zarya_test", [media_event(2, "animation")])
        await dialogue.claim("999")
        work = await process(media)
        call = await store.db.one(
            "SELECT model,cost_usd,pricing_profile FROM model_calls WHERE media_run_id=?",
            (work["id"],),
        )
        assert call[0] == actual
        assert (call[1] is None) == (actual == "unknown-model")
        assert (call[2] is None) == (actual == "unknown-model")


async def test_superseded_during_download_is_reusable_without_paid_retry(tmp_path):
    class BlockingDownloader(Downloader):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def download_media(self, file_id):
            self.entered.set()
            await self.release.wait()
            return b"synthetic"

    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        original = media_event(2)
        await store.ingest("999", "zarya_test", [original])
        await dialogue.claim("999")
        work = await media.claim("999")
        downloader = BlockingDownloader()
        task = asyncio.create_task(media.execute(work, downloader))
        await downloader.entered.wait()
        await store.ingest("999", "zarya_test", [event(3, text="Другой вопрос")])
        downloader.release.set()
        await task
        detail = await media.details(work["id"], "999")
        assert detail["state"] == "unavailable" and detail["error"] == "stale_request"
        assert not detail["calls"]
        await dialogue.claim("999")
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        await store.ingest(
            "999",
            "zarya_test",
            [event(4, text="Ответь на голосовое", reply_to_message=original["message"])],
        )
        assert (await dialogue.claim("999"))["skip"]
        await process(media)
        assert len(speech.calls) == 1


async def test_media_api_requires_session_scopes_bot_and_hides_revoked_frames(tmp_path):
    from test_app import running, setup

    async with running(tmp_path) as (app, client):
        assert (await client.get("/api/media")).status_code == 401
        assert (await client.get("/api/media/1/frames/0")).status_code == 401
        await setup(client, tmp_path)
        app.state.telegram.bot = {"id": "999", "username": "test"}
        store = app.state.telegram_store
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        await store.ingest("999", "zarya_test", [media_event(2, "video")])
        await dialogue.claim("999")
        work = await process(media)
        assert (await client.get("/api/media?scope=private")).json()["total"] == 1
        assert (await client.get("/api/media?scope=group")).json()["total"] == 0
        detail = (await client.get(f"/api/media/{work['id']}")).json()
        assert (await client.get(detail["frames"][0]["url"])).status_code == 200
        app.state.telegram.bot = {"id": "111"}
        assert (await client.get(f"/api/media/{work['id']}")).status_code == 404
        app.state.telegram.bot = {"id": "999"}
        version = (await store.db.one("SELECT version FROM telegram_access WHERE subject_id='42'"))[
            0
        ]
        await store.decide("999", "private", "42", "revoked", version)
        assert (await client.get(detail["frames"][0]["url"])).status_code == 404
        assert (await client.get(f"/api/media/{work['id']}")).json()["transcript"] is None


@pytest.mark.parametrize("dependencies", [1, 3])
async def test_new_dependency_after_stale_call_still_requeues_free_work(
    tmp_path, monkeypatch, dependencies
):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        original = media_event(2)
        await store.ingest("999", "zarya_test", [original])
        await dialogue.claim("999")
        work = await media.claim("999")
        original_call = media.call

        async def stale_then_new_dependency(*args, **kwargs):
            await store.ingest(
                "999",
                "zarya_test",
                [
                    event(3, text="Другой вопрос"),
                    event(4, text="Вернись к голосовому", reply_to_message=original["message"]),
                ],
            )
            for _ in range(3):
                await dialogue.claim("999")
            if dependencies == 3:
                # Two other completed members already belong to the new waiter's album.
                async with store.db.transaction() as conn:
                    waiter = await conn.execute("SELECT MAX(job_id) FROM media_dependencies")
                    waiter_id = (await waiter.fetchone())[0]
                    for index in (1, 2):
                        cursor = await conn.execute(
                            "INSERT INTO media_runs(bot_id,chat_id,scope,access_version,"
                            "message_id,event_id,kind,file_id,state,processing_version,"
                            "asr_model,model,settings_version,limits,created_at) "
                            "SELECT bot_id,chat_id,scope,access_version,message_id,event_id,"
                            "kind,file_id,'completed',processing_version||?,asr_model,model,"
                            "settings_version,limits,created_at FROM media_runs WHERE id=?",
                            (str(index), work["id"]),
                        )
                        await conn.execute(
                            "INSERT INTO media_dependencies VALUES (?,?)",
                            (waiter_id, cursor.lastrowid),
                        )
            return ModelResult(state="cancelled", error="stale_request")

        monkeypatch.setattr(media, "call", stale_then_new_dependency)
        await media.execute(work, Downloader())
        assert (await media.details(work["id"], "999"))["state"] == "unavailable"
        assert not speech.calls
        monkeypatch.setattr(media, "call", original_call)
        await dialogue.claim("999")
        await dialogue.claim("999")
        await process(media)
        assert len(speech.calls) == 1


async def test_free_preparation_cancellation_can_recover(tmp_path):
    class BlockingProcessor(Processor):
        def __init__(self):
            self.entered = asyncio.Event()

        async def prepare(self, *args):
            self.entered.set()
            await asyncio.Event().wait()

    async with store_at(tmp_path) as store:
        await ready(store)
        processor = BlockingProcessor()
        dialogue, media, speech, model = engines(store, tmp_path, processor=processor)
        await store.ingest("999", "zarya_test", [media_event(2)])
        await dialogue.claim("999")
        work = await media.claim("999")
        task = asyncio.create_task(media.execute(work, Downloader()))
        await processor.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not speech.calls
        await media.recover("999")
        assert (await media.claim("999"))["id"] == work["id"]


@pytest.mark.parametrize("duration,cap", [(23.057, 24), (120, 24), (120, 32), (4, 2), (4, 1)])
def test_dense_sampling_covers_whole_clip_and_respects_budget(duration, cap):
    marks = frame_times(duration, cap)
    assert len(marks) <= cap and marks == sorted(set(marks))
    if cap == 1:
        assert marks == [duration / 2]
    else:
        assert marks[0] == 0 and duration - 0.11 <= marks[-1] < duration
        if duration == 23.057:
            assert len(marks) == 13
            assert max(b - a for a, b in zip(marks, marks[1:], strict=False)) <= 2


async def test_joint_video_analysis_receives_question_caption_speech_and_ordered_frames(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100, caption="Подпись автора про шутку")
        question = event(3, -100, "Заря, в чём шутка?", reply_to_message=post["message"])
        await store.ingest("999", "zarya_test", [post, question])
        await dialogue.claim("999")  # ambient
        await dialogue.claim("999")  # addressed
        work = await process(media)
        request = model.calls[-1]
        content = request["input"][0]["content"]
        evidence = json.loads(content[0]["text"])
        assert evidence["question"] == "Заря, в чём шутка?"
        assert evidence["caption"] == "Подпись автора про шутку"
        assert evidence["transcript"] == speech.result.text
        assert evidence["frame_times"] == [2.0] and content[1]["type"] == "input_image"
        assert request["reasoning"] == {"effort": "medium"}
        result = (await media.details(work["id"], "999"))["result"]
        assert result["timeline"][0]["seconds"] == 2.0
        answer = await dialogue.claim("999")
        assert "Ответ на первоначальный вопрос" in answer["request"]["input"]
        await dialogue.execute(answer)
        # Another participant/new question gets content, not the previous answer.
        follow = event(4, -100, "Заря, что в конце?", reply_to_message=post["message"])
        follow["message"]["from"]["id"] = 43
        await store.ingest("999", "zarya_test", [follow])
        newer = await dialogue.claim("999")
        assert newer["snapshot"]["media_refs"] == [{"id": work["id"]}]
        selected = json.loads(newer["request"]["input"])["selected_material"]
        assert "Ответ на первоначальный вопрос" not in json.dumps(selected, ensure_ascii=False)
        assert len(speech.calls) == 1 and await media.claim("999") is None


async def test_upgrade_reuses_legacy_analysis_without_paid_retry(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video")
        await store.ingest("999", "zarya_test", [post])
        await dialogue.claim("999")
        work = await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE media_runs SET processing_version='telegram-media-v1'")
        await store.ingest(
            "999", "zarya_test", [event(3, text="Что в ролике?", reply_to_message=post["message"])]
        )
        follow = await dialogue.claim("999")
        assert follow["snapshot"]["media_refs"] == [{"id": work["id"]}]
        assert await media.claim("999") is None
        assert len(speech.calls) == 1
        assert (await store.db.one("SELECT COUNT(*) FROM media_runs"))[0] == 1


@pytest.mark.parametrize("during", ["asr", "vision", "after"])
async def test_edit_analysis_question_erases_derived_answer_keeps_independent_media(
    tmp_path, during
):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100, caption="Независимый ролик")
        question = event(3, -100, "Заря, объясни", reply_to_message=post["message"])
        await store.ingest("999", "zarya_test", [post, question])
        await dialogue.claim("999")
        await dialogue.claim("999")
        work = await media.claim("999")
        blocked = speech if during == "asr" else model
        if during != "after":
            blocked.release = asyncio.Event()
        task = asyncio.create_task(media.execute(work, Downloader()))
        if during == "after":
            await task
            answer = await dialogue.claim("999")
            await dialogue.execute(answer)
        else:
            await blocked.entered.wait()
        edit = event(4, -100, "Заря, другой вопрос", reply_to_message=post["message"])
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 3
        await store.ingest("999", "zarya_test", [edit])
        if during != "after":
            blocked.release.set()
            await task
        detail = await media.details(work["id"], "999")
        assert detail["available"] and detail["transcript"] == speech.result.text
        assert detail["result"] is None and len(detail["frames"]) == 1
        assert detail["state"] == "partial"
        calls = await store.db.all("SELECT operation,request_json FROM model_calls")
        assert all(c[1] is None for c in calls if c[0] == "vision")
        if during == "after":
            with pytest.raises(ValueError):
                await dialogue.replay(answer["run_id"], "recorded")


async def test_question_retention_purges_analysis_before_original_video(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100)
        await store.ingest(
            "999",
            "zarya_test",
            [post, event(3, -100, "Заря, объясни", reply_to_message=post["message"])],
        )
        await dialogue.claim("999")
        await dialogue.claim("999")
        work = await process(media)
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE events SET received_at='2020-01-01T00:00:00+00:00' WHERE update_id=3"
            )
        await media.cleanup("999")
        detail = await media.details(work["id"], "999")
        assert detail["available"] and detail["result"] is None
        assert detail["transcript"] and detail["frames"]


async def test_schema_upgrade_preserves_custom_frame_limit(tmp_path):
    from zarya.database import Database

    database = Database(tmp_path / "settings.db")
    await database.open()
    settings = (await database.settings()).settings
    settings.video_max_frames = 3
    await database.update_settings(1, settings)
    async with database.transaction() as conn:
        await conn.execute("DELETE FROM schema_migrations WHERE version='011_video_context.sql'")
        await conn.execute("ALTER TABLE media_runs DROP COLUMN analysis_context")
    await database.close()
    await database.open()
    snapshot = await database.settings()
    assert snapshot.settings.video_max_frames == 3 and snapshot.version == 2
    await database.close()


async def test_question_edit_erases_research_using_prior_media_analysis_with_memory_off(tmp_path):
    from test_research import Fetch, ResearchModel

    async with store_at(tmp_path) as store:
        await ready(store, -100)
        settings = await store.db.settings()
        settings.settings.memory_enabled = False
        await store.db.update_settings(settings.version, settings.settings)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100)
        first = event(3, -100, "Заря, объясни", reply_to_message=post["message"])
        await store.ingest("999", "zarya_test", [post, first])
        await dialogue.claim("999")
        await dialogue.claim("999")
        await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        dialogue.research = ResearchEngine(store, ResearchModel(), Fetch())
        follow = event(4, -100, "Заря, проверь", reply_to_message=post["message"])
        follow["message"]["from"]["id"] = 43
        await store.ingest("999", "zarya_test", [follow])
        assert (await dialogue.claim("999"))["skip"]
        research = await store.db.one("SELECT id,material,manifest FROM research_runs")
        assert "Красный фон" in research[1]
        assert any(ref["message_id"] == 3 for ref in json.loads(research[2]))
        edit = event(5, -100, "Заря, изменённый вопрос", reply_to_message=post["message"])
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 3
        await store.ingest("999", "zarya_test", [edit])
        cleared = await store.db.one(
            "SELECT state,material,result FROM research_runs WHERE id=?", (research[0],)
        )
        assert cleared == ("cancelled", "", None)
        assert await dialogue.research.claim("999") is None


async def test_real_ffmpeg_dense_clip_captures_beginning_and_ending(tmp_path):
    from PIL import Image

    processor = MediaProcessor()
    if not processor.available:
        pytest.skip("FFmpeg not installed")
    source = tmp_path / "sequence.mp4"
    await processor.run(
        processor.ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=320x240:d=8",
        "-f",
        "lavfi",
        "-i",
        "color=c=green:s=320x240:d=8",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x240:d=8",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        str(source),
    )
    result = await processor.prepare(
        source.read_bytes(), tmp_path / "1", "video", {"voice": 300, "video": 120, "frames": 24}
    )
    assert len(result["frames"]) == 13
    first = Image.open(tmp_path / result["frames"][0]["path"]).getpixel((10, 10))
    last = Image.open(tmp_path / result["frames"][-1]["path"]).getpixel((10, 10))
    assert first[0] > 200 and first[2] < 30
    assert last[2] > 200 and last[0] < 30


async def test_spoken_reply_is_question_about_selected_video_not_latest_media(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, media, speech, model = engines(
            store, tmp_path, Speech(ModelResult(text="Что происходит в конце ролика?"))
        )
        post = media_event(2, "video", caption="Первый ролик")
        await store.ingest("999", "zarya_test", [post])
        await dialogue.claim("999")
        first = await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        unrelated = media_event(3, "video", caption="Другой ролик")
        await store.ingest("999", "zarya_test", [unrelated])
        await dialogue.claim("999")
        await process(media)
        other_answer = await dialogue.claim("999")
        await dialogue.execute(other_answer)
        spoken = media_event(4, reply_to_message=post["message"])
        await store.ingest("999", "zarya_test", [spoken])
        assert (await dialogue.claim("999"))["skip"]
        voice = await process(media)
        follow = await dialogue.claim("999")
        assert {ref["id"] for ref in follow["snapshot"]["media_refs"]} == {first["id"], voice["id"]}
        assert "Первый ролик" in follow["request"]["input"]
        assert "Что происходит в конце ролика?" in follow["request"]["input"]


async def test_late_paid_replay_cannot_restore_erased_question_analysis(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, media, speech, model = engines(store, tmp_path)
        post = media_event(2, "video", -100)
        await store.ingest(
            "999",
            "zarya_test",
            [post, event(3, -100, "Заря, объясни", reply_to_message=post["message"])],
        )
        await dialogue.claim("999")
        await dialogue.claim("999")
        await process(media)
        answer = await dialogue.claim("999")
        await dialogue.execute(answer)
        follow = event(4, -100, "Заря, что в конце?", reply_to_message=post["message"])
        follow["message"]["from"]["id"] = 43
        await store.ingest("999", "zarya_test", [follow])
        second = await dialogue.claim("999")
        assert any(ref["message_id"] == 3 for ref in second["snapshot"]["source_manifest"])
        await dialogue.execute(second)
        model.entered.clear()
        model.release = asyncio.Event()
        settings = await store.db.settings()
        task = asyncio.create_task(dialogue.replay(second["run_id"], "paid", settings.version))
        await model.entered.wait()
        edit = event(5, -100, "Заря, другой вопрос", reply_to_message=post["message"])
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 3
        await store.ingest("999", "zarya_test", [edit])
        model.release.set()
        replay = await task
        assert replay["state"] == "cancelled" and not replay["response"]
        assert replay["snapshot"].get("invalidated")
        assert replay["call"]["usage"] is not None
        assert (
            await store.db.one(
                "SELECT request_json FROM model_calls WHERE run_id=?", (replay["id"],)
            )
        )[0] is None
