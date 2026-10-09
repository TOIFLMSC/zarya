"""Audit the Git index without printing secret values. Run before committing."""

import ast
import json
import re
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
}
DOCS = {"setup.md", "configuration.md", "architecture.md", "privacy.md", "operations.md"}
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
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def allowed(path: str) -> bool:
    p = PurePosixPath(path)
    if path in ROOT_FILES or path == ASSET:
        return True
    if any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in p.parts):
        return False
    if p.parent.as_posix() == "docs":
        return p.name in DOCS
    if p.parent.as_posix() == "scripts":
        return p.name in {"setup-media.ps1", "check-publication.py"}
    if p.parts[:2] == ("src", "zarya"):
        return p.suffix in {".py", ".sql"} or path == "src/zarya/prompts.example.json"
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


def main() -> int:
    entries = git("ls-files", "--stage", "-z").split(b"\0")
    entries = [entry for entry in entries if entry]
    if not entries:
        print("FAIL: Git index is empty; nothing has been audited")
        return 1
    chunks = set()
    local = Path("data/prompts.json")
    if local.exists():
        neutral = git("show", ":src/zarya/prompts.example.json").decode("utf-8")
        chunks = private_chunks(local.read_text(encoding="utf-8-sig"), neutral)
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
        f"local prompt comparison: {'yes' if local.exists() else 'not available'}"
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
