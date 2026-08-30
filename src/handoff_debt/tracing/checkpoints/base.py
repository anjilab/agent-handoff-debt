"""Shared checkpoint strategy types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from handoff_debt.schemas import Checkpoint

CheckpointDetector = Callable[[list[dict[str, Any]]], Checkpoint | None]


@dataclass(frozen=True)
class CheckpointStrategy:
    name: str
    detector: CheckpointDetector
