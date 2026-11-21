import asyncio
import hashlib
import importlib
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from zarya import backup


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "deploy"))
    return tuple(
        importlib.import_module(name) for name in ("deployer", "pull_release", "sqlite_release")
    )


def test_artifact_content_bound_to_revision_and_manifest(modules, tmp_path):
    _, pull, _ = modules
    sha = "a" * 40
    raw = (sha + "\n").encode()
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("REVISION", raw)
        zip_file.writestr("SHA256SUMS", hashlib.sha256(raw).hexdigest() + "  ./REVISION\n")
    pull.unpack(archive, tmp_path / "good", sha)
    with pytest.raises(ValueError, match="manifest or revision"):
        pull.unpack(archive, tmp_path / "wrong", "b" * 40)
    with zipfile.ZipFile(archive, "a") as zip_file:
        zip_file.writestr("../outside", b"")
    with pytest.raises(ValueError, match="unsafe"):
        pull.unpack(archive, tmp_path / "bad", sha)


def test_workflow_eligibility(modules, monkeypatch):
    _, pull, _ = modules
    gh = pull.GitHub("org/app", "main", "ci.yml", "app", "")
    sha = "a" * 40
    run = dict(
        id=42,
        status="completed",
        conclusion="success",
        head_sha=sha,
        head_branch="main",
        event="push",
        head_repository={"full_name": "org/app"},
        path=".github/workflows/ci.yml",
    )
    art = dict(id=1, expired=False, name="app-" + sha, digest="sha256:" + "c" * 64)

    def reply(path):
        return {"workflow_runs": [run]} if "/workflows/" in path else {"artifacts": [art]}

    monkeypatch.setattr(gh, "json", reply)
    assert gh.artifact(sha) == art
    run["path"] += "@main"
    assert gh.artifact(sha) == art
    run["path"] = ".github/workflows/ci.yml@other"
    with pytest.raises(ValueError, match="provenance"):
        gh.artifact(sha)
    run["path"] = ".github/workflows/ci.yml"
    art["expired"] = True
    with pytest.raises(ValueError, match="expired"):
        gh.artifact(sha)
    run["event"] = "pull_request"
    with pytest.raises(ValueError, match="provenance"):
        gh.artifact(sha)
    run["conclusion"] = "failure"
    assert gh.artifact(sha) is None


def test_preservation_rejects_dropped_empty_column(modules, tmp_path):
    _, _, adapter = modules
    paths = (tmp_path / "before.db", tmp_path / "after.db")
    for path, columns in zip(paths, ("id, content", "id"), strict=True):
        with closing(sqlite3.connect(path)) as db:
            db.execute(f"CREATE TABLE memory ({columns})")
    with pytest.raises(ValueError, match="removed existing columns"):
        adapter.preserved(*paths)


def test_busy_model_call_defers_cutover(modules, tmp_path, monkeypatch):
    cd, _, adapter = modules
    path = tmp_path / "zarya.sqlite3"
    asyncio.run(adapter.migrate(path))
    monkeypatch.setattr(cd, "DATA", tmp_path)
    assert not cd.in_flight()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO model_calls(provider, model, state, created_at) "
            "VALUES ('fixture', 'test', 'started', '')"
        )
    assert cd.in_flight()


@pytest.mark.skipif(sys.platform != "linux", reason="controller uses Linux flock/fsync/systemd")
@pytest.mark.parametrize("fault", ["none", "cloud", "prepare", "interrupt", "post-admission"])
def test_deploy_recovery_boundaries(modules, tmp_path, monkeypatch, fault):
    cd, _, adapter = modules
    data = tmp_path / "data"
    target = "1" * 40 + "-test"
    run = tmp_path / "runs" / target
    run.mkdir(parents=True)
    data.mkdir()
    path = data / "zarya.sqlite3"
    asyncio.run(adapter.migrate(path))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO users VALUES ('fixture',NULL,'original')")
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "DATA", data)

    def publish(*_):
        if fault == "cloud":
            raise RuntimeError("cloud outage")

    monkeypatch.setattr(cd.disk_backup, "publish", publish)
    running = True

    def control(action):
        nonlocal running
        running = action == "start"
        if running and cd.load()["phase"] == "admitted" and fault == "post-admission":
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("UPDATE users SET display_name='new traffic'")

    def invoke(release, *args):
        if args[0] == "-m":
            return getattr(backup, args[2])(*args[3:])
        if args[1] == "prepare":
            if fault == "prepare":
                raise ValueError("injected before promotion")
            adapter.prepare(*args[2:])
        else:
            adapter.copy_database(*args[2:], existing=True)
            if release == target and fault == "interrupt":
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute("UPDATE users SET display_name='unpublished change'")
                raise KeyboardInterrupt

    monkeypatch.setattr(cd, "python", invoke)
    monkeypatch.setattr(cd, "systemctl", control)
    monkeypatch.setattr(cd, "active", lambda: running)
    monkeypatch.setattr(cd, "healthy", lambda _: running and fault != "post-admission")
    state = {}
    cd.save(state, "idle", live=None)
    if fault == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            cd.deploy(state, target, run)
        assert cd.load()["phase"] == "switching"
        cd.cycle()
    elif fault == "cloud":
        with pytest.raises(RuntimeError, match="cloud outage"):
            cd.deploy(state, target, run)
        assert running and cd.load()["rejected"] is None and cd.load()["disk_retry_at"] > 0
    elif fault == "prepare":
        with pytest.raises(ValueError, match="before promotion"):
            cd.deploy(state, target, run)
    elif fault == "post-admission":
        with pytest.raises(RuntimeError, match="after admission"):
            cd.deploy(state, target, run)
        assert not running
        with pytest.raises(RuntimeError, match="held"):
            cd.cycle()
    else:
        cd.deploy(state, target, run)
    state = cd.load()
    assert state["phase"] == ("blocked" if fault == "post-admission" else "idle")
    assert state["live"] == (target if fault in {"none", "post-admission"} else None)
    with closing(sqlite3.connect(path)) as conn:
        expected = "new traffic" if fault == "post-admission" else "original"
        assert conn.execute("SELECT display_name FROM users").fetchall() == [(expected,)]
