import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update

from zarya.app import create_app
from zarya.config import Config
from zarya.database import ConflictError, Database
from zarya.telegram_runtime import AiogramTransport, TelegramRuntime
from zarya.telegram_store import TelegramStore


def event(number, chat=42, text="/start", **extra):
    return {
        "update_id": number,
        "message": {
            "message_id": number,
            "date": 1,
            "chat": {
                "id": chat,
                "type": "private" if chat > 0 else "supergroup",
                "title": "Тестовая группа",
                "is_forum": extra.get("message_thread_id") is not None,
            }
            if chat < 0
            else {"id": chat, "type": "private"},
            "from": {
                "id": 42,
                "first_name": "Тестовый участник",
                "is_bot": False,
                "username": "test_user",
            },
            "text": text,
            **extra,
        },
    }


class FakeTransport:
    def __init__(self, updates=None, fail_send=None):
        self.batch = updates or []
        self.sent = []
        self.offsets = []
        self.fail_send = fail_send
        self.closed = False

    async def identity(self):
        return {
            "id": "999",
            "username": "zarya_test",
            "name": "Заря",
            "privacy_disabled": True,
            "webhook": False,
        }

    async def updates(self, offset):
        self.offsets.append(offset)
        if self.batch:
            batch, self.batch = self.batch, []
            return batch
        await asyncio.sleep(0.03)
        return []

    async def send(self, chat_id, text, thread_id, reply_id=None):
        self.sent.append((chat_id, text, thread_id))
        if self.fail_send:
            raise self.fail_send
        return 123

    async def typing(self, chat_id, thread_id):
        return True

    async def close(self):
        self.closed = True


@asynccontextmanager
async def store_at(path):
    db = Database(path / "test.sqlite3")
    await db.open()
    store = TelegramStore(db)
    await store.recover("999")
    try:
        yield store
    finally:
        await db.close()


async def approve(store, scope="private", subject="42", bot="999"):
    version = (
        await store.db.one(
            "SELECT version FROM telegram_access WHERE bot_id=? AND scope=? AND subject_id=?",
            (bot, scope, subject),
        )
    )[0]
    await store.decide(bot, scope, subject, "approved", version)


async def queued(store):
    await store.ingest("999", "zarya_test", [event(1)])
    await approve(store)
    await store.ingest("999", "zarya_test", [event(2)])
    await store.process_one("999")


async def test_private_start_minimal_repeated_and_declined(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest(
            "999", "zarya_test", [event(1, text="secret before start"), event(2), event(2)]
        )
        peers = await s.peers("999", "private", 0)
        assert peers["total"] == 1 and peers["items"][0]["state"] == "pending"
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 0
        assert (await s.db.one("SELECT COUNT(*) FROM events"))[0] == 2
        assert all(item[0] == "{}" for item in await s.db.all("SELECT payload FROM events"))
        await s.decide("999", "private", "42", "rejected", 1)
        await s.ingest("999", "zarya_test", [event(3)])
        assert (await s.peers("999", "private", 0))["items"][0]["state"] == "rejected"
        assert await s.offset("999") == 4


async def test_scopes_owner_username_topics_and_edits(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100, "group secret")])
        await approve(s, "group", "-100")
        await s.ingest(
            "999",
            "zarya_test",
            [
                event(2),
                event(3, -100, "allowed", message_thread_id=8, reply_to_message={"message_id": 1}),
            ],
        )
        edited = event(4, -100, "edited", message_thread_id=9)
        edited["edited_message"] = edited.pop("message")
        await s.ingest("999", "zarya_test", [edited])
        assert (await s.peers("999", "private", 0))["items"][0]["state"] == "pending"
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 2
        stored = json.loads((await s.db.one("SELECT payload FROM events WHERE update_id=4"))[0])
        assert stored["edited_message"]["message_thread_id"] == 9
        assert (await s.db.one("SELECT COUNT(*) FROM telegram_access WHERE scope='group'"))[0] == 1
        # No username-based owner elevation or access inference anywhere in ingestion.
        assert (await s.db.one("SELECT payload FROM events WHERE update_id=2"))[0] == "{}"


async def test_atomic_ingest_failure_and_replay(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        commit = s.db.writer.commit

        async def fail():
            raise OSError("disk full")

        monkeypatch.setattr(s.db.writer, "commit", fail)
        with pytest.raises(OSError):
            await s.ingest("999", "zarya_test", [event(50)])
        monkeypatch.setattr(s.db.writer, "commit", commit)
        assert await s.offset("999") is None
        assert (await s.db.one("SELECT COUNT(*) FROM events"))[0] == 0
        await s.ingest("999", "zarya_test", [event(50)])
        await approve(s)
        await s.ingest("999", "zarya_test", [event(100)])
        await s.ingest("999", "zarya_test", [event(100)])
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 1
        assert await s.offset("999") == 101


async def test_outbox_atomicity_and_delivery_success(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1)])
        await approve(s)
        await s.ingest("999", "zarya_test", [event(2)])
        commit = s.db.writer.commit

        async def fail():
            raise OSError("disk full")

        monkeypatch.setattr(s.db.writer, "commit", fail)
        with pytest.raises(OSError):
            await s.process_one("999")
        monkeypatch.setattr(s.db.writer, "commit", commit)
        assert (await s.db.one("SELECT state FROM jobs"))[0] == "pending"
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 0
        await s.process_one("999")
        await s.process_one("999")
        t = FakeTransport()
        runtime = TelegramRuntime(s, transport=t)
        await runtime.deliver_one("999")
        await runtime.deliver_one("999")
        assert len(t.sent) == 1
        assert await s.db.one("SELECT state,message_id,attempts FROM outbox") == ("sent", 123, 1)


async def test_timeout_and_restart_never_resend_unknown(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        t = FakeTransport(fail_send=TimeoutError("ambiguous"))
        await TelegramRuntime(s, transport=t).deliver_one("999")
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "unknown"
    async with store_at(tmp_path) as s:
        t = FakeTransport()
        await TelegramRuntime(s, transport=t).deliver_one("999")
        assert t.sent == []
        await s.dismiss("999", 1)
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "dismissed"


async def test_interrupted_send_and_running_job_recovery(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET state='sending'")
            await c.execute("UPDATE jobs SET state='running'")
    async with store_at(tmp_path) as s:
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "unknown"
        assert (await s.db.one("SELECT state FROM jobs"))[0] == "pending"
        await s.process_one("999")
        assert (await s.db.one("SELECT COUNT(*) FROM outbox"))[0] == 1
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "unknown"


async def test_revoke_reapprove_cancels_old_work_and_stale_decision(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        await s.ingest("999", "zarya_test", [event(3)])
        await s.decide("999", "private", "42", "revoked", 2)
        with pytest.raises(ConflictError):
            await s.decide("999", "private", "42", "approved", 2)
        await approve(s)
        await s.process_one("999")
        t = FakeTransport()
        await TelegramRuntime(s, transport=t).deliver_one("999")
        assert t.sent == []
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "cancelled"
        assert (await s.db.one("SELECT state FROM jobs ORDER BY id DESC LIMIT 1"))[0] == "cancelled"


async def test_revoke_waits_for_started_send_then_prevents_new(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        entered, release = asyncio.Event(), asyncio.Event()
        t = FakeTransport()

        async def send(*args):
            entered.set()
            await release.wait()
            return 10

        t.send = send
        sending = asyncio.create_task(TelegramRuntime(s, transport=t).deliver_one("999"))
        await entered.wait()
        revoking = asyncio.create_task(s.decide("999", "private", "42", "revoked", 2))
        await asyncio.sleep(0)
        assert not revoking.done()
        release.set()
        await asyncio.gather(sending, revoking)
        await s.ingest("999", "zarya_test", [event(3)])
        assert (await s.db.one("SELECT COUNT(*) FROM jobs"))[0] == 1
        assert (await s.peers("999", "private", 0))["items"][0]["state"] == "revoked"


async def test_retry_after_respects_delay_and_attempt_bound(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        t = FakeTransport(
            fail_send=TelegramRetryAfter(
                method=SendMessage(chat_id=42, text="x"), message="wait", retry_after=2
            )
        )
        runtime = TelegramRuntime(s, transport=t)
        await runtime.deliver_one("999")
        value = await s.db.one("SELECT state,next_attempt FROM outbox")
        assert value[0] == "pending" and value[1] > time.time()
        await runtime.deliver_one("999")
        assert len(t.sent) == 1
        async with s.db.transaction() as c:
            await c.execute("UPDATE outbox SET next_attempt=0,attempts=3")
            await c.execute("UPDATE delivery_limits SET last_attempt=0,blocked_until=0")
        await runtime.deliver_one("999")
        assert (await s.db.one("SELECT state FROM outbox"))[0] == "failed"


async def test_bot_isolation_and_week_idle_cursor(tmp_path):
    async with store_at(tmp_path) as s:
        await queued(s)
        await s.recover("888")
        await s.ingest("888", "another_bot", [event(1)])
        assert (await s.peers("888", "private", 0))["items"][0]["state"] == "pending"
        assert not await s.process_one("888")
        async with s.gate:
            assert await s.claim_delivery("888") is None
        async with s.db.transaction() as c:
            await c.execute("UPDATE telegram_state SET last_received=0 WHERE bot_id='999'")
        assert await s.offset("999") is None
        await s.ingest("999", "zarya_test", [event(70)])
        assert await s.offset("999") == 71


async def test_migration_and_membership_require_reapproval(tmp_path):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest("999", "zarya_test", [event(2, -100, migrate_to_chat_id=-200)])
        await s.ingest("999", "zarya_test", [event(3, -200, migrate_from_chat_id=-100)])
        peers = (await s.peers("999", "group", 0))["items"]
        assert all(p["state"] == "revoked" for p in peers)
        with pytest.raises(ValueError):
            await approve(s, "group", "-100")
        await approve(s, "group", "-200")
        await s.ingest(
            "999",
            "zarya_test",
            [
                {
                    "update_id": 4,
                    "my_chat_member": {
                        "chat": {"id": -200, "type": "supergroup"},
                        "new_chat_member": {"status": "left"},
                    },
                }
            ],
        )
        assert (await s.db.one("SELECT state FROM telegram_access WHERE subject_id='-200'"))[
            0
        ] == "revoked"


async def test_adapter_preserves_telegram_aliases():
    transport = AiogramTransport("123456:" + "x" * 35)

    async def updates(**kwargs):
        assert kwargs["allowed_updates"] == ["message", "edited_message", "my_chat_member"]
        assert kwargs["request_timeout"] > kwargs["timeout"]
        return [Update.model_validate(event(1))]

    transport.bot.get_updates = updates
    try:
        assert "from" in (await transport.updates(None))[0]["message"]
    finally:
        await transport.close()


async def test_link_preview_defaults_do_not_poison_update_batch():
    transport = AiogramTransport("123456:" + "x" * 35)
    source = event(
        2,
        text="Статья https://example.org/article",
        link_preview_options={"url": "https://example.org/article", "prefer_large_media": False},
    )
    reply = event(3, text="Заря, проверь пост", reply_to_message=source["message"])

    async def updates(**kwargs):
        return [Update.model_validate(source), Update.model_validate(reply)]

    transport.bot.get_updates = updates
    try:
        values = await transport.updates(None)
        options = values[0]["message"]["link_preview_options"]
        assert options == {"url": "https://example.org/article", "prefer_large_media": False}
        assert values[1]["message"]["reply_to_message"]["link_preview_options"] == options
        assert "from" in values[1]["message"]
        assert json.loads(json.dumps(values)) == values
    finally:
        await transport.close()


@pytest.mark.parametrize("is_member,expected", [(False, "revoked"), (True, "approved")])
async def test_restricted_membership(tmp_path, is_member, expected):
    async with store_at(tmp_path) as s:
        await s.ingest("999", "zarya_test", [event(1, -100)])
        await approve(s, "group", "-100")
        await s.ingest("999", "zarya_test", [event(2, -100, "queued")])
        await s.ingest(
            "999",
            "zarya_test",
            [
                {
                    "update_id": 3,
                    "my_chat_member": {
                        "chat": {"id": -100, "type": "supergroup"},
                        "new_chat_member": {"status": "restricted", "is_member": is_member},
                    },
                }
            ],
        )
        assert (await s.db.one("SELECT state FROM telegram_access"))[0] == expected
        assert (await s.db.one("SELECT state FROM jobs"))[0] == (
            "pending" if is_member else "cancelled"
        )
        await s.ingest(
            "999",
            "zarya_test",
            [
                {
                    "update_id": 4,
                    "my_chat_member": {
                        "chat": {"id": -100, "type": "supergroup"},
                        "new_chat_member": {"status": "member"},
                    },
                }
            ],
        )
        assert (await s.db.one("SELECT state FROM telegram_access"))[0] == expected


async def test_runtime_stops_before_ack_on_commit_failure(tmp_path, monkeypatch):
    async with store_at(tmp_path) as s:
        await s.db.create_admin("test-hash")
        t = FakeTransport([event(1)])

        async def fail(*args):
            raise OSError("disk failure with SECRET")

        monkeypatch.setattr(s, "ingest", fail)
        r = TelegramRuntime(s, transport=t)
        r.start()
        try:
            await asyncio.wait_for(r.tasks[0], 2)
            assert t.offsets == [None]
            assert r.connection == "failed" and not r.healthy
            assert "SECRET" not in str(r.status())
        finally:
            await r.stop()
        assert t.closed


async def test_telegram_api_auth_csrf_and_decision(tmp_path):
    app = create_app(Config(data_dir=tmp_path, web_dir=tmp_path / "web"))
    async with app.router.lifespan_context(app):
        app.state.telegram.bot = await FakeTransport().identity()
        s = app.state.telegram_store
        await s.ingest("999", "zarya_test", [event(1)])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8787",
            headers={"Origin": "http://127.0.0.1:8787"},
        ) as c:
            assert (await c.get("/api/telegram")).status_code == 401
            token = (tmp_path / "bootstrap-token.txt").read_text()
            setup = await c.post(
                "/api/setup", json={"token": token, "password": "test-password-123"}
            )
            payload = {
                "bot_id": "999",
                "scope": "private",
                "subject_id": "42",
                "state": "approved",
                "expected_version": 1,
            }
            assert (await c.post("/api/telegram/access", json=payload)).status_code == 403
            c.headers["X-CSRF-Token"] = setup.json()["csrf"]
            assert (
                await c.post("/api/telegram/access", json={**payload, "bot_id": "888"})
            ).status_code == 409
            assert (await c.post("/api/telegram/access", json=payload)).status_code == 200
            assert (await c.post("/api/telegram/access", json=payload)).status_code == 409
            peers = (await c.get("/api/telegram/access?scope=private")).json()
            assert peers["items"][0]["state"] == "approved"
            assert (await c.get("/api/telegram/access?scope=other")).status_code == 422
            assert token not in (await c.get("/api/telegram")).text


async def test_token_local_file_and_env_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ZARYA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZARYA_TELEGRAM_TOKEN", raising=False)
    (tmp_path / "telegram-token.txt").write_text("local-secret", encoding="utf-8-sig")
    config = Config.from_env()
    assert config.telegram_token == "local-secret" and "local-secret" not in repr(config)
    monkeypatch.setenv("ZARYA_TELEGRAM_TOKEN", "env-secret")
    assert Config.from_env().telegram_token == "env-secret"
