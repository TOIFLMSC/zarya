import pytest
from test_behavior import ready_engine
from test_dialogue import run_one
from test_telegram import event, store_at

from zarya.chat_style import chat_dashes


def test_dashes_change_prose_but_not_exact_material():
    text = (
        "ну да — бывает, 2–3 раза\n"
        '`echo "а—б"`\n```python\ns = "а—б"\n```\n'
        '«слово — слово» “а–б” "а—б"\n> точная цитата — такая\n'
        "https://example.org/а—б?q=1–2\nобычный текст—продолжение"
    )
    expected = text.replace("да — бывает, 2–3", "да - бывает, 2-3").replace(
        "текст—продолжение", "текст-продолжение"
    )
    assert chat_dashes(text) == expected
    assert chat_dashes(expected) == expected


@pytest.mark.parametrize(
    "text", ["2 − 1 = 1", "щас гляну", "Python, Москва", "10.5 мг", "не включай", "```x—y"]
)
def test_style_does_not_mutate_words_numbers_or_unclosed_code(text):
    assert chat_dashes(text) == text


@pytest.mark.parametrize(
    "exact",
    [
        "``echo а—б``",
        "``a `x—y` b``",
        "``a ``` b—c``",
        "````\n```а—б```\n````",
        "example.org/а—б",
        "t.me/а—б",
        "WWW.Example.org/а—б",
        "'а—б'",
    ],
)
def test_code_delimiters_and_bare_urls_remain_exact(exact):
    assert chat_dashes("вот — " + exact + " — держи") == "вот - " + exact + " - держи"


async def test_behavior_response_outbox_and_replays_share_normalized_text(tmp_path):
    async with store_at(tmp_path) as store:
        engine, model, _ = await ready_engine(store, text="ну да — бывает")
        await store.ingest("999", "zarya_test", [event(2, text="Заря, ну бывает же")])
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert detail["response"] == "ну да - бывает"
        assert detail["snapshot"]["behavior"]["plan"]["text"] == detail["response"]
        assert detail["parts"][0]["text"] == detail["response"]
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["response"] == detail["response"] and len(model.calls) == 1
        paid = await engine.replay(work["run_id"], "paid", (await store.db.settings()).version)
        assert paid["response"] == detail["response"] and len(model.calls) == 2
