"""Run real systemd, SQLite, age and rclone on a disposable Ubuntu host."""

import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.request import urlopen

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
from backup import recover, seal  # noqa: E402
from bundle import bundle  # noqa: E402


def run(*args):
    subprocess.run(args, check=True)


def wait_for(predicate, label, seconds=45):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            value = predicate()
            if value:
                return value
        except (FileNotFoundError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {label}")


def release(fixtures, sha, version, marker=None):
    with tempfile.TemporaryDirectory(dir=fixtures) as scratch:
        payload = Path(scratch)
        shutil.copyfile(KIT / "demo/app.py", payload / "app.py")
        (payload / "VERSION").write_text(str(version))
        inputs = payload / "install-inputs"
        inputs.mkdir()
        (inputs / "package.whl").write_bytes(b"installation-only test fixture")
        if marker:
            (payload / marker).touch()
        bundle(payload, sha)
        with zipfile.ZipFile(fixtures / f"{sha}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in payload.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(payload).as_posix())


def replace(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def data_status(database):
    with closing(sqlite3.connect(database)) as db:
        return (
            db.execute("PRAGMA user_version").fetchone()[0],
            [record[0] for record in db.execute("SELECT value FROM records ORDER BY rowid")],
        )


def main():
    if os.environ.get("PULL_CD_DRILL") != "1" or os.geteuid() != 0:
        raise SystemExit("Use root on an Ubuntu test host with PULL_CD_DRILL=1")
    name = "drill-" + uuid.uuid4().hex[:8]
    assert re.fullmatch(r"drill-[0-9a-f]{8}", name)
    user = f"pcd-{name}"
    app = f"pca-{name}"
    stem = f"pull-cd-{name}"
    conf = Path("/etc/pull-cd") / name
    state = Path("/var/lib") / stem
    code = Path("/usr/local/lib/pull-cd") / name
    rules = Path(f"/etc/polkit-1/rules.d/50-{stem}.rules")
    unit_dir = Path("/etc/systemd/system")
    units = [
        f"pull-cd-{kind}@{name}.{extension}"
        for kind, extension in (
            ("app", "service"),
            ("deploy", "service"),
            ("backup", "service"),
            ("deploy", "timer"),
            ("backup", "timer"),
        )
    ]
    overrides = [unit_dir / (unit + ".d") for unit in units[1:]]
    reserved = [conf, state, code, rules, *overrides, *(unit_dir / unit for unit in units)]
    assert all(not p.exists() and not p.is_symlink() for p in reserved)
    for account in (user, app):
        try:
            pwd.getpwnam(account)
        except KeyError:
            pass
        else:
            raise RuntimeError("test user already exists")

    with tempfile.TemporaryDirectory(prefix=stem + "-", dir="/run") as scratch:
        work = Path(scratch)
        work.chmod(0o755)
        offsite = work / "offsite"
        offsite.mkdir()
        fixtures = work / "fixtures"
        fixtures.mkdir()
        identity = work / "identity"
        run("age-keygen", "-o", identity)
        recipient = subprocess.check_output(["age-keygen", "-y", identity], text=True)
        (work / "rclone.conf").write_text(f"[drill]\ntype = alias\nremote = {offsite}\n")
        (work / "rclone.conf").chmod(0o600)
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{port.getsockname()[1]}/healthz"
        profile = json.loads((KIT / "project.example.json").read_text())
        profile.update(
            name=name,
            health_url=url,
            health_timeout_seconds=4,
            poll_seconds=2,
            prune_install_inputs=True,
        )
        profile["backup"]["remote"] = "drill:backups/v1"
        (work / "project.json").write_text(json.dumps(profile))

        def journal():
            return json.loads((state / "state.json").read_text())

        def health():
            with urlopen(url, timeout=2) as response:
                return json.load(response)

        def promote(sha, *, green=True):
            replace(fixtures / "green", sha if green else "")
            replace(fixtures / "tip", sha)

        def landed(sha):
            doc = journal()
            return doc["phase"] == "idle" and doc["live"] == sha and health()["revision"] == sha

        def retained(sha, previous=None):
            if not landed(sha):
                return None
            doc = journal()
            snapshot = state / "predeploy" / doc["last_predeploy"]
            releases = {path.name for path in (state / "releases").iterdir()}
            expected = {sha, previous} if previous else {sha}
            if (
                releases == expected
                and all(
                    (state / "releases" / revision / "app.py").is_file()
                    and not (state / "releases" / revision / "install-inputs").exists()
                    for revision in expected
                )
                and list(snapshot.parent.iterdir()) == [snapshot]
                and (snapshot / "app.sqlite3").is_file()
            ):
                return snapshot
            return None

        a, b, c, d, e, f = [character * 40 for character in "abcdef"]
        database = state / "data/app.sqlite3"
        record = "preserve me " + "x" * 5000
        try:
            conf.mkdir(parents=True, mode=0o700)
            (conf / "github-token").write_text("fixture-no-github-credential\n")
            (conf / "age-recipient").write_text(recipient)
            for key in ("github-token", "age-recipient"):
                (conf / key).chmod(0o600)
            run(
                sys.executable,
                KIT / "install.py",
                work / "project.json",
                KIT / "demo/adapter.py",
                work / "rclone.conf",
            )
            run("install", "-d", "-m", "0750", "-o", user, "-g", user, state / "predeploy")
            contents = work / "checksum-input"
            contents.mkdir()
            (contents / "empty").mkdir()
            (contents / "file").write_text("checkpoint")
            os.link(contents / "file", contents / "second")
            sealed = work / "archive.age"
            seal(contents, sealed, conf / "age-recipient")
            extracted = work / "checksum-output"
            recover(sealed, identity, extracted)
            assert (extracted / "empty").is_dir()
            assert (extracted / "second").read_text() == (extracted / "file").read_text()
            run("chown", f"{user}:{user}", offsite)
            for sha, version, marker in (
                (a, 1, None),
                (b, 2, None),
                (c, 3, "FAIL_APPLY"),
                (d, 4, "FAIL_HEALTH"),
                (e, 3, "INTERRUPT_APPLY"),
                (f, 3, None),
            ):
                release(fixtures, sha, version, marker)
            (work / "feed.py").write_bytes((KIT / "tests/feed.py").read_bytes())
            overrides[0].mkdir()
            (overrides[0] / "fixture.conf").write_text(
                "[Service]\nExecStart=\n"
                f"ExecStart=/usr/bin/python3 -B {work}/feed.py {conf}/project.json {fixtures}\n"
                f"ReadWritePaths={offsite}\n"
            )
            overrides[1].mkdir()
            (overrides[1] / "fixture.conf").write_text(f"[Service]\nReadWritePaths={offsite}\n")
            overrides[2].mkdir()
            (overrides[2] / "fixture.conf").write_text("[Timer]\nOnActiveSec=1s\n")
            overrides[3].mkdir()
            (overrides[3] / "fixture.conf").write_text("[Timer]\nOnActiveSec=1s\nAccuracySec=1s\n")
            run("systemctl", "daemon-reload")
            promote(a, green=False)
            run("systemctl", "start", units[3])
            wait_for(
                lambda: subprocess.check_output(
                    ["systemctl", "show", "--value", "-p", "InvocationID", units[1]], text=True
                ).strip(),
                "automatic first poll",
            )
            assert not (state / "state.json").exists(), "red CI was deployed"
            promote(a)
            wait_for(lambda: landed(a), "timer applying release A")
            wait_for(lambda: retained(a), "cleanup after A")
            run("runuser", "-u", app, "--", "test", "!", "-w", state / "state.json")
            run("runuser", "-u", app, "--", "test", "!", "-r", state / "storage/rclone.conf")
            assert (
                subprocess.run(
                    [
                        "runuser",
                        "-u",
                        app,
                        "--",
                        "systemctl",
                        "--no-ask-password",
                        "stop",
                        units[0],
                    ],
                    check=False,
                    stderr=subprocess.DEVNULL,
                ).returncode
                != 0
            )
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("INSERT INTO records VALUES (?)", (record,))
            promote(b)
            snapshot_b = wait_for(lambda: retained(b, a), "promotion and cleanup after B")
            assert data_status(snapshot_b / "app.sqlite3") == (1, [record])
            assert snapshot_b.parent.stat().st_mode & 0o777 == 0o700
            run("runuser", "-u", app, "--", "test", "!", "-r", snapshot_b / "app.sqlite3")
            assert data_status(database) == (2, [record])
            assert (snapshot_b / "app.sqlite3").is_file()
            print("PASS: timer promoted a green artifact and preserved existing data", flush=True)

            promote(c)
            wait_for(lambda: journal().get("rejected") == c and landed(b), "pre-admission rollback")
            assert data_status(database) == (2, [record])
            assert (snapshot_b / "app.sqlite3").is_file()
            print(
                "PASS: failure after writing candidate restored original database and B", flush=True
            )

            promote(e)
            wait_for(lambda: journal().get("rejected") == e and landed(b), "rollback after SIGKILL")
            assert data_status(database) == (2, [record])
            print("PASS: next timer tick recovered an interrupted switch", flush=True)

            remote_parent = offsite / f"backups/v1/{name}"
            remote_parent.chmod(0o555)
            run("systemctl", "start", units[4])
            pending = state / "pending"
            wait_for(
                lambda: (
                    pending.is_dir()
                    and subprocess.check_output(
                        ["systemctl", "show", "--value", "-p", "Result", units[2]], text=True
                    ).strip()
                    == "exit-code"
                ),
                "upload failure with retained spool",
            )
            intended_id = (pending / "id").read_text()
            assert landed(b)
            remote_parent.chmod(0o755)
            run("systemctl", "start", units[2])
            nightly = remote_parent / "nightly"
            wait_for(lambda: journal().get("last_backup"), "nightly publishing an archive")
            (archive,) = nightly.glob("*.tar.gz.age")
            assert archive.name.startswith(intended_id + "-")
            downloaded = work / archive.name
            run(
                "rclone",
                "--config",
                work / "rclone.conf",
                "copyto",
                f"drill:backups/v1/{name}/nightly/{archive.name}",
                downloaded,
            )
            restored = work / "restore"
            run(sys.executable, KIT / "backup.py", downloaded, identity, restored)
            run(sys.executable, KIT / "demo/adapter.py", "verify", restored)
            assert data_status(restored / "app.sqlite3") == (2, [record])
            assert (restored / ".restore-quarantine").is_file()
            print(
                "PASS: backup timer, real age/rclone download and quarantined SQLite recovery",
                flush=True,
            )

            promote(f)
            snapshot_f = wait_for(lambda: retained(f, b), "rotation after third healthy release")
            assert data_status(snapshot_f / "app.sqlite3") == (2, [record])
            assert not snapshot_b.exists()
            print(
                "PASS: kept two releases and one snapshot, removed installation inputs", flush=True
            )
            promote(d)
            wait_for(lambda: journal()["phase"] == "blocked", "failed health after admission")
            wait_for(
                lambda: (
                    subprocess.run(
                        ["systemctl", "is-active", "--quiet", units[0]], check=False
                    ).returncode
                    != 0
                ),
                "stopped unhealthy release",
            )
            assert data_status(database) == (4, [record, "write after admission"])
            assert (snapshot_f / "app.sqlite3").is_file()
            assert (state / "attempt/snapshot/app.sqlite3").is_file()
            assert (state / "releases" / d / "install-inputs/package.whl").is_file()
            print(
                "PASS: after admission controller preserved new data and blocked bad release",
                flush=True,
            )
        except Exception:
            subprocess.run(
                [
                    "journalctl",
                    "--no-pager",
                    "-n",
                    "120",
                    "-u",
                    units[0],
                    "-u",
                    units[1],
                    "-u",
                    units[2],
                ],
                check=False,
            )
            raise
        finally:
            # All paths are generated from the fresh namespace checked above.
            subprocess.run(["systemctl", "stop", *units[3:]], check=False)
            subprocess.run(["systemctl", "stop", *units[:3]], check=False)
            for path in [*overrides, conf, state, code]:
                if path.is_dir():
                    shutil.rmtree(path)
            for path in [rules, *(unit_dir / unit for unit in units)]:
                path.unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            for account in (app, user):
                try:
                    pwd.getpwnam(account)
                except KeyError:
                    pass
                else:
                    run("userdel", account)


if __name__ == "__main__":
    main()
