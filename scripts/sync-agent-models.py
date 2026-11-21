"""Apply the project's model matrix to the existing local Codex profiles."""

import argparse
import re
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
        # Replace only the existing assignment, preserving indentation and comments.
        pattern = rf'(?m)^([ \t]*{key}[ \t]*=[ \t]*)("[^"\r\n]*")'
        match = re.search(pattern, text)
        if match is None:
            raise ValueError(f"{path}: cannot locate {key}")
        text = text[: match.start(2)] + f'"{value}"' + text[match.end(2) :]
        expected[key] = value
    if tomllib.loads(text) != expected:
        raise ValueError(f"{path}: unexpected change outside model settings")
    return original, text


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
            path.write_text(new, encoding="utf-8", newline="")
    print(f"Profiles checked: {len(targets)}. Differences: {len(changes)}.")
    return int(args.check and bool(changes))


if __name__ == "__main__":
    raise SystemExit(main())
