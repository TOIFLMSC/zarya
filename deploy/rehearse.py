"""Exercise promotion and pre-admission rollback in a quarantined temporary directory."""

import argparse
import hashlib
import json
import tempfile
from contextlib import closing
from pathlib import Path

from sqlite_release import copy_database, prepare

from zarya.backup import MARKER, assets, connect, fingerprint, relative


def logical_state(path):
    digest = hashlib.sha256()
    with closing(connect(path)) as db:
        versions = db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        for statement in db.iterdump():
            digest.update(statement.encode() + b"\0")
    return versions, digest.digest()


def rehearse(snapshot, sandbox):
    snapshot = snapshot.resolve(strict=True)
    sandbox = sandbox.resolve(strict=True)
    if sandbox.is_relative_to(snapshot):
        raise ValueError("rehearsal needs a separate temporary directory")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    with closing(connect(snapshot / "zarya.sqlite3")) as db:
        if assets(db) != set(manifest["files"]) - {"prompts.json"}:
            raise ValueError("snapshot manifest mismatch")
    for name, expected in manifest["files"].items():
        if fingerprint(snapshot / relative(name)) != expected:
            raise ValueError("snapshot checksum mismatch")
    with tempfile.TemporaryDirectory(dir=sandbox) as folder:
        work = Path(folder)
        (work / MARKER).write_text("{}")
        live = work / "zarya.sqlite3"
        candidate = work / "candidate.sqlite3"
        source = snapshot / "zarya.sqlite3"
        copy_database(source, live, existing=False)
        before = logical_state(live)
        prepare(snapshot, candidate, snapshot)
        copy_database(candidate, live, existing=True)
        after = logical_state(live)
        if after != logical_state(candidate):
            raise ValueError("promotion did not preserve candidate")
        copy_database(source, live, existing=True)
        if logical_state(live) != before:
            raise ValueError("rollback did not restore the original logical database")
        print(
            f"Schema {before[0]} -> {after[0]} -> {before[0]}, "
            f"{len(manifest['files'])} files verified; logical rollback matched"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("temp_parent", type=Path)
    options = parser.parse_args()
    rehearse(options.snapshot, options.temp_parent)
