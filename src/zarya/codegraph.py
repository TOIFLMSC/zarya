"""Offline development CLI. No application, runtime database, prompts or API clients."""

import argparse
import json
import os
import re
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from zarya import obsidian_fs as fs
from zarya.codegraph_extract import extract, file_id

SCHEMA = 1
EXTRACTOR = 1
DOCS = {
    "setup.md",
    "configuration.md",
    "architecture.md",
    "privacy.md",
    "operations.md",
    "codegraph.md",
    "memory-retrieval.md",
}
ROOTS = ("src/zarya", "tests", "web/src", "scripts")
MAX_FILE = 1_048_576
MAX_TOTAL = 16_777_216
MAX_FILES = 512
MAX_GRAPH = 32_000_000
CACHE = "data/codegraph/index.json"


class GraphError(Exception):
    """Sanitised reason codes only; never echo source lines or filesystem exceptions."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def allowed(path: str) -> bool:
    parts = path.split("/")
    if any(
        p.startswith(".")
        or p in {"..", "__pycache__", "node_modules", "data", "backups", "restore-checks", "logs"}
        for p in parts
    ):
        return False
    if any(not re.fullmatch(r"[\w.-]+", p) for p in parts):
        return False
    if path == "README.md":
        return True
    if len(parts) == 2 and parts[0] == "docs":
        return parts[1] in DOCS
    if path.startswith("src/zarya/"):
        return (
            path.endswith(".py")
            or path.startswith("src/zarya/migrations/")
            and path.endswith(".sql")
        )
    if path.startswith("tests/"):
        return path.endswith((".py", ".cjs"))
    if path.startswith("web/src/"):
        return path.endswith((".ts", ".tsx", ".css", ".js"))
    return path == "scripts/check-publication.py"


def walk(root: Path, relative: str) -> Iterator[str]:
    directory = root / relative
    if not directory.exists():
        return
    fs.no_link(directory)
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda v: v.name):
            if entry.name.startswith(".") or entry.name in {
                "__pycache__",
                "node_modules",
                "assets",
                "data",
                "backups",
                "restore-checks",
                "logs",
            }:
                continue
            path = relative + "/" + entry.name
            fs.no_link(Path(entry.path))
            if entry.is_dir(follow_symlinks=False):
                yield from walk(root, path)
            elif entry.is_file(follow_symlinks=False) and allowed(path):
                yield path


def sources(root: Path) -> tuple[dict[str, bytes], dict[str, str], list[dict[str, str]]]:
    """Enumerate only named code roots. Closed folders are not traversed or opened."""
    try:
        for parent in (root, *root.parents):
            fs.no_link(parent)
        names = sorted(
            set(p for prefix in ROOTS for p in walk(root, prefix))
            | {p for p in ("README.md", *("docs/" + name for name in DOCS)) if (root / p).exists()}
        )
        if len(names) > MAX_FILES:
            raise GraphError("too_many_files")
        content: dict[str, bytes] = {}
        hashes: dict[str, str] = {}
        diagnostics: list[dict[str, str]] = []
        total = 0
        for name in names:
            try:
                raw = fs.read(root, name, limit=MAX_FILE)
            except ValueError:
                # Both links and oversized files fail; never present the old graph as fresh.
                fs.no_link(root / name)
                if (root / name).stat().st_size <= MAX_FILE:
                    raise
                raise GraphError("source_size_limit") from None
            if raw is None:
                raise GraphError("source_changed")
            total += len(raw)
            if total > MAX_TOTAL:
                raise GraphError("source_total_limit")
            content[name], hashes[name] = raw, fs.digest(raw)
        return content, hashes, diagnostics
    except (OSError, ValueError) as exc:
        raise GraphError("source_path_unavailable_or_linked") from exc


def snapshot(root: Path) -> dict[str, Any]:
    for _ in range(3):
        try:
            content, hashes, diagnostics = sources(root)
            structure = extract(content, hashes)
            structure.diagnostics.extend(diagnostics)
            _, check, check_errors = sources(root)
        except GraphError as exc:
            if str(exc) == "source_changed":
                continue
            raise
        if hashes != check or diagnostics != check_errors:
            continue
        manifest = {
            "schema": SCHEMA,
            "extractor": EXTRACTOR,
            "policy": "structure-public-code-v1",
            "files": hashes,
        }
        graph = structure.document()
        doc = {
            "snapshot_id": fs.digest(canonical(manifest)),
            "manifest": manifest,
            "complete": not structure.diagnostics,
            "graph": graph,
        }
        if len(canonical(doc)) > MAX_GRAPH:
            raise GraphError("graph_size_limit")
        return doc
    raise GraphError("workspace_keeps_changing")


@contextmanager
def locked(root: Path, relative: str) -> Iterator[None]:
    """One writer. No lock file is created in --read-only mode."""
    with fs.parents(root, relative, create=True) as path:
        for attempt in range(20):
            try:
                stream = fs.opened(path, create=not path.exists(), write=True)
                opened = stream.__enter__()
                if os.name != "nt":
                    import fcntl

                    posix_lock = cast(Any, fcntl)  # Platform-specific stdlib stubs on Windows.
                    posix_lock.flock(opened.fileno(), posix_lock.LOCK_EX | posix_lock.LOCK_NB)
                break
            except (OSError, FileExistsError):
                if "opened" in locals():
                    stream.__exit__(None, None, None)
                    del opened
                if attempt == 19:
                    raise GraphError("writer_busy") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.__exit__(None, None, None)


def persist(root: Path, doc: dict[str, Any]) -> None:
    with locked(root, "data/codegraph/index.lock"):
        # Cache is output, never a source of executable instructions or query results.
        raw = canonical(doc)
        with fs.parents(root, CACHE, create=True) as target:
            if target.exists():
                fs.no_link(target)
            temporary = target.with_name(".index-" + str(uuid4()) + ".tmp")
            try:
                with fs.opened(temporary, create=True, write=True) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)


def meta(doc: dict[str, Any]) -> dict[str, Any]:
    graph = doc["graph"]
    return {
        "snapshot_id": doc["snapshot_id"],
        "complete": doc["complete"],
        "files": len(doc["manifest"]["files"]),
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "diagnostics": graph["diagnostics"],
        "unresolved_total": graph["unresolved_total"],
        "unresolved_note": "Dynamic/external calls are not runtime bindings. "
        "Frontend links are hints.",
    }


def select(doc: dict[str, Any], query: str) -> list[dict[str, Any]]:
    nodes = doc["graph"]["nodes"]
    query = query.replace("\\", "/")
    ids = [n for n in nodes if n["id"] == query or n["id"] == file_id(query)]
    if ids:
        return ids
    exact = [n for n in nodes if query == n["name"]]
    if exact:
        return exact
    value = query.casefold().replace("\\", "/")
    return [n for n in nodes if value in n["name"].casefold() or value in n["path"].casefold()]


def show(doc: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    matches = select(doc, query)
    if len(matches) != 1:
        return {
            "ambiguous": len(matches) > 1,
            "matches": matches[:limit],
            "truncated": len(matches) > limit,
            "total": len(matches),
        }
    node = matches[0]
    edges = [e for e in doc["graph"]["edges"] if node["id"] in {e["source"], e["target"]}]
    ids = {e["source"] for e in edges[:limit]} | {e["target"] for e in edges[:limit]}
    return {
        "node": node,
        "relations": edges[:limit],
        "related": [n for n in doc["graph"]["nodes"] if n["id"] in ids],
        "truncated": len(edges) > limit,
        "total": len(edges),
    }


def impact(doc: dict[str, Any], query: str, depth: int, limit: int) -> dict[str, Any]:
    matched = select(doc, query)
    seeds = sorted({n["path"] for n in matched})
    edges = doc["graph"]["edges"]
    reverse: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge["kind"] in {"imports", "import_hint"}:
            reverse.setdefault(edge["target"], []).append(edge)
    queue: deque[tuple[str, list[dict[str, Any]]]] = deque((file_id(path), []) for path in seeds)
    seen = {file_id(path) for path in seeds}
    results: list[dict[str, Any]] = []
    depth_limited = False
    while queue:
        id_, reasons = queue.popleft()
        for edge in reverse.get(id_, []):
            source = edge["source"]
            if source in seen:
                continue
            if len(reasons) == depth:
                depth_limited = True
                continue
            seen.add(source)
            chain = [*reasons, edge]
            results.append(
                {
                    "path": edge["path"],
                    "distance": len(chain),
                    "reasons": chain,
                    "hint": any(e["kind"] == "import_hint" for e in chain),
                }
            )
            queue.append((source, chain))
    tests = sorted(
        {p for p in seeds if p.startswith("tests/test_") and p.endswith(".py")}
        | {
            r["path"]
            for r in results
            if r["path"].startswith("tests/test_") and r["path"].endswith(".py")
        }
    )
    broad = not doc["complete"] or depth_limited or any(p.endswith("conftest.py") for p in seeds)
    return {
        "seeds": seeds,
        "dependents": results[:limit],
        "candidate_tests": tests[:limit],
        "candidate_test_total": len(tests),
        "truncated": len(results) > limit or len(tests) > limit,
        "depth_limited": depth_limited,
        "broad_validation_recommended": broad,
        "note": "Import impact suggests test files; "
        "it does not prove coverage or runtime call paths."
        if tests
        else "Related tests not established; this does not mean tests are unnecessary.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--read-only", action="store_true", help="fresh in-memory graph; no writes")
    parser.add_argument("--limit", type=int, default=30)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("build")
    subs.add_parser("status")
    for name in ("search", "show", "impact"):
        sub = subs.add_parser(name)
        sub.add_argument("query")
        if name == "impact":
            sub.add_argument("--depth", type=int, default=3)
    export = subs.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 200 or not 1 <= getattr(args, "depth", 1) <= 8:
        parser.error("limit: 1..200; depth: 1..8")
    if args.read_only and args.command == "export":
        parser.error("export writes files; --read-only forbids export")
    try:
        root = args.root.absolute()
        doc = snapshot(root)
        if not args.read_only:
            persist(root, doc)
        result = meta(doc)
        if args.command == "search":
            matches = select(doc, args.query)
            result.update(
                matches=matches[: args.limit],
                total=len(matches),
                truncated=len(matches) > args.limit,
            )
        elif args.command == "show":
            result.update(show(doc, args.query, args.limit))
        elif args.command == "impact":
            result.update(impact(doc, args.query, args.depth, args.limit))
        elif args.command == "export":
            from zarya.codegraph_export import export_vault

            result.update(export_vault(root, args.output.absolute(), doc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GraphError, OSError, ValueError):
        print(
            json.dumps(
                {
                    "error": "codegraph_unavailable",
                    "complete": False,
                    "note": "Check paths, file limits, links and concurrent changes. "
                    "No stale graph returned.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
