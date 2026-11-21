"""Pull and admit a verified release for one systemd service."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import backup
from pull_release import GitHub, unpack


class Controller:
    def __init__(self, config):
        self.config = config
        self.name = config["name"]
        self.unit = f"pull-cd-app@{self.name}.service"
        self.root = Path(f"/var/lib/pull-cd-{self.name}")
        self.data = self.root / "data"
        self.releases = self.root / "releases"
        self.run = self.root / "attempt"
        self.pending = self.root / "pending"
        self.adapter = Path(f"/etc/pull-cd/{self.name}/adapter.py")
        self.state_file = self.root / "state.json"

    def state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return dict(phase="idle", live=None, rejected=None, last_backup=None)

    def save(self, state, phase, **updates):
        updated = dict(state, phase=phase, **updates)
        with (self.root / "state.next").open("w", encoding="utf-8") as stream:
            json.dump(updated, stream)
            stream.flush()
            os.fchmod(stream.fileno(), 0o640)
            os.fsync(stream.fileno())
        os.replace(self.root / "state.next", self.state_file)
        fd = os.open(self.root, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        state.update(updated)
        print("phase", phase, "live", state["live"], "target", state.get("target"), flush=True)

    def release(self, sha):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("invalid release SHA")
        return self.releases / sha

    def environment(self, revision=None):
        env = dict(
            os.environ, PULL_CD_DATA=str(self.data), PULL_CD_HEALTH_URL=self.config["health_url"]
        )
        env.pop("CREDENTIALS_DIRECTORY", None)
        if revision:
            env["PULL_CD_REVISION"] = revision
        return env

    def invoke(self, operation, *paths):
        subprocess.run(
            [sys.executable, self.adapter, operation, *paths],
            check=True,
            env=self.environment(),
        )

    def service(self, operation, *, check=True):
        return (
            subprocess.run(
                ["systemctl", "--no-ask-password", operation, self.unit],
                check=check,
                stdout=subprocess.DEVNULL,
            ).returncode
            == 0
        )

    def start(self, state):
        if (self.data / ".restore-quarantine").exists():
            raise RuntimeError("quarantined data")
        if state["phase"] not in {"idle", "returning", "admitted", "night-returning"}:
            raise SystemExit(75)
        if not state["live"]:
            raise SystemExit(75)
        path = self.release(state["live"])
        os.chdir(path)
        os.execve(
            sys.executable,
            [sys.executable, str(self.adapter), "start", str(path)],
            self.environment(state["live"]),
        )

    def healthy(self, sha):
        if sha is None:
            return True
        end = time.monotonic() + self.config["health_timeout_seconds"]
        while time.monotonic() < end:
            try:
                if self.service("is-active", check=False):
                    with urlopen(self.config["health_url"], timeout=2) as response:
                        payload = json.loads(response.read(4097))
                        if (
                            isinstance(payload, dict)
                            and payload.get("status") == "ok"
                            and payload.get("revision") == sha
                        ):
                            return True
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(1)
        return False

    def snapshot(self, dest):
        dest.mkdir()
        self.invoke("snapshot", dest)
        self.invoke("verify", dest)

    def publish(self, source, spool, kind, stamp):
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        settings = self.config["backup"]
        return backup.publish(
            source,
            spool,
            f"{settings['remote']}/{self.name}/{kind}",
            stamp,
            credentials / "age-recipient",
            Path(settings["rclone_config"]),
            settings[f"keep_{kind}"],
        )

    def rollback(self, state):
        phase = state["phase"]
        if phase == "switching":
            self.service("stop")
            self.invoke("verify", self.run / "snapshot")
            self.invoke("apply", self.run / "snapshot")
            subprocess.run(["sync", "-f", self.data], check=True)
        if phase != "returning":
            rejected = None if phase == "offsite" else state["target"]
            retry_after = (
                time.time() + self.config["backup_retry_seconds"] if phase == "offsite" else 0
            )
            self.save(state, "returning", rejected=rejected, retry_after=retry_after)
        if state["live"]:
            self.service("start")
        if not self.healthy(state["live"]):
            raise RuntimeError("previous release requires attention")
        self.save(state, "idle")
        self.clean_attempt()

    def finish(self, state):
        if self.healthy(state["live"]):
            self.save(state, "idle", rejected=None, retry_after=0)
            self.clean_attempt()
            return
        self.save(state, "blocked")
        self.service("stop")
        raise RuntimeError("readiness failed after admission; current data kept")

    def clean_attempt(self):
        if self.run.is_dir():
            shutil.rmtree(self.run)

    def deploy(self, state, sha):
        self.clean_attempt()
        self.run.mkdir(mode=0o700)
        self.save(state, "stopping", target=sha)
        try:
            self.service("stop")
            original = self.run / "snapshot"
            self.snapshot(original)
            self.save(state, "offsite")
            self.publish(original, self.run, "predeploy", backup.new_id())
            self.save(state, "preparing")
            candidate = self.run / "candidate"
            shutil.copytree(original, candidate)
            self.invoke("migrate", self.release(sha), candidate)
            self.invoke("verify", candidate)
            subprocess.run(["sync", "-f", self.root], check=True)
            self.save(state, "switching")
            self.invoke("apply", candidate)
            subprocess.run(["sync", "-f", self.data], check=True)
            self.save(state, "admitted", live=sha, previous=state["live"])
        except Exception:
            actual = self.state()
            if actual["phase"] in {"stopping", "offsite", "preparing", "switching", "returning"}:
                self.rollback(actual)
            raise
        self.service("start", check=False)
        self.finish(state)

    def stage(self, feed, sha, artifact):
        target = self.release(sha)
        # A prior attempt may have already staged this immutable SHA.
        if target.is_dir():
            return
        with tempfile.TemporaryDirectory(prefix="stage-", dir=self.releases) as temp:
            scratch = Path(temp)
            archive = feed.download(artifact, scratch)
            release = scratch / "release"
            unpack(archive, release, sha)
            self.invoke("stage", release)
            subprocess.run(["chmod", "-R", "g-w,o-rwx", release], check=True)
            subprocess.run(["sync", "-f", scratch], check=True)
            release.rename(target)
            subprocess.run(["sync", "-f", self.releases], check=True)

    def poll(self, feed=None):
        state = self.state()
        phase = state["phase"]
        if phase in {"stopping", "offsite", "preparing", "switching", "returning"}:
            self.rollback(state)
            return
        if phase == "admitted":
            # A missed start after the admission journal must be recoverable.
            self.service("start", check=False)
            self.finish(state)
            return
        if phase in {"night-stopping", "night-returning"}:
            self.return_after_backup(state)
            return
        if phase == "blocked":
            self.service("stop")
            raise RuntimeError("deployment blocked")
        if phase != "idle":
            raise RuntimeError("unknown journal phase")
        if (self.data / ".restore-quarantine").exists():
            raise RuntimeError("quarantined data")
        for path in self.releases.iterdir():
            if (
                path.name not in {state["live"], state.get("previous")}
                and re.fullmatch(r"[0-9a-f]{40}", path.name)
                and path.is_dir()
                and not path.is_symlink()
            ):
                shutil.rmtree(path)
        if state["live"] and not self.service("is-active", check=False):
            return
        if time.time() < state.get("retry_after", 0):
            return
        if feed is None:
            src = self.config["github"]
            token_file = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token"
            feed = GitHub(
                src["repository"],
                src["branch"],
                src["workflow"],
                src["prefix"],
                token_file.read_text().strip(),
            )
        sha = feed.tip()
        if sha in {state["live"], state["rejected"]}:
            return
        artifact = feed.artifact(sha)
        if artifact is None:
            return
        self.stage(feed, sha, artifact)
        if feed.tip() != sha or (state["live"] and not self.service("is-active", check=False)):
            return
        if state["live"] and not self.can_stop():
            return
        self.deploy(state, sha)

    def can_stop(self):
        result = subprocess.run(
            [sys.executable, self.adapter, "can-stop"], env=self.environment(), check=False
        )
        if result.returncode == 75:
            return False
        result.check_returncode()
        return True

    def return_after_backup(self, state):
        self.save(state, "night-returning")
        if state["restart_after_backup"]:
            self.service("start")
            if not self.healthy(state["live"]):
                raise RuntimeError("service requires attention after snapshot")
        self.save(state, "idle")

    def nightly(self):
        state = self.state()
        if state["phase"] in {"night-stopping", "night-returning"}:
            self.return_after_backup(state)
        if state["phase"] != "idle" or state["live"] is None:
            return
        if (self.data / ".restore-quarantine").exists():
            raise RuntimeError("quarantined data")
        today = datetime.now(UTC).date().isoformat()
        if not self.pending.is_dir() and state["last_backup"] != today:
            running = self.service("is-active", check=False)
            if running and not self.can_stop():
                return
            self.save(state, "night-stopping", restart_after_backup=running)
            try:
                self.service("stop")
                with tempfile.TemporaryDirectory(prefix="night-", dir=self.root) as temp:
                    pending = Path(temp) / "pending"
                    pending.mkdir(mode=0o700)
                    self.snapshot(pending / "snapshot")
                    (pending / "id").write_text(backup.new_id())
                    (pending / "date").write_text(today)
                    subprocess.run(["sync", "-f", pending], check=True)
                    pending.rename(self.pending)
                    subprocess.run(["sync", "-f", self.root], check=True)
            finally:
                self.return_after_backup(state)
        if self.pending.is_dir():
            self.publish(
                self.pending / "snapshot",
                self.pending,
                "nightly",
                (self.pending / "id").read_text(),
            )
            self.save(state, "idle", last_backup=(self.pending / "date").read_text())
            # Rename the fully delivered spool before removing children.
            done = self.root / "delivered"
            if done.exists():
                shutil.rmtree(done)
            self.pending.rename(done)
            subprocess.run(["sync", "-f", self.root], check=True)
            shutil.rmtree(done)


def main():
    filename, operation = sys.argv[1:]
    config = json.loads(Path(filename).read_text())
    controller = Controller(config)
    if operation == "start":
        controller.start(controller.state())
    with (controller.root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if operation == "poll":
            controller.poll()
        elif operation == "nightly":
            controller.nightly()
        elif operation == "resume-current":
            state = controller.state()
            if state["phase"] != "blocked" or not state["live"]:
                raise SystemExit("only a blocked admitted release can be resumed")
            controller.save(state, "admitted")
            controller.service("start", check=False)
            controller.finish(state)
        else:
            raise SystemExit("expected poll, nightly, start or resume-current")


if __name__ == "__main__":
    main()
