"""Short-lived row reads; never memoize permissions or validation decisions."""

import re
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

import aiosqlite

MAX_ROWS = 2048
BATCH_SIZE = 128


class MemoryRead:
    """One synchronous scope inside an existing transaction, with write invalidation."""

    def __init__(self, conn: aiosqlite.Connection, *, enabled: bool = True):
        self.conn = conn
        self.enabled = enabled
        self.active = False
        self.closed = False
        self.changes = conn.total_changes
        self.transaction = getattr(conn, "_zarya_transaction", None)
        self.rows: OrderedDict[tuple[str, tuple[Any, ...]], Any] = OrderedDict()

    def __enter__(self) -> "MemoryRead":
        if self.active or self.closed:
            raise ValueError("read_scope_closed")
        self.active = True
        return self

    def __exit__(self, *args: Any) -> None:
        self.rows.clear()
        self.active, self.closed = False, True

    def usable(self, conn: aiosqlite.Connection) -> bool:
        if conn is not self.conn or not self.active or self.closed:
            raise ValueError("read_scope_mismatch")
        transaction = getattr(conn, "_zarya_transaction", None)
        if not conn.in_transaction or transaction is not self.transaction:
            self.rows.clear()
            self.enabled = False
        if self.changes != conn.total_changes:
            self.rows.clear()
            self.changes = conn.total_changes
        return self.enabled

    def remember(self, key: tuple[str, tuple[Any, ...]], value: Any) -> None:
        self.rows[key] = value
        self.rows.move_to_end(key)
        while len(self.rows) > MAX_ROWS:
            self.rows.popitem(last=False)

    async def one(
        self, conn: aiosqlite.Connection, sql: str, args: tuple[Any, ...], *, cache: bool = True
    ) -> Any:
        # Float arguments in these guards are live retention cutoffs. Never cache them.
        cache = self.usable(conn) and cache and not any(isinstance(arg, float) for arg in args)
        key = (sql, args)
        if cache and key in self.rows:
            self.rows.move_to_end(key)
            return self.rows[key]
        async with conn.execute(sql, args) as cursor:
            result = await cursor.fetchone()
        if cache:
            self.remember(key, result)
        return result

    async def prefetch(
        self,
        conn: aiosqlite.Connection,
        sql: str,
        arguments: Iterable[tuple[Any, ...]],
        *,
        id_slot: int = 0,
    ) -> None:
        """Populate exact single-row projections by integer ID without changing iteration order."""
        if not self.usable(conn):
            return
        groups: dict[tuple[Any, ...], dict[int, tuple[Any, ...]]] = {}
        for args in arguments:
            if type(args[id_slot]) is not int or any(isinstance(arg, float) for arg in args):
                continue
            if (sql, args) not in self.rows:
                scope = args[:id_slot] + args[id_slot + 1 :]
                groups.setdefault(scope, {})[args[id_slot]] = args
        for values in groups.values():
            ids = list(values)
            for offset in range(0, len(ids), BATCH_SIZE):
                chunk = ids[offset : offset + BATCH_SIZE]
                args = values[chunk[0]]
                batched, count = re.subn(
                    r"\bid=\?", "id IN (" + ",".join("?" for _ in chunk) + ")", sql, count=1
                )
                if count != 1 or not sql.startswith("SELECT "):
                    raise ValueError("invalid_batch_projection")
                batched = "SELECT id," + batched[len("SELECT ") :]
                params = args[:id_slot] + tuple(chunk) + args[id_slot + 1 :]
                async with conn.execute(batched, params) as cursor:
                    found = {row[0]: row[1:] for row in await cursor.fetchall()}
                for ident in chunk:
                    self.remember((sql, values[ident]), found.get(ident))
