"""Owned read-only Obsidian map; edited/foreign notes are never silently overwritten."""

import ctypes
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from zarya import obsidian_fs as fs
from zarya.codegraph import MAX_FILES, MAX_GRAPH, GraphError, canonical, locked

FOLDER = "CodeGraph Зари"
MARKER = ".zarya-codegraph.json"
MAX_OWNED = MAX_FILES + 2


def name_for(path: str) -> str:
    # Readable labels in Obsidian's graph, with a hash to disambiguate equal basenames.
    return "Модули/" + Path(path).name[:96] + "-" + fs.digest(path.encode())[:24] + ".md"


def notes(doc: dict[str, Any]) -> dict[str, bytes]:
    graph = doc["graph"]
    files = {n["id"]: n for n in graph["nodes"] if n["id"].startswith("file:")}
    result = {"graph.json": canonical(doc)}
    intro = [
        "# CodeGraph Зари",
        "",
        f"Снимок: `{doc['snapshot_id']}`",
        "",
        "Статическая карта разработки. Правки заметок не меняют код или поведение бота.",
        "Динамические вызовы неполны; frontend-связи — подсказки. Проверяй исходники.",
        "",
        "Обновление — явная команда export. Память, рабочие промпты и ключи не входят.",
        "",
    ]
    for id_, node in sorted(files.items()):
        target = name_for(node["path"])
        intro.append(f"- [[{target[:-3]}|{node['path']}]]")
        lines = [
            f"# {node['path']}",
            "",
            f"Снимок: `{doc['snapshot_id']}`",
            f"SHA-256: `{node['file_hash']}`",
            "",
            "[[Начало]]",
            "",
            "## Символы",
            "",
        ]
        for symbol in graph["nodes"]:
            if symbol["path"] == node["path"] and not symbol["id"].startswith("file:"):
                lines.append(
                    f"- `{symbol['name']}` · {symbol['kind']} · строки "
                    f"{symbol['line']}–{symbol['end_line']}"
                )
        lines.extend(("", "## Связи файлов", ""))
        seen = set()
        for edge in graph["edges"]:
            if edge["kind"] not in {"imports", "import_hint", "document_links_to"}:
                continue
            if id_ == edge["source"]:
                other, direction = edge["target"], "→"
            elif id_ == edge["target"]:
                other, direction = edge["source"], "←"
            else:
                continue
            if other not in files or (other, direction, edge["kind"]) in seen:
                continue
            seen.add((other, direction, edge["kind"]))
            linked = files[other]["path"]
            lines.append(f"- {direction} [[{name_for(linked)[:-3]}|{linked}]] · {edge['kind']}")
        result[target] = ("\n".join(lines) + "\n").encode()
    result["Начало.md"] = ("\n".join(intro) + "\n").encode()
    return result


def valid_names(files: Any, *, limit: int = MAX_OWNED) -> bool:
    return (
        isinstance(files, dict)
        and len(files) <= limit
        and all(
            isinstance(name, str)
            and (
                name in {"Начало.md", "graph.json"}
                or re.fullmatch(r"Модули/(?:[\w.-]{1,96}-)?[0-9a-f]{24}\.md", name)
            )
            and isinstance(hash_, str)
            and re.fullmatch(r"[0-9a-f]{64}", hash_)
            for name, hash_ in files.items()
        )
    )


def remove_owned(root: Path, name: str, expected: str) -> None:
    with fs.parents(root, name) as path:
        if not path.exists():
            return
        if sys.platform == "win32":
            import msvcrt
            from ctypes import wintypes

            # DELETE access + exclusive handle binds the hash check and deletion to one object.
            handle = fs.win_open(path, 0x80000000 | 0x10000, 3, 0x00200000)
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                if fs.digest(stream.read(MAX_GRAPH + 1)) != expected:
                    raise GraphError("export_note_changed")
                flag = wintypes.BOOL(True)
                fn = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
                fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
                fn.restype = wintypes.BOOL
                if not fn(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
                    raise GraphError("export_note_busy")
        else:
            with fs.opened(path) as stream:
                if fs.digest(stream.read(MAX_GRAPH + 1)) != expected:
                    raise GraphError("export_note_changed")
                if path.lstat().st_ino != os.fstat(stream.fileno()).st_ino:
                    raise GraphError("export_note_changed")
                path.unlink()


def export_vault(root: Path, output: Path, doc: dict[str, Any]) -> dict[str, Any]:
    root, output = root.absolute(), output.absolute()
    if output.name != FOLDER or root.is_relative_to(output) or output.is_relative_to(root):
        raise GraphError("export_needs_separate_codegraph_folder")
    if not output.parent.is_dir() or any(p.name.startswith(".") for p in output.parents):
        raise GraphError("export_parent_unavailable")
    for parent in (output.parent, *output.parent.parents):
        fs.no_link(parent)
    project = fs.digest(os.path.normcase(str(root)).encode())
    if output.exists():
        fs.no_link(output)
        original = fs.read(output, MARKER, limit=MAX_GRAPH)
        if original is None:
            raise GraphError("export_foreign_folder")
    else:
        original = canonical(
            {
                "schema": 1,
                "project": project,
                "files": {},
                "previous": {},
                "phase": "ready",
                "snapshot_id": "",
            }
        )
        if not fs.write(output.parent, FOLDER + "/" + MARKER, original, None, limit=MAX_GRAPH):
            raise GraphError("export_foreign_folder")
    with locked(output, ".export.lock"):
        raw = fs.read(output, MARKER, limit=MAX_GRAPH)
        if raw != original:
            raise GraphError("export_marker_changed")
        marker = json.loads(raw)
        if (
            set(marker) != {"schema", "project", "files", "previous", "phase", "snapshot_id"}
            or marker["schema"] != 1
            or marker["project"] != project
            or marker["phase"] not in {"ready", "writing"}
            or not valid_names(marker["files"])
            or not valid_names(marker["previous"], limit=MAX_OWNED * 2)
        ):
            raise GraphError("export_marker_invalid")
        previous = dict(marker["previous"]) | dict(marker["files"])
        desired = notes(doc)
        current: dict[str, str | None] = {}
        # Check all previous and desired names before publishing anything.
        for name in sorted(previous.keys() | desired.keys()):
            data = fs.read(output, name, limit=MAX_GRAPH)
            hash_ = fs.digest(data) if data is not None else None
            if hash_ is not None and hash_ not in {
                marker["files"].get(name),
                marker["previous"].get(name),
            }:
                raise GraphError("export_note_changed_or_foreign")
            current[name] = hash_
        previous_hashes = {name: hash_ for name, hash_ in current.items() if hash_ is not None}
        desired_hashes = {name: fs.digest(data) for name, data in desired.items()}
        if not valid_names(previous_hashes, limit=MAX_OWNED * 2) or not valid_names(desired_hashes):
            raise GraphError("export_recovery_limit")
        writing = {
            "schema": 1,
            "project": project,
            "phase": "writing",
            "snapshot_id": doc["snapshot_id"],
            "previous": previous_hashes,
            "files": desired_hashes,
        }
        written = canonical(writing)
        if not fs.write(output, MARKER, written, fs.digest(raw), limit=MAX_GRAPH):
            raise GraphError("export_marker_changed")
        for name, data in desired.items():
            if current[name] == fs.digest(data):
                continue
            if not fs.write(output, name, data, current[name], limit=MAX_GRAPH):
                raise GraphError("export_note_changed")
        for name in previous.keys() - desired.keys():
            expected = current[name]
            if expected is not None:
                remove_owned(output, name, expected)
        writing["phase"], writing["previous"] = "ready", {}
        if not fs.write(output, MARKER, canonical(writing), fs.digest(written), limit=MAX_GRAPH):
            raise GraphError("export_marker_changed")
    return {
        "exported_files": len(desired),
        "export_snapshot": doc["snapshot_id"],
        "note": "Read-only code map. Obsidian edits are not imported. "
        "Sync delivery is not verified.",
    }
