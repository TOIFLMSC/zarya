"""Validate and identify distinct event and publication dates in research evidence."""

import re
from datetime import date
from typing import Any

from zarya.research_fetch import check_target

MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
DATE_ROLES = ("event", "publication")
RELATIVE_DATES = re.compile(r"\b(?:сегодня|вчера|позавчера|today|yesterday)\b", re.I)
CALENDAR_DATE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[./]\d{{1,2}}|"
    rf"\d{{1,2}}(?:[-‑–](?:го|е)|\s+(?:числа|{'|'.join(MONTHS)})))\b",
    re.I,
)


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
