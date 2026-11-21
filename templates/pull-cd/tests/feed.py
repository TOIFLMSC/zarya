"""Serve fixture workflow metadata and artifact bytes through the production GitHub client."""

import fcntl
import json
import sys
from pathlib import Path

project, fixtures = map(Path, sys.argv[1:])
config = json.loads(project.read_text())
sys.path.insert(0, f"/usr/local/lib/pull-cd/{config['name']}")
from controller import Controller  # noqa: E402
from pull_release import GitHub, digest  # noqa: E402


class FixtureFeed(GitHub):
    def json(self, path):
        sha = (fixtures / "tip").read_text().strip()
        if path.startswith("/branches/"):
            return {"commit": {"sha": sha}}
        good = (fixtures / "green").read_text().strip() == sha
        if path.startswith("/actions/workflows/"):
            return {
                "workflow_runs": [
                    {
                        "id": 2,
                        "status": "completed",
                        "conclusion": "success" if good else "failure",
                        "path": f".github/workflows/{self.workflow}",
                        "head_sha": sha,
                        "head_branch": self.branch,
                        "event": "push",
                        "head_repository": {"full_name": self.repository},
                    }
                ]
            }
        assert path.startswith("/actions/runs/2/artifacts?")
        return {
            "artifacts": [
                {
                    "name": f"{self.prefix}-{sha}",
                    "expired": False,
                    "id": 11,
                    "digest": "sha256:" + digest(fixtures / f"{sha}.zip"),
                }
            ]
        }

    def open(self, path):
        assert path == "/actions/artifacts/11/zip"
        sha = (fixtures / "tip").read_text().strip()
        return (fixtures / f"{sha}.zip").open("rb")


controller = Controller(config)
source = config["github"]
with (controller.root / "lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    controller.poll(
        FixtureFeed(
            source["repository"], source["branch"], source["workflow"], source["prefix"], ""
        )
    )
