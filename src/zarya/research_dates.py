"""Carry a supported event date and publication date through to the answer."""

import re
from datetime import date
from typing import Any

from zarya.research_fetch import check_target

DATES_MARKER = "[RESEARCH_DATES]"
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
DATE_ROLES = ("event", "publication")
RELATIVE_DATES = re.compile(r"\b(?:сегодня|вчера|позавчера|today|yesterday)\b", re.I)
CALENDAR_DATE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[./]\d{{1,2}}|"
    rf"\d{{1,2}}(?:[-‑–](?:го|е)|\s+(?:числа|{'|'.join(MONTHS)})))\b",
    re.I,
)
URL = re.compile(r"https?://[^\s<>]+", re.I)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])(?=\s)|(?=\n)")


def claim_dates(raw: Any, sources: dict[str, int]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    subject = raw.get("subject")
    if not isinstance(subject, str) or not re.fullmatch(r"[\w ,.«»()–—'’\-]{3,120}", subject):
        return None
    dates: dict[str, Any] = {"subject": subject.strip()}
    if not dates["subject"]:
        return None
    for role in DATE_ROLES:
        evidence = raw.get(role)
        if not isinstance(evidence, dict):
            return None
        literal, url = evidence.get("date"), evidence.get("url")
        if not isinstance(literal, str) or not isinstance(url, str):
            return None
        try:
            actual = date.fromisoformat(literal)
            source_id = sources.get(check_target(url))
        except ValueError:
            return None
        if literal != actual.isoformat() or source_id is None:
            return None
        dates[role] = {"date": literal, "source_id": source_id}
    return None if CALENDAR_DATE.search(subject) or RELATIVE_DATES.search(subject) else dates


def dates_key(claim: dict[str, Any]) -> tuple[str, ...]:
    dates = claim.get("dates")
    if not dates:
        return ()
    return (dates["subject"].casefold(), *(dates[role]["date"] for role in DATE_ROLES))


def answer_dates(research: dict[str, Any]) -> dict[str, Any] | None:
    if research.get("mode") != "search" or research.get("state") != "completed":
        return None
    analysis = research.get("analysis") or {}
    pairs = {
        dates_key(claim): claim["dates"]
        for claim in analysis.get("claims", [])
        if claim.get("dates")
        and claim.get("kind") == "fact"
        and claim.get("verdict") == "supported"
    }
    if len(pairs) != 1:
        return None
    pair = next(iter(pairs.values()))
    return pair if pair["event"]["date"] != pair["publication"]["date"] else None


def unsafe_date(text: str, dates: dict[str, Any]) -> bool:
    text = URL.sub("", text)
    values = [date.fromisoformat(dates[role]["date"]) for role in DATE_ROLES]
    return bool(RELATIVE_DATES.search(text)) or any(
        re.search(
            rf"\b(?:{value.isoformat()}|0?{value.day}[./]0?{value.month}(?:[./]\d{{2,4}})?|"
            rf"0?{value.day}(?:[-‑–](?:го|е)|\s+числа)|"
            rf"0?{value.day}\s+{MONTHS[value.month - 1]})\b",
            text,
            re.I,
        )
        for value in values
    )


def render_answer_dates(text: str, dates: dict[str, Any]) -> str:
    def day(role: str) -> str:
        evidence = dates[role]
        value = date.fromisoformat(evidence["date"])
        return f"{value.day} {MONTHS[value.month - 1]} {value.year} [S{evidence['source_id']}]"

    line = f"{dates['subject']} — {day('event')}. Об этом сообщили {day('publication')}."
    if unsafe_date(text, dates):
        # Keep the rest of the answer if only a sentence or line mistates the dates.
        text = "".join(
            piece for piece in SENTENCE_BREAK.split(text) if not unsafe_date(piece, dates)
        )
    if DATES_MARKER in text:
        return text.replace(DATES_MARKER, line, 1).replace(DATES_MARKER, "")
    return "\n\n".join(part for part in (line, text.strip()) if part)
