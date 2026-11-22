import asyncio
import hashlib
import importlib
import os
import sqlite3
import sys
import zipfile
from contextlib import closing, nullcontext
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


def test_preupload_failures_do_not_accumulate_local_snapshots(modules, tmp_path, monkeypatch):
    cd, _, _ = modules
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "sync", lambda _: None)
    monkeypatch.setattr(cd.subprocess, "run", lambda *_, **__: None)
    running = True

    def control(action):
        nonlocal running
        running = action == "start"

    def capture(_release, *args):
        assert not running
        snapshot = args[-1]
        snapshot.mkdir()
        (snapshot / "zarya.sqlite3").write_bytes(b"fixture")

    def deny_write(_snapshot, spool, *_args):
        (spool / "snapshot-fixture.age").write_bytes(b"ciphertext fixture")
        raise RuntimeError("storage quota")

    monkeypatch.setattr(cd, "systemctl", control)
    monkeypatch.setattr(cd, "active", lambda: running)
    monkeypatch.setattr(cd, "healthy", lambda _: running)
    monkeypatch.setattr(cd, "python", capture)
    monkeypatch.setattr(cd.disk_backup, "publish", deny_write)
    state = {}
    cd.save(state, "idle", live=None)
    for attempt in range(3):
        target = "a" * 40 + f"-{attempt}"
        run = tmp_path / "runs" / target
        run.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="storage quota"):
            cd.deploy(state, target, run)
        state = cd.load()
        assert running and state["phase"] == "idle" and state["disk_retry_at"] > 0
    assert not list((tmp_path / "runs").iterdir())


def test_preupload_cleanup_resumes_after_interruption(modules, tmp_path, monkeypatch):
    cd, _, _ = modules
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "sync", lambda _: None)
    monkeypatch.setattr(cd, "systemctl", lambda action: None)
    run = tmp_path / "runs" / ("a" * 40 + "-test")
    run.mkdir(parents=True)
    db = run / "fixture.sqlite3"
    db.write_bytes(b"fixture")
    cd.save({}, "preupload", live=None, old=None, target="a" * 40, run=run.name)
    monkeypatch.setattr(cd, "healthy", lambda _: False)
    with pytest.raises(RuntimeError, match="needs operator attention"):
        cd.cycle()
    assert cd.load()["phase"] == "returning"
    assert db.is_file()
    monkeypatch.setattr(cd, "healthy", lambda _: True)
    cd.cycle()
    assert cd.load()["phase"] == "idle"
    assert not run.exists()


def test_idle_poll_bounds_successful_deploys_across_upgrade_and_failure(
    modules, tmp_path, monkeypatch
):
    cd, _, _ = modules
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "sync", lambda _: None)
    running = True

    def control(action):
        nonlocal running
        running = action == "start"

    monkeypatch.setattr(cd, "systemctl", control)
    monkeypatch.setattr(cd, "active", lambda: running)
    monkeypatch.setattr(cd, "healthy", lambda _: True)
    monkeypatch.setattr(cd, "lock", lambda _: nullcontext())
    monkeypatch.setattr(cd.subprocess, "run", lambda *_, **__: None)
    monkeypatch.setattr(cd.disk_backup, "publish", lambda *_: None)
    monkeypatch.setattr(cd, "python", lambda *_: None)

    releases = tmp_path / "releases"
    runs = tmp_path / "runs"
    releases.mkdir()
    runs.mkdir()
    initial = "a" * 40
    state = {}
    cd.save(state, "idle", live=initial + "-env", old=None, run=initial + "-copy", target=initial)
    (releases / state["live"]).mkdir()
    (runs / state["run"]).mkdir()
    legacy = runs / ("0" * 40 + "-unclassified")
    legacy.mkdir()

    class GitHub:
        def tip(self):
            return cd.load()["live"][:40]

    monkeypatch.setattr(cd, "configured", GitHub)
    for char in "bcd":
        sha = char * 40
        target = sha + "-env"
        run = runs / (sha + "-copy")
        (releases / target).mkdir()
        run.mkdir()
        for name in ("candidate.sqlite3", "candidate.sqlite3-wal", "candidate.sqlite3-shm"):
            (run / name).touch()
        if char == "c":
            with monkeypatch.context() as patch:

                def fail_prepare(_release, *args):
                    if args[1] == "prepare":
                        raise ValueError("bad migration")

                patch.setattr(cd, "python", fail_prepare)
                with pytest.raises(ValueError, match="bad migration"):
                    cd.deploy(cd.load(), target, run)
        else:
            cd.deploy(cd.load(), target, run)
            assert not list(run.glob("candidate.sqlite3*"))
        cd.cycle()
        cd.cycle()
        state = cd.load()
        assert (releases / state["live"]).is_dir()
        assert (releases / ("a" * 40 + "-env")).exists() == (char != "d")
        if char == "c":
            assert (runs / ("b" * 40 + "-copy")).is_dir()
    assert {folder.name for folder in releases.iterdir()} == {c * 40 + "-env" for c in "bd"}
    assert {folder.name for folder in runs.iterdir()} == {
        legacy.name,
        "c" * 40 + "-copy",
        "d" * 40 + "-copy",
    }


def test_upgrade_after_legacy_rollback_keeps_unidentified_previous(modules, tmp_path, monkeypatch):
    cd, _, _ = modules
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    old = tmp_path / "releases" / ("a" * 40 + "-env")
    old.mkdir(parents=True)
    os.utime(old, (946684800, 946684800))
    live = "b" * 40 + "-env"
    (old.parent / live).mkdir()
    cd.prune_local(dict(live=live, old=live, rejected="c" * 40))
    assert old.is_dir()


def test_retired_run_cleanup_retries_after_interruption(modules, tmp_path, monkeypatch):
    cd, _, _ = modules
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "sync", lambda _: None)
    (tmp_path / "releases").mkdir()
    runs = tmp_path / "runs"
    old = runs / ("a" * 40 + "-old")
    current = runs / ("b" * 40 + "-current")
    old.mkdir(parents=True)
    current.mkdir()
    cd.save(
        {}, "idle", live=None, retired_run=old.name, run=current.name, successful_run=current.name
    )
    with monkeypatch.context() as patch:

        def interrupt(_):
            raise InterruptedError

        patch.setattr(cd, "sync", interrupt)
        with pytest.raises(InterruptedError):
            cd.prune_local(cd.load())
    assert not old.exists() and cd.load()["retired_run"] == old.name
    cd.prune_local(cd.load())
    assert current.is_dir() and cd.load()["retired_run"] is None
    for unexpected in (current.name, "../outside"):
        with pytest.raises(ValueError, match="invalid retired"):
            cd.prune_local(dict(cd.load(), retired_run=unexpected))
    assert current.is_dir()


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
