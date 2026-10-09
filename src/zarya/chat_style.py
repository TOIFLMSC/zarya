"""Conversational voice and narrowly scoped typography for generated replies."""

import re

# Exact material stays exact, including dashes inside URLs, quotes and code.
_EXACT = re.compile(
    r"(?P<ticks>`+)(?!`)[\s\S]*?(?:(?<!`)(?P=ticks)(?!`)|\Z)"
    r"|(?:https?://|www\.|t\.me/)[^\s<>]+"
    r"|(?<![\w@])(?:[a-zа-я0-9][a-zа-я0-9-]*\.)+"
    r"(?:com|org|net|ru|io|dev|ai|co|uk|me|info|edu|gov|рф)\b(?:/[^\s<>]*)?"
    r'|«[^»]*»|“[^”]*”|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|(?m:^[ \t]*>.*$)',
    re.IGNORECASE,
)


def chat_dashes(text: str) -> str:
    result = []
    start = 0
    for match in _EXACT.finditer(text):
        result.append(text[start : match.start()].replace("—", "-").replace("–", "-"))
        result.append(match.group())
        start = match.end()
    result.append(text[start:].replace("—", "-").replace("–", "-"))
    return "".join(result)
