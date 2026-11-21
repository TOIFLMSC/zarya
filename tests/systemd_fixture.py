"""One-shot adapter for an ephemeral GitHub-hosted VM, using its local CI bundle."""

import importlib
import sys
from pathlib import Path


def main():
    sys.path.insert(0, "/usr/local/lib/zarya-cd")
    cd = importlib.import_module("deployer")
    sha = Path("/opt/zarya/artifact/REVISION").read_text().strip()

    class Source:
        def tip(self):
            return sha

        def artifact(self, _sha):
            return {"fixture": True}

        def download(self, _artifact, folder):
            import shutil

            return Path(shutil.make_archive(str(folder / "artifact"), "zip", "/opt/zarya/artifact"))

    cd.configured = Source
    with cd.lock(cd.ROOT / "deploy.lock"):
        cd.cycle()
    state = cd.load()
    assert state["phase"] == "idle" and state["live"].startswith(sha)
    # Exercise idempotency of a second poll.
    with cd.lock(cd.ROOT / "deploy.lock"):
        cd.cycle()
    assert cd.load() == state


if __name__ == "__main__":
    main()
