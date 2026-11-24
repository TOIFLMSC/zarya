"""Private Telegram forwards bind the author's question to the chosen post."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from test_dialogue import FakeModel, drain, private_ready
from test_research import Fetch, ResearchModel
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.openai_adapter import ModelResult
from zarya.research import ResearchEngine
from zarya.research_intent import expects_post_comment, local_post_request, needs_forwarded_text
from zarya.telegram_content import content_text
from zarya.telegram_runtime import AiogramTransport

ASK = "тут большой пост, можешь его содержание немного ужать"
BODY = "Новый пост про сюжет игры и её героев."
OLD = "Старый пост про Ялту"
UNREADABLE = "Не получилось прочитать пересланный пост"
USDT_QUESTION = "Реально будут брать НДС при покупке USDT? Я заплачу за 1000 как за 1220?"


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


@pytest.mark.parametrize("same_poll", [True, False])
async def test_private_question_and_forward_read_public_link_and_reply_once(tmp_path, same_poll):
    article = "https://example.org/exchange-fee"
    post_text = "Мосбиржа будет взимать НДС 22% со сделок с USDT."
    date = int(time.time())
    question = event(2, text=USDT_QUESTION, date=date)
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
        if same_poll:
            await store.ingest("999", "zarya_test", [question, post])
        else:
            await store.ingest("999", "zarya_test", [question])
            assert await dialogue.claim("999") is None
            assert await research.claim("999") is None
            await store.ingest("999", "zarya_test", [post])
        assert await dialogue.claim("999") == {"skip": True}
        lookup = await research.claim("999")
        assert lookup["question"] == USDT_QUESTION and post_text in lookup["material"]
        await research.execute(lookup)
        await answer(dialogue, research, 2)
        await drain(dialogue)
        assert fetch.calls == [article] and len(model.calls) == 1
        body = json.loads(model.calls[0]["input"])
        assert body["current_message"]["text"] == USDT_QUESTION
        assert body["selected_material"]["messages"][0]["text"] == post_text
        (payload,) = await store.db.one("SELECT payload FROM outbox")
        assert json.loads(payload)["reply_id"] == 2
        assert await store.db.all("SELECT message_id FROM dialogue_runs") == [(3,)]


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
        question = "Какая сегодня погода в Казани?"
        now = int(time.time())
        post = rich_post(3)
        post["message"]["date"] = now
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
        assert body in str(first) and article in str(first)
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
            assert body in hosted and article in hosted

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
        assert public in payload["provided_material"]
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
    assert not local_post_request(message)


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
