"""Private Telegram forwards bind the author's question to the chosen post."""

import json
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from test_dialogue import FakeModel, private_ready
from test_research import Fetch, ResearchModel
from test_source_material import answer
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.research import ResearchEngine
from zarya.research_intent import expects_post_comment, local_post_request, needs_forwarded_text
from zarya.telegram_content import content_text
from zarya.telegram_runtime import AiogramTransport

ASK = "тут большой пост, можешь его содержание немного ужать"
BODY = "Новый пост про сюжет игры и её героев."
OLD = "Старый пост про Ялту"
UNREADABLE = "Не получилось прочитать пересланный пост"


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
        number = 2 if scenario == "alone" else 3
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
            assert len(model.calls) == 1
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
        assert not model.calls and not search.calls
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
        await store.ingest("999", "zarya_test", [event(2, text=question), rich_post(3)])
        assert await dialogue.claim("999") == {"skip": True}
        work = await research.claim("999")
        assert work["question"] == question
        assert work["material"] == "" and work["sources"] == []


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
