"""Build same-chat Telegram permalinks from retained, revision-bound messages."""

import json
import re
import time
from typing import Any

import aiosqlite

from zarya.dialogue_logic import explicit_reply
from zarya.response_links import URL_TARGET, without_links
from zarya.source_material import accepted, one

POLICY = (
    "\nПользователь просит ссылку на конкретную реплику в этом чате. "
    "В message_link_candidates найди точное исходное сообщение и вставь его marker как есть. "
    "Признак direct_reply обозначает реплику, на которую пользователь ответил в Telegram. "
    "Если просьба касается слов другого участника из quoted_answer, выбирай его оригинал, "
    "не ответ Зари и не похожую реплику. Для явной просьбы о ссылке на ответ Зари "
    "можно выбрать её ответ. Вставляй маркер обычным текстом без Markdown. "
    "Приложение само подставит ссылку или точную фразу для поиска, если ссылки нет. "
    "При нескольких похожих репликах уточни, какую ищут. Если точного совпадения нет, "
    "скажи об этом и предложи поиск по характерной фразе, если она известна. "
    "Не пиши URL и технические номера сообщений. Если есть схема плана поведения, "
    "выбери действие text."
)
FALLBACK = "Точную ссылку найти не удалось. Попробуй поиск по чату."
MARKER = re.compile(r"\[M\d+\]")
TECHNICAL_ID = re.compile(
    r"(?:\s*[,—–]\s*)?(?:\bэто\s+)?"
    r"\b(?:message_id|id|сообщени[еяюи]|номер[уа]?(?:\s+сообщения)?|вот)"
    r"\s*[:№#—–-]?\s*[`*_]*\d+[`*_]*|(?<!\w)[№#]\s*\d+",
    re.IGNORECASE,
)
BARE_ID = re.compile(r'^[\s`*_«“"]*\d+[\s`*_.!?»”"]*$')
QUOTED_TEXT = re.compile(r'(«[^»\n]*»|“[^”\n]*”|„[^“”\n]*[“”]|"[^"\n]*")')
TELEGRAM_SCHEME = re.compile(r"tg://[^\s<>]+", re.IGNORECASE)
MAX_CANDIDATES = 64


def permalink(chat: dict[str, Any], message_id: int) -> str | None:
    chat_id = chat.get("id")
    if (
        chat.get("type") != "supergroup"
        or type(chat_id) is not int
        or not -1_997_852_516_352 <= chat_id < -1_000_000_000_000
        or message_id <= 0
    ):
        return None
    return f"https://t.me/c/{-chat_id - 1_000_000_000_000}/{message_id}"


async def prepare(
    conn: aiosqlite.Connection,
    bot: str,
    chat: str,
    grant: int,
    message: dict[str, Any],
    manifest: list[dict[str, Any]],
    refs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    from zarya.memory_context import valid_snapshot

    target = explicit_reply(message).get("message_id")
    parent: dict[str, Any] = {}
    quoted_answer = ""
    if target:
        sent = await one(
            conn,
            "SELECT r.id,o.id,r.snapshot,m.text FROM outbox o "
            "JOIN dialogue_runs r ON r.job_id=o.job_id JOIN recent_messages m "
            "ON m.bot_id=o.bot_id AND m.chat_id=o.chat_id AND m.message_id=o.message_id "
            "WHERE o.bot_id=? AND o.chat_id=? AND o.access_version=? AND o.message_id=? "
            "AND o.state='sent' AND r.mode='live' AND r.state='completed' "
            "AND m.role='assistant' AND m.received_at>?",
            (bot, chat, grant, target, time.time() - 90 * 86400),
        )
        if sent:
            ref = {"run_id": sent[0], "outbox_id": sent[1]}
            if await valid_snapshot(
                conn,
                bot,
                {
                    "retrieval_chat_id": chat,
                    "source_access_version": grant,
                    "context_refs": {"assistants": [ref]},
                },
            ):
                parent, quoted_answer = json.loads(sent[2]), sent[3]
                if ref not in refs["assistants"]:
                    refs["assistants"].append(ref)

    candidates: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}

    def add(mid: int, author: str, text: str) -> None:
        marker = f"[M{len(candidates) + 1}]"
        snippet = text[:500]
        candidates.append(
            {"marker": marker, "author": author, "text": snippet, "direct_reply": mid == target}
        )
        link = permalink(message["chat"], mid)
        if not link:
            query = without_links(TELEGRAM_SCHEME.sub("", " ".join(snippet.split())))[:100]
            link = (
                f"Прямой ссылки здесь нет. В поиске по чату набери «{query}»."
                if query and query != "[Вложение]"
                else FALLBACK
            )
        replacements[marker] = link

    if type(target) is int and quoted_answer:
        add(target, "Заря", quoted_answer)
    revisions = {r["message_id"]: r["event_id"] for r in manifest}
    # Prefer originals that were available when the quoted answer was generated.
    for group in (
        parent.get("source_manifest", []),
        parent.get("topic_context", []),
        reversed(parent.get("context_refs", {}).get("humans", [])),
        reversed(refs["humans"]),
    ):
        for ref in group:
            revisions.setdefault(ref["message_id"], ref["event_id"])
    if target in revisions:
        revisions = {target: revisions[target], **revisions}
    for mid, event in revisions.items():
        if len(candidates) >= MAX_CANDIDATES:
            break
        if mid >= message["message_id"]:
            continue
        source = await accepted(conn, bot, chat, grant, mid)
        if not source or source[0] != event:
            continue
        original = source[1]
        author = original.get("from") or original.get("sender_chat") or {}
        text = str(original.get("text") or original.get("caption") or "[Вложение]")
        add(mid, str(author.get("first_name") or author.get("title") or "Участник")[:80], text)
        ref = {"message_id": mid, "event_id": event}
        if ref not in manifest:
            manifest.append(ref)
    return {
        "message_link_candidates": candidates,
        "quoted_answer": quoted_answer[:2000],
    }, replacements


def render(text: str, replacements: dict[str, str]) -> str:
    cleaned = without_links(TELEGRAM_SCHEME.sub("", text))
    selected = dict.fromkeys(MARKER.findall(cleaned))
    replies = [replacements[marker] for marker in selected if marker in replacements]
    if replies:
        return "\n".join(dict.fromkeys(replies))
    if selected or URL_TARGET.search(text) or TELEGRAM_SCHEME.search(text):
        return FALLBACK
    parts = QUOTED_TEXT.split(cleaned)
    if BARE_ID.fullmatch(cleaned) or any(BARE_ID.fullmatch(part) for part in parts[1::2]):
        return FALLBACK
    if any(TECHNICAL_ID.search(part) for part in parts[::2]):
        if len(parts) == 1:
            return FALLBACK
        parts[::2] = [TECHNICAL_ID.sub("", part) for part in parts[::2]]
        return "".join(parts).strip(" \t\n.,:;—–()")
    return cleaned or FALLBACK
