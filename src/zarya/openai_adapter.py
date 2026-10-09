"""Bounded official Responses adapter; sanitized outcomes and no SDK retries."""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

RATES = {
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "gpt-6.1-sol": (2.0, 0.10, 2.50, 10.0),
}


def pricing_model(model: str) -> str | None:
    return next(
        (
            name
            for name in RATES
            if re.fullmatch(re.escape(name) + r"(?:-\d{4}-\d{2}-\d{2})?", model)
        ),
        None,
    )


def pricing_profile(model: str) -> str | None:
    name = pricing_model(model)
    return f"{name.rsplit('-', 1)[-1]}-standard-2026-10-07" if name else None


@dataclass
class ModelResult:
    text: str = ""
    state: str = "completed"
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    request_id: str | None = None
    error: str | None = None
    service_tier: str | None = "default"
    model: str | None = None
    search_actions: list[dict[str, Any]] | None = None
    citations: list[dict[str, Any]] | None = None


class ModelAdapter(Protocol):
    async def generate(self, request: dict[str, Any]) -> ModelResult: ...
    async def close(self) -> None: ...


class SpeechAdapter(Protocol):
    async def transcribe(self, model: str, audio: bytes) -> ModelResult: ...


def estimate_speech_cost(result: ModelResult, model: str) -> float | None:
    usage = result.usage or {}
    rates = {"gpt-4o-mini-transcribe": (1.25, 5.0), "gpt-4o-transcribe": (2.50, 10.0)}
    rate = rates.get(result.model or model)
    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    if rate is None or usage.get("type") != "tokens":
        return None
    if not all(type(v) is int and v >= 0 for v in (inputs, outputs)):
        return None
    assert inputs is not None and outputs is not None
    return (int(inputs) * rate[0] + int(outputs) * rate[1]) / 1e6


def estimate_cost(result: ModelResult, requested_model: str) -> float | None:
    usage = result.usage
    model = pricing_model(result.model or requested_model)
    if model is None:
        return None
    if not usage or result.service_tier not in {None, "default", "standard"}:
        return None
    details = usage.get("input_tokens_details") or {}
    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    if not all(isinstance(v, int) and v >= 0 for v in (inputs, outputs, cached, written)):
        return None
    assert inputs is not None and outputs is not None
    inputs, outputs, cached, written = (int(v) for v in (inputs, outputs, cached, written))
    if inputs > 272000 or cached + written > inputs:
        return None
    input_rate, cached_rate, write_rate, output_rate = RATES[model]
    return float(
        (
            (inputs - cached - written) * input_rate
            + cached * cached_rate
            + written * write_rate
            + outputs * output_rate
        )
        / 1e6
    )


class OpenAIAdapter:
    def __init__(self, key: str):
        self.client = AsyncOpenAI(api_key=key, max_retries=0, timeout=60.0)

    async def transcribe(self, model: str, audio: bytes) -> ModelResult:
        try:
            async with asyncio.timeout(65):
                response = await self.client.audio.transcriptions.create(
                    model=model,
                    file=("audio.wav", audio, "audio/wav"),
                    response_format="json",
                    timeout=60,
                    stream=False,
                )
            text = response.text.strip()
            raw = response.model_dump()
            return ModelResult(
                text=text[:12000],
                state="completed" if len(text) <= 12000 else "invalid",
                usage=raw.get("usage"),
                model=model,
                request_id=response._request_id,
                error=None if len(text) <= 12000 else "transcript_too_long",
            )
        except APIStatusError as exc:
            return ModelResult(
                state="rejected"
                if exc.status_code in {400, 401, 403, 404, 422, 429}
                else "unknown",
                error=f"http_{exc.status_code}",
                request_id=exc.request_id,
            )
        except (APIConnectionError, TimeoutError):
            return ModelResult(state="unknown", error="connection_uncertain")

    async def generate(self, request: dict[str, Any]) -> ModelResult:
        try:
            async with asyncio.timeout(65):
                response = await self.client.responses.create(**request)
            completed = (
                response.status == "completed"
                and bool(response.output_text.strip())
                and len(response.output_text.encode("utf-16-le")) // 2 <= 8000
            )
            actions: list[dict[str, Any]] = []
            citations: list[dict[str, Any]] = []
            for item in getattr(response, "output", []):
                if getattr(item, "type", "") == "web_search_call":
                    action = item.action.model_dump(exclude_none=True)
                    actions.append({**action, "id": item.id, "status": item.status})
                if getattr(item, "type", "") == "message":
                    for part in getattr(item, "content", []):
                        for annotation in getattr(part, "annotations", []):
                            if getattr(annotation, "type", "") == "url_citation":
                                citations.append(annotation.model_dump(exclude_none=True))
            return ModelResult(
                text=response.output_text,
                state="completed" if completed else "invalid",
                usage=response.usage.model_dump() if response.usage else None,
                response_id=response.id,
                request_id=response._request_id,
                error=None if completed else "incomplete_or_empty",
                service_tier=response.service_tier,
                model=response.model,
                search_actions=actions if request.get("tools") else None,
                citations=citations,
            )
        except APIStatusError as exc:
            rejected = exc.status_code in {400, 401, 403, 404, 422, 429}
            return ModelResult(
                state="rejected" if rejected else "unknown",
                error=f"http_{exc.status_code}",
                request_id=exc.request_id,
            )
        except (APIConnectionError, TimeoutError):
            return ModelResult(state="unknown", error="connection_uncertain")

    async def close(self) -> None:
        await self.client.close()
