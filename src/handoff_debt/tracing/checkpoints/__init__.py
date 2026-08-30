"""Checkpoint strategy public API."""

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
from handoff_debt.tracing.checkpoints.registry import (
    detect_checkpoint,
    detect_from_jsonl,
    detect_lifecycle_from_jsonl,
)
from handoff_debt.tracing.checkpoints.signals import (
    is_source_file,
    validation_result,
)

__all__ = [
    "FIRST_MEANINGFUL_MODIFICATION",
    "POST_FAILED_REPAIR_EDIT",
    "POST_FIRST_VALIDATION_RESULT",
    "detect_first_meaningful_modification",
    "detect_lifecycle_checkpoints",
    "detect_lifecycle_from_jsonl",
    "detect_post_failed_repair_edit",
    "detect_post_first_validation_result",
    "detect_standard_handoff_checkpoint",
    "detect_checkpoint",
    "detect_from_jsonl",
    "is_source_file",
    "validation_result",
]
