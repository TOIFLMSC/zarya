import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from filelock import Timeout

from zarya.adapters import FakeModel, FakeTelegram
from zarya.app import COOKIE, create_app
from zarya.config import Config
from zarya.database import Database
from zarya.models import Settings

ORIGIN = "http://127.0.0.1:8787"
PASSWORD = "local-test-password-only"


@asynccontextmanager
async def running(path: Path):
    config = Config(data_dir=path, web_dir=path / "web")
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield app, client


async def setup(client, path):
    token = (path / "bootstrap-token.txt").read_text()
    response = await client.post("/api/setup", json={"token": token, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return token, response


async def test_setup_and_settings_persist_but_sessions_do_not(tmp_path):
    async with running(tmp_path) as (app, client):
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/api/settings")).status_code == 401
        token, response = await setup(client, tmp_path)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert not (tmp_path / "bootstrap-token.txt").exists()
        initial = (await client.get("/api/settings")).json()
        initial["settings"]["owner_telegram_id"] = "9007199254740993"
        initial["settings"]["owner_name"] = "Алексей"
        saved = await client.put(
            "/api/settings", json={"expected_version": 1, "settings": initial["settings"]}
        )
        assert saved.status_code == 200
        old_cookie = client.cookies.get(COOKIE)
        assert PASSWORD not in (await app.state.db.admin_hash())
        for route in ["/api/settings", "/api/status", "/api/session"]:
            output = (await client.get(route)).text
            assert PASSWORD not in output and token not in output
        # Simulate a leftover bootstrap file after a crash between commit and unlink.
        (tmp_path / "bootstrap-token.txt").write_text(token)
    async with running(tmp_path) as (_, client):
        client.cookies.set(COOKIE, old_cookie)
        assert (await client.get("/api/settings")).status_code == 401
        assert (await client.get("/api/session")).json()["setup_required"] is False
        assert not (tmp_path / "bootstrap-token.txt").exists()
        login = await client.post("/api/login", json={"password": PASSWORD})
        assert login.status_code == 200
        saved = (await client.get("/api/settings")).json()
        assert saved["settings"]["owner_name"] == "Алексей"
        assert saved["settings"]["owner_telegram_id"] == "9007199254740993"
        assert saved["version"] == 2


async def test_bootstrap_survives_restart_and_single_admin(tmp_path):
    async with running(tmp_path):
        token = (tmp_path / "bootstrap-token.txt").read_text()
    async with running(tmp_path) as (_, client):
        assert (tmp_path / "bootstrap-token.txt").read_text() == token
        invalid = await client.post("/api/setup", json={"token": "я" * 32, "password": PASSWORD})
        assert invalid.status_code == 403
        responses = await asyncio.gather(
            *[
                client.post("/api/setup", json={"token": token, "password": PASSWORD})
                for _ in range(2)
            ]
        )
        assert sorted(response.status_code for response in responses) == [200, 409]


async def test_boundaries_csrf_validation_and_logout(tmp_path):
    async with running(tmp_path) as (app, client):
        _, response = await setup(client, tmp_path)
        csrf = response.json()["csrf"]
        payload = {"expected_version": 1, "settings": Settings().model_dump()}
        for headers in [
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Origin": ""},
            {"X-CSRF-Token": "wrong"},
        ]:
            assert (
                await client.put("/api/settings", json=payload, headers=headers)
            ).status_code == 403
        assert (
            await client.get("/api/status", headers={"Host": "evil.example"})
        ).status_code == 400
        assert (await client.post("/api/login", content="x")).status_code == 415
        assert (
            await client.post("/api/login", json={"password": PASSWORD + "x"})
        ).status_code == 401
        bad = await client.post("/api/login", json={"password": "SENSITIVE", "extra": PASSWORD})
        assert bad.status_code == 422 and "SENSITIVE" not in bad.text and PASSWORD not in bad.text
        invalid = await client.put(
            "/api/settings", json={**payload, "settings": {"owner_telegram_id": "not-id"}}
        )
        assert invalid.status_code == 422
        assert (await client.post("/api/login", json={"password": "x" * 40000})).status_code == 413
        assert (await client.get("/api/settings")).json()["version"] == 1
        session = app.state.sessions.get(client.cookies.get(COOKIE))
        session.expires = 0
        assert (await client.get("/api/settings")).status_code == 401
        login = await client.post("/api/login", json={"password": PASSWORD})
        client.headers["X-CSRF-Token"] = login.json()["csrf"]
        token = client.cookies.get(COOKIE)
        assert login.json()["csrf"] != csrf
        assert (await client.post("/api/logout", json={})).status_code == 200
        client.cookies.set(COOKIE, token)
        assert (await client.get("/api/settings")).status_code == 401


async def test_settings_optimistic_concurrency(tmp_path):
    async with running(tmp_path) as (_, client):
        await setup(client, tmp_path)
        responses = await asyncio.gather(
            *[
                client.put(
                    "/api/settings", json={"expected_version": 1, "settings": {"owner_name": name}}
                )
                for name in ["Первая вкладка", "Вторая вкладка"]
            ]
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        saved = (await client.get("/api/settings")).json()
        winner = next(response.json() for response in responses if response.status_code == 200)
        assert saved == winner


async def test_transaction_rollback_and_cancellation(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    await db.open()
    try:
        with pytest.raises(RuntimeError):
            async with db.transaction() as connection:
                await connection.execute("INSERT INTO users VALUES ('1', 'one', 'One')")
                raise RuntimeError("rollback")
        assert await db.one("SELECT 1 FROM users WHERE telegram_id='1'") is None
        inserted = asyncio.Event()

        async def cancelled_writer():
            async with db.transaction() as connection:
                await connection.execute("INSERT INTO users VALUES ('2', 'two', 'Two')")
                inserted.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancelled_writer())
        await inserted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await db.one("SELECT 1 FROM users WHERE telegram_id='2'") is None
        saved = await db.update_settings(1, Settings(owner_name="После отмены"))
        assert saved.version == 2
        row = await db.one("SELECT document FROM settings_versions WHERE version=2")
        assert json.loads(row[0])["owner_name"] == "После отмены"
    finally:
        await db.close()


async def test_single_instance_and_offline_adapters(tmp_path):
    async with running(tmp_path):
        with pytest.raises(Timeout):
            async with running(tmp_path):
                pytest.fail("second instance started")
    model, telegram = FakeModel(), FakeTelegram()
    await telegram.send("123", await model.reply("Привет"))
    assert model.calls == ["Привет"]
    assert telegram.sent == [("123", "Тестовый ответ без API: Привет")]


async def test_login_rate_limit(tmp_path):
    async with running(tmp_path) as (_, client):
        await setup(client, tmp_path)
        replies = [
            await client.post("/api/login", json={"password": "bad-password-123"}) for _ in range(8)
        ]
        assert replies[-1].status_code == 429


@pytest.mark.parametrize("dev_ui, expected", [(False, 403), (True, 200)])
async def test_vite_origin_is_explicitly_opt_in(tmp_path, dev_ui, expected):
    config = Config(data_dir=tmp_path, web_dir=tmp_path / "web", dev_ui=dev_ui)
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=ORIGIN,
            headers={"Origin": "http://127.0.0.1:5173"},
        ) as client:
            token = (tmp_path / "bootstrap-token.txt").read_text()
            response = await client.post("/api/setup", json={"token": token, "password": PASSWORD})
            assert response.status_code == expected


async def test_bootstrap_cleanup_failure_does_not_reset_admin(tmp_path, monkeypatch):
    real_unlink = Path.unlink

    def locked_file(path, *args, **kwargs):
        if path.name == "bootstrap-token.txt":
            raise PermissionError("File is open in an editor")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked_file)
        async with running(tmp_path) as (_, client):
            token = (tmp_path / "bootstrap-token.txt").read_text()
            response = await client.post("/api/setup", json={"token": token, "password": PASSWORD})
            assert response.status_code == 200
        async with running(tmp_path) as (_, client):
            assert (await client.get("/api/session")).json()["setup_required"] is False
            assert (await client.post("/api/login", json={"password": PASSWORD})).status_code == 200
