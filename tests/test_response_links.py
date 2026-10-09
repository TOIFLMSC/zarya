import pytest
from test_dialogue import FakeModel, private_ready, run_one
from test_research import research_ready
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.openai_adapter import ModelResult
from zarya.research_logic import evidence_text
from zarya.response_links import message_requests_sources, wants_sources, without_links


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Скинь источники этой инфы", True),
        ("Приведи ссылки на статьи", True),
        ("Где источники?", True),
        ("Откуда эта инфа?", True),
        ("Найди источник NASA", True),
        ("Пришли ссылку на этот мем", True),
        ("Можно ссылку?", True),
        ("Ссылку дашь?", True),
        ("Заря, источники?", True),
        ("Заря, проверь, правда ли это", False),
        ("Объясни https://example.org/post", False),
        ("Почему источники противоречат друг другу?", False),
        ("Проверь источники статьи", False),
        ("Поясни без ссылок", False),
        ("Не нужны источники", False),
        ("Не дай ссылки", False),
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
