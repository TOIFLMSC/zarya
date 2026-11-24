"""Extract visible Telegram text without reading embedded media or link targets."""

from typing import Any

CONTENT_LIMIT = 8000
NODE_LIMIT = 4096
DEPTH_LIMIT = 24
MEDIA_BLOCKS = {"animation", "audio", "document", "map", "photo", "video", "voice_note"}


def forwarded(message: dict[str, Any]) -> bool:
    return bool(message.get("forward_origin") or message.get("forward_date"))


def content_text(message: dict[str, Any], limit: int = CONTENT_LIMIT) -> str:
    return extract_content(message, limit)[0]


def extract_content(message: dict[str, Any], limit: int = CONTENT_LIMIT) -> tuple[str, bool]:
    """Return visible text and whether the rich payload has unread sections."""
    plain = message.get("text") or message.get("caption")
    if isinstance(plain, str) and plain.strip():
        return plain[:limit], len(plain) > limit
    rich = message.get("rich_message")
    if not isinstance(rich, dict) or not isinstance(rich.get("blocks"), list):
        return "", False
    parts: list[str] = []
    remaining = limit
    nodes = NODE_LIMIT
    incomplete = False

    def append(text: str) -> None:
        nonlocal remaining, incomplete
        incomplete |= len(text) > remaining
        piece = text[:remaining]
        parts.append(piece)
        remaining -= len(piece)

    def visit(value: Any, depth: int, separator: str = "") -> None:
        nonlocal nodes, incomplete
        if not remaining or not nodes or depth > DEPTH_LIMIT:
            incomplete = True
            return
        nodes -= 1
        if isinstance(value, str):
            append(value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if not remaining or not nodes:
                    incomplete = True
                    break
                if index and separator:
                    append(separator)
                visit(item, depth + 1, separator)
        elif isinstance(value, dict):
            kind = value.get("type")
            if kind == "buttons":
                return
            if isinstance(kind, str) and kind in MEDIA_BLOCKS:
                incomplete = True
            # Formatting wrappers store another RichText in text. Captions and
            # nested containers use only these fields, never URLs or file metadata.
            for key, join in (
                ("text", ""),
                ("expression", ""),
                ("summary", ""),
                ("blocks", "\n\n"),
                ("items", "\n"),
                ("cells", "\n"),
                ("caption", ""),
            ):
                if key in value:
                    before = len(parts)
                    visit(value[key], depth + 1, join)
                    if key in {"summary", "blocks", "items", "cells"} and len(parts) > before:
                        append("\n")

    visit(rich["blocks"], 0, "\n\n")
    return "".join(parts).strip(), incomplete
