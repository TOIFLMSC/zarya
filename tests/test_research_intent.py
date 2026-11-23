import json

import pytest
from test_dialogue import run_one
from test_research import research_ready
from test_research_quote import reply, setup_chain
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.research_intent import blocks_external, search_kind
from zarya.research_logic import wants_search


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Заря, че там по моексу, сколько уже летим вверх?", True),
        ("Почему индекс Мосбиржи сегодня растёт?", True),
        ("Как изменился NASDAQ за неделю?", True),
        ("Чё там с золотом?", True),
        ("Сколько стоил индекс Мосбиржи вчера?", True),
        ("Какая котировка USDT в рублях?", True),
        ("Цена токена HYPE?", True),
        ("Цена акций Газпрома?", True),
        ("Какая цена doge?", True),
        ("Сколько стоит токен пепе?", True),
        ("Какой курс юаня?", True),
        ("Какие новости по Мосбирже?", True),
        ("Что произошло в политике Китая?", True),
        ("Какие прогнозы аналитиков по Мосбирже вышли сегодня?", True),
        ("Что такое MOEX?", True),
        ("Ты любишь моекс?", False),
        ("Объясни историю Мосбиржи", True),
        ("Почему индексы вообще растут?", True),
        ("Моекс завтра вырастет?", True),
        ("Какой прогноз курса BTC на неделю?", True),
        ("Сколько акций MOEX у меня?", False),
        ("Какие новости у тебя?", False),
        ("Сколько стоит твоя куртка?", False),
        ("Я читаю новости про Мосбиржу", False),
        ("Ща вернусь", False),
        ("Сколько USDT получится по курсу выше?", False),
        ("А сколько дней растём?", False),
        ("Заря, проверь тезис", True),
        ("Заря, цена BTC, без ссылок", True),
        ("Не ищи, цена BTC?", False),
        ("Без интернета просто объясни рост рынка", False),
        ("В посте написано «не ищи». Проверь новости по Мосбирже", True),
        ("Почему чёрные дыры испаряются", True),
        ("Расскажи про водяное охлаждение", True),
        ("Объясни, что такое сервитут", True),
        ("Посоветуй, как выбрать роутер", True),
        ("Подбери способ хранения фотографий", True),
        ("У меня водянка сломалась, могу вернуть?", True),
        ("Спасибо, как вернуть неисправную покупку?", True),
        ("Ты помнишь, кто изобрёл радио?", True),
        ("Что ты обо мне помнишь?", False),
        ("Заря, привет!", False),
        ("Спасибо тебе ❤️", False),
        ("да, понятно", False),
        ("Как дела?", False),
        ("Привет, как дела?", False),
        ("Меня сегодня уволили", False),
        ("Сколько будет 17×23?", False),
        ("Вычисли (17+3)/4", False),
        ("Сколько сейчас 100 долларов в рублях?", True),
        ("Сократи этот абзац", False),
        ("Сократи этот текст и расскажи, как работает турбина", True),
        ("Переведи этот текст на английский: Где я работаю?", False),
        ("Переведи этот текст на английский: Цена BTC?", False),
        ("Переведи этот текст на английский: Как растёт?", False),
        ("Перепиши этот текст\nКакой курс доллара?", False),
        ("Сколько USDT получится по курсу выше, и какой курс сейчас?", True),
        ("Что ты помнишь обо мне?", False),
        ("Напомни, где я работаю?", False),
        ("Ты меня любишь?", False),
        ("Наш секрет ABC123, о чём видео? https://youtube.com/watch?v=test&si=TRACK", False),
        ("https://example.org/article?topic=physics", False),
        ("О чём видео и правда ли всё, что там сказано?", True),
        ("Дай ссылку на разъяснение ЦБ", True),
        ("Пришли ссылку на публикацию ведомства", True),
        ("Не ищи в интернете, дай ссылку на разъяснение ЦБ", False),
        ("Я уже нашёл первоисточник", False),
        ("У меня есть официальный документ", False),
        ("Результаты моего мини-ресерча готовы", False),
    ],
)
def test_general_request_routing_has_no_subject_or_recency_whitelist(question, expected):
    assert wants_search(question) is expected
    assert search_kind(question) == ("request" if expected else "none")


@pytest.mark.parametrize("quoted", [False, True])
def test_optout_uses_speaker_entities_and_utf16_offsets(quoted):
    text = "😀 не ищи в интернете, цена BTC?"
    message = {"text": text}
    if quoted:
        message["entities"] = [{"type": "blockquote", "offset": 3, "length": 19}]
    assert blocks_external(message) is not quoted
    assert not blocks_external({**message, "forward_origin": {"type": "channel"}})
    assert not blocks_external({"text": "Цена BTC, без ссылок"})


@pytest.mark.parametrize(
    ("question", "mode"),
    [
        ("Заря, че там по моексу, сколько уже летим вверх?", "analysis"),
        ("Почему индекс Мосбиржи сегодня растёт?", "analysis"),
        ("Какие новости по Мосбирже?", "analysis"),
        ("Заря, что такое чёрная дыра?", "analysis"),
        ("Заря, расскажи историю Мосбиржи", "analysis"),
        ("Заря, у меня водянка сломалась, могу вернуть?", "analysis"),
        ("Заря, посоветуй как выбрать роутер", "analysis"),
        ("Заря, какой курс доллара?", "brief"),
        ("Почему индекс Мосбиржи сегодня растёт? Коротко.", "brief"),
    ],
)
async def test_implicit_request_searches_once_then_dialogue_without_private_search_context(
    tmp_path, question, mode
):
    async with store_at(tmp_path) as store:
        dialogue, research, model, fetch, work = await research_ready(store, question)
        assert work["question"] == question  # Do not rewrite growth/duration into USD spot price.
        await research.execute(work)
        request = model.calls[0]
        assert request["tool_choice"] == "required" and request["max_tool_calls"] == 6
        assert request["tools"][0]["type"] == "web_search"
        data = json.loads(request["input"])
        assert data == {"question": question, "provided_material": "", "sources": []}
        result = await run_one(dialogue)
        details = await dialogue.details(result["run_id"])
        assert len(model.calls) == 2 and not model.calls[1].get("tools")
        assert details["snapshot"]["research_id"] == work["id"]
        assert details["snapshot"]["answer_mode"] == mode
        assert model.calls[1]["text"]["verbosity"] == ("medium" if mode == "analysis" else "low")
        assert "https://" not in details["response"] and fetch.calls == []
        assert (await store.db.one("SELECT count(*) FROM research_runs"))[0] == 1


@pytest.mark.parametrize("case", ["optout_quote", "optout_link", "disabled", "ambient"])
async def test_no_external_work_when_disabled_or_not_addressed(tmp_path, case):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        model.calls.clear()
        question = "Заря, че там по моексу, сколько уже летим вверх?"
        if case == "disabled":
            current = await store.db.settings()
            await store.db.update_settings(
                current.version, current.settings.model_copy(update={"research_enabled": False})
            )
        elif case == "optout_quote":
            question = "Заря, не ищи, цена BTC?"
        elif case == "optout_link":
            question = "Заря, без интернета, объясни https://example.org/post"
        else:
            question = "Какие новости по Мосбирже?"
        incoming = event(3, -100, question)
        if case == "ambient":
            incoming["message"]["from"]["id"] = 77  # No active dialogue for this speaker.
        await store.ingest("999", "zarya_test", [incoming])
        if case == "ambient":
            work = await dialogue.claim("999")
            assert work is None or work.get("skip")
        else:
            await answer(dialogue, research, 3)
        assert not any(call.get("tools") for call in model.calls)
        assert (await store.db.one("SELECT count(*) FROM research_runs"))[0] == 1


@pytest.mark.parametrize("barrier", [None, "topic", "unsent", "changed", "unrelated"])
async def test_market_followup_inherits_only_retained_public_subject(tmp_path, barrier):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        target = 1000
        if barrier == "unsent":
            async with store.db.transaction() as conn:
                await conn.execute("UPDATE outbox SET state='unknown' WHERE message_id=1000")
        elif barrier == "changed":
            edit = event(3, -100, "Заря, привет")
            edit["edited_message"] = edit.pop("message")
            edit["edited_message"]["message_id"] = 2
            await store.ingest("999", "zarya_test", [edit])
            await dialogue.claim("999")
        elif barrier == "unrelated":
            await store.ingest("999", "zarya_test", [reply(3, "Как дела?", 1000)])
            other = await answer(dialogue, research, 3)
            await deliver(store, other, 1001)
            target = 1001
        incoming = reply(4, "А сколько дней растёт?", target)
        if barrier == "topic":
            incoming["message"]["chat"]["is_forum"] = True
            incoming["message"]["message_thread_id"] = 33
        await store.ingest("999", "zarya_test", [incoming])
        work = await answer(dialogue, research, 4)
        assert bool(work["snapshot"].get("research_id")) == (barrier is None)
        if barrier is None:
            details = await research.details(work["snapshot"]["research_id"], "999")
            assert details["question"] == "Bitcoin: А сколько дней растёт?"
            search_input = json.loads(
                next(c for c in reversed(model.calls) if c.get("tools"))["input"]
            )
            assert search_input["question"] == details["question"]
            assert search_input["provided_material"] == "" and not search_input["sources"]


@pytest.mark.parametrize(
    "question",
    [
        "Сколько USDT получится по курсу выше?",
        "Что ты обо мне помнишь?",
        "Сколько будет 17*23?",
        "Что ты помнишь обо мне?",
        "Напомни, где я работаю?",
        "Переведи этот текст на английский: Где я работаю?",
        "Переведи этот текст на английский: Цена BTC?",
        "Переведи этот текст на английский: Как растёт?",
    ],
)
async def test_quote_normalizer_does_not_bypass_local_task(tmp_path, question):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        model.calls.clear()
        await store.ingest("999", "zarya_test", [reply(3, question, 1000)])
        work = await answer(dialogue, research, 3)
        assert not work["snapshot"].get("research_id")
        assert not any(c.get("tools") for c in model.calls)


@pytest.mark.parametrize("question", ["Что такое курс BTC?", "Какой прогноз курса BTC на неделю?"])
async def test_definition_and_forecast_search_preserve_original_question(tmp_path, question):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        await store.ingest("999", "zarya_test", [reply(3, question, 1000)])
        work = await answer(dialogue, research, 3)
        details = await research.details(work["snapshot"]["research_id"], "999")
        assert details["question"] == question and "в USD" not in details["question"]


@pytest.mark.parametrize("quoted", ["не ищи", "найди", "цена BTC"])
async def test_telegram_entities_cannot_command_search_or_quote_normalization(tmp_path, quoted):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        question = "Какие новости по Мосбирже?" if quoted == "не ищи" else "Заря, привет"
        text = f"{quoted}\n{question}"
        await store.ingest(
            "999",
            "zarya_test",
            [
                event(
                    3,
                    -100,
                    text,
                    entities=[{"type": "blockquote", "offset": 0, "length": len(quoted)}],
                )
            ],
        )
        work = await answer(dialogue, research, 3)
        assert bool(work["snapshot"].get("research_id")) == (quoted == "не ищи")
        if quoted == "не ищи":
            details = await research.details(work["snapshot"]["research_id"], "999")
            assert details["question"] == text


async def test_two_market_followups_keep_subject_but_do_not_export_dialogue(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        await store.ingest("999", "zarya_test", [reply(3, "А сколько дней растёт?", 1000)])
        first = await answer(dialogue, research, 3)
        await deliver(store, first, 1001)
        await store.ingest("999", "zarya_test", [reply(4, "А сколько процентов прибавил?", 1001)])
        second = await answer(dialogue, research, 4)
        details = await research.details(second["snapshot"]["research_id"], "999")
        assert details["question"] == "Bitcoin: А сколько процентов прибавил?"
        search_input = json.loads(next(c for c in reversed(model.calls) if c.get("tools"))["input"])
        assert search_input["provided_material"] == "" and not search_input["sources"]


async def test_definition_reply_does_not_become_price_switch_context(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        await store.ingest("999", "zarya_test", [reply(3, "Что такое курс BTC?", 1000)])
        first = await answer(dialogue, research, 3)
        await deliver(store, first, 1001)
        await store.ingest("999", "zarya_test", [reply(4, "а HYPE?", 1001)])
        second = await answer(dialogue, research, 4)
        assert not second["snapshot"].get("research_id")


async def test_multiple_new_subjects_never_inherit_previous_market_subject(tmp_path):
    async with store_at(tmp_path) as store:
        dialogue, research, model = await setup_chain(store)
        question = "А золото и нефть сколько дней растут?"
        await store.ingest("999", "zarya_test", [reply(3, question, 1000)])
        work = await answer(dialogue, research, 3)
        details = await research.details(work["snapshot"]["research_id"], "999")
        assert details["question"] == question and "Bitcoin" not in details["question"]
