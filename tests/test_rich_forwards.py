"""Telegram forwards bind the author's question to the chosen post."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from test_dialogue import FakeModel, drain, private_ready
from test_photos import PhotoTransport, VisionModel, analyze, photo, ready
from test_research import Fetch, ResearchModel
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.openai_adapter import ModelResult
from zarya.photos import PhotoEngine
from zarya.research import ResearchEngine
from zarya.research_intent import expects_post_comment, needs_forwarded_text
from zarya.telegram_content import content_text
from zarya.telegram_runtime import AiogramTransport

ASK = "тут большой пост, можешь его содержание немного ужать"
BODY = "Новый пост про сюжет игры и её героев."
OLD = "Старый пост про Ялту"
UNREADABLE = "Не получилось прочитать пересланный пост"
USDT_QUESTION = "Реально будут брать НДС при покупке USDT? Я заплачу за 1000 как за 1220?"
GROUP_ASK = "Давай комментарии, Заря"
GROUP_NAMED_SUBJECT = "Дай комментарий, Заря, по поводу закрытия стадиона"
GROUP_CAPTION = "Тарифы биржи на вывод BTC. Подробности"
GROUP_URL = "https://example.org/tariffs"


def group_post(number, date, **extra):
    post = photo(number, -100, caption=GROUP_CAPTION, file="timetable")
    post["message"].update(
        date=date,
        forward_origin={"type": "channel"},
        caption_entities=[{"type": "text_link", "offset": 0, "length": 1, "url": GROUP_URL}],
        **extra,
    )
    return post


@pytest.mark.parametrize(
    "delivery",
    [
        "one_poll",
        "two_polls",
        "without_batch",
        "edited",
        "edited_after_delivery",
        "mention",
        "related_subject",
    ],
)
async def test_addressed_group_question_binds_forward_with_photo_and_link(tmp_path, delivery):
    date = int(time.time())
    question_text = GROUP_ASK
    question_extra = {}
    caption = GROUP_CAPTION
    if delivery == "mention":
        mention = "@zarya_test"
        question_text = f"{mention}, прокомментируй"
        question_extra["entities"] = [{"type": "mention", "offset": 0, "length": len(mention)}]
    if delivery == "related_subject":
        question_text = GROUP_NAMED_SUBJECT
        caption = "Стала известна причина закрытия стадиона. Подробности"
    question = event(2, -100, question_text, date=date, **question_extra)
    post = group_post(3, date, caption=caption)
    async with store_at(tmp_path) as store:
        await ready(store, -100, message_batches=delivery != "without_batch")
        model = FakeModel(ModelResult(text="Подробности в публикации."))
        fetch = Fetch()
        transport = PhotoTransport()
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos = PhotoEngine(store, VisionModel(), tmp_path)
        dialogue.research = research = ResearchEngine(store, ResearchModel(), fetch)
        if delivery == "one_poll":
            await store.ingest("999", "zarya_test", [question, post])
        else:
            await store.ingest("999", "zarya_test", [question])
            assert await dialogue.claim("999") is None
            await store.ingest("999", "zarya_test", [post])
        jobs = await store.db.all("SELECT state,payload FROM jobs ORDER BY id")
        assert [(state, json.loads(p)["message_id"]) for state, p in jobs] == [
            ("cancelled", 2),
            ("pending", 3),
        ]
        assert json.loads(jobs[-1][1])["post_question"]["message_id"] == 2
        if delivery == "edited":
            question_text = "Заря, поясни, что меняется в тарифах"
            edited = event(4, -100, question_text, date=date, message_id=2)
            await store.ingest(
                "999", "zarya_test", [{"update_id": 4, "edited_message": edited["message"]}]
            )
        assert await dialogue.claim("999") == {"skip": True}
        await analyze(photos, transport)
        work = await answer(dialogue, research, 2)
        await drain(dialogue)
        body = json.loads(work["request"]["input"])
        assert body["current_message"]["text"] == question_text
        assert caption in body["selected_material"]["messages"][0]["text"]
        assert {r["message_id"] for r in work["snapshot"]["source_manifest"]} == (
            {3} if delivery == "edited" else {2, 3}
        )
        assert transport.downloads == ["timetable"]
        assert fetch.calls == [GROUP_URL] and len(model.calls) == 1
        (payload,) = await store.db.one("SELECT payload FROM outbox")
        assert json.loads(payload)["reply_id"] == 2
        if delivery in {"edited", "edited_after_delivery"}:
            await deliver(store, work, 100)
            if delivery == "edited_after_delivery":
                edited = event(4, -100, "Заря, про тарифы биржи", date=date, message_id=2)
                await store.ingest(
                    "999", "zarya_test", [{"update_id": 4, "edited_message": edited["message"]}]
                )
            followup_text = "Сколько стоит вывод BTC на этой бирже?"
            followup = event(
                5,
                -100,
                followup_text,
                date=date,
                reply_to_message={"message_id": 100, "from": {"id": 999, "is_bot": True}},
            )
            await store.ingest("999", "zarya_test", [followup])
            work = await answer(dialogue, research, 5)
            selected = json.loads(work["request"]["input"])["selected_material"]
            assert selected["messages"][0]["message_id"] == 3
            assert selected["messages"][0]["urls"] == [GROUP_URL]
            stored = await store.db.one(
                "SELECT question,sources FROM research_runs WHERE job_id=?", (work["job_id"],)
            )
            assert stored[0] == followup_text
            assert GROUP_URL in stored[1] and fetch.calls.count(GROUP_URL) == 2


async def test_group_pair_adds_album_tail_to_open_photo_batch(tmp_path):
    date = int(time.time())
    first = group_post(3, date, media_group_id="tariffs")
    tail = photo(4, -100, album="tariffs", file="tariffs-tail")
    async with store_at(tmp_path) as store:
        await ready(store, -100, message_batches=True)
        model = FakeModel(ModelResult(text="Тарифы указаны в посте."))
        vision = VisionModel()
        dialogue = DialogueEngine(store, model)
        dialogue.photos = photos = PhotoEngine(store, vision, tmp_path)
        await store.ingest("999", "zarya_test", [event(2, -100, GROUP_ASK, date=date), first])
        assert await dialogue.claim("999") == {"skip": True}
        await store.ingest("999", "zarya_test", [tail])
        transport = PhotoTransport()
        await analyze(photos, transport)
        assert transport.downloads == ["timetable", "tariffs-tail"] and len(vision.calls) == 1
        work = await answer(dialogue, None, 2)
        assert {r["message_id"] for r in work["snapshot"]["photo_refs"]} == {3, 4}
        assert len(model.calls) == 1
        assert await dialogue.claim("999") is None


@pytest.mark.parametrize(
    "boundary",
    [
        "other_author",
        "other_topic",
        "two_texts",
        "unaddressed",
        "unrelated",
        "named_subject",
        "inline_quote",
        "blockquote_entity",
        "late",
    ],
)
async def test_group_question_does_not_capture_unrelated_forward(tmp_path, boundary):
    date = int(time.time())
    text = {
        "unaddressed": "Давай комментарии",
        "unrelated": "Как пожарить кабачки, Заря?",
        "named_subject": GROUP_NAMED_SUBJECT,
        "inline_quote": f"{GROUP_ASK}\n«Стадион закрывают на ремонт»",
        "blockquote_entity": f"{GROUP_ASK}\nСтадион закрывают на ремонт",
    }.get(boundary, GROUP_ASK)
    question_extra = {"message_thread_id": 8} if boundary == "other_topic" else {}
    if boundary == "blockquote_entity":
        offset = len(GROUP_ASK) + 1
        question_extra["entities"] = [
            {"type": "blockquote", "offset": offset, "length": len(text) - offset}
        ]
    question = event(2, -100, text, date=date, **question_extra)
    post = group_post(4 if boundary == "two_texts" else 3, date + (3 if boundary == "late" else 0))
    if boundary == "other_author":
        post["message"]["from"]["id"] = 43
    if boundary == "other_topic":
        post["message"]["chat"]["is_forum"] = True
        post["message"]["message_thread_id"] = 9
    updates = [question]
    if boundary == "two_texts":
        updates.append(event(3, -100, "Заря, назови цвет неба", date=date))
    updates.append(post)
    async with store_at(tmp_path) as store:
        await ready(store, -100, message_batches=True)
        await store.ingest("999", "zarya_test", updates)
        jobs = await store.db.all("SELECT state,payload FROM jobs ORDER BY id")
        assert all(
            state == "pending" and not json.loads(p).get("post_question") for state, p in jobs
        )
        assert json.loads(jobs[-1][1])["trigger"] == "forwarded"
        if boundary == "two_texts":
            assert await store.db.all(
                "SELECT message_id FROM text_batch_members ORDER BY message_id"
            ) == [(2,), (3,)]


def rich_post(number, *, readable=True):
    blocks = [
        {"type": "heading", "size": 2, "text": {"type": "bold", "text": "Сюжет игры"}},
        {
            "type": "paragraph",
            "text": [
                "Новый пост про ",
                {"type": "italic", "text": ["сюжет игры"]},
                " и ",
                {"type": "url", "text": "её героев", "url": "https://example.org/private"},
                ".",
            ],
        },
        {
            "type": "photo",
            "photo": [
                {"file_id": "UNREAD_IMAGE_ID", "file_unique_id": "image", "width": 1, "height": 1}
            ],
        },
    ]
    post = event(
        number,
        forward_origin={
            "type": "channel",
            "chat": {"id": -100, "type": "channel", "title": "Синтетический канал"},
            "date": 1,
            "message_id": 10,
        },
        rich_message={"blocks": blocks if readable else blocks[-1:]},
    )
    del post["message"]["text"]
    return post


@pytest.mark.parametrize("delivery", ["one_poll", "two_polls", "edited"])
@pytest.mark.parametrize(
    ("question_text", "post_text"),
    [
        (USDT_QUESTION, "Мосбиржа будет взимать НДС 22% со сделок с USDT."),
        ("Сколько стоит вывод BTC на этой бирже?", "Тарифы биржи на вывод BTC."),
        (
            "Когда откроют новую станцию метро?",
            "Открытие новой станции метрополитена состоится в декабре.",
        ),
    ],
)
async def test_private_question_and_forward_read_public_link_and_reply_once(
    tmp_path, delivery, question_text, post_text
):
    article = "https://example.org/exchange-fee"
    date = int(time.time())
    question = event(2, text=question_text, date=date)
    post = event(
        3,
        text=post_text,
        date=date,
        forward_origin={"type": "channel"},
        entities=[{"type": "text_link", "offset": 0, "length": 8, "url": article}],
    )
    async with store_at(tmp_path) as store:
        await private_ready(store)
        model = FakeModel(ModelResult(text="Расчёт зависит от условий сделки."))
        fetch = Fetch()
        dialogue = DialogueEngine(store, model)
        dialogue.research = research = ResearchEngine(store, ResearchModel(), fetch)
        if delivery == "one_poll":
            await store.ingest("999", "zarya_test", [question, post])
        else:
            await store.ingest("999", "zarya_test", [question])
            assert await dialogue.claim("999") is None
            assert await research.claim("999") is None
            await store.ingest("999", "zarya_test", [post])
        if delivery == "edited":
            question_text += " В рублях."
            edited = event(4, message_id=2, text=question_text)
            await store.ingest(
                "999", "zarya_test", [{"update_id": 4, "edited_message": edited["message"]}]
            )
        assert await dialogue.claim("999") == {"skip": True}
        lookup = await research.claim("999")
        assert lookup["question"] == question_text and post_text in lookup["material"]
        await research.execute(lookup)
        await answer(dialogue, research, 2)
        await drain(dialogue)
        assert fetch.calls == [article] and len(model.calls) == 1
        body = json.loads(model.calls[0]["input"])
        assert body["current_message"]["text"] == question_text
        assert body["selected_material"]["messages"][0]["text"] == post_text
        (payload,) = await store.db.one("SELECT payload FROM outbox")
        assert json.loads(payload)["reply_id"] == 2
        assert await store.db.all("SELECT message_id FROM dialogue_runs") == [
            (2 if delivery == "edited" else 3,)
        ]


@pytest.mark.parametrize("scenario", ["alone", "comment", "same_poll_comment", "reply"])
async def test_rich_forward_supplies_the_exact_post_and_separate_question(tmp_path, scenario):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        model = FakeModel(ModelResult(text="Ответ по посту"))
        dialogue = DialogueEngine(store, model)
        search = ResearchModel()
        dialogue.research = research = ResearchEngine(store, search, Fetch())
        if scenario == "comment":
            await store.ingest("999", "zarya_test", [event(2, text=ASK)])
            assert await dialogue.claim("999") is None
            await store.ingest("999", "zarya_test", [rich_post(3)])
        elif scenario == "same_poll_comment":
            await store.ingest("999", "zarya_test", [event(2, text="Как тебе?"), rich_post(3)])
        else:
            post = rich_post(2)
            await store.ingest("999", "zarya_test", [post])
            if scenario == "reply":
                await answer(dialogue, research, 2)
                post["message"]["rich_message"]["blocks"] = []
                await store.ingest(
                    "999",
                    "zarya_test",
                    [event(3, text=ASK, reply_to_message=post["message"])],
                )
        number = 3 if scenario == "reply" else 2
        work = await answer(dialogue, research, number)
        snapshot = work["snapshot"]
        request = json.loads(snapshot["request"]["input"])
        assert request["selected_material"]["messages"][0]["content_incomplete"] is True
        assert "часть содержимого недоступна" in snapshot["request"]["instructions"]
        selected = request["selected_material"]["messages"]
        assert len(selected) == 1 and BODY in selected[0]["text"]
        assert sorted(ref["message_id"] for ref in snapshot["source_manifest"]) == (
            [2] if scenario in {"alone", "reply"} else [2, 3]
        )
        if scenario != "alone":
            expected = "Как тебе?" if scenario == "same_poll_comment" else ASK
            assert request["current_message"]["text"] == expected
        if scenario in {"comment", "same_poll_comment"}:
            assert len(model.calls) == 1 and work["reply_id"] == 2
        assert not search.calls
        assert "https://example.org/private" not in snapshot["request"]["input"]
        assert "UNREAD_IMAGE_ID" not in snapshot["request"]["input"]


@pytest.mark.parametrize("scenario", ["alone", "comment", "reply", "missing"])
async def test_unreadable_post_never_summarizes_an_old_post(tmp_path, scenario):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        model = FakeModel(ModelResult(text="Чужой ответ, который не должен уйти"))
        dialogue = DialogueEngine(store, model)
        search = ResearchModel()
        dialogue.research = ResearchEngine(store, search, Fetch())
        await store.ingest("999", "zarya_test", [event(2, text=OLD)])
        await answer(dialogue, dialogue.research, 2)
        model.calls.clear()
        post = rich_post(3 if scenario == "alone" else 4, readable=False)
        if scenario == "alone":
            updates = [post]
            reply_id = 3
        elif scenario == "comment":
            updates = [event(3, text=ASK), post]
            reply_id = 4
        elif scenario == "reply":
            post = rich_post(3, readable=False)
            updates = [post, event(4, text=ASK, reply_to_message=post["message"])]
            reply_id = 4
        else:
            updates = [event(3, text=ASK)]
            reply_id = 3
        await store.ingest("999", "zarya_test", updates)
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE jobs SET payload=json_set(payload,'$.post_wait_until',0) "
                "WHERE json_extract(payload,'$.message_id')=?",
                (reply_id,),
            )
        for _ in range(8):
            work = await dialogue.claim("999")
            if work and not work.get("skip"):
                await dialogue.execute(work)
        (payload,) = await store.db.one(
            "SELECT o.payload FROM outbox o JOIN dialogue_runs r ON r.job_id=o.job_id "
            "WHERE r.message_id=?",
            (reply_id,),
        )
        assert json.loads(payload)["text"].startswith(UNREADABLE)
        assert json.loads(payload)["reply_id"] == (3 if scenario == "comment" else reply_id)
        assert not model.calls and not search.calls
        (snapshot,) = await store.db.one(
            "SELECT snapshot FROM dialogue_runs WHERE message_id=?", (reply_id,)
        )
        assert OLD not in snapshot
        if scenario == "reply":
            (run_id,) = await store.db.one(
                "SELECT id FROM dialogue_runs WHERE message_id=?", (reply_id,)
            )
            with pytest.raises(ValueError, match="Текст пересланного поста недоступен"):
                await dialogue.replay(run_id, "paid")
            replay = await dialogue.replay(run_id, "recorded")
            assert replay["response"].startswith(UNREADABLE)
            assert not model.calls


async def test_unrelated_question_and_forward_do_not_share_search_material(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Погода")))
        dialogue.research = research = ResearchEngine(store, ResearchModel(), Fetch())
        question = "Как сейчас с погодой в Казани?"
        now = int(time.time())
        post = event(
            3,
            text="Как изменились правила провоза багажа",
            date=now,
            forward_origin={"type": "channel"},
        )
        await store.ingest("999", "zarya_test", [event(2, text=question, date=now), post])
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["question"] == question
        assert work["material"] == "" and work["sources"] == []


@pytest.mark.parametrize("before_forward", ["generating", "waiting_delivery"])
async def test_standalone_forward_does_not_cancel_an_unrelated_answer(tmp_path, before_forward):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ на отдельный вопрос")))
        await store.ingest("999", "zarya_test", [event(2, text="Сколько будет 2 + 2?")])
        work = await dialogue.claim("999")
        assert work and not work.get("skip")
        if before_forward == "waiting_delivery":
            await dialogue.execute(work)
        await store.ingest("999", "zarya_test", [rich_post(3)])
        if before_forward == "generating":
            await dialogue.execute(work)
        (state, payload) = await store.db.one(
            "SELECT state,payload FROM outbox WHERE job_id=?", (work["job_id"],)
        )
        assert state == "pending"
        assert json.loads(payload)["text"] == "Ответ на отдельный вопрос"


@pytest.mark.parametrize(
    "question", ["Проверь этот пост по новостям", "О чём пост и сверь по СМИ дату?"]
)
async def test_comment_can_request_web_search_about_the_selected_post(tmp_path, question):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ")))
        dialogue.research = research = ResearchEngine(store, ResearchModel(), Fetch())
        await store.ingest("999", "zarya_test", [event(2, text=question)])
        assert await dialogue.claim("999") is None
        await store.ingest("999", "zarya_test", [rich_post(3)])
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["question"] == question
        assert BODY in work["material"]
        await research.execute(work)
        assert research.adapter.calls[0]["tools"][0]["type"] == "web_search"


@pytest.mark.parametrize(
    "edit_at", ["before_forward", "after_forward", "queued_search", "after_delivery"]
)
async def test_news_comment_edit_keeps_post_and_link_through_replies(tmp_path, edit_at):
    question = "Объясни смысл этой новости. Сколько заплачу за покупку?"
    revised = "Объясни смысл этой новости. Посчитай для 80 000 рублей."
    body = "На бирже объявили новый сбор. Подробности"
    label = "Подробности"
    article = "https://example.org/exchange-fee"
    forward = event(
        3,
        text=body,
        forward_origin=rich_post(3)["message"]["forward_origin"],
        entities=[
            {
                "type": "text_link",
                "offset": body.index(label),
                "length": len(label),
                "url": article,
            }
        ],
    )
    edit = event(4 if edit_at != "before_forward" else 3, text=revised, message_id=2)
    edit["edited_message"] = edit.pop("message")
    if edit_at == "before_forward":
        forward["update_id"] = 4
    async with store_at(tmp_path) as store:
        await private_ready(store)
        search = ResearchModel()
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ по источнику")))
        dialogue.research = research = ResearchEngine(store, search, Fetch())
        await store.ingest("999", "zarya_test", [event(2, text=question)])
        assert await dialogue.claim("999") is None
        if edit_at == "before_forward":
            await store.ingest("999", "zarya_test", [edit, forward])
        else:
            await store.ingest("999", "zarya_test", [forward])
            if edit_at == "queued_search":
                assert await dialogue.claim("999") == {"skip": True}
            if edit_at == "after_delivery":
                work = await answer(dialogue, research, 2)
                await deliver(store, work, 100)
            await store.ingest("999", "zarya_test", [edit])
        if edit_at == "after_delivery":
            edited_work = await dialogue.claim("999")
            if edited_work and not edited_work.get("skip"):
                await dialogue.execute(edited_work)
            assert len(dialogue.adapter.calls) == 1
            assert await store.db.one(
                "SELECT state,response FROM dialogue_runs WHERE id=?", (work["run_id"],)
            ) == ("cancelled", None)
        else:
            work = await answer(dialogue, research, 2)
            await deliver(store, work, 100)
        assert len(search.calls) == 1
        first = json.loads(search.calls[0]["input"])
        assert first["question"] == (question if edit_at == "after_delivery" else revised)
        assert body in search.intent_calls[0]["input"]
        assert first["provided_material"] == "" and article in str(first)
        assert await dialogue.claim("999") is None

        for question_id, reply_id in ((5, 100), (6, 101)):
            await store.ingest(
                "999",
                "zarya_test",
                [
                    event(
                        question_id,
                        text="Поищи по ссылке и проверь",
                        reply_to_message={
                            "message_id": reply_id,
                            "from": {"id": 999, "is_bot": True},
                        },
                    )
                ],
            )
            followup = await answer(dialogue, research, question_id)
            if question_id == 5:
                await deliver(store, followup, 101)
            selected = str(
                json.loads(followup["snapshot"]["request"]["input"])["selected_material"]
            )
            hosted = search.calls[-1]["input"]
            assert body in selected and article in selected
            assert body in search.intent_calls[-1]["input"]
            assert body not in hosted and article in hosted

        if edit_at == "after_delivery":
            changed_post = event(
                7,
                message_id=3,
                text="Новая редакция поста. https://example.org/updated",
                forward_origin=forward["message"]["forward_origin"],
            )
            changed_post["edited_message"] = changed_post.pop("message")
            await store.ingest(
                "999",
                "zarya_test",
                [
                    changed_post,
                    event(
                        8,
                        text="Поищи по ссылке и проверь",
                        reply_to_message={"message_id": 100, "from": {"id": 999, "is_bot": True}},
                    ),
                ],
            )
            call_counts = len(search.calls), len(dialogue.adapter.calls)
            await drain(dialogue)
            (delivery,) = await store.db.one(
                "SELECT o.payload FROM outbox o JOIN dialogue_runs r USING(job_id) "
                "WHERE r.message_id=8 AND r.state='service'"
            )
            assert json.loads(delivery)["text"].startswith(UNREADABLE)
            assert await research.claim("999") is None
            assert (len(search.calls), len(dialogue.adapter.calls)) == call_counts


async def test_followup_edit_recovers_post_without_using_erased_reply_text(tmp_path):
    article = "https://example.org/post"
    question = "Поищи по ссылке и проверь"
    erased = "Текст уже аннулированного ответа"

    def reply(number, parent, **extra):
        return event(
            number,
            text=question,
            reply_to_message={"message_id": parent, "from": {"id": 999, "is_bot": True}, **extra},
        )

    async with store_at(tmp_path) as store:
        await private_ready(store)
        search = ResearchModel()
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ по посту")))
        dialogue.research = research = ResearchEngine(store, search, Fetch())
        post = rich_post(3)
        post["message"]["rich_message"]["blocks"].append({"type": "paragraph", "text": article})
        await store.ingest("999", "zarya_test", [event(2, text="Проверь этот пост"), post])
        initial = await answer(dialogue, research, 2)
        await deliver(store, initial, 100)
        await store.ingest("999", "zarya_test", [reply(4, 100)])
        followup = await answer(dialogue, research, 4)
        await deliver(store, followup, 101)

        edit = event(5, message_id=4, text="Проверь точную дату в источнике")
        edit["edited_message"] = edit.pop("message")
        await store.ingest("999", "zarya_test", [edit, reply(6, 101, text=erased)])
        memory = MemoryEngine(store, FakeModel(), tmp_path)
        memory.context = AsyncMock(wraps=memory.context)
        dialogue.memory = memory
        continued = await answer(dialogue, research, 6)
        assert article in search.calls[-1]["input"]
        assert erased not in continued["snapshot"]["request"]["input"]
        assert erased not in memory.context.await_args.kwargs["query"]


async def test_recovering_edited_question_does_not_read_revised_album_sibling(tmp_path):
    article = "https://example.org/album-original"
    updated = "https://example.org/album-edited"

    def album(number, text, **extra):
        return event(
            number,
            text=text,
            forward_origin=rich_post(number)["message"]["forward_origin"],
            media_group_id="album",
            **extra,
        )

    async with store_at(tmp_path) as store:
        await private_ready(store)
        search = ResearchModel()
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ по альбому")))
        dialogue.research = research = ResearchEngine(store, search, Fetch())
        await store.ingest(
            "999",
            "zarya_test",
            [event(2, text="Проверь этот пост"), album(3, "Первый элемент"), album(4, article)],
        )
        initial = await answer(dialogue, research, 2)
        assert article in search.calls[-1]["input"]
        await deliver(store, initial, 100)
        question = event(5, message_id=2, text="Уточни дату в этом посте")
        sibling = album(6, updated, message_id=4)
        for edit in (question, sibling):
            edit["edited_message"] = edit.pop("message")
        await store.ingest(
            "999",
            "zarya_test",
            [
                question,
                sibling,
                event(
                    7,
                    text="Поищи по ссылке и проверь",
                    reply_to_message={"message_id": 100, "from": {"id": 999, "is_bot": True}},
                ),
            ],
        )
        call_counts = len(search.calls), len(dialogue.adapter.calls)
        await drain(dialogue)
        (delivery,) = await store.db.one(
            "SELECT o.payload FROM outbox o JOIN dialogue_runs r USING(job_id) "
            "WHERE r.message_id=7 AND r.state='service'"
        )
        assert json.loads(delivery)["text"].startswith(UNREADABLE)
        assert await research.claim("999") is None
        assert (len(search.calls), len(dialogue.adapter.calls)) == call_counts


async def test_visible_rich_post_link_reaches_the_fetcher_without_hidden_targets(tmp_path):
    public = "https://x.com/i/status/1234567890123456789"
    hidden = "https://example.org/hidden-target"
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Ответ по посту")))
        model, fetch = ResearchModel(), Fetch()
        dialogue.research = research = ResearchEngine(store, model, fetch)
        post = rich_post(3)
        post["message"]["rich_message"]["blocks"] = [
            {
                "type": "paragraph",
                "text": [public, " ", {"type": "url", "text": "см.", "url": hidden}],
            }
        ]
        await store.ingest(
            "999", "zarya_test", [event(2, text="Проверь этот пост по новостям"), post]
        )
        assert await dialogue.claim("999") == {"skip": True}
        await research.execute(await research.claim("999"))
        assert fetch.calls == [public]
        payload = json.loads(model.calls[0]["input"])
        assert public in model.intent_calls[0]["input"]
        assert payload["provided_material"] == ""
        assert public in {source["url"] for source in payload["sources"]}
        assert hidden not in model.calls[0]["input"]


async def test_private_topics_do_not_share_waiting_forward_comment(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        first = event(2, text=ASK, message_thread_id=71, is_topic_message=True)
        second = rich_post(3)
        second["message"].update(message_thread_id=72, is_topic_message=True)
        await store.ingest("999", "zarya_test", [first, second])
        payloads = await store.db.all(
            "SELECT payload FROM jobs WHERE json_extract(payload,'$.message_id') IN (2,3) "
            "ORDER BY id"
        )
        first_payload, second_payload = [json.loads(payload) for (payload,) in payloads]
        assert first_payload["thread_id"] == 71 and first_payload["post_wait_until"]
        assert second_payload["thread_id"] == 72 and not second_payload.get("post_question")


async def test_attachment_caption_is_not_the_comment_for_a_subsequent_forward(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        own_document = event(
            2, caption="Как тебе?", document={"file_id": "synthetic", "file_unique_id": "doc"}
        )
        del own_document["message"]["text"]
        await store.ingest("999", "zarya_test", [own_document])
        await store.ingest("999", "zarya_test", [rich_post(3)])
        (first,), (second,) = await store.db.all(
            "SELECT payload FROM jobs WHERE json_extract(payload,'$.message_id') IN (2,3) "
            "ORDER BY id"
        )
        assert not json.loads(first).get("post_wait_until")
        assert not json.loads(second).get("post_question")


@pytest.mark.parametrize("question", ["О чём статья 228 УК РФ?", "Сократи текст «Можно короче»"])
def test_other_local_requests_do_not_wait_for_a_forward(question):
    message = {"text": question}
    assert not expects_post_comment(message)
    assert not needs_forwarded_text(message)


def test_rich_text_extracts_nested_blocks_without_metadata():
    message = {
        "rich_message": {
            "blocks": [
                {
                    "type": "details",
                    "summary": "Подробности",
                    "blocks": [
                        {
                            "type": "list",
                            "items": [{"blocks": [{"type": "paragraph", "text": "Первый пункт"}]}],
                        }
                    ],
                },
                {"type": "table", "cells": [[{"text": "Ячейка"}]], "caption": "Таблица"},
                {"type": "photo", "photo": [{"file_id": "hidden"}], "caption": {"text": "Подпись"}},
                {"type": "buttons", "buttons": [{"text": "Кнопка"}]},
            ],
        }
    }
    text = content_text(message)
    assert all(v in text for v in ("Подробности", "Первый пункт", "Ячейка", "Таблица", "Подпись"))
    assert "hidden" not in text and "Кнопка" not in text
    assert len(content_text(message, 10)) <= 10


async def test_aiogram_serialization_keeps_rich_blocks():
    transport = AiogramTransport("1:fake")
    try:
        transport.bot.get_updates = AsyncMock(return_value=[Update.model_validate(rich_post(2))])
        (received,) = await transport.updates(None)
        assert received["message"]["rich_message"] == rich_post(2)["message"]["rich_message"]
        assert "text" not in received["message"]
    finally:
        await transport.bot.session.close()
