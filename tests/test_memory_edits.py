"""Edits must not erase a causally earlier quote from a live conversation."""

import json

import pytest
from test_dialogue import FakeModel
from test_memory import ready
from test_source_material import answer, deliver
from test_telegram import event, store_at

from zarya.dialogue import DialogueEngine
from zarya.memory import MemoryEngine
from zarya.memory_logic import invalidate
from zarya.openai_adapter import ModelResult

RATE = "Ранее было 91.20-92.30 рубля за USDT; свежий курс не подтверждён"


def edit(update, message, text):
    value = event(update, -100, text)
    value["edited_message"] = value.pop("message")
    value["edited_message"]["message_id"] = message
    return value


async def quote(s, tmp_path):
    await ready(s)
    model = FakeModel(ModelResult(text=RATE))
    dialogue = DialogueEngine(s, model)
    dialogue.memory = MemoryEngine(s, model, tmp_path)
    await s.ingest("999", "zarya_test", [event(4, -100, "Заря, какой курс?")])
    run = await answer(dialogue, None, 4)
    await deliver(s, run, 1000)
    return dialogue, run


async def test_edit_amount_keeps_prior_rate_but_scrubs_old_amount_response(tmp_path):
    async with store_at(tmp_path) as s:
        dialogue, first = await quote(s, tmp_path)
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(
                    5,
                    -100,
                    "Посчитай 120",
                    reply_to_message={"message_id": 1000, "from": {"id": 999, "is_bot": True}},
                )
            ],
        )
        dialogue.adapter.result = ModelResult(text="Сколько чего?")
        second = await answer(dialogue, None, 5)
        await deliver(s, second, 1001)
        await s.ingest("999", "zarya_test", [edit(6, 5, "Посчитай 120 USDT в рублях")])
        kept = await dialogue.details(first["run_id"])
        assert kept["response"] == RATE
        assert kept["snapshot"]["context_event_watermark"]
        erased = await dialogue.details(second["run_id"])
        assert erased["snapshot"]["invalidated"] and erased["response"] is None
        assert (await s.db.one("SELECT text FROM recent_messages WHERE message_id=1000"))[0] == RATE
        assert not await s.db.one("SELECT text FROM recent_messages WHERE message_id=1001")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(
                    7,
                    -100,
                    "Заря, умножь на курс выше",
                    reply_to_message={
                        "message_id": 1001,
                        "from": {"id": 999, "is_bot": True},
                        "text": "НЕ ВОССТАНАВЛИВАТЬ УДАЛЁННЫЙ ОТВЕТ",
                    },
                )
            ],
        )
        followup = await answer(dialogue, None, 7)
        request = followup["request"]["input"]
        assert RATE in request and "120 USDT" in request
        assert "НЕ ВОССТАНАВЛИВАТЬ" not in request
        # A later edit to the quote's own source must erase the quote too.
        await s.ingest("999", "zarya_test", [edit(8, 4, "Другой вопрос")])
        assert not await s.db.one("SELECT 1 FROM recent_messages WHERE message_id=1000")


@pytest.mark.parametrize("kind", ["legacy", "shared", "delete", "late_snapshot", "undelivered"])
async def test_no_preservation_without_causal_and_delivery_proof(tmp_path, kind):
    async with store_at(tmp_path) as s:
        dialogue, first = await quote(s, tmp_path)
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, сумма 120")])
        source = await s.db.one("SELECT event_id FROM memory_sources WHERE message_id=5")
        async with s.db.transaction() as conn:
            if kind == "legacy":
                await conn.execute(
                    "UPDATE dialogue_runs SET snapshot="
                    "json_remove(snapshot,'$.context_event_watermark') "
                    "WHERE id=?",
                    (first["run_id"],),
                )
            if kind == "late_snapshot":
                await conn.execute(
                    "UPDATE dialogue_runs SET snapshot="
                    "json_set(snapshot,'$.context_event_watermark',?) "
                    "WHERE id=?",
                    (source[0], first["run_id"]),
                )
            if kind == "undelivered":
                await conn.execute(
                    "UPDATE outbox SET state='pending' WHERE job_id=?", (first["job_id"],)
                )
            if kind in {"shared", "delete"}:
                await invalidate(
                    conn,
                    "999",
                    "-100",
                    kind == "shared",
                    before_event=source[0] if kind == "shared" else None,
                )
        if kind not in {"shared", "delete"}:
            await s.ingest("999", "zarya_test", [edit(6, 5, "Сумма 120 USDT")])
        assert not await s.db.one("SELECT 1 FROM recent_messages WHERE message_id=1000")
        detail = await dialogue.details(first["run_id"])
        assert detail["snapshot"]["invalidated"] and detail["response"] is None


async def test_kept_quote_keeps_its_research_and_is_purged_on_later_forget(tmp_path):
    async with store_at(tmp_path) as s:
        dialogue, first = await quote(s, tmp_path)
        # Minimal retained research record attached to the already sent answer.
        async with s.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO research_runs(bot_id,chat_id,scope,access_version,job_id,state,"
                "mode,question,material,manifest,sources,result,actions,model,reasoning,"
                "settings_version,created_at) VALUES "
                "('999','-100','group',2,?,'completed','live','rate','{}','[]','[]','{}','[]',"
                "'test','low',1,datetime('now'))",
                (first["job_id"],),
            )
            research = (await (await conn.execute("SELECT MAX(id) FROM research_runs")).fetchone())[
                0
            ]
            await conn.execute(
                "UPDATE dialogue_runs SET snapshot=json_set(snapshot,'$.research_id',?) WHERE id=?",
                (research, first["run_id"]),
            )
        await s.ingest("999", "zarya_test", [event(5, -100, "Заря, сумма 120")])
        await s.ingest("999", "zarya_test", [edit(6, 5, "Сумма 120 USDT")])
        assert (await s.db.one("SELECT state FROM research_runs WHERE id=?", (research,)))[
            0
        ] == "completed"
        assert not (await dialogue.details(first["run_id"]))["snapshot"].get("invalidated")
        async with s.db.transaction() as conn:
            await invalidate(conn, "999", "-100")
        assert (await s.db.one("SELECT state FROM research_runs WHERE id=?", (research,)))[
            0
        ] == "cancelled"
        assert json.loads(
            (await s.db.one("SELECT snapshot FROM dialogue_runs WHERE id=?", (first["run_id"],)))[0]
        )["invalidated"]
