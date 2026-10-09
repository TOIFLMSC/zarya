import asyncio
import json

import pytest
from test_photos import picture, ready
from test_telegram import FakeTransport, event, store_at

from zarya.avatar_logic import valid
from zarya.avatars import AvatarEngine
from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.operations import Operations
from zarya.telegram_runtime import TelegramRuntime


class Model:
    def __init__(self):
        self.requests = []
        self.release = None
        self.entered = asyncio.Event()
        self.result = None

    async def generate(self, request):
        self.requests.append(request)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.result:
            return self.result
        if isinstance(request["input"], list):
            count = sum(p["type"] == "input_image" for p in request["input"][0]["content"])
            text = json.dumps(
                {"descriptions": [f"Красная картинка {i}" for i in range(count)], "uncertainty": ""}
            )
        else:
            text = "прикольная ава, цвет прям в тему"
        return ModelResult(text=text, usage={"input_tokens": 100, "output_tokens": 40})


class Transport(FakeTransport):
    def __init__(self, counts=None):
        super().__init__()
        self.ids = {
            str(k): [f"{k}-{i}" for i in range(n)] for k, n in (counts or {42: 12, 77: 3}).items()
        }
        self.list_calls = []
        self.downloads = []
        self.fail = False

    async def profile_photos(self, user_id, offset, limit):
        self.list_calls.append((user_id, offset, limit))
        if self.fail:
            raise TimeoutError
        ids = self.ids.get(user_id, [])
        return {
            "total_count": len(ids),
            "photos": [
                [
                    {
                        "file_id": i,
                        "file_unique_id": i,
                        "width": 100,
                        "height": 100,
                    }
                ]
                for i in ids[offset : offset + limit]
            ],
        }

    async def download_photo(self, file_id):
        self.downloads.append(file_id)
        return picture()


def engines(store):
    model = Model()
    avatars = AvatarEngine(store, model)
    dialogue = DialogueEngine(store, model)
    dialogue.avatars = avatars
    return dialogue, avatars, model


async def process(store, dialogue, avatars, transport, number, text, reply=None, chat=42):
    await store.ingest(
        "999",
        "zarya_test",
        [event(number, chat, text, **({"reply_to_message": reply} if reply else {}))],
    )
    result = await dialogue.claim("999")
    if result["skip"]:
        work = await avatars.claim("999")
        if work and not work.get("skip"):
            await avatars.execute(work, transport)
        result = await dialogue.claim("999")
    assert result and not result["skip"]
    await dialogue.execute(result)
    async with store.db.transaction() as c:
        await c.execute("UPDATE outbox SET next_attempt=0")
        await c.execute("UPDATE delivery_limits SET last_attempt=0")
    await TelegramRuntime(store, transport=transport, dialogue=dialogue).deliver_one("999")
    return result


async def test_own_current_followups_cache_and_cost(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        first = await process(store, dialogue, avatars, transport, 2, "Как тебе моя ава?")
        assert transport.downloads == ["42-0"]
        assert first["snapshot"]["avatar_id"]
        assert json.loads(first["request"]["input"])["avatar_profile"]["analysed_indices"] == [1]
        second = await process(store, dialogue, avatars, transport, 3, "Как тебе моя ава?")
        assert len([r for r in model.requests if isinstance(r["input"], list)]) == 1
        assert json.loads(second["request"]["input"])["avatar_profile"]["state"] == "cache"
        # The fake sender uses message_id=123 for its successfully sent message.
        reply = {"message_id": 123, "from": {"id": 999, "is_bot": True}}
        third = await process(store, dialogue, avatars, transport, 4, "А остальные?", reply)
        profile = json.loads(third["request"]["input"])["avatar_profile"]
        assert profile["user_id"] == "42" and profile["analysed_indices"] == list(range(2, 10))
        assert profile["available_count"] == 12 and profile["has_more"]
        stats = await Operations(store.db).overview(bot="999")
        assert next(r for r in stats["breakdown"]["task"] if r["label"] == "avatar")["calls"] == 2
        assert not await store.db.one(
            "SELECT 1 FROM model_calls WHERE request_json LIKE '%base64%'"
        )


async def test_reply_selects_person_not_latest_photo_and_typing(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        target = event(2, -100, "Сообщение участника")["message"]
        target["from"] = {"id": 77, "is_bot": False, "first_name": "Другой"}
        await store.ingest("999", "zarya_test", [{"update_id": 2, "message": target}])
        await dialogue.claim("999")  # Ambient message is not an avatar request.
        assert not transport.list_calls
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, -100, "Заря, как тебе его ава?", reply_to_message=target)],
        )
        assert (await dialogue.claim("999"))["skip"]
        pulses = []

        async def typing(chat, thread):
            pulses.append(chat)
            return True

        transport.typing = typing
        await TelegramRuntime(store, transport=transport, dialogue=dialogue).type_one("999")
        assert pulses == ["-100"]
        work = await avatars.claim("999")
        assert work["user"] == "77"
        await avatars.execute(work, transport)
        result = await dialogue.claim("999")
        assert result["snapshot"]["photo_refs"] == []
        assert transport.downloads == ["77-0"]


@pytest.mark.parametrize("kind", ["empty", "network", "anonymous"])
async def test_unavailable_honest_no_paid_vision(tmp_path, kind):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport({42: 0} if kind == "empty" else None)
        transport.fail = kind == "network"
        reply = {"message_id": 99, "sender_chat": {"id": -100}} if kind == "anonymous" else None
        answer = await process(
            store,
            dialogue,
            avatars,
            transport,
            2,
            "Как тебе его ава?" if reply else "Как тебе моя ава?",
            reply,
        )
        profile = json.loads(answer["request"]["input"])["avatar_profile"]
        assert profile["observations"] is None and profile["analysed_indices"] == []
        assert not [r for r in model.requests if isinstance(r["input"], list)]


async def test_profile_shift_follows_identity_and_deleted_anchor_is_not_reused(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, _ = engines(store)
        transport = Transport()
        await process(store, dialogue, avatars, transport, 2, "Моя ава как тебе?")
        transport.ids["42"].insert(0, "new")
        reply = {"message_id": 123, "from": {"id": 999, "is_bot": True}}
        answer = await process(store, dialogue, avatars, transport, 3, "А предыдущая?", reply)
        assert transport.downloads[-1] == "42-1"  # Not old numeric offset 1 -> 42-0.
        assert json.loads(answer["request"]["input"])["avatar_profile"]["list_changed"]
        transport.ids["42"].remove("42-1")
        answer = await process(store, dialogue, avatars, transport, 4, "А предыдущая?", reply)
        assert json.loads(answer["request"]["input"])["avatar_profile"]["observations"] is None


async def test_revoke_during_paid_call_keeps_cost_erases_content(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        await store.ingest("999", "zarya_test", [event(2, text="Моя ава как тебе?")])
        await dialogue.claim("999")
        work = await avatars.claim("999")
        model.release = asyncio.Event()
        task = asyncio.create_task(avatars.execute(work, transport))
        await model.entered.wait()
        await store.decide("999", "private", "42", "revoked", 2)
        model.release.set()
        await task
        assert (await store.db.one("SELECT state,result,user_id FROM avatar_runs")) == (
            "cancelled",
            None,
            "",
        )
        assert (await store.db.one("SELECT cost_usd FROM model_calls"))[0] > 0
        assert not await store.db.one("SELECT 1 FROM outbox")


async def test_restart_unknown_no_repeated_paid_call_and_edit_purge(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        answer = await process(store, dialogue, avatars, transport, 2, "Моя ава как тебе?")
        run = answer["snapshot"]["avatar_id"]
        async with store.db.transaction() as c:
            await c.execute("UPDATE avatar_runs SET state='analyzing'")
            await c.execute(
                "UPDATE model_calls SET state='started' WHERE avatar_run_id IS NOT NULL"
            )
        await avatars.recover("999")
        assert (await store.db.one("SELECT state FROM avatar_runs"))[0] == "unknown"
        assert await avatars.claim("999") is None
        update = event(3, text="Новый текст")
        update["edited_message"] = update.pop("message")
        update["edited_message"]["message_id"] = 2
        await store.ingest("999", "zarya_test", [update])
        async with store.db.transaction() as c:
            assert not await valid(c, run)
        assert (await store.db.one("SELECT result FROM avatar_runs"))[0] is None
        assert json.loads((await store.db.one("SELECT snapshot FROM dialogue_runs"))[0])[
            "invalidated"
        ]


async def test_scope_cache_isolation_and_unrelated_followup(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        await process(store, dialogue, avatars, transport, 2, "Как тебе моя ава?")
        reply = {"message_id": 123, "from": {"id": 999, "is_bot": True}}
        await store.ingest(
            "999",
            "zarya_test",
            [event(3, text="А какую видеокарту лучше купить?", reply_to_message=reply)],
        )
        work = await dialogue.claim("999")
        assert work["snapshot"]["avatar_id"] is None
        await dialogue.execute(work)
        from test_telegram import approve

        await store.ingest("999", "zarya_test", [event(10, -100)])
        await approve(store, "group", "-100")
        await process(store, dialogue, avatars, transport, 20, "Заря, как тебе моя ава?", chat=-100)
        assert len([r for r in model.requests if isinstance(r["input"], list)]) == 2


async def test_metadata_pagination_and_unavailable_changed_during_download(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport({42: 105})
        answer = await process(store, dialogue, avatars, transport, 2, "Посмотри все мои аватарки")
        assert ("42", 100, 100) in transport.list_calls
        profile = json.loads(answer["request"]["input"])["avatar_profile"]
        assert profile["available_count"] == 105 and len(profile["analysed_indices"]) == 8
        original = transport.download_photo

        async def disappear(file_id):
            data = await original(file_id)
            transport.ids["42"] = []
            return data

        transport.download_photo = disappear
        answer = await process(store, dialogue, avatars, transport, 3, "Моя ава как тебе?")
        assert json.loads(answer["request"]["input"])["avatar_profile"]["observations"] is None
        assert len([r for r in model.requests if isinstance(r["input"], list)]) == 1


@pytest.mark.parametrize(
    "question,indices",
    [
        ("Оцени цвет моей авы", [1]),
        ("Как тебе моя вторая ава?", [2]),
        ("Как тебе моя третья аватарка?", [3]),
        ("Сравни первую и вторую мои аватарки", list(range(1, 9))),
        ("Посмотри все мои предыдущие аватарки", list(range(2, 10))),
    ],
)
async def test_selection_modes(tmp_path, question, indices):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, _ = engines(store)
        answer = await process(store, dialogue, avatars, Transport(), 2, question)
        assert answer["snapshot"]["avatar_profile"]["analysed_indices"] == indices


async def test_nonforum_raw_thread_reply_and_replay_no_profile_fetch(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store, -100)
        dialogue, avatars, _ = engines(store)
        transport = Transport()
        first = await process(
            store, dialogue, avatars, transport, 2, "Заря, как тебе моя ава?", chat=-100
        )
        count = len(transport.list_calls)
        await dialogue.replay(first["run_id"], "recorded")
        await dialogue.replay(first["run_id"], "paid", (await store.db.settings()).version)
        assert len(transport.list_calls) == count
        update = event(
            3,
            -100,
            "А остальные?",
            message_thread_id=6,
            reply_to_message={"message_id": 123, "from": {"id": 999, "is_bot": True}},
        )
        update["message"]["chat"]["is_forum"] = False
        await store.ingest("999", "zarya_test", [update])
        assert (await dialogue.claim("999"))["skip"]
        work = await avatars.claim("999")
        assert work and work["user"] == "42" and work["mode"] == "next"


async def test_new_question_while_downloading_prevents_stale_payment(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, model = engines(store)
        transport = Transport()
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(file_id):
            entered.set()
            await release.wait()
            return picture()

        transport.download_photo = blocked
        await store.ingest("999", "zarya_test", [event(2, text="Как тебе моя ава?")])
        await dialogue.claim("999")
        task = asyncio.create_task(avatars.execute(await avatars.claim("999"), transport))
        await entered.wait()
        await store.ingest("999", "zarya_test", [event(3, text="Лучше расскажи про DNS")])
        release.set()
        await task
        assert not model.requests
        assert not await store.db.one("SELECT 1 FROM model_calls")


async def test_retention_erases_avatar_observations_and_replays(tmp_path):
    from zarya.memory import MemoryEngine

    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, _ = engines(store)
        answer = await process(store, dialogue, avatars, Transport(), 2, "Как тебе моя ава?")
        async with store.db.transaction() as c:
            await c.execute("UPDATE events SET received_at='2020-01-01T00:00:00+00:00'")
        await MemoryEngine(store, None, tmp_path).cleanup("999", force=True)
        assert (await store.db.one("SELECT result,selection FROM avatar_runs")) == (None, "{}")
        with pytest.raises(ValueError):
            await dialogue.replay(answer["run_id"], "recorded")


async def test_ordinal_followup_with_opinion_wording(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        dialogue, avatars, _ = engines(store)
        transport = Transport()
        await process(store, dialogue, avatars, transport, 2, "Моя ава как тебе?")
        answer = await process(
            store,
            dialogue,
            avatars,
            transport,
            3,
            "А как тебе вторая?",
            {"message_id": 123, "from": {"id": 999, "is_bot": True}},
        )
        assert answer["snapshot"]["avatar_profile"]["analysed_indices"] == [2]
