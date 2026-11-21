"""Plan, apply, and verify a project-scoped development agent team."""

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

KIT = Path(__file__).resolve().parent
SETTINGS = ".codex/dev-team.toml"
STATE = ".codex/dev-team-state.json"
CONFIG = ".codex/config.toml"
AGENTS = "AGENTS.md"
BEGIN = b"<!-- dev-agent-team:begin -->"
END = b"<!-- dev-agent-team:end -->"
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
MODEL_FIELDS = ("model", "model_reasoning_effort")


def checksum(content):
    return (
        hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest() if content is not None else None
    )


def line_endings(content, existing):
    if content is not None and existing and b"\r\n" in existing:
        return content.replace(b"\n", b"\r\n")
    return content


def read(root, name):
    path = root / name
    if path.resolve() != path or path.is_symlink() or path.is_junction():
        raise ValueError(f"unsafe project path: {name}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"expected a file: {name}")
    return path.read_bytes()


def decode(raw):
    return raw.decode("utf-8-sig")


def string(value):
    return json.dumps(value, ensure_ascii=False)


def preferences(defaults, raw, profile):
    settings = tomllib.loads(decode(raw)) if raw is not None else {"profile": profile}
    if set(settings) - {"profile", "parallelism", "roles"}:
        raise ValueError("unknown settings key")
    selected = settings.get("profile", "backend")
    if selected not in {"backend", "ui"}:
        raise ValueError("profile must be backend or ui")
    parallelism = settings.get("parallelism", defaults["parallelism"])
    if type(parallelism) is not int or not 1 <= parallelism <= 6:
        raise ValueError("parallelism must be between 1 and 6")
    overrides = settings.get("roles", {})
    if not isinstance(overrides, dict) or set(overrides) - defaults["roles"].keys():
        raise ValueError("unknown role in settings")
    roles = {}
    for role, original in defaults["roles"].items():
        values = overrides.get(role, {})
        if not isinstance(values, dict) or set(values) - {*MODEL_FIELDS, "callsign", "enabled"}:
            raise ValueError(f"unknown setting for {role}")
        info = dict(original, enabled=role != "designer" or selected == "ui")
        info.update(values)
        if type(info["enabled"]) is not bool or (role == "primary" and not info["enabled"]):
            raise ValueError(f"invalid enabled flag for {role}")
        if not isinstance(info["model"], str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", info["model"]
        ):
            raise ValueError(f"invalid model for {role}")
        effort = info["model_reasoning_effort"]
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise ValueError(f"invalid reasoning effort for {role}")
        callsign = info["callsign"]
        if not isinstance(callsign, str) or not re.fullmatch(r"[\w -]{1,32}", callsign):
            raise ValueError(f"invalid callsign for {role}")
        roles[role] = info
    return parallelism, roles


def rendered_agent(role, info):
    contract = (
        "Investigate and report. Leave repository edits and integration to the primary agent. "
        "Do not mutate project or external state or delegate further. Respect the tools and "
        "permissions available in this session.\n\n" + info["instructions"]
    )
    spec = {
        "name": f"dev_team_{role}",
        "description": f"{info['callsign']} — {info['trigger']}",
        "model": info["model"],
        "model_reasoning_effort": info["model_reasoning_effort"],
        "sandbox_mode": "read-only",
        "developer_instructions": contract,
    }
    content = "".join(f"{key} = {string(value)}\n" for key, value in spec.items())
    assert tomllib.loads(content) == spec
    return content.encode()


def rendered_rules(roles, parallelism, version):
    primary = roles["primary"]
    lines = [
        BEGIN.decode(),
        f"## Development agent team ({version})",
        "",
        f"The main chat acts as {primary['callsign']}. It owns implementation, integration, "
        "and the final verification. Follow this project's instructions and actual checks.",
        "",
        "Handle simple lookups and small edits directly. For substantial changes, assign "
        "bounded specialist work where it adds value. The primary agent writes code; "
        "specialists investigate or review. Give each a question, exact scope, constraints, "
        "and observable evidence to return.",
        "",
        f"Use up to {parallelism} concurrent specialists within the actual runtime limit.",
        "Prefer the project agent when selectable. Otherwise pass the equivalent read-only "
        "contract when delegation is supported. Report if independent review is unavailable.",
        "",
    ]
    for role, info in roles.items():
        if role != "primary" and info["enabled"]:
            lines.append(f"- **{info['callsign']}** (`dev_team_{role}`). {info['trigger']}")
    if roles["reviewer"]["enabled"]:
        lines += [
            "",
            "Give the reviewer a fresh context, exact diff, expected behavior and checks. "
            "Ask for independent findings before sharing the author's conclusions.",
        ]
    return "\n".join([*lines, END.decode()]).encode()


def replace_rules(document, rendered, previous):
    body = document or b""
    ending = b"\r\n" if b"\r\n" in body else b"\n"
    desired = rendered.replace(b"\n", ending)
    if BEGIN not in body and END not in body:
        if previous is not None:
            return body, None, "previously installed markers missing"
        prefix = (
            ending * 2 if body and not body.endswith((b"\n", b"\r")) else ending if body else b""
        )
        return body + prefix + desired + ending, desired, None
    pattern = rb"(?m)^" + re.escape(BEGIN) + rb"\r?$[\s\S]*?^" + re.escape(END) + rb"(?=\r?$)"
    match = re.search(pattern, body)
    if not match or body.count(BEGIN) != 1 or body.count(END) != 1:
        return body, None, "malformed managed markers"
    current = match[0]
    if current != desired and checksum(current) != previous:
        return body, None, "locally edited or untracked managed block"
    return body[: match.start()] + desired + body[match.end() :], desired, None


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            staged.chmod(stat.S_IMODE(path.stat().st_mode))
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def plan(root, profile):
    version = (KIT / "VERSION").read_text().strip()
    defaults = tomllib.loads((KIT / "team.toml").read_text(encoding="utf-8"))
    settings = read(root, SETTINGS)
    journal = read(root, STATE)
    previous = json.loads(decode(journal)) if journal else {"files": {}, "rules": None}
    parallelism, roles = preferences(defaults, settings, profile)
    targets = {
        f".codex/agents/dev-team-{role}.toml": rendered_agent(role, info)
        if info["enabled"]
        else None
        for role, info in roles.items()
        if role != "primary"
    }
    primary = roles["primary"]
    config = "".join(f"{key} = {string(primary[key])}\n" for key in MODEL_FIELDS)
    targets[CONFIG] = (
        config + f"\n[agents]\nmax_concurrent_threads_per_session = {parallelism}\n"
    ).encode()
    current_agents = read(root, AGENTS)
    decode(current_agents or b"")
    new_agents, block, conflict = replace_rules(
        current_agents, rendered_rules(roles, parallelism, version), previous["rules"]
    )
    changes = {}
    conflicts = []
    known = {}
    identifiers = {
        f"dev_team_{role}": f".codex/agents/dev-team-{role}.toml"
        for role, info in roles.items()
        if role != "primary" and info["enabled"]
    }
    for name, desired in targets.items():
        current = read(root, name)
        desired = line_endings(desired, current)
        old_hash = previous["files"].get(name)
        if current is not None and checksum(current) != old_hash and current != desired:
            if name != CONFIG:
                conflicts.append((name, "local file preserved"))
            continue
        if current != desired:
            changes[name] = (current, desired)
        if desired is not None:
            known[name] = checksum(desired)
    for path in (root / ".codex/agents").glob("*.toml"):
        name = path.relative_to(root).as_posix()
        if name in targets:
            continue
        other = tomllib.loads(decode(read(root, name))).get("name")
        if other in identifiers:
            conflicts.append((name, f"agent name {other} conflicts with {identifiers[other]}"))
    if conflict:
        conflicts.append((AGENTS, conflict))
    elif new_agents != current_agents:
        changes[AGENTS] = (current_agents, new_agents)
    if settings is None:
        changes[SETTINGS] = (
            None,
            f'# Project overrides stay here. See the kit README.\nprofile = "{profile}"\n'.encode(),
        )
    next_state = {
        "version": version,
        "files": known,
        "rules": previous["rules"] if conflict else checksum(block),
    }
    metadata = line_endings(
        (json.dumps(next_state, indent=2, sort_keys=True) + "\n").encode(), journal
    )
    if journal != metadata:
        changes[STATE] = (journal, metadata)
    return changes, conflicts


def main():
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--profile",
        choices=("backend", "ui"),
        default="backend",
        help="initial profile, used only when there are no project preferences",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        root = args.project.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project must be an existing directory")
        changes, conflicts = plan(root, args.profile)
        for name, reason in conflicts:
            print(f"PRESERVE {name}: {reason}")
        if conflicts and args.apply:
            print("No files changed. Resolve the reported conflicts before applying.")
            return 1
        for name, (old, new) in changes.items():
            if args.apply:
                if read(root, name) != old:
                    raise ValueError(f"{name} changed during installation; retry")
                if new is None:
                    (root / name).unlink()
                else:
                    atomic_write(root / name, new)
                print(f"APPLIED {name}")
            elif not args.check:
                print(
                    "".join(
                        difflib.unified_diff(
                            decode(old or b"").splitlines(keepends=True),
                            decode(new or b"").splitlines(keepends=True),
                            fromfile=name,
                            tofile=name,
                            n=0,
                        )
                    ),
                    end="",
                )
            else:
                print(f"DRIFT {name}")
        print(f"Changes: {len(changes)}. Local conflicts: {len(conflicts)}.")
        return int(bool(conflicts or (args.check and changes)))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"dev-agent-team: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
