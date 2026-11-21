"""Loopback-only test service with an observable data record and revision."""

import json
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DB = Path(os.environ["PULL_CD_DATA"]) / "app.sqlite3"
URL = urlsplit(os.environ["PULL_CD_HEALTH_URL"])
REVISION = os.environ["PULL_CD_REVISION"]
FAIL = Path("FAIL_HEALTH").is_file()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != URL.path:
            self.send_error(404)
            return
        with closing(sqlite3.connect(DB)) as db:
            db.execute("SELECT 1 FROM records LIMIT 1").fetchone()
        payload = [] if FAIL else dict(status="ok", revision=REVISION)
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with closing(sqlite3.connect(DB)) as db, db:
        if FAIL:
            db.execute("INSERT INTO records VALUES ('write after admission')")
    HTTPServer(("127.0.0.1", URL.port), Handler).serve_forever()
