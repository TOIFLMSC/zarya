"""Offline backup/verify/restore drill. Never creates clients or reads API keys."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock

from zarya.prompts import PromptBundle

MARKER = "restore-quarantine.json"


def regular(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Ссылки и junction в путях копии не разрешены")


def relative(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or p.is_absolute()
        or value != p.as_posix()
        or any(x in {"..", "."} for x in value.split("/"))
    ):
        raise ValueError("Недопустимый путь в копии")
    if value not in {"zarya.sqlite3", "prompts.json"} and (
        len(p.parts) < 2 or p.parts[0] not in {"photos", "media"}
    ):
        raise ValueError("Неизвестный файл копии")
    return value


def connect(path: Path) -> sqlite3.Connection:
    regular(path)
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def assets(db: sqlite3.Connection) -> set[str]:
    result = {"zarya.sqlite3"}
    for raw, image in db.execute("SELECT raw_path,image_path FROM photo_items"):
        for name in (raw, image):
            if name:
                result.add(relative("photos/" + name))
    for (frames,) in db.execute("SELECT frames FROM media_runs"):
        for frame in json.loads(frames):
            result.add(relative("media/" + frame["path"]))
    return result


def check_db(db: sqlite3.Connection) -> set[str]:
    if (
        db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("Нарушена целостность базы")
    installed = {
        p.name for p in files("zarya").joinpath("migrations").iterdir() if p.name.endswith(".sql")
    }
    if {r[0] for r in db.execute("SELECT version FROM schema_migrations")} != installed:
        raise ValueError("Версия схемы отличается от установленного приложения")
    return assets(db)


def fingerprint(path: Path) -> dict[str, Any]:
    regular(path)
    if not path.is_file():
        raise ValueError("Отсутствует файл копии")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"size": path.stat().st_size, "sha256": digest}


def verify(folder: Path) -> dict[str, Any]:
    regular(folder)
    manifest_path = folder / "manifest.json"
    regular(manifest_path)
    if manifest_path.stat().st_size > 8_000_000:
        raise ValueError("Слишком большой манифест")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Неизвестный формат копии")
    names = list(manifest["files"])
    if len({n.casefold() for n in names}) != len(names):
        raise ValueError("Конфликт имён файлов")
    for name, evidence in manifest["files"].items():
        path = folder / relative(name)
        if fingerprint(path) != evidence:
            raise ValueError("Файл изменён или повреждён: " + name)
    db = connect(folder / "zarya.sqlite3")
    try:
        if check_db(db) != set(names) - {"prompts.json"}:
            raise ValueError("Набор файлов не соответствует базе")
        if "prompts.json" in names:
            PromptBundle.load(folder / "prompts.json")
    finally:
        db.close()
    return dict(manifest)


def destination(source: Path, target: Path) -> tuple[Path, Path]:
    regular(source)
    regular(target)
    source, target = source.resolve(), target.resolve()
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Нужен новый каталог вне источника")
    target.parent.mkdir(parents=True, exist_ok=True)
    return source, target


def backup(source: Path, target: Path) -> dict[str, Any]:
    source, target = destination(source, target)
    if not (source / "zarya.sqlite3").is_file():
        raise ValueError("База не найдена")
    regular(source / "instance.lock")
    with FileLock(source / "instance.lock", timeout=0):
        stage = Path(tempfile.mkdtemp(prefix=".zarya-backup-", dir=target.parent))
        try:
            src = connect(source / "zarya.sqlite3")
            dst = sqlite3.connect(stage / "zarya.sqlite3")
            try:
                src.backup(dst)
                wanted = check_db(dst)
                if (source / "prompts.json").exists():
                    regular(source / "prompts.json")
                    PromptBundle.load(source / "prompts.json")
                    wanted.add("prompts.json")
            finally:
                src.close()
                dst.close()
            for name in wanted - {"zarya.sqlite3"}:
                origin = source / name
                regular(origin)
                if not origin.is_file():
                    raise ValueError("Нет связанного медиа: " + name)
                (stage / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, stage / name)
            manifest = {
                "format": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "files": {name: fingerprint(stage / name) for name in sorted(wanted)},
                "excludes": "API keys, logs, temporary/unreferenced media. "
                "Database includes private data and admin password hash.",
            }
            (stage / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            verify(stage)
            stage.rename(target)
            return manifest
        except BaseException:
            # Keep incomplete data for diagnosis; never publish it as a completed backup.
            raise


def restore(source: Path, target: Path) -> dict[str, Any]:
    source, target = destination(source, target)
    manifest = verify(source)
    stage = Path(tempfile.mkdtemp(prefix=".zarya-restore-", dir=target.parent))
    # Publish only a verified, quarantined installation. No runtime/keys are started.
    for name in manifest["files"]:
        (stage / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, stage / name)
    (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    verify(stage)
    (stage / MARKER).write_text(
        json.dumps(
            {
                "restored_at": datetime.now(UTC).isoformat(),
                "source_created_at": manifest["created_at"],
            }
        ),
        encoding="utf-8",
    )
    stage.rename(target)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Заря: offline backup / verify / restore в новый каталог"
    )
    parser.add_argument("action", choices=["backup", "verify", "restore"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "verify":
            manifest = verify(args.source)
        else:
            if args.target is None:
                parser.error("Нужен новый каталог назначения")
            manifest = (backup if args.action == "backup" else restore)(args.source, args.target)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "files": len(manifest["files"]),
                    "created_at": manifest["created_at"],
                    "quarantined": args.action == "restore",
                }
            )
        )
    except Exception as exc:
        parser.exit(1, f"Операция не завершена: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
