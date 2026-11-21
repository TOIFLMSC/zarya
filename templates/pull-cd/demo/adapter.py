"""Reference adapter for the small SQLite demo. Copy and adapt it for your service."""

import os
import signal
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

DATA = Path(os.environ["PULL_CD_DATA"]) if "PULL_CD_DATA" in os.environ else None
DB = "app.sqlite3"


def database_copy(source, destination):
    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as copy,
    ):
        original.backup(copy)


def verify(folder):
    path = folder / DB
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("invalid database")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("broken foreign keys")
        db.execute("SELECT value FROM records").fetchall()


def main():
    operation, *raw = sys.argv[1:]
    paths = [Path(path) for path in raw]
    if operation == "verify":
        verify(paths[0])
        return
    if DATA is None:
        raise ValueError("PULL_CD_DATA is required")
    if operation == "stage":
        for name in ("app.py", "VERSION"):
            if not (paths[0] / name).is_file():
                raise ValueError(f"missing {name}")
        compile((paths[0] / "app.py").read_bytes(), "app.py", "exec")
    elif operation == "can-stop":
        return  # The demo handles a single synchronous request at a time.
    elif operation == "snapshot":
        if (DATA / DB).is_file():
            database_copy(DATA / DB, paths[0] / DB)
        else:
            with closing(sqlite3.connect(paths[0] / DB)) as db, db:
                db.execute("CREATE TABLE records(value TEXT NOT NULL)")
                db.execute("PRAGMA user_version=0")
    elif operation == "migrate":
        release, candidate = paths
        version = int((release / "VERSION").read_text())
        with closing(sqlite3.connect(candidate / DB)) as db, db:
            if version < db.execute("PRAGMA user_version").fetchone()[0]:
                raise ValueError("unexpected downgrade")
            db.execute(f"PRAGMA user_version={version}")
        for fault in ("FAIL_APPLY", "INTERRUPT_APPLY"):
            if (release / fault).exists():
                (candidate / fault).touch()
    elif operation == "apply":
        database_copy(paths[0] / DB, DATA / DB)
        (DATA / DB).chmod(0o660)
        if (paths[0] / "INTERRUPT_APPLY").exists():
            os.kill(os.getppid(), signal.SIGKILL)
        if (paths[0] / "FAIL_APPLY").exists():
            raise RuntimeError("fixture failure after copying the candidate database")
    elif operation == "start":
        app = paths[0] / "app.py"
        os.execv(sys.executable, [sys.executable, "-u", str(app)])
    else:
        raise ValueError("unknown adapter operation")


if __name__ == "__main__":
    main()
