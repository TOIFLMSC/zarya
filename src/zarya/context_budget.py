"""Full-request preflight. Preserve selected evidence; never silently truncate history."""

import copy
import hashlib
import json
from typing import Any

PROFILE_VERSION = "openai-docs-2026-10-13"
PROFILES = {
    "gpt-6-luna": (1_050_000, 128_000),
    "gpt-6.1-sol": (1_050_000, 128_000),
}
MARGIN = 8192
# Engineering allowance, not a documented image tokenizer or certified upper bound.
IMAGE_ALLOWANCE = 131072
COUNT_FIELDS = {
    "model",
    "input",
    "instructions",
    "text",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "personality",
    "truncation",
}
SOURCE = "https://developers.openai.com/api/docs/models/"
SERVICES = {
    "context_overflow": "Контекст не помещается в один запрос. Нужен более короткий набор "
    "материалов или меньшая глубина истории",
    "budget_unverified": "Не получилось проверить размер большого контекста, "
    "поэтому ответ пока не подготовила. Попробуй чуть позже",
}


def count_payload(request: dict[str, Any]) -> dict[str, Any]:
    # No provider conversations, stored response IDs, hosted search, or hidden side effects.
    return {k: v for k, v in request.items() if k in COUNT_FIELDS}


def fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pack(request: dict[str, Any]) -> dict[str, Any]:
    """Compact JSON whitespace only. Different roles/materials keep their own bindings."""
    result = copy.deepcopy(request)
    if isinstance(result.get("input"), str):
        try:
            body = json.loads(result["input"])
        except ValueError:
            pass
        else:
            result["input"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    result["truncation"] = "disabled"
    return result


def inspect(request: dict[str, Any], *, expected_images: int = 0) -> dict[str, Any]:
    """Estimate the actual adapter payload, not its larger persisted snapshot.

    UTF-8 bytes + framing + a coarse image allowance are conservative engineering
    estimates. Near the budget we ask the server; a high estimate is NOT proof of overflow.
    """
    sections: list[dict[str, Any]] = []
    unknown = False
    images = 0

    def add(kind: str, value: Any, count: int = 1) -> None:
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        size = len(raw.encode())
        sections.append(dict(kind=kind, count=count, bytes=size, estimate=size + 32 * count))

    instructions = request.get("instructions", "")
    add("instructions", instructions, int(bool(instructions)))
    content = request.get("input", "")
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("role") not in {
                "user",
                "assistant",
                "system",
                "developer",
            }:
                unknown = True
                continue
            parts = item.get("content", [])
            if isinstance(parts, str):
                texts.append(parts)
                continue
            if not isinstance(parts, list):
                unknown = True
                continue
            for part in parts:
                if not isinstance(part, dict):
                    unknown = True
                    continue
                if part.get("type") == "input_image":
                    images += 1
                elif part.get("type") in {"input_text", "output_text"}:
                    texts.append(part.get("text", ""))
                else:
                    unknown = True
    else:
        unknown = True
    for text in texts:
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            add("other_input", text)
            continue
        if not isinstance(body, dict):
            add("other_input", body)
            continue
        for key, value in body.items():
            if (
                key == "recent_chat"
                and isinstance(value, list)
                and all(isinstance(m, dict) for m in value)
            ):
                for role, category in (
                    ("user", "recent_originals"),
                    ("summary", "recent_summaries"),
                    ("assistant", "assistant_replies"),
                ):
                    selected = [m for m in value if m.get("role") == role]
                    if selected:
                        add(category, selected, len(selected))
                others = [m for m in value if m.get("role") not in {"user", "summary", "assistant"}]
                if others:
                    add("other_input", others, len(others))
            else:
                kind = {
                    "current_message": "current_message",
                    "memory": "memory",
                    "selected_material": "material",
                    "research": "research",
                    "behavior": "behavior",
                    "avatar_profile": "avatar",
                }.get(key, "other_input")
                add(kind, value, len(value) if isinstance(value, list) else 1)
    # Include schema/tool configuration and wrapper overhead, even when input is short.
    for key in ("text", "tools", "reasoning", "tool_choice", "parallel_tool_calls", "personality"):
        if key in request:
            add("schema_tools", {key: request[key]})
    if images:
        sections.append(
            dict(kind="images", count=images, bytes=None, estimate=images * IMAGE_ALLOWANCE)
        )
    estimate = sum(s["estimate"] for s in sections) + 2048 + len(texts) * 64
    profile = PROFILES.get(request["model"])
    output = request.get("max_output_tokens", 0)
    available = profile[0] - output - MARGIN if profile and type(output) is int else None
    excluded = []
    if expected_images > images:
        excluded.append(
            dict(kind="images", count=expected_images - images, reason="image_byte_limit")
        )
    if not profile or type(output) is not int or not 0 < output <= profile[1]:
        state = "unverified"
    elif available is not None and available < 0:
        state = "overflow"
    elif unknown or available is None or estimate > available // 2:
        state = "needs_count"
    else:
        state = "fits_estimate"
    return dict(
        version=1,
        profile=PROFILE_VERSION,
        model=request["model"],
        profile_source=SOURCE + request["model"] if profile else None,
        context_limit=profile[0] if profile else None,
        output_limit=profile[1] if profile else None,
        output_reserve=output,
        safety_margin=MARGIN,
        input_budget=available,
        estimate=estimate,
        estimate_method="utf8-framing-image-allowance-v1",
        unknown_input=unknown,
        state=state,
        sections=sections,
        excluded=excluded,
        request_hash=fingerprint(request),
        provider_attempted=False,
        api_count={
            "state": "not_needed" if state == "fits_estimate" else "not_started",
            "tokens": None,
            "error": None,
            "latency_ms": None,
            "cost_usd": None,
        },
        decision="pending",
    )


def resolve(
    budget: dict[str, Any], *, tokens: int | None, state: str, error: str | None
) -> str | None:
    budget["api_count"].update(state=state, tokens=tokens, error=error)
    if state == "overflow":
        budget["state"] = "overflow"
        return "context_overflow"
    if (
        state != "completed"
        or type(tokens) is not int
        or tokens < 0
        or budget["input_budget"] is None
    ):
        budget["state"] = "unverified"
        return "budget_unverified"
    if tokens > budget["input_budget"]:
        budget["state"] = "overflow"
        return "context_overflow"
    budget["state"] = "fits_api"
    return None
