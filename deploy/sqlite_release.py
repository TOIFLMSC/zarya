"""Zarya's offline adapter. Invoked with the selected release's Python."""

import argparse
import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from zarya.backup import check_db, connect, fingerprint, regular
from zarya.database import Database


def copy_database(source, target, *, existing):
    regular(target)
    if existing != target.is_file():
        raise ValueError("unexpected destination database state")
    # SQLite's backup transaction includes committed WAL pages and commits atomically.
    mode = "rw" if existing else "rwc"
    with (
        closing(connect(source)) as src,
        closing(sqlite3.connect(target.resolve().as_uri() + "?mode=" + mode, uri=True)) as dst,
    ):
        dst.execute("PRAGMA synchronous=FULL")
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("database copy failed integrity check")


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def preserved(before, after):
    """Check all original columns, including multiplicity, without logging row contents."""
    with closing(connect(after)) as db:
        db.execute("ATTACH DATABASE ? AS prior", (before.resolve().as_uri() + "?mode=ro",))
        tables = db.execute(
            "SELECT name FROM prior.sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ).fetchall()
        for (name,) in tables:
            if name == "schema_migrations":
                continue
            table = quoted(name)
            previous = [r[1] for r in db.execute(f"PRAGMA prior.table_info({table})")]
            current_columns = {r[1] for r in db.execute(f"PRAGMA main.table_info({table})")}
            if not current_columns.issuperset(previous):
                raise ValueError(f"migration removed existing columns in {name}")
            columns = ",".join(quoted(column) for column in previous)

            prior, current = (
                f"SELECT {columns},count(*) FROM {schema}.{table} GROUP BY {columns}"
                for schema in ("prior", "main")
            )
            for left, right in ((prior, current), (current, prior)):
                if db.execute(f"{left} EXCEPT {right} LIMIT 1").fetchone():
                    raise ValueError(f"migration changed existing records in {name}")


async def migrate(path):
    db = Database(path)
    await db.open()
    await db.close()


def prepare(snapshot, candidate, data):
    source = snapshot / "zarya.sqlite3"
    working = data / "zarya.sqlite3"
    if working.resolve() != source.resolve():
        preserved(source, working)
        with closing(connect(source)) as old, closing(connect(working)) as current:
            sql = "SELECT version FROM schema_migrations ORDER BY version"
            if old.execute(sql).fetchall() != current.execute(sql).fetchall():
                raise ValueError("working schema changed after snapshot")
    copy_database(source, candidate, existing=False)
    asyncio.run(migrate(candidate))
    preserved(source, candidate)
    with closing(connect(candidate)) as db:
        for name in check_db(db) - {"zarya.sqlite3"}:
            if fingerprint(snapshot / name) != fingerprint(data / name):
                raise ValueError("referenced media differs from snapshot")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("prepare")
    for arg in ("snapshot", "candidate", "data"):
        staging.add_argument(arg, type=Path)
    switch = commands.add_parser("copy")
    for arg in ("source", "target"):
        switch.add_argument(arg, type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.snapshot, args.candidate, args.data)
    else:
        copy_database(args.source, args.target, existing=True)
