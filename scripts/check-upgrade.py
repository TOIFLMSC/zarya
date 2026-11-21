"""Exercise the candidate's SQLite upgrade against the base commit's public schema."""

import argparse
import asyncio
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from zarya.database import Database

MIGRATIONS = "src/zarya/migrations"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


async def open_candidate(path: Path) -> None:
    db = Database(path)
    await db.open()
    await db.close()


def check(base: str, path: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("expected the full base commit SHA")
    names = git("ls-tree", "--name-only", f"{base}:{MIGRATIONS}").decode().splitlines()
    previous = sorted(name for name in names if name.endswith(".sql"))
    current = sorted(p.name for p in Path(MIGRATIONS).glob("*.sql"))
    if not previous or current[: len(previous)] != previous:
        raise ValueError("migration history must be append-only")
    for number, name in enumerate(current, 1):
        if not re.fullmatch(rf"{number:03d}_[a-z0-9_]+\.sql", name):
            raise ValueError(f"unexpected migration number or name: {name}")

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)")
        for name in previous:
            sql = git("show", f"{base}:{MIGRATIONS}/{name}").decode()
            if sql.replace("\r\n", "\n") != (Path(MIGRATIONS) / name).read_text(encoding="utf-8"):
                raise ValueError(f"published migration changed: {name}")
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (name,))
        conn.execute(
            "INSERT INTO users(telegram_id, display_name) VALUES ('ci-user', 'CI fixture')"
        )
        conn.execute("INSERT INTO telegram_state(bot_id, next_offset) VALUES ('ci-bot', 42)")

    for _ in range(2):
        asyncio.run(open_candidate(path))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        applied = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert applied == [(name,) for name in current]
        assert conn.execute(
            "SELECT display_name FROM users WHERE telegram_id='ci-user'"
        ).fetchone() == ("CI fixture",)
        assert conn.execute(
            "SELECT next_offset FROM telegram_state WHERE bot_id='ci-bot'"
        ).fetchone() == (42,)
    print(f"Upgrade {previous[-1]} -> {current[-1]} and second open passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    with TemporaryDirectory() as directory:
        check(parser.parse_args().base, Path(directory) / "upgrade.sqlite3")
