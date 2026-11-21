import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path

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
    monkeypatch.setattr(transport, "listing", lambda k: (k, foreign + names))
    monkeypatch.setattr(transport, "rclone", lambda *args: calls.append(args))
    now = datetime(2026, 11, 21, tzinfo=UTC)
    transport.prune("predeploy", now)
    assert calls == [("deletefile", "predeploy/" + n, "--yandex-hard-delete") for n in names[:-10]]
    calls.clear()
    transport.prune("nightly", now)
    assert calls == [("deletefile", "nightly/" + n, "--yandex-hard-delete") for n in names[:-1]]


def test_bad_remote_never_finalized(transport, tmp_path, monkeypatch):
    stamp = transport.new_id()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "age-recipient").write_text("fixture")
    blob = tmp_path / "snapshot.age"
    blob.write_bytes(b"encrypted fixture")
    (tmp_path / "ciphertext.sha256").write_text(transport.sha256(blob))
    monkeypatch.setattr(transport, "listing", lambda _: ("folder", []))
    commands = []

    def invoke(command, *args):
        commands.append(command)
        return "wrong  object" if command == "hashsum" else ""

    monkeypatch.setattr(transport, "rclone", invoke)
    with pytest.raises(ValueError, match="remote ciphertext"):
        transport.publish(tmp_path, tmp_path, "nightly", stamp)
    assert commands == ["copyto", "hashsum"]
    assert blob.exists()


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
