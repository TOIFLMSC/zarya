import asyncio
import hashlib
import json
import time

import pytest
from test_participant_context import group_ready, human_event
from test_telegram import store_at

from zarya.memory import MemoryEngine
from zarya.memory_context import valid_snapshot
from zarya.models import Settings
from zarya.openai_adapter import ModelResult
from zarya.participant_context import select
from zarya.participant_summaries import SummaryEngine, compact

TEXT = "Обсуждали поездку с друзьями, планы на выходные и любимые занятия. " * 8


class Summarizer:
    def __init__(self, mode="ok"):
        self.calls = []
        self.mode = mode

    async def generate(self, request):
        self.calls.append(request)
        messages = json.loads(request["input"])["original_messages"]
        ids = [m["message_id"] for m in messages]
        if self.mode == "unknown":
            return ModelResult(state="unknown", error="network")
        return ModelResult(
            text=json.dumps(
                {
                    "summary": "Обсуждали поездку, друзей и планы на выходные."
                    + (" Число 987654321." if self.mode == "number" else ""),
                    "source_ids": ids[:-1] + [99999] if self.mode == "foreign" else ids,
                }
            ),
            usage={"input_tokens": 800, "output_tokens": 100},
        )


async def prepare(store, **extra):
    grant = await group_ready(store)
    events = [human_event(i, text=extra.get(i, TEXT)) for i in range(2, 22)]
    question = human_event(22, text="Заря, продолжим разговор")
    await store.ingest("999", "zarya_test", events + [question])
    return grant, question


async def choose(store, question, **settings):
    grant = (await store.db.one("SELECT version FROM telegram_access WHERE subject_id='-100'"))[0]
    async with store.db.transaction() as c:
        return await select(
            c,
            "999",
            "-100",
            grant,
            question["message"],
            Settings(**settings),
            enqueue_summaries=True,
        )


async def drain(engine):
    while work := await engine.claim("999"):
        await engine.execute(work)


async def test_background_blocks_compact_without_wait_or_changing_personal_window(tmp_path):
    async with store_at(tmp_path) as store:
        grant, question = await prepare(store)
        context, diag, refs = await choose(store, question)
        assert len(context) == 19 and not refs["summaries"]
        assert len(diag["participants"][0]["summaries"]["blocks"]) == 3
        model = Summarizer()
        engine = SummaryEngine(store, model)
        await drain(engine)
        assert len(model.calls) == 3
        for request in model.calls:
            data = json.loads(request["input"])
            assert len(data["original_messages"]) == 5
            assert all(m["role"] == "user" for m in data["original_messages"])
            assert "tools" not in request and "memory" not in data
        packed, diag, refs = await choose(store, question)
        p = diag["participants"][0]
        assert p["human_messages"] == 20 and p["summarized_messages"] == 15
        assert p["original_messages"] == 5
        assert len(packed) == 7 and len(refs["humans"]) == 20 and len(refs["summaries"]) == 3
        assert 22 not in [m["message_id"] for m in packed]
        snapshot = dict(context_refs=refs, retrieval_chat_id="-100", source_access_version=grant)
        assert await valid_snapshot(store.db.writer, "999", snapshot)
        before = len(json.dumps(context))
        assert len(json.dumps(packed)) < before / 2
        await drain(engine)
        assert len(model.calls) == 3  # read/select/settings changes cannot resubmit ready blocks
        assert diag["participants"][0]["summaries"]["covered_ids"] == list(range(3, 18))


async def test_sliding_boundaries_fixed_memberships_and_settings_do_not_resummarize(tmp_path):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        engine = SummaryEngine(store, Summarizer())
        await drain(engine)
        original = await store.db.all("SELECT block_id,event_id FROM participant_summary_sources")
        for mid in range(23, 28):
            question = human_event(mid, text=TEXT)
            await store.ingest("999", "zarya_test", [question])
            packed, diag, _ = await choose(store, question)
            await drain(engine)
            for block in diag["participants"][0]["summaries"]["blocks"]:
                if not block["full_window"]:
                    assert not block["used_ids"] and block["summary"] is None
            assert all(m["message_id"] >= mid - 19 for m in packed)
        assert (
            original
            == (await store.db.all("SELECT block_id,event_id FROM participant_summary_sources"))[
                :15
            ]
        )
        assert not await store.db.all(
            "SELECT event_id,COUNT(*) FROM participant_summary_sources "
            "GROUP BY event_id HAVING COUNT(*)>1"
        )
        calls = len(engine.adapter.calls)
        settings = await store.db.settings()
        await store.db.update_settings(
            settings.version, settings.settings.model_copy(update={"memory_model": "gpt-6.1-sol"})
        )
        await choose(store, question, participant_context_messages=100)
        await drain(engine)
        assert len(engine.adapter.calls) == calls


@pytest.mark.parametrize(
    "mode,state", [("unknown", "unknown"), ("foreign", "error"), ("number", "error")]
)
async def test_failed_paid_blocks_fall_back_raw_and_never_automatically_retry(
    tmp_path, mode, state
):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        engine = SummaryEngine(store, Summarizer(mode))
        await drain(engine)
        context, _, refs = await choose(store, question)
        assert len(context) == 19 and not refs["summaries"]
        assert {
            r[0] for r in await store.db.all("SELECT state FROM participant_summary_blocks")
        } == {state}
        await engine.recover("999")
        await choose(store, question, memory_model="gpt-6.1-sol", participant_context_messages=100)
        await drain(engine)
        assert len(engine.adapter.calls) == 3
        assert (await store.db.one("SELECT COUNT(*) FROM model_calls"))[0] == 3


async def test_restart_started_unknown_and_disable_use_raw(tmp_path):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        model = Summarizer()
        engine = SummaryEngine(store, model)
        work = await engine.claim("999")
        assert work
        await engine.recover("999")
        await choose(store, question)
        assert (
            await store.db.one(
                "SELECT state FROM participant_summary_blocks WHERE id=?", (work["id"],)
            )
        )[0] == "unknown"
        assert not model.calls
        await drain(engine)
        for opts in ({"participant_summaries_enabled": False}, {"memory_enabled": False}):
            context, diag, refs = await choose(store, question, **opts)
            assert len(context) == 19 and not refs["summaries"]
            assert not diag["participants"][0]["summaries"]["enabled"]


async def test_exact_values_caveats_links_forward_media_and_reply_target_stay_raw(tmp_path):
    async with store_at(tmp_path) as store:
        grant, question = await prepare(store)
        edits = {
            3: "153.14 USDT, курс 87,21–88,20 ₽, 10.10.2026 - старый, не текущий",
            4: "Ссылка https://example.org/quote",
            5: "Я никогда этого не говорил",
        }
        for mid, text in edits.items():
            edited = human_event(mid, text=text)
            await store.ingest(
                "999", "zarya_test", [{"update_id": 100 + mid, "edited_message": edited["message"]}]
            )
        attachment = human_event(
            7, text="Оригинальный документ с описанием", document={"file_id": "local-doc"}
        )
        await store.ingest(
            "999", "zarya_test", [{"update_id": 107, "edited_message": attachment["message"]}]
        )
        await choose(store, question)
        engine = SummaryEngine(store, Summarizer())
        await drain(engine)
        async with store.db.transaction() as c:
            from zarya.participant_context import window

            history, sources, _, _ = await window(c, "999", "-100", grant, "42", 19, before=22)
            packed, covered = await compact(
                c, "999", "-100", grant, "42", history, sources, Settings(), exact_ids={6}
            )
        for mid, text in edits.items():
            assert next(m for m in packed if m["message_id"] == mid)["text"] == text
        assert next(m for m in packed if m["message_id"] == 6)["role"] == "user"
        assert not {3, 4, 5, 6, 20, 21} & set(covered["covered_ids"])
        # Whole collector must retain the first exact original, even when a summary
        # is anchored to the same message ID. It is this result that enters the model.
        context, _, _ = await choose(store, question)
        assert any(
            m["message_id"] == 7 and m["role"] == "user" and m["content_kinds"] == ["document"]
            for m in context
        )
        for mid, text in edits.items():
            assert (
                next(m for m in context if m["message_id"] == mid and m["role"] == "user")["text"]
                == text
            )


@pytest.mark.parametrize("change", ["edit", "erase", "revoke", "age", "result"])
async def test_provenance_and_purge_invalidate_completed_summary_and_accounting_survives(
    tmp_path, change
):
    async with store_at(tmp_path) as store:
        grant, question = await prepare(store)
        await choose(store, question)
        engine = SummaryEngine(store, Summarizer())
        await drain(engine)
        _, _, refs = await choose(store, question)
        snapshot = dict(context_refs=refs, retrieval_chat_id="-100", source_access_version=grant)
        assert await valid_snapshot(store.db.writer, "999", snapshot)
        if change == "edit":
            edited = human_event(3, text="Изменённый источник")
            await store.ingest(
                "999", "zarya_test", [{"update_id": 90, "edited_message": edited["message"]}]
            )
        else:
            async with store.db.transaction() as c:
                if change == "erase":
                    await c.execute(
                        "UPDATE events SET payload='{}' WHERE id=(SELECT event_id FROM "
                        "participant_messages WHERE message_id=3)"
                    )
                if change == "revoke":
                    await c.execute("UPDATE telegram_access SET version=version+1")
                if change == "age":
                    await c.execute(
                        "UPDATE participant_messages SET received_at=? WHERE message_id=3",
                        (time.time() - 91 * 86400,),
                    )
                if change == "result":
                    await c.execute(
                        "UPDATE participant_summary_blocks SET result='подменено' WHERE id=?",
                        (refs["summaries"][0]["id"],),
                    )
        assert not await valid_snapshot(store.db.writer, "999", snapshot)
        if change in {"erase", "age"}:
            await MemoryEngine(store, None, tmp_path).cleanup("999", force=True)
            assert (
                await store.db.one(
                    "SELECT result FROM participant_summary_blocks WHERE id=?",
                    (refs["summaries"][0]["id"],),
                )
            )[0] is None
        assert (await store.db.one("SELECT COUNT(*) FROM model_calls WHERE state='completed'"))[
            0
        ] == 3


async def test_inflight_cancellation_keeps_actual_provider_ledger(tmp_path):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        started, release = asyncio.Event(), asyncio.Event()

        class Delayed(Summarizer):
            async def generate(self, request):
                started.set()
                await release.wait()
                return await super().generate(request)

        engine = SummaryEngine(store, Delayed())
        work = await engine.claim("999")
        task = asyncio.create_task(engine.execute(work))
        await asyncio.wait_for(started.wait(), 2)
        changed = human_event(3, text="Правка оригинала")
        await store.ingest(
            "999", "zarya_test", [{"update_id": 99, "edited_message": changed["message"]}]
        )
        release.set()
        await task
        assert (
            await store.db.one(
                "SELECT state,result FROM participant_summary_blocks WHERE id=?", (work["id"],)
            )
        ) == ("cancelled", None)
        assert (
            await store.db.one(
                "SELECT state,request_json FROM model_calls WHERE participant_summary_id=?",
                (work["id"],),
            )
        ) == ("completed", None)


async def test_summary_demand_is_bounded_and_ui_reads_never_start_work(tmp_path):
    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        memory = MemoryEngine(store, Summarizer(), tmp_path)
        data = await memory.participant_window("999", "-100", "42")
        assert not data["summaries"]["blocks"]
        assert not await store.db.all("SELECT id FROM participant_summary_blocks")
        await choose(store, question)
        # A second participant and a separate private scope cannot inherit these blocks.
        other = await memory.participant_window("999", "-100", "84")
        assert not other["summaries"]["blocks"]
        assert await memory.participant_window("999", "42", "42") is None
        await drain(memory.summaries)
        data = await memory.participant_window("999", "-100", "42")
        assert data["summaries"]["covered_ids"] and data["human_messages"] == 20
        for b in data["summaries"]["blocks"]:
            if b["used_ids"]:
                assert hashlib.sha256(b["summary"].encode()).hexdigest()
        assert len(memory.adapter.calls) == 3


async def test_dialogue_uses_saved_summary_and_replay_never_creates_blocks(tmp_path):
    from test_dialogue import FakeModel
    from test_dialogue import drain as drain_dialogue

    from zarya.dialogue import DialogueEngine

    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        summaries = SummaryEngine(store, Summarizer())
        await drain(summaries)
        dialogue = DialogueEngine(store, FakeModel(ModelResult(text="Продолжим разговор")))
        await drain_dialogue(dialogue)
        run = await store.db.one("SELECT id,snapshot FROM dialogue_runs WHERE message_id=22")
        snapshot = json.loads(run[1])
        assert len(snapshot["context_refs"]["summaries"]) == 3
        data = json.loads(snapshot["request"]["input"])
        assert len([m for m in data["recent_chat"] if m["role"] == "summary"]) == 3
        assert data["current_message"]["message_id"] == 22
        before = (await store.db.one("SELECT COUNT(*) FROM participant_summary_blocks"))[0]
        recorded = await dialogue.replay(run[0], "recorded")
        assert recorded["snapshot"]["context"] == snapshot["context"]
        assert (await store.db.one("SELECT COUNT(*) FROM participant_summary_blocks"))[0] == before
        changed = human_event(3, text="исправление исходного сообщения")
        await store.ingest(
            "999", "zarya_test", [{"update_id": 123, "edited_message": changed["message"]}]
        )
        for rid in (run[0], recorded["id"]):
            result = await store.db.one(
                "SELECT state,response,snapshot FROM dialogue_runs WHERE id=?", (rid,)
            )
            assert result[0] == "cancelled" and result[1] is None
            assert json.loads(result[2])["invalidated"]


async def test_summary_cost_and_queue_scoping_are_in_operations(tmp_path):
    from zarya.operations import Operations

    async with store_at(tmp_path) as store:
        _, question = await prepare(store)
        await choose(store, question)
        ops = Operations(store.db)
        queues = (await ops.overview(bot="999", scope="group", chat="-100"))["queues"]
        assert any(r["queue"] == "participant_summary" and r["count"] == 3 for r in queues)
        await drain(SummaryEngine(store, Summarizer()))
        data = await ops.overview(bot="999", scope="group", chat="-100")
        assert data["summary"]["calls"] == 3
        assert {r["task"] for r in data["calls"]} == {"participant_summary"}
        assert data["summary"]["known_cost_usd"] > 0
        assert not (await ops.overview(bot="999", scope="private"))["calls"]
        assert TEXT not in json.dumps(data)


@pytest.mark.parametrize("mode", ["shadow", "live"])
async def test_initiative_cannot_admit_paid_summaries(tmp_path, mode, monkeypatch):
    from test_behavior import ready_engine

    from zarya.models import GroupBehaviorUpdate

    monkeypatch.setattr("zarya.behavior.random.random", lambda: 0.01)
    async with store_at(tmp_path) as store:
        engine, _, behavior = await ready_engine(store, -100)
        await store.ingest("999", "zarya_test", [human_event(i, text=TEXT) for i in range(2, 22)])
        # The ambient backlog predates enabling initiative, discard it without a draw.
        async with store.db.transaction() as c:
            await c.execute("UPDATE jobs SET state='done'")
        await behavior.update_policy(
            "999", "-100", GroupBehaviorUpdate(expected_version=0, mode=mode)
        )
        await store.ingest(
            "999", "zarya_test", [human_event(22, text="Вот это интересный поворот в разговоре")]
        )
        work = await engine.claim("999")
        assert work and work["snapshot"]["behavior"]["mode"] == mode
        assert not await store.db.all("SELECT id FROM participant_summary_blocks")
