import json
import time

import pytest
from test_dialogue import FakeModel
from test_memory import context, mutation, ready
from test_telegram import event, store_at

from zarya.memory import MemoryEngine
from zarya.memory_context import valid_snapshot
from zarya.openai_adapter import ModelResult


async def observe(
    engine,
    number,
    text,
    *,
    age=0,
    author=42,
    kind="interest",
    relation="new",
    existing=None,
    reply=None,
    proposal_updates=None,
):
    await engine.store.ingest(
        "999",
        "zarya_test",
        [
            event(
                number,
                -100,
                text,
                **{
                    "from": {"id": author, "first_name": str(author)},
                    **({"reply_to_message": {"message_id": reply}} if reply else {}),
                },
            )
        ],
    )
    async with engine.db.transaction() as conn:
        await conn.execute(
            "UPDATE memory_sources SET received_at=? WHERE message_id=?",
            (time.time() - age, number),
        )
    work = await engine.claim("999", force=True)
    assert work
    proposal = {
        "sender_id": "42",
        "category": {"interest": "interest", "nickname": "name", "habit": "observation"}[kind],
        "key": "test_" + kind,
        "text": "Похоже, интересуется Python"
        if kind == "interest"
        else "В чате называют Дюшей"
        if kind == "nickname"
        else "Часто шутит про дедлайны",
        "provenance": "inference" if author == 42 else "other",
        "source_ids": [work["sources"][0]["id"]],
        "observation_kind": kind,
        "relation": relation,
        "existing_fact_id": existing,
    }
    proposal.update(proposal_updates or {})
    await engine.finish(
        work, ModelResult(text=json.dumps({"summary": "Эпизод обсуждения", "facts": [proposal]})), 1
    )
    assert (await engine.db.one("SELECT state FROM memory_batches WHERE id=?", (work["id"],)))[
        0
    ] == "completed"
    return (await engine.profile("999", "-100"))["facts"][0], work, proposal


async def test_repeated_interest_enters_local_context_as_hypothesis(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        fact, _, _ = await observe(engine, 4, "Написал ещё один скрипт на Python", age=3600)
        assert fact["confidence"]["level"] == "single"
        assert not (await context(engine))["facts"]
        fact, _, _ = await observe(
            engine,
            5,
            "Разбираюсь в новых возможностях Python",
            age=2500,
            relation="supports",
            existing=fact["id"],
        )
        assert fact["confidence"]["level"] == "some"
        fact, _, _ = await observe(
            engine,
            6,
            "Сравнивал библиотеки Python для своего проекта",
            relation="supports",
            existing=fact["id"],
        )
        assert fact["state"] == "proposed" and fact["provenance"] == "inference"
        assert fact["confidence"]["eligible"] and fact["confidence"]["messages"] == 3
        selected = (await context(engine))["facts"]
        assert len(selected) == 1 and selected[0]["tentative"]
        assert not (await context(engine, "-200"))["facts"]
        with pytest.raises(ValueError):
            await engine.mutate("999", fact["id"], mutation(fact, "share"))
        async with store.db.transaction() as conn:
            assert await valid_snapshot(conn, "999", {"memory": {"facts": selected}})
            # A dropped witness invalidates an already prepared answer too.
            await conn.execute("UPDATE memory_sources SET valid=0 WHERE message_id=6")
            assert not await valid_snapshot(conn, "999", {"memory": {"facts": selected}})
        assert not (await context(engine))["facts"]


async def test_duplicates_and_reprocessing_do_not_inflate_strength(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        fact, work, proposal = await observe(engine, 4, "Разбираю Python!", age=3600)
        fact, _, _ = await observe(
            engine, 5, "разбираю python", age=1800, relation="supports", existing=fact["id"]
        )
        assert fact["confidence"]["messages"] == 1
        # Even directly replaying the same proposal cannot add another witness.
        from zarya.memory import Proposal

        async with store.db.transaction() as conn:
            await engine.apply_proposal(conn, work, Proposal(**proposal))
        fact = (await engine.profile("999", "-100"))["facts"][0]
        assert fact["confidence"]["messages"] == 1
        fact, _, _ = await observe(
            engine, 6, "Python для проекта", relation="uncertain", existing=fact["id"]
        )
        assert fact["confidence"]["messages"] == 1


async def test_one_episode_and_third_party_interest_stay_unused(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        fact = None
        for number in range(4, 7):
            fact, _, _ = await observe(
                engine,
                number,
                f"Разбираю Python часть {number}",
                relation="supports" if fact else "new",
                existing=fact["id"] if fact else None,
            )
        assert fact["confidence"]["messages"] == 3 and not fact["confidence"]["eligible"]
        fact, _, _ = await observe(
            engine,
            7,
            "Он часто обсуждает Python",
            age=3600,
            author=77,
        )
        assert fact["confidence"]["messages"] == 3 and not fact["confidence"]["eligible"]


async def test_nickname_repetition_is_not_consent_to_address(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        # Establish the target participant in this chat, without using that message as evidence.
        await store.ingest("999", "zarya_test", [event(4, -100, "Привет всем")])
        work = await engine.claim("999", force=True)
        await engine.finish(work, ModelResult(text='{"summary":"Приветствие","facts":[]}'), 1)
        fact, _, _ = await observe(engine, 5, "Дюша, привет", age=3600, author=77, kind="nickname")
        fact, _, _ = await observe(
            engine,
            6,
            "Дюша, погнали",
            age=2200,
            author=77,
            kind="nickname",
            relation="supports",
            existing=fact["id"],
        )
        assert not fact["confidence"]["eligible"]
        fact, _, _ = await observe(
            engine,
            7,
            "Дюша опять пришёл",
            author=88,
            kind="nickname",
            relation="supports",
            existing=fact["id"],
        )
        assert fact["confidence"]["eligible"] and fact["confidence"]["authors"] == 2
        selected = (await context(engine))["facts"]
        assert selected[0]["tentative"] and not selected[0]["addressing_allowed"]
        assert fact["provenance"] == "other"
        await engine.mutate("999", fact["id"], mutation(fact, "accept"))
        assert not (await context(engine))["facts"][0]["addressing_allowed"]


async def test_conflicting_observation_is_not_used(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        fact, _, _ = await observe(engine, 4, "Первый раз про дедлайн", age=3600, kind="habit")
        fact, _, _ = await observe(
            engine,
            5,
            "Второй раз про дедлайн",
            age=2000,
            kind="habit",
            relation="supports",
            existing=fact["id"],
        )
        fact, _, _ = await observe(
            engine,
            6,
            "Третий раз про дедлайн",
            kind="habit",
            relation="supports",
            existing=fact["id"],
        )
        assert fact["confidence"]["eligible"]
        async with store.db.transaction() as conn:
            await conn.execute(
                "UPDATE memory_facts SET state='disputed',version=version+1 WHERE id=?",
                (fact["id"],),
            )
        assert not (await context(engine))["facts"]


async def test_losing_first_reply_witness_keeps_sufficient_independent_observations(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        await store.ingest(
            "999", "zarya_test", [event(4, -100, "Обсуждаем Python?", **{"from": {"id": 77}})]
        )
        work = await engine.claim("999", force=True)
        await engine.finish(work, ModelResult(text='{"summary":"Вопрос","facts":[]}'), 1)
        fact, _, _ = await observe(engine, 5, "Да, смотрел там библиотеки", reply=4, age=7200)
        for number, age in [(6, 3600), (7, 2000), (8, 0)]:
            fact, _, _ = await observe(
                engine,
                number,
                f"Новый проект Python {number}",
                age=age,
                relation="supports",
                existing=fact["id"],
            )
        assert fact["confidence"]["messages"] == 4
        old_version = fact["version"]
        edit = event(9, -100, "Обсуждаем Rust?", **{"from": {"id": 77}})
        edit["edited_message"] = edit.pop("message")
        edit["edited_message"]["message_id"] = 4
        await store.ingest("999", "zarya_test", [edit])
        fact = (await engine.profile("999", "-100"))["facts"][0]
        assert fact["state"] == "proposed" and fact["confidence"]["messages"] == 3
        assert fact["version"] > old_version
        assert (await context(engine))["facts"][0]["tentative"]


async def test_observation_self_is_downgraded_and_known_id_canonicalizes_key(tmp_path):
    async with store_at(tmp_path) as store:
        await ready(store)
        engine = MemoryEngine(store, FakeModel(), tmp_path)
        fact, _, _ = await observe(
            engine, 4, "Читал про Python", proposal_updates={"provenance": "self"}
        )
        assert fact["state"] == "proposed" and fact["provenance"] == "inference"
        fact, _, _ = await observe(
            engine,
            5,
            "Пишу ещё один проект Python",
            relation="supports",
            existing=fact["id"],
            proposal_updates={"key": "different_model_wording"},
        )
        assert fact["confidence"]["messages"] == 2
        assert len((await engine.profile("999", "-100"))["facts"]) == 1
