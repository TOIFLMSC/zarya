"""Single-host pull deployment. This controller runs as the service account."""

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from pull_release import configured

ROOT = Path("/var/lib/zarya-cd")
DATA = Path("/var/lib/zarya")
LEGACY = Path("/opt/zarya")
TOOLS = Path(__file__).resolve().parent
UNIT = "zarya.service"
HEALTH = "http://127.0.0.1:8787/healthz"


def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(state, phase, **changes):
    updated = dict(state, phase=phase, **changes)
    with (ROOT / "next.json").open("w", encoding="utf-8") as stream:
        json.dump(updated, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(ROOT / "next.json", ROOT / "state.json")
    sync(ROOT)
    state.update(updated)
    print("deploy", phase, state.get("target"), flush=True)


def load():
    return json.loads((ROOT / "state.json").read_text())


@contextlib.contextmanager
def lock(path):
    import fcntl

    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def runtime(release):
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("CREDENTIALS_DIRECTORY", None)
    if release is None:
        env.update(
            PYTHONPATH=str(LEGACY / "src"),
            ZARYA_WEB_DIR=str(LEGACY / "web/dist"),
            ZARYA_RELEASE_REVISION="legacy",
        )
        return LEGACY / ".venv/bin/python", env
    if not re.fullmatch(r"[0-9a-f]{40}-[a-zA-Z0-9_-]+", release):
        raise ValueError("invalid installed release")
    directory = ROOT / "releases" / release
    env.pop("PYTHONPATH", None)
    env.update(ZARYA_WEB_DIR=str(directory / "bundle/web"), ZARYA_RELEASE_REVISION=release[:40])
    return directory / "venv/bin/python", env


def python(release, *args):
    executable, env = runtime(release)
    subprocess.run([executable, *args], env=env, check=True)


def launch():
    state = load()
    if state["phase"] not in {"idle", "admitted", "returning"}:
        raise SystemExit(75)
    executable, env = runtime(state["live"])
    os.execve(executable, [str(executable), "-u", "-m", "zarya"], env)


def systemctl(command):
    subprocess.run(["systemctl", "--no-ask-password", command, UNIT], check=True)


def active():
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0


def healthy(release):
    revision = (release or "legacy")[:40]
    for _ in range(30):
        try:
            pid = subprocess.check_output(
                ["systemctl", "show", "--value", "-p", "MainPID", UNIT], text=True
            ).strip()
            env = Path(f"/proc/{int(pid)}/environ").read_bytes().split(b"\0")
            if f"ZARYA_RELEASE_REVISION={revision}".encode() in env:
                with urlopen(HEALTH, timeout=2) as reply:
                    if json.load(reply).get("status") == "ok" and active():
                        return True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    return False


def stage(github, sha, artifact):
    directory = Path(tempfile.mkdtemp(prefix=sha + "-", dir=ROOT / "releases"))
    try:
        from pull_release import unpack

        archive = github.download(artifact, directory)
        bundle = directory / "bundle"
        unpack(archive, bundle, sha)
        archive.unlink()
        (wheel,) = bundle.glob("zarya-*.whl")
        if not (bundle / "web/index.html").is_file():
            raise ValueError("missing web build")
        subprocess.run(["/usr/bin/python3.12", "-m", "venv", directory / "venv"], check=True)
        # Never run release code with root privileges. Require hashed binary dependencies.
        pip = [
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-cache-dir",
        ]
        python(
            directory.name,
            *pip,
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            bundle / "requirements.txt",
        )
        python(directory.name, *pip, "--no-index", "--no-deps", wheel)
        python(directory.name, "-m", "pip", "check")
        python(directory.name, "-c", "import zarya.app, zarya.database")
        # Flush installed code/venv and directory entries before making the release selectable.
        subprocess.run(["sync", "-f", str(directory)], check=True)
        return directory.name
    except BaseException:
        shutil.rmtree(directory)
        raise


def verify_snapshot(state):
    python(state["old"], "-m", "zarya.backup", "verify", ROOT / "runs" / state["run"] / "backup")


def rollback(state):
    run = ROOT / "runs" / state["run"]
    if state["phase"] == "switching":
        systemctl("stop")
        with lock(DATA / "instance.lock"):
            verify_snapshot(state)
            python(
                state["old"],
                TOOLS / "sqlite_release.py",
                "copy",
                run / "backup/zarya.sqlite3",
                DATA / "zarya.sqlite3",
            )
    save(state, "returning", live=state["old"], rejected=state["target"])
    systemctl("start")
    if not healthy(state["live"]):
        raise RuntimeError("previous service needs operator attention")
    save(state, "idle")


def finish(state):
    if healthy(state["live"]):
        (ROOT / "runs" / state["run"] / "candidate.sqlite3").unlink(missing_ok=True)
        save(state, "idle", rejected=None)
        return
    try:
        save(state, "blocked")
    finally:
        systemctl("stop")
    raise RuntimeError("health failed after admission; current database retained")


def deploy(state, target, run):
    save(state, "stopping", old=state["live"], target=target[:40], run=run.name)
    try:
        systemctl("stop")
        if active():
            raise RuntimeError("service still active")
        python(state["old"], "-m", "zarya.backup", "backup", DATA, run / "backup")
        subprocess.run(["sync", "-f", str(run)], check=True)
        with lock(DATA / "instance.lock"):
            python(
                target,
                TOOLS / "sqlite_release.py",
                "prepare",
                run / "backup",
                run / "candidate.sqlite3",
                DATA,
            )
            save(state, "switching")
            python(
                target,
                TOOLS / "sqlite_release.py",
                "copy",
                run / "candidate.sqlite3",
                DATA / "zarya.sqlite3",
            )
            save(state, "admitted", live=target)
    except Exception:
        # A failed fsync/rename may already have published an admission record.
        # Re-read durable intent. Never restore a snapshot past the admission boundary.
        state = load()
        if state["phase"] in {"stopping", "switching"}:
            rollback(state)
        raise
    systemctl("start")
    finish(state)


def in_flight():
    uri = (DATA / "zarya.sqlite3").as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        return bool(
            db.execute(
                "SELECT EXISTS(SELECT 1 FROM model_calls WHERE state='started') "
                "OR EXISTS(SELECT 1 FROM outbox WHERE state='sending') "
                "OR EXISTS(SELECT 1 FROM jobs WHERE state='running')"
            ).fetchone()[0]
        )


def cycle():
    state = load()
    phase = state["phase"]
    if phase in {"stopping", "switching", "returning"}:
        rollback(state)
        return
    if phase == "admitted":
        finish(state)
        return
    if phase != "idle":
        if phase == "blocked" and active():
            systemctl("stop")
        raise RuntimeError("deployment held; inspect state and journal")
    if not active():
        return
    # Retain both sides of the last deployment; age out only expendable code/venvs.
    for folder in (ROOT / "releases").iterdir():
        if (
            folder.name not in {state["live"], state.get("old")}
            and re.fullmatch(r"[a-f0-9]{40}-[a-zA-Z0-9_-]+", folder.name)
            and not folder.is_symlink()
            and folder.is_dir()
            and time.time() - folder.stat().st_mtime > 86400
        ):
            shutil.rmtree(folder)
    github = configured()
    sha = github.tip()
    if sha in {(state["live"] or "")[:40], state.get("rejected")}:
        return
    artifact = github.artifact(sha)
    if artifact is None:
        return
    if in_flight():
        return
    if (DATA / "restore-quarantine.json").exists():
        raise RuntimeError("quarantined installation")
    size = sum(p.stat().st_size for p in DATA.rglob("*") if p.is_file() and not p.is_symlink())

    def space_available():
        db_size = (DATA / "zarya.sqlite3").stat().st_size
        return (
            shutil.disk_usage(ROOT).free >= size * 3 + 512 * 1024**2
            and shutil.disk_usage(DATA).free >= 2 * db_size + 64 * 1024**2
        )

    if not space_available():
        raise RuntimeError("insufficient disk space for release and backup")
    target = stage(github, sha, artifact)
    try:
        ready = github.tip() == sha and active() and space_available() and not in_flight()
    except Exception:
        shutil.rmtree(ROOT / "releases" / target)
        raise
    if not ready:
        shutil.rmtree(ROOT / "releases" / target)
        return
    run = Path(tempfile.mkdtemp(prefix=sha + "-", dir=ROOT / "runs"))
    sync(run.parent)
    deploy(state, target, run)


if __name__ == "__main__":
    if sys.argv[1:] == ["launch"]:
        launch()
    elif sys.argv[1:] == ["poll"]:
        with lock(ROOT / "deploy.lock"):
            cycle()
    elif sys.argv[1:] == ["resume-current"]:
        with lock(ROOT / "deploy.lock"):
            held = load()
            if held["phase"] != "blocked":
                raise SystemExit("only a blocked, already-admitted release can be resumed")
            save(held, "admitted")
            systemctl("start")
            finish(held)
    else:
        raise SystemExit("expected launch, poll or resume-current")
