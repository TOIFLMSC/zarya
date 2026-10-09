"""Offline test doubles; real providers are introduced in later stages."""

from dataclasses import dataclass, field
from typing import Protocol


class ModelAdapter(Protocol):
    async def reply(self, text: str) -> str: ...


class TelegramAdapter(Protocol):
    async def send(self, chat_id: str, text: str) -> None: ...


@dataclass
class FakeModel:
    calls: list[str] = field(default_factory=list)

    async def reply(self, text: str) -> str:
        self.calls.append(text)
        return f"Тестовый ответ без API: {text}"


@dataclass
class FakeTelegram:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))
