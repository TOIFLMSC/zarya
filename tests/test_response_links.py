import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_dialogue import FakeModel, drain, private_ready, run_one
from test_participant_context import human_event
from test_research import research_ready
from test_telegram import FakeTransport, approve, event, store_at

from zarya.dialogue import DialogueEngine
from zarya.message_links import FALLBACK, permalink, render
from zarya.models import Settings
from zarya.openai_adapter import ModelResult
from zarya.research_logic import evidence_text
from zarya.response_links import (
    message_requests_permalink,
    message_requests_sources,
    wants_sources,
    without_links,
)
from zarya.telegram_runtime import TelegramRuntime


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Скинь источники этой инфы", True),
        ("Приведи ссылки на статьи", True),
        ("Где источники?", True),
        ("Откуда эта инфа?", True),
        ("Найди источник NASA", True),
        ("Пришли ссылку на этот мем", True),
        ("Кинь ссылку на разъяснение ЦБ", True),
        ("Пришли первоисточник", True),
        ("Можно ссылку?", True),
        ("А, я пропустил, есть ссылка на сообщение его?", True),
        ("Есть ссылка на его сообщение? Но ответь без ссылок", False),
        ("Ссылку дашь?", True),
        ("Заря, источники?", True),
        ("Заря, проверь, правда ли это", False),
        ("Объясни https://example.org/post", False),
        ("Почему источники противоречат друг другу?", False),
        ("Проверь источники статьи", False),
        ("Поясни без ссылок", False),
        ("Не нужны источники", False),
        ("Не дай ссылки", False),
        ("Не кинь ссылку на статью", False),
        ("Кинь ссылку на статью, но пришли ответ без ссылок", False),
        ("Не отправь ссылки", False),
        ("Не покажи источники", False),
        ("Дай источники, но не присылай мне ссылки", False),
        ("Don't include sources", False),
        ("Скинь источники, но ссылки не нужны", False),
        ("Ссылки не нужны. А теперь дай источник", True),
        ("В посте сказано «скинь источники». Поясни смысл", False),
    ],
)
def test_only_explicit_current_question_requests_links(question, expected):
    assert wants_sources(question) is expected


@pytest.mark.parametrize("caption", [False, True])
def test_telegram_formatted_quote_is_not_source_request(caption):
    quote = "Дай источники"
    text = "🙂 " + quote + "\nПоясни смысл"
    message = {
        "caption" if caption else "text": text,
        "caption_entities" if caption else "entities": [
            {"type": "blockquote", "offset": 3, "length": len(quote)}
        ],
    }
    assert not message_requests_sources(message)
    message["caption" if caption else "text"] += " и скинь ссылки"
    assert message_requests_sources(message)


def test_strip_plain_markdown_html_links_preserves_explanation():
    text = (
        "Есть свежий мем [на Reddit](https://reddit.com/r/test), "
        "возможно это обработка (https://example.org/). [S1]"
    )
    assert without_links(text) == "Есть свежий мем на Reddit, возможно это обработка."
    assert without_links('Текст <a href="https://example.org">источника</a>.') == "Текст источника."
    assert without_links("Пояснение [в статье](example.org/article).") == "Пояснение в статье."
    assert without_links("Пояснение (example.org/article).") == "Пояснение."
    assert "https://example.org" not in evidence_text(
        "Вывод [S1]", [{"id": 1, "url": "https://example.org"}], show_links=False
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Есть ссылка на сообщение его?", True),
        ("У тебя есть ссылка на его сообщение?", True),
        ("Где ссылка на сообщение?", True),
        ("Есть ссылка на сообщение Серёги?", True),
        ("Заря, а ссылка на его сообщение?", True),
        ("Ссылку на это сообщение, пожалуйста", True),
        ("Скинь ссылочку на эту реплику", True),
        ("А можешь скинуть ссылку на его сообщение?", True),
        ("Дашь ссылку на сообщение?", True),
        ("Дай ссылку на твой ответ", True),
        ("Почему ссылка на сообщение не открывается?", False),
        ("Заря, ссылка на сообщение работает", False),
        ("Ссылка на сообщение открылась", False),
        ("В статье есть ссылка на сообщение Роскомнадзора. Проверь статью.", False),
        ("Мне не нужна ссылка на сообщение. Дай ссылку на статью ЦБ.", False),
        ("Дай ссылку на ответ ЦБ на его сайте", False),
        ("Дай ссылку на сообщение МЧС на официальном сайте", False),
        ("Он спросил «Есть ссылка на сообщение его?». Поясни", False),
        ("Кинь ссылку на сообщение, но ответь без ссылок", False),
    ],
)
def test_chat_link_intent_is_a_request_about_the_message(text, expected):
    assert message_requests_permalink({"text": text}) is expected
    assert not message_requests_permalink({"text": text, "forward_date": 1})


def test_chat_link_render_uses_original_and_rejects_guessed_targets():
    url = "https://t.me/c/1234567890/3"
    source = {"[M1]": url}
    assert render("Вот [M1] https://t.me/c/12/456", source) == url
    assert render("Набери «Включил резерв» — это сообщение 38131", source) == (
        "Набери «Включил резерв»"
    )
    unclear = "Ты про «Включили запасной сервер» или «Переключились на резерв»?"
    assert render(unclear, source) == unclear
    assert render("Вот [M38131]", source) == FALLBACK
    assert render("Сообщение 38131", source) == FALLBACK
    assert render("Найди по номеру 38131", {}) == FALLBACK
    assert render("Вот: 38131", {}) == FALLBACK
    assert render("Вот «38131»", {}) == FALLBACK
    hint = "Набери «Возьми номер 15 у стойки»"
    assert render(hint, {}) == hint
    assert render("Вот tg://openmessage?chat_id=1234&message_id=4321", source) == FALLBACK
    assert permalink({"id": -1001234567890, "type": "supergroup"}, 3) == url
    assert permalink({"id": -1234567890, "type": "group"}, 3) is None
    assert permalink({"id": 42, "type": "private"}, 3) is None


def test_link_opt_out_at_end_of_long_message_remains_visible():
    text = "Дай ссылку на его сообщение. " + "я" * 4000 + ". Но ответь без ссылок"
    assert 4000 < len(text) <= 4096
    assert not message_requests_sources({"text": text})
    assert not message_requests_permalink({"text": text})


@pytest.mark.parametrize("ask", [False, True])
async def test_factcheck_links_optional_but_evidence_remains_in_ui_and_replays(tmp_path, ask):
    async with store_at(tmp_path) as store:
        question = "Заря, проверь публичный тезис" + (", скинь источники" if ask else "")
        dialogue, research, model, fetch, work = await research_ready(store, question)
        await research.execute(work)
        answer = await run_one(dialogue)
        detail = await dialogue.details(answer["run_id"])
        assert ("https://example.org/source" in detail["response"]) is ask
        assert detail["snapshot"]["sources_requested"] is ask
        evidence = await research.details(work["id"], "999")
        assert any(s.get("url") == "https://example.org/source" for s in evidence["sources"])
        recorded = await dialogue.replay(answer["run_id"], "recorded")
        paid = await dialogue.replay(answer["run_id"], "paid", (await store.db.settings()).version)
        for replay in (recorded, paid):
            assert ("https://example.org/source" in replay["response"]) is ask


async def test_unsolicited_direct_links_removed_without_research_and_old_recorded_replay(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        dialogue = DialogueEngine(
            store,
            FakeModel(
                ModelResult(
                    text="Мем есть на Reddit (https://www.reddit.com/r/test/). "
                    "Возможно, лицо обработали."
                )
            ),
        )
        await store.ingest("999", "zarya_test", [event(2, text="Может, это свежий мем?")])
        answer = await run_one(dialogue)
        details = await dialogue.details(answer["run_id"])
        assert "https://" not in details["response"]
        assert "Reddit" in details["response"]
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE dialogue_runs SET response=? WHERE id=?",
                ("Вывод (https://example.org/).", answer["run_id"]),
            )
        recorded = await dialogue.replay(answer["run_id"], "recorded")
        assert "https://" not in recorded["response"]
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE dialogue_runs SET response=? WHERE id=?",
                ("https://example.org/", answer["run_id"]),
            )
        with pytest.raises(ValueError, match="нет текста"):
            await dialogue.replay(answer["run_id"], "recorded")


async def test_forwarded_post_request_is_not_user_request_for_links(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        model = FakeModel(ModelResult(text="Это пост https://example.org/"))
        dialogue = DialogueEngine(store, model)
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    2, text="Дай источники https://example.org/", forward_origin={"type": "channel"}
                )
            ],
        )
        answer = await run_one(dialogue)
        assert answer["snapshot"]["sources_requested"] is False
        assert "https://" not in (await dialogue.details(answer["run_id"]))["response"]


async def test_message_link_follows_original_from_sent_answer(tmp_path):
    class Transport(FakeTransport):
        async def send(self, chat_id, text, thread_id, reply_id=None):
            await super().send(chat_id, text, thread_id, reply_id)
            return 123 if len(self.sent) == 1 else 125

    chat = -1001234567890
    async with store_at(tmp_path) as store:
        await store.ingest("999", "zarya_test", [human_event(1, chat=chat)])
        await approve(store, "group", str(chat))
        await store.db.update_settings(1, Settings(topic_context_chat_id=str(chat)))
        anchor = human_event(2, chat=chat, text="А когда это было?", message_thread_id=7)
        await store.ingest(
            "999",
            "zarya_test",
            [
                anchor,
                human_event(3, sender=84, chat=chat, text="Включил резерв", message_thread_id=7),
                human_event(
                    4,
                    chat=chat,
                    text="Заря, кто включил резерв?",
                    message_thread_id=7,
                    reply_to_message=anchor["message"],
                ),
            ],
        )
        model = FakeModel(ModelResult(text="Участник 84 включил резерв."))
        dialogue = DialogueEngine(store, model)
        await drain(dialogue)
        previous = json.loads(model.calls[-1]["input"])
        assert [m["message_id"] for m in previous["topic_context"]] == [3]
        transport = Transport()
        runtime = TelegramRuntime(store, transport=transport, dialogue=dialogue)
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
        await runtime.deliver_one("999")
        assert transport.sent
        await store.db.update_settings(2, Settings())
        await store.ingest(
            "999",
            "zarya_test",
            [
                human_event(
                    124,
                    chat=chat,
                    text="Есть ссылка на сообщение его?",
                    message_thread_id=7,
                    reply_to_message={"message_id": 123, "from": {"id": 999}},
                )
            ],
        )
        work = await dialogue.claim("999")
        assert work and not work["skip"]
        body = json.loads(work["request"]["input"])
        assert not any(m["message_id"] == 3 for m in body["recent_chat"])
        candidate = next(
            m for m in body["message_link_candidates"] if m["text"] == "Включил резерв"
        )
        model.result = ModelResult(text=f"Вот его сообщение {candidate['marker']}")
        await dialogue.execute(work)
        async with store.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET next_attempt=0")
            await conn.execute("UPDATE delivery_limits SET last_attempt=0")
        await runtime.deliver_one("999")
        link = "https://t.me/c/1234567890/3"
        assert transport.sent[-1] == (str(chat), link, 7)
        recorded = await dialogue.replay(work["run_id"], "recorded")
        assert link in recorded["response"]
        model.result = ModelResult(text=candidate["marker"])
        paid = await dialogue.replay(work["run_id"], "paid", (await store.db.settings()).version)
        assert paid["response"] == link
        assert len(transport.sent) == 2
        edited = human_event(3, sender=84, chat=chat, text="Выключил резерв", message_thread_id=7)
        await store.ingest(
            "999", "zarya_test", [{"update_id": 125, "edited_message": edited["message"]}]
        )
        assert (await dialogue.details(work["run_id"]))["state"] == "cancelled"
        with pytest.raises(ValueError, match="нет сохранённого ответа"):
            await dialogue.replay(work["run_id"], "recorded")


async def test_message_link_to_basic_group_video_suggests_caption_without_fetching(tmp_path):
    chat = {"id": -12345, "type": "group", "title": "Тестовая группа"}
    admission = human_event(1, chat=chat["id"])
    original = human_event(
        2,
        sender=84,
        chat=chat["id"],
        text="",
        caption="Резерв включён на втором сервере",
        video={"file_id": "synthetic-video"},
    )
    followup = human_event(
        3,
        chat=chat["id"],
        text="Заря, дай ссылку на это сообщение",
        reply_to_message=original["message"],
    )
    for update in (admission, original, followup):
        update["message"]["chat"] = chat
    async with store_at(tmp_path) as store:
        await store.ingest("999", "zarya_test", [admission])
        await approve(store, "group", str(chat["id"]))
        await store.ingest("999", "zarya_test", [original, followup])
        model = FakeModel()
        dialogue = DialogueEngine(store, model)
        await run_one(dialogue)  # Ambient original.
        media = AsyncMock(side_effect=AssertionError("Unexpected media preparation"))
        search = AsyncMock(side_effect=AssertionError("Unexpected web search"))
        dialogue.media = SimpleNamespace(prepare=media)
        dialogue.research = SimpleNamespace(prepare=search)
        work = await dialogue.claim("999")
        assert work and not work["skip"]
        candidates = json.loads(work["request"]["input"])["message_link_candidates"]
        direct = next(m for m in candidates if m["direct_reply"])
        assert direct["text"] == original["message"]["caption"]
        model.result = ModelResult(text=direct["marker"])
        await dialogue.execute(work)
        detail = await dialogue.details(work["run_id"])
        assert detail["response"] == (
            "Прямой ссылки здесь нет. В поиске по чату набери «Резерв включён на втором сервере»."
        )
        media.assert_not_awaited()
        search.assert_not_awaited()
