"""Seal the exact directory that GitHub Actions uploads as the release artifact."""

import argparse
import re
from pathlib import Path

from pull_release import digest


def bundle(payload, sha):
    if not re.fullmatch("[a-f0-9]{40}", sha):
        raise ValueError("expected a Git revision")
    paths = sorted(payload.rglob("*"))
    for path in paths:
        parts = path.relative_to(payload).parts
        if (
            any(p.startswith(".") or not re.fullmatch("[A-Za-z0-9_@.+-]+", p) for p in parts)
            or path.is_symlink()
            or path.is_junction()
            or not (path.is_dir() or path.is_file())
        ):
            raise ValueError(f"unsupported release path: {path.relative_to(payload)}")
    files = [p for p in paths if p.is_file()]
    if not files:
        raise ValueError("release payload is empty")
    rev = payload / "REVISION"
    with rev.open("x", encoding="utf-8") as output:
        output.write(sha + "\n")
    manifest = payload / "SHA256SUMS"
    with manifest.open("x", encoding="utf-8") as output:
        for path in [*files, rev]:
            output.write(f"{digest(path)}  ./{path.relative_to(payload).as_posix()}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    bundle(args.payload, args.revision)
