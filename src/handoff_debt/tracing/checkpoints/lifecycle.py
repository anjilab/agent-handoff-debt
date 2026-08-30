"""Lifecycle checkpoint detection for model-agnostic handoff states."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from handoff_debt.schemas import Checkpoint
from handoff_debt.tracing.checkpoints.signals import (
    is_source_file,
    is_terminal_event,
    modified_source_files,
    validation_command_from_event,
    validation_result_from_event,
)

FIRST_MEANINGFUL_MODIFICATION = "first_meaningful_modification"
POST_FIRST_VALIDATION_RESULT = "post_first_validation_result"
POST_FAILED_REPAIR_EDIT = "post_failed_repair_edit"
LIFECYCLE_ORDER = [
    FIRST_MEANINGFUL_MODIFICATION,
    POST_FIRST_VALIDATION_RESULT,
    POST_FAILED_REPAIR_EDIT,
]
GIT_STATE_COMMAND_RE = re.compile(
    r"\bgit\s+(stash|restore|checkout|reset|clean)\b",
    flags=re.IGNORECASE,
)


def _diff_file_hashes(diff: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                files[current_path] = current_lines
            parts = line.split()
            current_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else None
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)

    if current_path is not None:
        files[current_path] = current_lines

    return {
        path: hashlib.sha1("".join(lines).encode()).hexdigest()
        for path, lines in files.items()
    }


def _changed_source_files(
    event: dict[str, Any],
    previous_file_hashes: dict[str, str],
) -> list[str]:
    git = event.get("git") or {}
    current_file_hashes = _diff_file_hashes(str(git.get("diff") or ""))
    return sorted(
        path
        for path, file_hash in current_file_hashes.items()
        if is_source_file(path) and previous_file_hashes.get(path) != file_hash
    )


def _is_git_state_management_event(event: dict[str, Any]) -> bool:
    """Return true for commands that restore/remove prior diffs rather than repair code."""
    if not is_terminal_event(event):
        return False

    raw = event.get("raw") or {}
    command = (
        (raw.get("action") or {}).get("command")
        or (raw.get("observation") or {}).get("command")
        or str(event.get("text") or "")
    )
    return GIT_STATE_COMMAND_RE.search(str(command)) is not None


def detect_lifecycle_checkpoints(events: list[dict[str, Any]]) -> list[Checkpoint]:
    """Detect the canonical handoff checkpoints from observable lifecycle states."""
    seen_source_files: set[str] = set()
    first_edit_step: int | None = None
    last_validation_command: str | None = None
    pending_validation_command: str | None = None
    failed_validation_step: int | None = None
    previous_file_hashes: dict[str, str] = {}
    checkpoints: dict[str, Checkpoint] = {}

    for event in events:
        step = int(event["step"])
        changed_source_files = _changed_source_files(event, previous_file_hashes)
        if changed_source_files:
            seen_source_files.update(modified_source_files(event) or changed_source_files)
            if first_edit_step is None:
                first_edit_step = step
            checkpoints.setdefault(
                FIRST_MEANINGFUL_MODIFICATION,
                Checkpoint(
                    checkpoint_id=f"{FIRST_MEANINGFUL_MODIFICATION}_step_{step}",
                    kind=FIRST_MEANINGFUL_MODIFICATION,
                    step=step,
                    reason="First observed non-test source-code modification.",
                    modified_source_files=sorted(seen_source_files),
                    validation_command=None,
                    metadata={"selection_priority": 1},
                ),
            )
            if (
                failed_validation_step is not None
                and POST_FAILED_REPAIR_EDIT not in checkpoints
                and step > failed_validation_step
                and not _is_git_state_management_event(event)
            ):
                checkpoints[POST_FAILED_REPAIR_EDIT] = Checkpoint(
                    checkpoint_id=f"{POST_FAILED_REPAIR_EDIT}_step_{step}",
                    kind=POST_FAILED_REPAIR_EDIT,
                    step=step,
                    reason=(
                        "First observed non-test source-code modification after "
                        f"a failed validation at step {failed_validation_step}."
                    ),
                    modified_source_files=sorted(seen_source_files),
                    validation_command=last_validation_command,
                    metadata={"selection_priority": 3},
                )

        if first_edit_step is None:
            previous_file_hashes = _diff_file_hashes(
                str((event.get("git") or {}).get("diff") or "")
            )
            continue

        command = validation_command_from_event(event)
        if command is not None:
            last_validation_command = command
            pending_validation_command = command

        result = validation_result_from_event(event)
        if (
            event.get("source") == "environment"
            and is_terminal_event(event)
            and (pending_validation_command is not None or result is not None)
        ):
            checkpoints.setdefault(
                POST_FIRST_VALIDATION_RESULT,
                Checkpoint(
                    checkpoint_id=f"{POST_FIRST_VALIDATION_RESULT}_step_{step}",
                    kind=POST_FIRST_VALIDATION_RESULT,
                    step=step,
                    reason=(
                        "First observed terminal result from a validation, build, "
                        "lint, or reproduction command after source modification "
                        f"at step {first_edit_step}."
                    ),
                    modified_source_files=sorted(seen_source_files),
                    validation_command=last_validation_command,
                    metadata={"selection_priority": 2},
                ),
            )
            pending_validation_command = None
        if result == "failed" and failed_validation_step is None:
            failed_validation_step = step
        previous_file_hashes = _diff_file_hashes(
            str((event.get("git") or {}).get("diff") or "")
        )

    return [checkpoints[kind] for kind in LIFECYCLE_ORDER if kind in checkpoints]


def detect_first_meaningful_modification(
    events: list[dict[str, Any]],
) -> Checkpoint | None:
    return _find_kind(events, FIRST_MEANINGFUL_MODIFICATION)


def detect_post_first_validation_result(
    events: list[dict[str, Any]],
) -> Checkpoint | None:
    return _find_kind(events, POST_FIRST_VALIDATION_RESULT)


def detect_post_failed_repair_edit(events: list[dict[str, Any]]) -> Checkpoint | None:
    return _find_kind(events, POST_FAILED_REPAIR_EDIT)


def detect_standard_handoff_checkpoint(events: list[dict[str, Any]]) -> Checkpoint | None:
    """Return the strongest available lifecycle checkpoint for existing single-view flows."""
    checkpoints = detect_lifecycle_checkpoints(events)
    if not checkpoints:
        return None
    return max(
        checkpoints,
        key=lambda checkpoint: int(checkpoint.metadata.get("selection_priority") or 0),
    )


def _find_kind(events: list[dict[str, Any]], kind: str) -> Checkpoint | None:
    return next(
        (
            checkpoint
            for checkpoint in detect_lifecycle_checkpoints(events)
            if checkpoint.kind == kind
        ),
        None,
    )
