"""Local quote intent from retained reply provenance, never from nearby chat text."""

import re
from dataclasses import dataclass
from typing import Any

import aiosqlite

from zarya.dialogue_logic import topic_id
from zarya.source_material import accepted, one, valid

ALIASES = {
    "BTC": r"btc|bitcoin|биткоин\w*|биток|битка",
    "ETH": r"eth|ethereum|эфир(?:а|иум)?",
    "HYPE": r"hype|hyperliquid|хайп",
    "SOL": r"sol|solana|солан[аыу]",
}
NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "HYPE": "Hyperliquid", "SOL": "Solana"}
CURRENCIES = {
    "USD": r"usd|доллар\w*|бакс\w*",
    "KZT": r"kzt|тенге",
    "RUB": r"rub|рубл\w*",
    "EUR": r"eur|евро",
}
PRICE = re.compile(r"\b(?:цен\w*|котиров\w*|курс\w*|скок\w*|сколько|поч[её]м)\b", re.I)
CONFIRM = re.compile(r"(?:да[, ]+он|да[, ]+именно\s+он|именно\s+он)[.!? ]*", re.I)


@dataclass(frozen=True)
class Quote:
    asset: str
    currency: str

    def question(self) -> str:
        name = f" ({NAMES[self.asset]})" if self.asset in NAMES else " (кандидат в тикер)"
        return (
            f"Найди текущую цену актива {self.asset}{name} в {self.currency}. "
            "Проверь идентичность актива; при неоднозначном тикере не подменяй его другим. "
            "Если финансовый поток не знает актив, найди актуальную котировку в веб-источниках. "
            "Укажи время котировки, только если оно известно источнику."
        )


def currency(text: str) -> str | None:
    found = [
        code for code, pattern in CURRENCIES.items() if re.search(rf"\b(?:{pattern})\b", text, re.I)
    ]
    return found[0] if len(found) == 1 else None


def asset(text: str) -> str | None:
    found = {
        code for code, pattern in ALIASES.items() if re.search(rf"\b(?:{pattern})\b", text, re.I)
    }
    for match in re.finditer(
        r"\$([A-Za-z][A-Za-z0-9]{1,9})\b|\b([A-Z][A-Z0-9]{1,9})\b|\bтокен\w*\s+([A-Za-z][A-Za-z0-9]{1,9})\b",
        text,
    ):
        value = next(value for value in match.groups() if value).upper()
        if value not in CURRENCIES and not any(
            re.fullmatch(pattern, value, re.I) for pattern in ALIASES.values()
        ):
            found.add(value)
    return next(iter(found)) if len(found) == 1 else None


def advance(text: str, previous: Quote | None) -> Quote | None:
    """A new subject breaks inheritance. Bare acknowledgement is handled separately."""
    if (
        sum(bool(re.search(rf"\b(?:{pattern})\b", text, re.I)) for pattern in CURRENCIES.values())
        > 1
    ):
        return None
    if len(text) > 250 or re.search(
        r"\b(?:вчера|позавчера|завтра|назад|прошл\w*|будущ\w*|прогноз\w*|"
        r"почему|зачем|объясни\w*|если|сравни\w*|выраст\w*|раст[её]т|упад\w*|будет)\b|\b20\d{2}\b",
        text,
        re.I,
    ):
        return None
    symbol = asset(text)
    if symbol and PRICE.search(text):
        if not re.search(r"\b(?:цен\w*|котиров\w*|курс\w*|поч[её]м|стоит|стоимость)\b", text, re.I):
            # 'How many bitcoins does Satoshi own?' is not a price request.
            if not re.fullmatch(
                r"\s*(?:Заря[, ]+)?(?:скок\w*|сколько)\s+\$?\w+\s+"
                r"(?:ща|щас|сейчас|сегодня)(?:\s+в\s+\w+)?[?!. ]*",
                text,
                re.I,
            ):
                return None
        return Quote(symbol, currency(text) or "USD")
    if previous:
        short = re.fullmatch(r"\s*(?:а|а\s+что\s+насч[её]т)\s+(.{1,40}?)\s*[?!.]?\s*", text, re.I)
        if short:
            fragment = short[1].rstrip("?!.").strip()
            symbol = asset(fragment)
            # Restrict abbreviated switches to an asset token/name, not an arbitrary sentence.
            if symbol and re.fullmatch(r"(?:токен\w*\s+)?\$?[\w]{2,12}", fragment, re.I):
                return Quote(symbol, previous.currency)
            if re.fullmatch(r"в\s+\w+", fragment, re.I) and (code := currency(fragment)):
                return Quote(previous.asset, code)
    return None


async def resolve_quote(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    message: dict[str, Any],
    selected: dict[str, Any],
) -> Quote | None:
    text = str(message.get("text") or "")
    # Articles, attachments and forwarded material retain their normal research path.
    if (
        message.get("forward_origin")
        or message.get("forward_date")
        or any(message.get(key) for key in ("photo", "video", "animation", "voice", "video_note"))
        or re.search(r"https?://", text)
        or any(e.get("type") == "text_link" for e in (message.get("entities") or []))
    ):
        return None
    if direct := advance(text, None):
        return direct
    refs = selected.get("manifest", [])
    if (
        not message.get("reply_to_message")
        or selected.get("unavailable")
        or not refs
        or not await valid(conn, bot, chat, grant, refs)
    ):
        return None
    previous = None
    for ref in reversed(refs):
        source = await accepted(conn, bot, chat, grant, ref["message_id"])
        if not source or source[0] != ref["event_id"]:
            return None
        original = source[1]
        if (
            topic_id(original) != topic_id(message)
            or original.get("forward_origin")
            or original.get("forward_date")
        ):
            return None
        previous = advance(str(original.get("text") or ""), previous)
    if previous and CONFIRM.fullmatch(text):
        # Legacy clarification: accept only an actual sent identity question, never arbitrary yes.
        sent = await one(
            conn,
            "SELECT r.response FROM outbox o JOIN dialogue_runs r ON r.job_id=o.job_id "
            "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.message_id=? "
            "AND o.state='sent' AND r.state='completed' AND r.mode='live' "
            "AND COALESCE(r.thread_id,0)=?",
            (bot, chat, grant, message["reply_to_message"]["message_id"], topic_id(message) or 0),
        )
        name = NAMES.get(previous.asset)
        if (
            sent
            and name
            and re.match(
                rf"\s*{re.escape(previous.asset)}\s*[-—–,:]?\s*(?:это\s+)?токен\s+{re.escape(name)}\s*\?",
                sent[0] or "",
                re.I,
            )
        ):
            return previous
        return None
    return advance(text, previous)
