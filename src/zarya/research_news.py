"""Recheck a public news item from a recent, delivered reply in the same scope."""

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiosqlite

from zarya.dialogue_logic import explicit_reply, topic_id
from zarya.research_fetch import check_target
from zarya.research_intent import QUESTION, local_task, speaker_text
from zarya.research_plan import followup_input
from zarya.source_material import one, valid

REPLY_MAX_AGE = 48 * 3600
RECHECK_QUESTION = (
    "Заново проверь свежую новость по публичным ориентирам. Выполни независимый поиск "
    "по СМИ и найди первоисточник: официальное заявление, документ или данные площадки. "
    "Раздельно проверь основные утверждения, даты события и публикации, участников, "
    "инструменты и причины. Если предметом новости стала остановка торгов, уточни биржу "
    "и инструмент. Исходный пост задаёт тему, а не служит независимым подтверждением. "
    "Если остался пробел, явно укажи, что не удалось подтвердить."
)
RECENT = re.compile(
    r"\b(?:сегодня\w*|вчера\w*|недавно|только\s+что|на\s+днях|"
    r"этой\s+(?:ночью|неделе)|свеж\w+|сейчас|только\s+утром)\b",
    re.I,
)
CORRECTION = re.compile(
    r"\b(?:подожди|погоди|стоп|уточню|поправлю|точнее|разве|вообще[- ]то|"
    r"ты\s+перепутал\w*|я\s+име[ллю]\w*\s+в\s+виду|(?:он[аи]?|его|е[её]|их)\s+же?)\b",
    re.I,
)
DAY_OR_TIME = r"(?:сегодня|вчера|позавчера|утром|дн[её]м|вечером|ночью)"
TIME_CONTRAST = re.compile(
    rf"\b(?:не\s+{DAY_OR_TIME}\s*,?\s+а\s+{DAY_OR_TIME}|"
    rf"{DAY_OR_TIME}\s*,?\s+а\s+не\s+{DAY_OR_TIME})\b",
    re.I,
)
NEWS_DETAIL = re.compile(
    r"\b(?:новост\w*|событи\w*|увольн\w*|увол\w*|отставк\w*|снял\w*|сменил\w*|"
    r"вернул\w*|восстановил\w*|назначил\w*|подал\w*|объявил\w*|остановил\w*|"
    r"приостановил\w*|возобновил\w*|опубликовал\w*|отменил\w*|отозвал\w*|"
    r"вступил\w*|подписал\w*|принял\w*|одобрил\w*|торг\w*|фьючерс\w*|акци\w*|"
    r"бирж\w*|индекс\w*|фейк\w*|уш[её]л|ушла|ушли|опроверг\w*|подтверд\w*|"
    r"произош\w*|случил\w*|когда|во\s+сколько)\b",
    re.I,
)
NEWS_SECTION = re.compile(
    r"/(?:news|newsroom|articles?|press-releases?|world|business|markets|politics|tech(?:nology)?)/",
    re.I,
)
NEWS_PUBLISHERS = frozenset(
    {
        "apnews.com",
        "bbc.co.uk",
        "bbc.com",
        "bloomberg.com",
        "cnbc.com",
        "ft.com",
        "interfax.ru",
        "kommersant.ru",
        "rbc.ru",
        "reuters.com",
        "tass.ru",
        "vedomosti.ru",
    }
)


def news_followup(text: str) -> bool:
    text = re.sub(r"https?://[^\s<>]+", " ", text)
    return bool(
        text
        and not local_task(text)
        and (
            TIME_CONTRAST.search(text)
            or NEWS_DETAIL.search(text)
            and (RECENT.search(text) or CORRECTION.search(text) or QUESTION.search(text))
        )
    )


def social_post_url(value: str) -> str | None:
    from zarya.x_post import post_id, post_url

    if identifier := post_id(value):
        return post_url(identifier)
    try:
        parts = urlsplit(check_target(value))
    except ValueError:
        return None
    if parts.hostname in {"t.me", "telegram.me", "www.t.me"}:
        post = re.fullmatch(
            r"/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/([1-9][0-9]{0,11})/?", parts.path
        )
        if post and post[1].lower() not in {"joinchat", "addlist", "share", "login"}:
            return f"https://t.me/{post[1]}/{post[2]}"
    return None


def news_article_url(value: str) -> str | None:
    try:
        parts = urlsplit(check_target(value))
    except ValueError:
        return None
    host = parts.hostname or ""
    if host.removeprefix("www.") not in NEWS_PUBLISHERS:
        return None
    path = parts.path.rstrip("/")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", path):
        return None
    sections = path.split("/")[1:]
    if any(not section or section in {".", ".."} for section in sections):
        return None
    editorial_slug = NEWS_SECTION.search(path) and re.fullmatch(
        r"(?:[A-Za-z0-9]{1,24}-){2,}[A-Za-z0-9]{1,24}(?:\.html)?", sections[-1]
    )
    tass_id = host.removeprefix("www.") == "tass.ru" and re.fullmatch(r"/[a-z-]+/\d{4,12}", path)
    if editorial_slug or tass_id:
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return None


def search_discovered_urls(actions: list[dict[str, Any]]) -> set[str]:
    observed = set()
    for action in actions:
        if action.get("status") == "completed" and action.get("type") == "search":
            for item in action.get("sources") or []:
                try:
                    observed.add(check_target(item.get("url") or ""))
                except ValueError:
                    continue
    return observed


async def fresh_news_sources(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    message: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]] | None:
    target = explicit_reply(message).get("message_id")
    if not target or not news_followup(speaker_text(message)):
        return None
    ready = await one(
        conn,
        "SELECT w.sources,w.actions,w.manifest FROM outbox o "
        "JOIN dialogue_runs r ON r.job_id=o.job_id "
        "JOIN research_runs w ON w.id=json_extract(r.snapshot,'$.research_id') "
        "AND w.bot_id=o.bot_id "
        "AND w.chat_id=o.chat_id AND w.access_version=o.access_version AND w.job_id=o.job_id "
        "JOIN recent_messages m ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id "
        "AND m.message_id=o.message_id AND m.role='assistant' "
        "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.message_id=? "
        "AND o.state='sent' AND r.state='completed' AND r.mode='live' AND w.state='completed' "
        "AND COALESCE(r.thread_id,0)=? AND m.received_at>?",
        (bot, chat, grant, target, topic_id(message) or 0, time.time() - REPLY_MAX_AGE),
    )
    if not ready:
        return None
    sources, actions, manifest = map(json.loads, ready)
    if not manifest or not await valid(conn, bot, chat, grant, manifest):
        return None
    urls = []
    for source in sources:
        if source.get("error") or source.get("coverage") == "unavailable":
            continue
        value = str(source.get("requested_url") or source.get("url") or "")
        anchor = social_post_url(value)
        if anchor:
            urls.append(anchor)
        elif source.get("coverage") == "partial_text" and (anchor := news_article_url(value)):
            urls.append(anchor)
    observed = search_discovered_urls(actions)
    public_results = [source for source in sources if source.get("url") in observed]
    plan = followup_input("study", {"needs_followup": True}, public_results, observed)
    if plan:
        urls.extend(social_post_url(url) or url for url in plan["public_urls"])
    selected_urls = list(dict.fromkeys(urls))[:3]
    return (selected_urls, manifest) if selected_urls else None


def public_search_sources(
    inputs: list[dict[str, Any]], fetched: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    output = []
    for entry, source in zip(inputs, fetched, strict=True):
        anchor = entry["requested_url"]
        result_url = str(source.get("url") or "")
        result_anchor = social_post_url(result_url) or news_article_url(result_url) or result_url
        title = (
            source.get("title", "") if not source.get("error") and result_anchor == anchor else ""
        )
        output.append({"id": source["id"], "url": anchor, "title": str(title)[:300]})
    return output


def news_followup_plan(
    analysis: dict[str, Any], inputs: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not analysis.get("needs_followup"):
        return None
    observed = search_discovered_urls(actions)
    results = [{"url": url} for url in sorted(observed)]
    discovered = followup_input("study", analysis, results, observed)
    anchors = [entry["requested_url"] for entry in inputs]
    if discovered:
        anchors.extend(social_post_url(url) or url for url in discovered["public_urls"])
    return {"reason": "evidence_gap", "public_urls": list(dict.fromkeys(anchors))[:3]}


def limit_unconfirmed_claims(analysis: dict[str, Any], origin: str, observed: set[str]) -> None:
    """Publication metadata by itself confirms only what the post says."""

    def host(url: str) -> str | None:
        parts = urlsplit(social_post_url(url) or url)
        return (parts.hostname or "").removeprefix("www.") or None

    publisher = host(origin)
    uncertain = False
    for claim in analysis["claims"]:
        if claim["verdict"] == "uncertain":
            continue
        if not any(url in observed and host(url) != publisher for url in claim["urls"]):
            claim.update(kind="uncertain", verdict="uncertain")
            claim.pop("dates", None)
            uncertain = True
    if uncertain or not any(c["verdict"] != "uncertain" for c in analysis["claims"]):
        analysis["summary"] = "Для части или всех утверждений нет независимого подтверждения."
        analysis["needs_followup"] = True
