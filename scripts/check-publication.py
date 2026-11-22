"""Audit the Git index without printing secret values. Run before committing."""

import ast
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path, PurePosixPath

ROOT_FILES = {
    ".env.example",
    ".gitignore",
    ".python-version",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "start.ps1",
    ".github/workflows/ci.yml",
    ".github/workflows/pull-cd-template.yml",
    "config/codex-agent-models.toml",
    "scripts/sync-agent-models.py",
    "deploy/install.sh",
    "deploy/check-systemd.sh",
    "deploy/80-pull-cd.conf",
    "deploy/50-zarya-cd.rules",
    "deploy/zarya-cd.service",
    "deploy/zarya-cd.timer",
    "deploy/deployer.py",
    "deploy/pull_release.py",
    "deploy/sqlite_release.py",
    "deploy/rehearse.py",
    "deploy/disk_backup.py",
    "deploy/restore_disk.py",
    "deploy/zarya-backup.service",
    "deploy/zarya-backup.timer",
}
DOCS = {
    "setup.md",
    "configuration.md",
    "architecture.md",
    "privacy.md",
    "operations.md",
    "pull-deployment.md",
    "disk-backup.md",
    "codegraph.md",
    "memory-retrieval.md",
}
PULL_CD_FILES = {
    "VERSION",
    "README.md",
    "backup.py",
    "bundle.py",
    "controller.py",
    "demo/adapter.py",
    "demo/app.py",
    "install.py",
    "install-skill.py",
    "project.example.json",
    "pull_release.py",
    "scaffold.py",
    "skill/pull-cd-onboarding/SKILL.md",
    "tests/feed.py",
    "tests/systemd_drill.py",
    "workflow.yml",
}
DEV_AGENT_TEAM_FILES = {
    "README.md",
    "VERSION",
    "install-skill.py",
    "install.py",
    "skill/dev-team-onboarding/SKILL.md",
    "team.toml",
}
WEB_FILES = {
    "index.html",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "vite.config.ts",
}
ASSET = "web/src/assets/zarya-avatar-v1.png"
SECRETS = (
    re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{50,}"),
    re.compile(r"y[0-9]_[A-Za-z0-9_-]{35,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def allowed(path: str) -> bool:
    p = PurePosixPath(path)
    if path in ROOT_FILES or path == ASSET:
        return True
    if path.startswith("templates/pull-cd/"):
        return path.removeprefix("templates/pull-cd/") in PULL_CD_FILES
    if path.startswith("templates/dev-agent-team/"):
        return path.removeprefix("templates/dev-agent-team/") in DEV_AGENT_TEAM_FILES
    if any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in p.parts):
        return False
    if p.parent.as_posix() == "docs":
        return p.name in DOCS
    if p.parent.as_posix() == "scripts":
        return p.name in {"setup-media.ps1", "check-publication.py", "check-upgrade.py"}
    if p.parts[:2] == ("src", "zarya"):
        return p.suffix in {".py", ".sql"} or path in {
            "src/zarya/prompts.example.json",
            "src/zarya/e5_license.txt",
        }
    if p.parts[0] == "tests":
        return p.suffix in {".py", ".cjs"}
    if p.parent.as_posix() == "web":
        return p.name in WEB_FILES
    return p.parts[:2] == ("web", "src") and p.suffix in {".ts", ".tsx", ".css"}


def decoded(text: str, path: str) -> str:
    values = [text]
    if path.endswith(".py"):
        try:
            values.extend(
                n.value
                for n in ast.walk(ast.parse(text))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
        except SyntaxError:
            pass
    if path.endswith(".json"):

        def walk(value):
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        try:
            walk(json.loads(text))
        except ValueError:
            pass
    return "\n".join(values)


def normalized(text: str) -> str:
    return " ".join(text.split())


def private_chunks(raw: str, neutral: str) -> set[str]:
    doc = json.loads(raw)
    public = normalized(decoded(neutral, "example.json"))
    chunks = set()
    for fragment in doc["fragments"].values():
        text = normalized(fragment)
        for pos in range(0, max(0, len(text) - 159), 80):
            chunk = text[pos : pos + 160]
            if chunk not in public:
                chunks.add(chunk)
    return chunks


def findings(path: str, raw: bytes, chunks: set[str]) -> list[str]:
    if not allowed(path):
        return ["path is outside the publication allowlist"]
    if path == ASSET:
        return []  # This deliberately selected portrait is reviewed separately.
    try:
        text = decoded(raw.decode("utf-8-sig"), path)
    except UnicodeError:
        return ["unexpected binary file"]
    problems = []
    if any(pattern.search(text) for pattern in SECRETS):
        problems.append("possible credential/private key")
    if any(chunk in normalized(text) for chunk in chunks):
        problems.append("local prompt fragment")
    return problems


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def local_packages(data: Path, prompt_file: Path) -> list[str]:
    """Read private snapshots without creating or modifying a working database."""
    packages = []
    if prompt_file.exists():
        packages.append(prompt_file.read_text(encoding="utf-8-sig"))
    db = data / "zarya.sqlite3"
    if db.is_file():
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "configuration_releases" in tables:
                packages.extend(
                    r[0] for r in c.execute("SELECT prompts_json FROM configuration_releases")
                )
            if "configuration_drafts" in tables:
                packages.extend(
                    json.dumps(json.loads(r[0])["prompts"], ensure_ascii=False)
                    for r in c.execute("SELECT document FROM configuration_drafts")
                )
    return packages


def main() -> int:
    entries = git("ls-files", "--stage", "-z").split(b"\0")
    entries = [entry for entry in entries if entry]
    if not entries:
        print("FAIL: Git index is empty; nothing has been audited")
        return 1
    chunks = set()
    data = Path(os.environ.get("ZARYA_DATA_DIR", "data"))
    local = Path(os.environ.get("ZARYA_PROMPTS_FILE", str(data / "prompts.json")))
    try:
        packages = local_packages(data, local)
        neutral = git("show", ":src/zarya/prompts.example.json").decode("utf-8")
        for package in packages:
            chunks.update(private_chunks(package, neutral))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        print("FAIL: cannot inspect local prompt packages; audit is incomplete")
        return 1
    failures = 0
    for entry in entries:
        meta, name = entry.split(b"\t", 1)
        mode, oid, stage = meta.split()
        path = name.decode("utf-8")
        if mode not in {b"100644", b"100755"} or stage != b"0":
            problems = ["unsupported Git mode or unresolved merge"]
        else:
            problems = findings(path, git("cat-file", "blob", oid.decode()), chunks)
        for problem in problems:
            failures += 1
            print(f"FAIL: {path}: {problem}")
    print(
        f"Audited {len(entries)} index files; findings: {failures}; "
        f"local prompt comparison: {'yes' if packages else 'not available'}"
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
