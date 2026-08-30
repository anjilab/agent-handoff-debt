"""Readable OpenHands trajectory artifacts saved alongside research traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    return {"repr": repr(value)}


def save_openhands_trajectory(conversation: Any, trajectory_path: Path) -> None:
    """Save the native OpenHands event list in a compact, shareable JSON file."""

    events = [_to_jsonable(event) for event in conversation.state.events]
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory_path.write_text(json.dumps(events, indent=2))


def save_openhands_state(conversation: Any, state_path: Path) -> None:
    """Save final conversation state, including status and usage metadata."""

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(_to_jsonable(conversation.state), indent=2))
