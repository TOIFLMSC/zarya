"""Pure dialogue rules; Telegram content never becomes developer instructions."""

import json
import re
from typing import Any

from zarya.models import Settings
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle


def message_text(message: dict[str, Any]) -> str:
    text = str(message.get("text") or message.get("caption") or "")
    media = [
        kind
        for kind in ("photo", "voice", "video", "video_note", "sticker", "animation")
        if message.get(kind)
    ]
    if media:
        text += "\n[Вложение: " + ", ".join(media) + "; содержимое пока не распознано]"
    return text[:4000]


def topic_id(message: dict[str, Any]) -> int | None:
    # Non-forum supergroup replies also carry a thread ID. It is not a forum topic.
    value = message.get("message_thread_id")
    return int(value) if value is not None and message.get("chat", {}).get("is_forum") else None


def direct_trigger(message: dict[str, Any], bot_id: str, username: str, name: str) -> str:
    if message.get("from", {}).get("is_bot") or message.get("sender_chat"):
        return "bot_or_channel"
    if message.get("chat", {}).get("type") == "private":
        return "private" if message_text(message) else "no_text"
    if message.get("forward_origin") or message.get("forward_date"):
        return "forwarded"
    reply = message.get("reply_to_message") or {}
    if str(reply.get("from", {}).get("id")) == bot_id:
        return "reply"
    text = str(message.get("text") or message.get("caption") or "")
    # Telegram entities use UTF-16 offsets, not Python codepoint offsets.
    encoded = text.encode("utf-16-le")
    for entity in message.get("entities") or message.get("caption_entities") or []:
        if entity.get("type") == "mention":
            start, end = entity["offset"] * 2, (entity["offset"] + entity["length"]) * 2
            if (
                encoded[start:end].decode("utf-16-le", errors="replace").casefold()
                == ("@" + username).casefold()
            ):
                return "mention"
        if entity.get("type") == "text_mention" and str(entity.get("user", {}).get("id")) == bot_id:
            return "mention"
    if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE):
        return "name"
    return "ambient"


def answer_mode(text: str) -> str:
    """Local length hint; the model still decides what the question means."""
    text = text.casefold()
    detail = r"подробн\w*|детальн\w*|разв[её]рнут\w*|больше деталей|по шагам"
    if re.search(detail, text) and not re.search(
        r"(?:не|без|не надо|не нужно|не стоит)\s+(?:очень\s+)?(?:" + detail + r")", text
    ):
        return "detailed"
    if len(text) > 400 or re.search(r"https?://|www\.|\b(?:стать\w*|пост\w*)\b", text):
        return "analysis"
    return "brief"


def max_parts(mode: str) -> int:
    return {"brief": 2, "analysis": 3, "detailed": 4}[mode]


def chunks(
    text: str,
    limit: int = 4,
    *,
    split_paragraphs: bool | None = None,
    preferred_chars: int = 1600,
) -> list[str]:
    """Prefer paragraphs, then sentence boundaries; merge excess parts without truncation."""
    text = text.strip()[:8000]
    if (
        split_paragraphs is None
        and limit == 2
        and text
        and len(text.encode("utf-16-le")) // 2 <= 600
    ):
        return [text]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if split_paragraphs is False:
        combined: list[str] = []
        for paragraph in paragraphs:
            if combined and len(combined[-1] + "\n\n" + paragraph) <= preferred_chars:
                combined[-1] += "\n\n" + paragraph
            else:
                combined.append(paragraph)
        paragraphs = combined
    piece_limit = 1800 if split_paragraphs is None else preferred_chars
    join_limit = 1600 if split_paragraphs is None else preferred_chars
    result: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if split_paragraphs is False and len(paragraph) <= preferred_chars:
            result.append(paragraph)
            continue
        for piece in re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z])", paragraph):
            while len(piece) > piece_limit:
                cut = piece.rfind(" ", 0, piece_limit)
                cut = cut if cut > piece_limit // 2 else piece_limit
                for link in re.finditer(r"https?://[^\s]+", piece):
                    if link.start() < cut < link.end():
                        cut = link.start() if link.start() else link.end()
                        break
                if current:
                    result.append(current)
                    current = ""
                result.append(piece[:cut].strip())
                piece = piece[cut:].strip()
            joined = (current + " " + piece).strip()
            if len(joined) > join_limit:
                result.append(current)
                current = piece
            else:
                current = joined
        if current:
            result.append(current)
            current = ""
    if current:
        result.append(current)
    if len(result) > limit:
        # Preserve paragraph boundaries by merging neighbouring parts within Telegram's cap.
        while len(result) > limit:
            candidates = [
                (len(a) + len(b), i)
                for i, (a, b) in enumerate(zip(result, result[1:], strict=False))
                if len((a + "\n\n" + b).encode("utf-16-le")) // 2 <= 4096
            ]
            if not candidates:
                # Uneven paragraphs can block a merge even when the full text fits.
                # Repartition at whitespace within Telegram's UTF-16 limit instead.
                result = []
                remaining = text
                while remaining:
                    cut = min(len(remaining), 4096)
                    while len(remaining[:cut].encode("utf-16-le")) // 2 > 4096:
                        cut -= 1
                    if cut < len(remaining):
                        boundary = remaining.rfind(" ", 0, cut)
                        # Do not waste capacity needed to stay within the part budget.
                        tail_units = len(remaining[boundary:].encode("utf-16-le")) // 2
                        if boundary > 0 and tail_units <= (limit - len(result) - 1) * 4096:
                            cut = boundary
                    result.append(remaining[:cut].strip())
                    remaining = remaining[cut:].strip()
                break
            _, index = min(candidates)
            result[index : index + 2] = [result[index] + "\n\n" + result[index + 1]]
    return [part for part in result if part]


def delivery_chunks(text: str, snapshot: dict[str, Any]) -> list[str]:
    mode = snapshot.get("answer_mode")
    state = snapshot.get("behavior", {})
    plan = state.get("plan", {})
    if plan.get("action") in {"reaction", "silent"}:
        return []
    if plan.get("tone") == "angry" and state.get("expressiveness") != "restrained":
        return chunks(
            text, state.get("emotional_max_parts", 4), split_paragraphs=True, preferred_chars=180
        )
    return chunks(text, max_parts(mode) if mode else 4, **snapshot.get("delivery_layout", {}))


def writing_style(draw: float) -> str:
    """One conversational style hint per answer, without a quota of deliberate mistakes."""
    return "relaxed" if draw < 0.65 else "rushed"


def instructions(
    settings: Settings,
    owner: bool,
    mode: str = "brief",
    photos: bool = False,
    style: str = "relaxed",
    sources_requested: bool = False,
    prompts: PromptBundle = DEFAULT_PROMPTS,
) -> str:
    from zarya.response_links import source_policy

    return source_policy(sources_requested, prompts) + (
        prompts.text("dialogue.identity_prefix")
        + str(settings.display_name)
        + prompts.text("dialogue.identity")
        + (
            prompts.text("dialogue.photo_available")
            if photos
            else prompts.text("dialogue.photo_unavailable")
        )
        + prompts.text("dialogue.context")
        + prompts.text("dialogue.style_general")
        + {
            "brief": prompts.text("dialogue.length_brief"),
            "analysis": prompts.text("dialogue.length_analysis"),
            "detailed": prompts.text("dialogue.length_detailed"),
        }[mode]
        + (
            prompts.text("dialogue.tone_label")
            + str(settings.tone)
            + prompts.text("dialogue.persona_label")
            + str(settings.persona)
            + "\n"
        )
        + prompts.text("dialogue.voice")
        + {
            "clean": prompts.text("dialogue.style_clean"),
            "relaxed": prompts.text("dialogue.style_relaxed"),
            "rushed": prompts.text("dialogue.style_rushed"),
        }[style]
        + (
            prompts.text("dialogue.owner")
            + str(settings.owner_name or "по имени в сообщении")
            + ".\n"
            if owner
            else prompts.text("dialogue.other")
        )
        + prompts.text("dialogue.discretion")
    )


def model_input(
    context: list[dict[str, Any]], incoming: dict[str, Any], memory: dict[str, Any] | None = None
) -> str:
    return json.dumps(
        {"recent_chat": context, "current_message": incoming, "memory": memory or {}},
        ensure_ascii=False,
    )
