"""Agent and model configuration loading."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from handoff_debt.agents.openhands_adapter import (
    OpenHandsConfig,
    dockerized_openhands_config,
    remap_base_url_for_docker,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_openhands_config(path: Path) -> OpenHandsConfig:
    data = tomllib.loads(path.read_text())
    llm = data.get("llm") or {}
    # Config files define stable profiles; env vars let local model servers
    # change model names, ports, and tool-calling behavior without new files.
    return OpenHandsConfig(
        model=os.getenv("HANDOFF_LLM_MODEL", llm["model"]),
        base_url=os.getenv("HANDOFF_LLM_BASE_URL", llm.get("base_url")),
        api_key=os.getenv("HANDOFF_LLM_API_KEY", llm.get("api_key", "")),
        max_iterations=int(
            os.getenv("HANDOFF_LLM_MAX_ITERATIONS", llm.get("max_iterations", 500))
        ),
        conversation_timeout_seconds=int(
            os.getenv(
                "HANDOFF_CONVERSATION_TIMEOUT_SECONDS",
                llm.get("conversation_timeout_seconds", 21600),
            )
        ),
        native_tool_calling=_env_bool(
            "HANDOFF_LLM_NATIVE_TOOL_CALLING",
            bool(llm.get("native_tool_calling", True)),
        ),
    )

__all__ = [
    "dockerized_openhands_config",
    "load_openhands_config",
    "remap_base_url_for_docker",
]
