"""Numeric diagnostics only; never part of prompts, permissions or replay inputs."""

import json
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

STAGES = (
    "queue_ms",
    "claim_ms",
    "context_ms",
    "prepare_ms",
    "model_ms",
    "postprocess_ms",
    "finish_wait_ms",
    "finish_work_ms",
    "outbox_wait_ms",
    "delivery_check_ms",
    "telegram_ms",
    "result_write_ms",
    "total_first_reply_ms",
)
FIELDS = (*STAGES, "received_at", "live_text")


def clean(value: Any) -> dict[str, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict) or value.get("version") != 1:
        return {}
    return {
        key: float(number)
        for key in FIELDS
        if isinstance((number := value.get(key)), (int, float))
        and not isinstance(number, bool)
        and math.isfinite(number)
        and number >= 0
        and (key != "live_text" or number in (0, 1))
    }


def encode(value: dict[str, Any]) -> str:
    return json.dumps({"version": 1, **clean({**value, "version": 1})})


def elapsed(start: float) -> float:
    return max(0, (time.monotonic() - start) * 1000)


class Clock:
    """Wall milestones are usable only within this process with stable clocks."""

    def __init__(self) -> None:
        self.wall = time.time()
        self.mono = time.monotonic()
        self.valid = True

    def since(self, start: float, end: float | None = None) -> float | None:
        wall, mono = time.time(), time.monotonic()
        if abs((wall - self.wall) - (mono - self.mono)) > 1:
            self.valid = False
        end = wall if end is None else end
        if not self.valid or start < self.wall or not self.wall <= start <= end <= wall:
            return None
        return (end - start) * 1000


@asynccontextmanager
async def finishing(conn: aiosqlite.Connection, work: dict[str, Any]) -> AsyncIterator[None]:
    started = time.monotonic()
    measured = work.setdefault("timings", {})
    measured["finish_wait_ms"] = elapsed(work.pop("_finish_start", started))
    # Duplicate finish calls cannot replace the first attempt's measurements.
    work["_finish_claimed"] = False
    try:
        yield
    finally:
        if work.pop("_finish_claimed", False):
            measured["finish_work_ms"] = elapsed(started)
            await conn.execute(
                "UPDATE dialogue_runs SET timings_json=? WHERE id=?",
                (encode(measured), work["run_id"]),
            )


def diagnostic(
    raw: Any, delivery: Any, *, state: str, mode: str, attempts: int = 0
) -> dict[str, Any]:
    stages = {
        key: number for key, number in {**clean(raw), **clean(delivery)}.items() if key in STAGES
    }
    return {
        "stages": stages,
        "attempts": attempts,
        "status": "test"
        if mode != "live"
        else (
            "historical"
            if not stages
            else "complete"
            if "total_first_reply_ms" in stages
            else "pending"
            if state == "generating"
            else "partial"
        ),
    }


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, list[float]] = {key: [] for key in STAGES}
    historical = partial = retried = measured = 0
    for record in records:
        values = diagnostic(
            record["run_timings"], record["delivery_timings"], state=record["state"], mode="live"
        )["stages"]
        historical += not bool(values)
        measured += bool(values)
        partial += bool(values) and "total_first_reply_ms" not in values
        retried += record["attempts"] > 1
        for key, number in values.items():
            stages[key].append(number)
    result = {}
    for key, values in stages.items():
        values.sort()
        count = len(values)
        result[key] = {
            "samples": count,
            "avg_ms": sum(values) / count if count else None,
            "p95_ms": values[math.ceil(count * 0.95) - 1] if count else None,
        }
    return {
        "operations": len(records),
        "measured": measured,
        "historical": historical,
        "partial": partial,
        "retried": retried,
        "stages": result,
    }
