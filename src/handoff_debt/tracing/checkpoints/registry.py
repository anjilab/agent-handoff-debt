"""Registry for selectable checkpoint detection strategies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from handoff_debt.io import read_jsonl
from handoff_debt.schemas import Checkpoint
from handoff_debt.tracing.checkpoints.base import CheckpointStrategy
from handoff_debt.tracing.checkpoints.lifecycle import (
    FIRST_MEANINGFUL_MODIFICATION,
    POST_FAILED_REPAIR_EDIT,
    POST_FIRST_VALIDATION_RESULT,
    detect_first_meaningful_modification,
    detect_lifecycle_checkpoints,
    detect_post_failed_repair_edit,
    detect_post_first_validation_result,
    detect_standard_handoff_checkpoint,
)

DEFAULT_CHECKPOINT_STRATEGY = "standard_handoff"

CHECKPOINT_STRATEGIES = {
    DEFAULT_CHECKPOINT_STRATEGY: CheckpointStrategy(
        name=DEFAULT_CHECKPOINT_STRATEGY,
        detector=detect_standard_handoff_checkpoint,
    ),
    FIRST_MEANINGFUL_MODIFICATION: CheckpointStrategy(
        name=FIRST_MEANINGFUL_MODIFICATION,
        detector=detect_first_meaningful_modification,
    ),
    POST_FIRST_VALIDATION_RESULT: CheckpointStrategy(
        name=POST_FIRST_VALIDATION_RESULT,
        detector=detect_post_first_validation_result,
    ),
    POST_FAILED_REPAIR_EDIT: CheckpointStrategy(
        name=POST_FAILED_REPAIR_EDIT,
        detector=detect_post_failed_repair_edit,
    ),
}


def detect_checkpoint(
    events: list[dict[str, Any]],
    *,
    strategy: str = DEFAULT_CHECKPOINT_STRATEGY,
) -> Checkpoint | None:
    try:
        checkpoint_strategy = CHECKPOINT_STRATEGIES[strategy]
    except KeyError as exc:
        available = ", ".join(sorted(CHECKPOINT_STRATEGIES))
        raise ValueError(f"Unknown checkpoint strategy: {strategy}. Available: {available}") from exc
    return checkpoint_strategy.detector(events)


def detect_from_jsonl(
    events_path: Path,
    *,
    strategy: str = DEFAULT_CHECKPOINT_STRATEGY,
) -> Checkpoint | None:
    if not events_path.exists():
        return None
    return detect_checkpoint(read_jsonl(events_path), strategy=strategy)


def detect_lifecycle_from_jsonl(events_path: Path) -> list[Checkpoint]:
    if not events_path.exists():
        return []
    return detect_lifecycle_checkpoints(read_jsonl(events_path))
