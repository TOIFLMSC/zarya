"""Browser test server with synthetic Telegram events and zero external calls."""

import asyncio
import io
import json

import uvicorn
from PIL import Image, ImageDraw

from zarya.app import create_app
from zarya.config import Config
from zarya.openai_adapter import ModelResult


class BrowserModel:
    async def transcribe(self, model, audio):
        return ModelResult(
            text="Привет, это короткий ролик из книжного клуба.",
            model=model,
            usage={"type": "tokens", "input_tokens": 100, "output_tokens": 25},
        )

    async def generate(self, request):
        await asyncio.sleep(0.1)
        if request.get("text", {}).get("format", {}).get("name") == "behavior_plan":
            state = json.loads(request["input"])["behavior"]
            return ModelResult(
                text=json.dumps(
                    {
                        "action": "text" if state["mode"] == "shadow" else "reaction",
                        "text": "О, вот это сюжетный поворот" if state["mode"] == "shadow" else "",
                        "reaction": None if state["mode"] == "shadow" else "❤",
                        "tone": "warm",
                        "intensity": 0.3,
                        "public_reason": "Дружеская шутка в разговоре",
                    },
                    ensure_ascii=False,
                ),
                usage={"input_tokens": 200, "output_tokens": 80},
            )
        if request.get("tools"):
            return ModelResult(
                text=json.dumps(
                    {
                        "summary": "Нужны источники, а не уверенный тон.",
                        "claims": [
                            {
                                "text": "Утверждение подтверждается источником",
                                "kind": "fact",
                                "verdict": "supported",
                                "urls": ["https://example.org/evidence"],
                            }
                        ],
                    }
                ),
                usage={"input_tokens": 350, "output_tokens": 100},
                search_actions=[
                    {
                        "type": "search",
                        "queries": ["проверка публичного утверждения"],
                        "sources": [{"url": "https://example.org/evidence"}],
                        "status": "completed",
                    }
                ],
            )
        if request.get("text", {}).get("format", {}).get("name") == "participant_memory":
            sources = json.loads(request["input"])["sources"]
            return ModelResult(
                text=json.dumps(
                    {
                        "summary": "Обсудили аргументы и источники в соседних топиках.",
                        "facts": [
                            {
                                "sender_id": "42",
                                "category": "interest",
                                "key": "interest_python",
                                "text": "Любит Python и проверять источники.",
                                "provenance": "self",
                                "source_ids": [sources[0]["id"]],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                usage={"input_tokens": 200, "output_tokens": 100},
            )
        if request.get("text", {}).get("format", {}).get("name") == "media_observation":
            evidence = json.loads(request["input"][0]["content"][0]["text"])
            return ModelResult(
                text=json.dumps(
                    {
                        "observations": "Светлая вывеска магазина книг.",
                        "visible_text": "Книжный клуб",
                        "interpretation": "Похоже на небольшой книжный магазин.",
                        "timeline": [
                            {
                                "seconds": evidence["frame_times"][0],
                                "visual": "В начале видна вывеска",
                            },
                            {
                                "seconds": evidence["frame_times"][-1],
                                "visual": "В конце — вход в магазин",
                            },
                        ],
                        "speech_summary": "Говорят про книжный клуб.",
                        "question_answer": "Ролик про книжный клуб.",
                        "uncertainty": "Промежутки не просмотрены.",
                        "evidence_basis": "sampled_frames_and_transcript",
                    },
                    ensure_ascii=False,
                ),
                usage={"input_tokens": 900, "output_tokens": 200},
            )
        if "format" in request["text"]:
            return ModelResult(
                text=json.dumps(
                    {
                        "observations": "Светлая вывеска магазина книг.",
                        "visible_text": "Книжный клуб",
                        "interpretation": "Похоже на небольшой книжный магазин.",
                        "uncertainty": "Нижняя строка обрезана.",
                    },
                    ensure_ascii=False,
                ),
                usage={"input_tokens": 900, "output_tokens": 120},
            )
        return ModelResult(
            text="Давай разберёмся. В соседнем топике уже был похожий вопрос.\n\n"
            "Есть две точки зрения: сравним аргументы и уточним, что можно проверить.",
            usage={"input_tokens": 350, "output_tokens": 75},
        )

    async def close(self):
        pass


class BrowserTelegram:
    def __init__(self):
        self.delivered = False
        self.question = False
        self.store = None
        self.message_id = 100
        self.photos_delivered = False
        self.media_delivered = False
        self.media_requested = False

    async def identity(self):
        return {
            "id": "999",
            "username": "zarya_fixture_bot",
            "name": "Заря",
            "privacy_disabled": True,
            "webhook": False,
        }

    async def updates(self, offset):
        if not self.delivered:
            self.delivered = True
            return [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 42, "type": "private"},
                        "from": {
                            "id": 42,
                            "first_name": "Тестовый собеседник",
                            "username": "test_person",
                        },
                        "text": "/start",
                    },
                },
                {
                    "update_id": 2,
                    "my_chat_member": {
                        "chat": {
                            "id": -100,
                            "type": "supergroup",
                            "title": "Тестовая группа друзей",
                        },
                        "new_chat_member": {"status": "member"},
                    },
                },
            ]
        await asyncio.sleep(0.1)
        if self.store and not self.question:
            access = await self.store.db.one(
                "SELECT state FROM telegram_access WHERE bot_id='999' "
                "AND scope='group' AND subject_id='-100'"
            )
            if access and access[0] == "approved":
                self.question = True
                return [
                    {
                        "update_id": i,
                        "message": {
                            "message_id": i,
                            "chat": {"id": -100, "type": "supergroup", "is_forum": True},
                            "from": {"id": 42, "first_name": "Тестовый собеседник"},
                            "message_thread_id": 7 if i == 3 else 8,
                            "text": "В соседнем топике обсуждали источники."
                            if i == 3
                            else "Заря, как сравнивать аргументы и проверять источники?",
                        },
                    }
                    for i in (3, 4)
                ]
        if self.question and not self.photos_delivered:
            self.photos_delivered = True
            return [
                {
                    "update_id": 20,
                    "message": {
                        "message_id": 20,
                        "chat": {"id": -100, "type": "supergroup"},
                        "from": {"id": 42, "first_name": "Тестовый собеседник"},
                        "caption": "Новая вывеска нашего книжного",
                        "photo": [
                            {
                                "file_id": "fixture-photo",
                                "file_unique_id": "fixture-photo",
                                "width": 640,
                                "height": 400,
                            }
                        ],
                    },
                }
            ]
        if self.store and self.photos_delivered:
            if self.media_requested and not self.media_delivered:
                pending = await self.store.db.one(
                    "SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')"
                )
                if not pending[0]:
                    self.media_delivered = True
                    return [
                        {
                            "update_id": 21,
                            "message": {
                                "message_id": 21,
                                "chat": {"id": -100, "type": "supergroup"},
                                "from": {"id": 42, "first_name": "Тестовый собеседник"},
                                "caption": "Заря, что в этом ролике?",
                                "video": {"file_id": "fixture-video", "duration": 4},
                            },
                        }
                    ]
            async with self.store.gate, self.store.db.transaction() as conn:
                await conn.execute(
                    "UPDATE memory_sources SET received_at=received_at-61 WHERE processed=0 "
                    "AND received_at>strftime('%s','now')-60"
                )
        return []

    async def download_photo(self, file_id):
        output = io.BytesIO()
        image = Image.new("RGB", (640, 400), "#FAF7F5")
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 80, 590, 300), fill="#8B3445")
        draw.text((140, 160), "BOOK CLUB", fill="white", font_size=48)
        image.save(output, "JPEG")
        return output.getvalue()

    async def download_media(self, file_id):
        return await self.download_photo(file_id)

    async def send(self, chat_id, text, thread_id, reply_id=None):
        self.message_id += 1
        return self.message_id

    async def typing(self, chat_id, thread_id):
        return True

    async def react(self, chat_id, message_id, emoji):
        return True

    async def close(self):
        pass


class BrowserFetch:
    async def fetch(self, url):
        return {
            "url": url,
            "title": "Тестовая статья",
            "text": "Тестовый фрагмент статьи.",
            "coverage": "partial_text",
            "error": None,
        }


class BrowserMediaProcessor:
    available = True

    async def prepare(self, data, directory, kind, limits):
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(2):
            name = f"frame-{index}.jpg"
            (directory / name).write_bytes(data)
            frames.append({"path": f"{directory.name}/{name}", "seconds": float(index * 2 + 1)})
        return {
            "duration": 4.0,
            "frames": frames,
            "audio": b"synthetic-audio",
            "coverage": {
                "audio_signal": "present",
                "audio_intervals": [],
                "frame_times": [1.0, 3.0],
                "note": "Выбранные кадры; промежутки между ними не просмотрены.",
            },
        }


if __name__ == "__main__":
    # Synthetic UI harness deliberately uses the neutral published instructions.
    # It must not require or load the working installation's private prompt file.
    from dataclasses import replace

    config = replace(Config.from_env(), prompts_file=None)
    transport = BrowserTelegram()
    app = create_app(
        config,
        telegram_transport=transport,
        model_adapter=BrowserModel(),
        research_fetcher=BrowserFetch(),
        speech_adapter=BrowserModel(),
    )
    app.state.media.processor = BrowserMediaProcessor()
    transport.store = app.state.telegram_store

    @app.get("/fixture-media")
    async def request_media():
        # Test server only; enables the synthetic update after dialogue assertions.
        transport.media_requested = True
        return {"synthetic": True}

    @app.get("/fixture-behavior")
    async def request_behavior(kind: str):
        # Test-only synthetic events; never installed on the production application.
        cfg = await app.state.db.settings()
        await app.state.db.update_settings(
            cfg.version, cfg.settings.model_copy(update={"memory_enabled": False})
        )
        index = 40 if kind == "addressed" else 41
        if kind == "ambient":
            async with app.state.db.transaction() as conn:
                await conn.execute("DELETE FROM active_dialogues")
        await transport.store.ingest(
            "999",
            "zarya_fixture_bot",
            [
                {
                    "update_id": index,
                    "message": {
                        "message_id": index,
                        "chat": {"id": -100, "type": "supergroup"},
                        "from": {"id": 42, "first_name": "Тестовый собеседник"},
                        "text": "Заря, спасибо, смешно получилось"
                        if kind == "addressed"
                        else "Вот это сегодня сюжетный поворот",
                    },
                }
            ],
        )
        return {"synthetic": True}

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="warning",
    )
