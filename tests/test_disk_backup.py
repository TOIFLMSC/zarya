import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "deploy"))
    return importlib.import_module("disk_backup")


@pytest.mark.parametrize(
    "name",
    [
        "../file",
        "/tmp/x",
        "photos/../../x",
        "photos//x",
        "photos/x:stream",
        "photos/.. /x",
        "photos/CON",
        "openai-api-key.txt",
        "rclone.conf",
        "instance.lock",
    ],
)
def test_archive_allowlist(transport, name):
    try:
        accepted = transport.member(name)
    except ValueError:
        accepted = False
    assert not accepted


def test_prune_only_verified_names_in_class(transport, monkeypatch):
    names = [f"202610{i:02d}T030000Z-{'a' * 32}-{'b' * 64}.tar.gz.age" for i in range(1, 16)]
    foreign = ["other.age", "photos", f"upload-{names[0]}.partial", "2026-personal.tar.gz.age"]
    calls = []
    abandoned = "upload-20261001T030000Z-" + "a" * 32 + ".partial"
    monkeypatch.setattr(transport, "listing", lambda k: (k, foreign + names + [abandoned]))
    monkeypatch.setattr(transport, "rclone", lambda *args: calls.append(args))
    now = datetime(2026, 11, 21, tzinfo=UTC)
    transport.prune("predeploy", now)
    assert calls == [
        ("deletefile", "predeploy/" + n, "--yandex-hard-delete") for n in [*names[:-10], abandoned]
    ]
    calls.clear()
    transport.prune("nightly", now)
    assert calls == [("deletefile", "nightly/" + n, "--yandex-hard-delete") for n in names[:-1]]


@pytest.mark.parametrize("command", ["copyto", "hashsum", "moveto"])
def test_transfer_can_exceed_three_minutes_with_progress(transport, monkeypatch, command):
    def run(args, *, timeout=None, **_kwargs):
        # 500 MiB at 20 Mbit/s, with data flowing continuously.
        assert args[args.index("--timeout") + 1] == "25s"
        if timeout is not None and timeout < 210:
            raise transport.subprocess.TimeoutExpired(args, timeout)
        return SimpleNamespace(returncode=0, stdout="completed", stderr="")

    monkeypatch.setattr(transport.subprocess, "run", run)
    assert transport.rclone(command, "fixture") == "completed"


def test_damaged_old_manifest_does_not_block_newer_snapshot(transport, tmp_path, monkeypatch):
    cd = importlib.import_module("deployer")
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "DATA", tmp_path / "data")
    monkeypatch.setattr(cd, "sync", lambda _: None)
    monkeypatch.setattr(cd, "active", lambda: True)
    monkeypatch.setattr(cd, "in_flight", lambda: True)
    monkeypatch.setattr(transport, "prune", lambda *_: None)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "age-recipient").write_text("age1" + "q" * 58)
    queue = tmp_path / "nightly"
    old = queue / ("20261120T060000Z-" + "a" * 32)
    new = queue / ("20261121T060000Z-" + "b" * 32)
    for path in (old, new):
        (path / "backup").mkdir(parents=True)
    (old / "backup/manifest.json").write_text("{}")
    (new / "backup/manifest.json").write_text('{"format":1}')
    publish = transport.publish
    delivered = []

    def deliver(snapshot, spool, kind, stamp):
        if spool == old:
            publish(snapshot, spool, kind, stamp)
        delivered.append(stamp)

    monkeypatch.setattr(transport, "publish", deliver)
    cd.save({}, "idle", live=None)
    with pytest.raises(RuntimeError, match="backup integrity failure"):
        cd.nightly()
    assert delivered == [new.name]
    assert old.is_dir()
    assert not new.exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_bad_remote_never_finalized(transport, tmp_path, monkeypatch, interrupted):
    stamp = transport.new_id()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "age-recipient").write_text("fixture")
    blob = tmp_path / "snapshot.age"
    blob.write_bytes(b"encrypted fixture")
    stored = tmp_path / f"snapshot-{transport.sha256(blob)}.age"
    if not interrupted:
        blob.rename(stored)
    else:
        monkeypatch.setattr(transport, "seal", lambda _, p, __: p.write_bytes(b"encrypted fixture"))
    monkeypatch.setattr(transport, "listing", lambda _: ("folder", []))
    commands = []

    def invoke(command, *args):
        commands.append(command)
        return "wrong  object" if command == "hashsum" else ""

    monkeypatch.setattr(transport, "rclone", invoke)
    with pytest.raises(ValueError, match="remote ciphertext"):
        transport.publish(tmp_path, tmp_path, "nightly", stamp)
    assert commands == ["copyto", "hashsum"]
    assert stored.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux service journal and directory fsync")
def test_nightly_restores_service_before_network_and_retries(transport, monkeypatch, tmp_path):
    cd = importlib.import_module("deployer")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    monkeypatch.setattr(cd, "DATA", data)
    running = True
    starts = []

    def control(action):
        nonlocal running
        running = action == "start"
        starts.append(action)

    monkeypatch.setattr(cd, "active", lambda: running)
    monkeypatch.setattr(cd, "systemctl", control)
    monkeypatch.setattr(cd, "in_flight", lambda: False)
    monkeypatch.setattr(cd, "healthy", lambda _: running)

    def capture(_release, *args):
        assert not running
        folder = args[-1]
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")

    monkeypatch.setattr(cd, "python", capture)
    attempts = []

    def deliver(_snapshot, _folder, _kind, stamp):
        assert running and cd.load()["phase"] == "idle"
        attempts.append(stamp)
        if len(attempts) == 1:
            raise RuntimeError("quota")

    monkeypatch.setattr(transport, "publish", deliver)
    monkeypatch.setattr(transport, "prune", lambda *_: None)
    cd.save({}, "idle", live=None)
    with pytest.raises(RuntimeError, match="quota"):
        cd.nightly()
    assert len(list((tmp_path / "nightly").iterdir())) == 1
    cd.nightly()
    assert attempts[0] == attempts[1]
    assert starts == ["stop", "start"]
    assert not list((tmp_path / "nightly").iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux journal directory fsync")
def test_interrupted_capture_cleanup_is_repeatable(transport, tmp_path, monkeypatch):
    cd = importlib.import_module("deployer")
    monkeypatch.setattr(cd, "ROOT", tmp_path)
    stamp = transport.new_id()
    folder = tmp_path / "nightly" / stamp
    folder.mkdir(parents=True)
    (folder / "incomplete").write_text("partial snapshot")
    state = {}
    cd.save(state, "night-capture", live=None, night_id=stamp, night_restart=False)
    with monkeypatch.context() as patch:

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        patch.setattr(cd, "save", interrupt)
        with pytest.raises(KeyboardInterrupt):
            cd.recover_night(state)
    assert not folder.exists() and cd.load()["phase"] == "night-capture"
    cd.cycle()
    assert cd.load()["phase"] == "idle" and cd.load()["last_night"] is None
