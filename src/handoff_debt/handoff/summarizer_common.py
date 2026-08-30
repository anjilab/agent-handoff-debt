"""Shared helpers for model-assisted handoff generation."""

from __future__ import annotations

import json
from typing import Any

from litellm import completion

from handoff_debt.agents.openhands_adapter import OpenHandsConfig

FORBIDDEN_GENERATED_PHRASES = (
    "do not include patch text",
    "checkpoint kind",
    "checkpoint step",
    "internal checkpoint reason",
    "official swe-bench validation",
)


def completion_text(
    config: OpenHandsConfig,
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, str] | None = None,
) -> str:
    kwargs: dict[str, Any] = {}
    if response_format is not None:
        kwargs["response_format"] = response_format
    if "qwen" in config.model.lower():
        # Qwen thinking models can spend the whole cap in reasoning and return
        # empty content for small JSON summarization calls.
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    response = completion(
        model=config.model,
        api_key=config.api_key,
        api_base=config.base_url,
        messages=messages,
        temperature=0,
        max_tokens=1600,
        **kwargs,
    )
    content = response["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Summarizer returned empty content")
    return content.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Summarizer did not return strict JSON. "
                f"First 500 chars: {text[:500]!r}"
            ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Summarizer did not return a JSON object")
    return value


def validate_generated_handoff_text(text: str) -> None:
    lower = text.lower()
    forbidden = [phrase for phrase in FORBIDDEN_GENERATED_PHRASES if phrase in lower]
    if forbidden:
        raise RuntimeError(
            "Generated handoff contains forbidden content: "
            + ", ".join(sorted(set(forbidden)))
        )
