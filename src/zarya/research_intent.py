"""Local research routing; no model calls or chat-memory input."""

import re
from typing import Any, Literal

Kind = Literal["request", "none"]
SUBJECTS = {
    "Мосбиржа": r"мо[еэ]кс\w*|мосбирж\w*|московск\w+\s+бирж\w+|moex|imoex",
    "индекс РТС": r"ртс|rtsi",
    "Bitcoin": r"btc|bitcoin|биткоин\w*|биток|битка",
    "Ethereum": r"eth|ethereum|эфир(?:а|иум)?",
    "Hyperliquid (HYPE)": r"hype|hyperliquid|хайп",
    "Solana": r"sol|solana|солан[аыу]",
    "USDT": r"usdt|tether|юсдт",
    "доллар США": r"usd|доллар\w*|бакс\w*",
    "евро": r"eur|евро",
    "нефть": r"нефт\w*|brent|wti",
    "золото": r"золот\w*|gold|xau",
    "Сбербанк": r"сбер\w*|sber",
    "Газпром": r"газпром\w*|gazp",
    "NASDAQ": r"nasdaq|насдак\w*",
    "S&P 500": r"s&p\s*500|spx",
}
# SUBJECTS only normalizes an already validated financial reply, never gates search.
DYNAMICS = re.compile(
    r"\b(?:раст[её]м|раст[её]т|растут|рос\w*|рост\w*|пад[аёе]\w*|упал\w*|"
    r"сниж\w*|подорож\w*|подешев\w*|отскок\w*|ралли|вырос\w*|летим|"
    r"прибав\w*|просел\w*|просад\w*|изменил\w*)\b",
    re.I,
)
QUESTION = re.compile(
    r"[?？]|\b(?:как\w*|скок\w*|сколько|почему|зачем|что|че|чё|кто|где|куда|"
    r"откуда|когда|чем|чего|кому|каков\w*|ли|поч[её]м)\b",
    re.I,
)
REQUEST = re.compile(
    r"\b(?:расскажи\w*|объясни\w*|поясни\w*|разбери\w*|разберись|подскажи\w*|"
    r"помоги\w*|сравни\w*|сопоставь\w*|сверь\w*|оцени\w*|посоветуй\w*|"
    r"подбери\w*|проанализируй\w*|"
    r"проверь\w*|провер[иь]\w*|найди\w*|поищи\w*|загугл\w*|узнай\w*|"
    r"исследуй\w*|"
    r"посмотри\w*|глянь\w*|гляни\w*|подтверди\w*|опровергни\w*|факт.?чек|"
    r"хочу\s+(?:узнать|понять|разобраться)|нуж(?:ен|на|но|ны)\s+\w+)\b|"
    r"^\s*(?:Заря[,!:\s]+)?(?:цен\w*|курс\w*|котиров\w*|стоимость|"
    r"информаци\w*|справк\w*|новост\w*)\s+\S+",
    re.I,
)
OPT_OUT = re.compile(
    r"\b(?:не\s+(?:(?:надо|нужно)\s+)?(?:ищи\w*|искать|гугли\w*|гуглить|"
    r"проверяй\w*|проверять|открывай\w*|открывать|загружай\w*|загружать)|"
    r"без\s+(?:интернет\w*|инет\w*|поиска)|по\s+памяти|"
    r"не\s+ходи\s+в\s+(?:инет\w*|интернет\w*))\b",
    re.I,
)
SOURCE_REQUEST = re.compile(
    r"\b(?:первоисточник\w*|исходн\w+\s+документ\w*|"
    r"официальн\w+\s+(?:разъяснен|публикац|источник|документ)\w*|"
    r"в\s+каком\s+(?:документе|законе|постановлении))\b",
    re.I,
)
DIRECT_SOURCE_REQUEST = re.compile(
    r"\b(?:найди|дай|скинь|пришли|кинь)(?:те)?\s+(?:(?:мне|нам|пожалуйста|прямую)\s+)*"
    r"(?:ссылк\w+\s+на|первоисточник\w*)\b",
    re.I,
)
DIRECT_STUDY_REQUEST = re.compile(
    r"\b(?:(?:сделай|проведи|начни)(?:те)?\s+"
    r"(?:(?:небольш\w*|кратк\w*)\s+)?(?:мини[-\s])?(?:рес[её]рч\w*|исследовани\w*)|"
    r"(?:проверь|сверь|посмотри|собери|сопоставь)\s+"
    r"(?:(?:ещ[её]|сразу|хотя\s+бы)\s+)?несколько\s+источник\w*)\b",
    re.I,
)
DEEP = re.compile(
    r"\b(?:сравни\w*|исследуй\w*|рес[её]рч\w*|исследовани[еяю]|"
    r"(?:разбери|объясни|собери)\w*\s+(?:подробн\w*|по\s+пунктам)|"
    r"(?:по|из)\s+(?:нескольким|нескольких)\s+источник\w*)\b",
    re.I,
)


def speaker_text(message: dict[str, Any]) -> str:
    """Ignore quoted/code/forwarded instructions; preserve UTF-16 entity offsets."""
    if message.get("forward_origin") or message.get("forward_date"):
        return ""
    text = str(message.get("text") or message.get("caption") or "")[:4000]
    entities = message.get("entities") if message.get("text") else message.get("caption_entities")
    encoded = bytearray(text.encode("utf-16-le"))
    for entity in entities or []:
        if entity.get("type") in {"blockquote", "expandable_blockquote", "pre", "code"}:
            start = max(0, int(entity.get("offset", 0))) * 2
            end = min(len(encoded), start + max(0, int(entity.get("length", 0))) * 2)
            if start < end:
                encoded[start:end] = b" \x00" * ((end - start) // 2)
    return re.sub(
        r"```[\s\S]*?```|`[^`]*`|«[^»]*»|“[^”]*”|\"[^\"]*\"|(?m:^>.*$)",
        " ",
        encoded.decode("utf-16-le", errors="replace"),
    )


def blocks_external(message: dict[str, Any]) -> bool:
    return bool(OPT_OUT.search(speaker_text(message)))


def subject(text: str) -> str | None:
    """Only a single public market label may be inherited, never free-form chat text."""
    found = [
        name for name, pattern in SUBJECTS.items() if re.search(rf"\b(?:{pattern})\b", text, re.I)
    ]
    return found[0] if len(found) == 1 else None


def ordinary_question(question: str) -> bool:
    # Preserve the original meaning instead of rewriting into a current price in USD.
    # This guard applies to quote normalization only, not to the search policy.
    if re.match(r"\s*(?:Заря[,!:\s]+)?(?:переведи|сократи|перепиши)\b", question, re.I):
        return True
    if re.search(
        r"\b(?:у\s+(?:меня|тебя|нас|него|не[её])|мой|моя|мои|мо[её]|моего|моей|"
        r"моих|моим|мою|моему|мо[её]м|моими|тво\w*|наш\w*|вообще|"
        r"любишь|нравится|истори\w*)\b|\bчто\s+такое\b",
        question,
        re.I,
    ):
        return True
    return bool(
        re.search(
            r"\bпо\s+(?:курс\w*|цен\w*)\s+(?:выше|из|того|назван\w*|указан\w*)\b", question, re.I
        )
    )


def local_task(question: str) -> bool:
    """Whole local requests only; pronouns/history/forecast alone are never exclusions."""
    clean = re.sub(r"^\s*заря\b[,!:\s]*", "", question, flags=re.I).strip().rstrip("?!.")
    social = (
        r"(?:(?:а|ну)\s+)?(?:привет\w*|здравствуй\w*|спасибо\w*|благодарю|да|нет|"
        r"ага|угу|понятно|ясно|ок\w*|готово|доброе\s+утро|добрый\s+(?:день|вечер)|"
        r"как\s+(?:ты|дела)|как\s+твои\s+дела|какие\s+(?:у\s+тебя\s+новости|новости\s+у\s+тебя))"
        r"(?:[,!\s]+(?:да|понятно|тебе|тебя|заря))*[\s❤️❤😊🙂👍!]*"
    )
    if all(
        re.fullmatch(social, part.strip(), re.I)
        for part in re.split(r"[,!]+", clean)
        if part.strip()
    ):
        return True
    if re.fullmatch(
        r"(?:ты\s+)?(?:меня\s+)?(?:любишь|нравится\s+ли\s+тебе)(?:\s+[^?\n,;]+)?|"
        r"как\s+ты\s+(?:ко\s+мне|к\s+нам)\s+относишься|"
        r"что\s+ты\s+(?:обо|про)\s+мне\s+(?:помнишь|знаешь)|"
        r"что\s+ты\s+(?:помнишь|знаешь)\s+(?:обо|про)\s+мне|"
        r"(?:вспомни|напомни)[,\s]+(?:где\s+я\s+(?:работаю|живу)|"
        r"как\s+меня\s+зовут|что\s+я\s+люблю|чем\s+я\s+увлекаюсь)|"
        r"(?:помнишь[,\s]+)?как\s+меня\s+зовут|кто\s+я|"
        r"(?:вспомни|напомни)\s+(?:мо[ийюяеё]\w*|наши)\s+[^?\n,;]+|"
        r"сколько\s+[^?\n,;]+\s+у\s+меня|сколько\s+стоит\s+тво[йяеё]\s+[^?\n,;]+",
        clean,
        re.I,
    ):
        return True
    rate = re.search(
        r"\bпо\s+(?:курс\w*|цен\w*)\s+(?:выше|из\s+сообщения(?:\s+выше)?|назван\w*|указан\w*)\b",
        clean,
        re.I,
    )
    if rate and re.fullmatch(r"[,\s]*(?:пожалуйста)?", clean[rate.end() :], re.I):
        operation = re.match(
            r"(?:сколько|скок\w*|посчитай|вычисли|пересчитай|умножь)\s+", clean, re.I
        )
        if operation and not (
            QUESTION.search(clean[operation.end() : rate.start()])
            or REQUEST.search(clean[operation.end() : rate.start()])
        ):
            return True
    arithmetic = re.sub(
        r"^(?:(?:посчитай|вычисли|умножь|сколько\s+(?:будет|получится))\s+)", "", clean, flags=re.I
    )
    if re.fullmatch(r"[\d.,\s()+*/×÷xX−-]+", arithmetic) and re.search(
        r"\d\s*[+*/×÷xX−-]", arithmetic
    ):
        return True
    # A colon/newline explicitly separates the transformation's supplied data.
    if re.match(
        r"(?:переведи|сократи|перепиши)\s+(?:(?:этот|эту|это)\s+)?(?:текст|фразу|абзац)"
        r"(?:[ \t]+на[ \t]+\w+)?[ \t]*(?::|\n)",
        clean,
        re.I,
    ):
        return True
    if re.fullmatch(
        r"(?:а\s+)?(?:почему|как\s+так|подробнее|приведи\s+(?:ещ[её]\s+)?пример|"
        r"сколько\s+(?:дней|процентов)\s+(?:уже\s+)?(?:раст[её]м|раст[её]т|прибавил[аи]?))|"
        r"(?:что|кто)\s+(?:тут|здесь|на\s+(?:этом\s+)?(?:фото|видео|стикере|скрине))"
        r"(?:\s+(?:написано|изображено|показано))?|"
        r"(?:переведи|сократи|перепиши)\s+(?:(?:этот|эту|это)\s+)?(?:текст|фразу|абзац)"
        r"(?:\s+на\s+\w+)?[,\s:]*(?:пожалуйста)?|"
        r"(?:а|а\s+что\s+насч[её]т)\s+\$?\w{2,12}|"
        r"(?:кратко\s+)?(?:поясни|объясни|перескажи|суммируй)\s+"
        r"(?:(?:этот|эту|переданный|переданную|выбранный|выбранную)\s+)?(?:пост|статью)",
        clean,
        re.I,
    ):
        return True
    content = re.search(
        r"(?:(?:расскажи|поясни|объясни)[,\s]+)?(?:о\s+ч[её]м\s+(?:(?:это|этот)\s+)?"
        r"(?:видео|ролик)|(?:что|кто)\s+(?:тут|здесь|на\s+(?:этом\s+)?"
        r"(?:фото|видео|стикере|скрине))(?:\s+(?:написано|изображено|показано))?)$",
        clean,
        re.I,
    )
    if content and not (
        QUESTION.search(clean[: content.start()]) or REQUEST.search(clean[: content.start()])
    ):
        return True
    return False


def search_kind(question: str) -> Kind:
    """Questions and information requests search by default, regardless of subject."""
    question = speaker_text({"text": question})
    # URL query strings are source data, not questions by the speaker.
    question = re.sub(r"https?://[^\s<>]+", " ", question)
    if OPT_OUT.search(question) or local_task(question):
        return "none"
    if (
        QUESTION.search(question)
        or REQUEST.search(question)
        or DIRECT_SOURCE_REQUEST.search(question)
        or DIRECT_STUDY_REQUEST.search(question)
    ):
        return "request"
    return "none"


def research_profile(speaker_question: str) -> str:
    speaker_question = re.sub(r"https?://[^\s<>]+", " ", speaker_question)
    if DIRECT_SOURCE_REQUEST.search(speaker_question) or SOURCE_REQUEST.search(speaker_question):
        return "source"
    if DIRECT_STUDY_REQUEST.search(speaker_question) or DEEP.search(speaker_question):
        return "study"
    return "answer"


def market_followup(text: str) -> bool:
    clean = speaker_text({"text": text})
    return bool(
        len(clean) <= 250
        and QUESTION.search(clean)
        and DYNAMICS.search(clean)
        and not any(re.search(rf"\b(?:{pattern})\b", clean, re.I) for pattern in SUBJECTS.values())
        and not OPT_OUT.search(clean)
    )
