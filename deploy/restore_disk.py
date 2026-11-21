"""Decrypt a downloaded age archive and create a verified, quarantined restore."""

import argparse
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from disk_backup import member

from zarya.backup import restore


def recover(archive, identity, target):
    if target.exists():
        raise ValueError("restore requires a new destination")
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        folder = Path(temp)
        # Finish authenticated decryption before interpreting any part of the archive.
        decrypted = folder / "decrypted.tar.gz"
        subprocess.run(["age", "-d", "-i", identity, "-o", decrypted, archive], check=True)
        snapshot = folder / "snapshot"
        snapshot.mkdir()
        seen = set()
        with tarfile.open(decrypted, "r:gz") as tar:
            for entry in tar:
                if not entry.isreg() or not member(entry.name) or entry.name.casefold() in seen:
                    raise ValueError("unexpected entry in encrypted snapshot")
                seen.add(entry.name.casefold())
                if entry.size > shutil.disk_usage(folder).free:
                    raise ValueError("insufficient space to restore snapshot")
                dest = snapshot / entry.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(entry) as source, dest.open("xb") as output:
                    shutil.copyfileobj(source, output)
        restore(snapshot, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("archive", "identity", "target"):
        parser.add_argument(argument, type=Path)
    args = parser.parse_args()
    recover(args.archive, args.identity, args.target)
