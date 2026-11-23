"""Show links only when the current speaker explicitly asks for them."""

import re
from typing import Any

from zarya.prompts import DEFAULT_PROMPTS, PromptBundle
from zarya.research_intent import DIRECT_SOURCE_REQUEST

NOUN = r"(?:первоисточник\w*|источник\w*|ссыл(?:к\w*|ок)|пруф\w*|линк\w*|url|адрес\w*)"
POSITIVE = re.compile(
    rf"{DIRECT_SOURCE_REQUEST.pattern}|"
    rf"\b(?:скинь\w*|кинь\w*|пришли\w*|дай|дайте|отправь\w*|покажи\w*|приложи\w*|"
    rf"приведи|укажи\w*|добавь\w*|поделись|найди|хочу|нуж(?:ен|на|ны|но)|где)\b"
    rf"[^\n.!?]{{0,65}}\b{NOUN}\b|\b(?:send|show|provide|give|include)\b"
    rf"[^\n.!?]{{0,45}}\b(?:sources|links|references|citations|urls)\b|"
    r"\bоткуда\s+(?:(?:эта|такая)\s+)?(?:инфа|информация|данные)\b|"
    r"\bна\s+что\s+(?:ты\s+)?ссылаешься\b|"
    rf"\bможно\s+(?:мне\s+)?{NOUN}\b|"
    rf"\b{NOUN}\s+(?:дашь|скинешь|пришлёшь|пришлешь|покажешь)\b",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    rf"\b(?:без|не\s+(?:дай|дава\w*|дайте|добав\w*|пришли\w*|присыла\w*|"
    rf"отправ\w*|пока\w*|скин\w*|кин\w*|скиды\w*|прила\w*|приводи\w*|указ\w*|"
    rf"надо|хочу|нуж(?:ен|на|ны|но)))"
    rf"\s+(?:(?:никаких|мне|нам|сюда|пожалуйста|больше|вообще|пока)\s+)*{NOUN}\b|"
    rf"\b{NOUN}\s+не\s+(?:нуж(?:ен|на|ны|но)|надо)\b|"
    r"\b(?:do\s+not|don['’]?t|without|no)\s+(?:(?:include|give|show|send|provide)\s+)?"
    r"(?:sources|links|references|citations|urls)\b",
    re.IGNORECASE,
)
URL_TARGET = re.compile(
    r"(?:https?://|www\.|t\.me/)[^\s<>]+|"
    r"(?<![\w@])(?:[a-zа-я0-9][a-zа-я0-9-]*\.)+"
    r"(?:com|org|net|ru|io|dev|ai|co|uk|me|info|edu|gov|рф)\b(?:/[^\s<>]*)?",
    re.IGNORECASE,
)


def wants_sources(question: str) -> bool:
    # Quoted posts/examples are not instructions by the current speaker.
    question = re.sub(r"```[\s\S]*?```|«[^»]*»|“[^”]*”|\"[^\"]*\"|(?m:^>.*$)", "", question)
    negatives = list(NEGATIVE.finditer(question))
    signals = [
        (m.start(), True)
        for m in POSITIVE.finditer(question)
        if not any(n.start() < m.end() and m.start() < n.end() for n in negatives)
    ]
    signals += [(m.start(), False) for m in negatives]
    if signals:
        return max(signals)[1]
    return bool(
        re.fullmatch(
            rf"\s*(?:Заря[,!]?\s+)?(?:а\s+)?(?:{NOUN}|sources|links|references)\s*[?!]*\s*",
            question,
            re.IGNORECASE,
        )
    )


def message_requests_sources(message: dict[str, Any]) -> bool:
    if message.get("forward_origin") or message.get("forward_date"):
        return False
    text = str(message.get("text") or message.get("caption") or "")
    entities = message.get("entities") if message.get("text") else message.get("caption_entities")
    encoded = bytearray(text.encode("utf-16-le"))
    for entity in entities or []:
        if entity.get("type") in {"blockquote", "expandable_blockquote", "pre", "code"}:
            start = max(0, int(entity.get("offset", 0))) * 2
            end = min(len(encoded), start + max(0, int(entity.get("length", 0))) * 2)
            if start < end:
                encoded[start:end] = b" \x00" * ((end - start) // 2)
    return wants_sources(encoded.decode("utf-16-le", errors="replace"))


def requested(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot.get(
            "sources_requested",
            wants_sources(
                snapshot.get("question_text", snapshot.get("incoming", {}).get("text", ""))
            ),
        )
    )


def without_links(text: str) -> str:
    """Keep prose and Markdown labels; strip source targets and citation markers."""
    text = re.sub(r"\[([^\]\n]+)\]\(\s*<?[^\s)]+>?\s*\)", r"\1", text)
    text = re.sub(r"<a\b[^>]*href=[^>]*>(.*?)</a>", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\[S\d+\]", "", text)

    def remove_url(match: re.Match[str]) -> str:
        url, suffix = match.group(), ""
        while url:
            last = url[-1]
            if last in ".,!?;:" or (
                last in ")]}" and url.count(last) > url.count({")": "(", "]": "[", "}": "{"}[last])
            ):
                suffix = last + suffix
                url = url[:-1]
            else:
                break
        return suffix

    text = URL_TARGET.sub(remove_url, text)
    text = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", text)
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,!?;:])", r"\1", text)
    return text.strip()


def display_text(text: str, snapshot: dict[str, Any]) -> str:
    return text if requested(snapshot) else without_links(text)


def source_policy(show: bool, prompts: PromptBundle = DEFAULT_PROMPTS) -> str:
    research_status = (
        "Если research.state означает ошибку или незавершённую проверку, "
        "скажи, что проверку не удалось завершить, а не что нужной информации нет. "
        "Технический сбой поиска не доказывает отсутствие источника. "
    )
    if show:
        return (
            research_status
            + prompts.text("links.requested")
            + (
                "Для research используй маркеры [S<id>] с точным полем id источника: "
                "например, id=33 означает [S33], даже если источник первый в списке. "
                "Выбирай id из analysis.claims.source_ids и research.sources с доступным url; "
                "не цитируй unavailable и не перенумеровывай источники. "
                "Приложение само подставит URL вместо маркера; не переписывай URL вручную. "
                "Если найденный первоисточник содержит нужное разъяснение в разделе или FAQ, "
                "дай ссылку на страницу и назови раздел; отдельный документ не обязателен. "
                "Недоступность исходной новости не отменяет подтверждённый первоисточник. "
            )
        )
    return research_status + prompts.text("links.not_requested")
