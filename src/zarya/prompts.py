"""Immutable, non-executable local prompt bundles; no private text in this module."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

MAX_BYTES = 131072


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate prompt key")
        result[key] = value
    return result


@dataclass(frozen=True)
class PromptBundle:
    fragments: Mapping[str, str]
    sha256: str

    @classmethod
    def parse(cls, raw: bytes, expected: set[str] | None = None) -> "PromptBundle":
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError
            doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique)
            if not isinstance(doc, dict) or set(doc) != {"schema_version", "fragments"}:
                raise ValueError
            if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
                raise ValueError
            fragments = doc["fragments"]
            if not isinstance(fragments, dict) or (
                expected is not None and set(fragments) != expected
            ):
                raise ValueError
            if any(not isinstance(v, str) or len(v) > 32768 for v in fragments.values()):
                raise ValueError
            canonical = json.dumps(doc, ensure_ascii=True, sort_keys=True).encode()
            return cls(MappingProxyType(dict(fragments)), hashlib.sha256(canonical).hexdigest())
        except (ValueError, TypeError, UnicodeError):
            raise ValueError(
                "Некорректный пакет промптов: проверьте версию, ключи и размеры."
            ) from None

    @classmethod
    def load(cls, path: Path) -> "PromptBundle":
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except OSError:
            raise ValueError(
                "Локальный пакет промптов недоступен. Подготовьте data/prompts.json "
                "или задайте ZARYA_PROMPTS_FILE."
            ) from None
        return cls.parse(raw, set(DEFAULT_PROMPTS.fragments))

    def text(self, key: str) -> str:
        return self.fragments[key]


# Neutral published example, for explicit programmatic/test configurations only.
# Normal CLI startup requires a local file and never silently falls back to this.
DEFAULT_PROMPTS = PromptBundle.parse(files("zarya").joinpath("prompts.example.json").read_bytes())
