"""Apply the project's model matrix to the existing local Codex profiles."""

import argparse
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def updated(path: Path, settings: dict[str, str]) -> tuple[str, str]:
    with path.open(encoding="utf-8", newline="") as stream:
        original = stream.read()
    expected = tomllib.loads(original)
    text = original
    for key in ("model", "model_reasoning_effort"):
        value = settings[key]
        if expected[key] == value:
            continue
        expected[key] = value
        pattern = rf"""(?m)^[ \t]*{key}[ \t]*=[ \t]*(["'])([^"'\r\n]*)\1[ \t]*(?:\#.*)?\r?$"""
        for match in re.finditer(pattern, text):
            candidate = text[: match.start(2)] + value + text[match.end(2) :]
            # A similar assignment may occur inside the multiline role instructions.
            if tomllib.loads(candidate) == expected:
                text = candidate
                break
        else:
            raise ValueError(f"{path}: cannot locate top-level {key}")
    return original, text


def replace_profile(path: Path, text: str) -> None:
    fd, filename = tempfile.mkstemp(dir=path.parent)
    staged = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, staged)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args()
    with (ROOT / "config/codex-agent-models.toml").open("rb") as stream:
        matrix = tomllib.load(stream)
    codex = ROOT / ".codex"
    targets = {codex / "config.toml": matrix["primary"]}
    targets.update({codex / "agents" / f"{role}.toml": values for role, values in matrix.items()})
    changes = {}
    try:
        for path, values in targets.items():
            original, new = updated(path, values)
            if original != new:
                changes[path] = new
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"{error}\n")
    for path, new in changes.items():
        print(path.relative_to(ROOT))
        if not args.check:
            replace_profile(path, new)
    print(f"Profiles checked: {len(targets)}. Differences: {len(changes)}.")
    return int(args.check and bool(changes))


if __name__ == "__main__":
    raise SystemExit(main())
