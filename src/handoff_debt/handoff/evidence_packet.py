"""Deterministic evidence packet construction for model-written handoffs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from handoff_debt.io import read_jsonl
from handoff_debt.schemas import Checkpoint, TaskSpec

MAX_EVENT_TEXT_CHARS = 1200
MAX_EVENTS = 80
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_ASSIGNMENT_RE = re.compile(r"\b[\w.]+(?:\[[^\n]{0,120}\])?\s*=\s*[^=\n]{1,160}")
PATCH_HINTS = (
    "str_replace",
    "old_str",
    "new_str",
    '"command": "edit"',
    '"command": "str_replace"',
    "it should be:",
    "should be:",
    "change it to",
    "replace it with",
)
SCRATCH_NAME_PREFIXES = ("reproduce", "repro", "scratch", "tmp", "debug")


def clip_text(text: str, limit: int = MAX_EVENT_TEXT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def sanitize_event_text(text: str) -> str:
    lower = text.lower()
    if any(marker in lower for marker in PATCH_HINTS[:5]):
        return "[file edit action observed; patch text excluded]"
    text = CODE_BLOCK_RE.sub("[code snippet excluded]", text)
    sanitized_lines: list[str] = []
    for line in text.splitlines():
        line_lower = line.lower()
        if any(marker in line_lower for marker in PATCH_HINTS):
            sanitized_lines.append("[patch-like guidance excluded]")
            continue
        sanitized_lines.append(INLINE_ASSIGNMENT_RE.sub("[assignment snippet excluded]", line))
    return clip_text("\n".join(sanitized_lines))


def events_until_checkpoint(events_path: Path, checkpoint: Checkpoint) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    return [
        event
        for event in read_jsonl(events_path)
        if int(event.get("step") or 0) <= checkpoint.step
    ]


def is_scratch_artifact(path: str) -> bool:
    path_obj = Path(path)
    normalized = path.replace("\\", "/")
    if "/" in normalized:
        return False
    return path_obj.stem.lower().startswith(SCRATCH_NAME_PREFIXES)


def changed_source_files_at_handoff(checkpoint: Checkpoint) -> list[str]:
    return sorted(
        path
        for path in set(checkpoint.modified_source_files)
        if not is_scratch_artifact(path)
    )


def observed_artifacts(events: list[dict[str, Any]]) -> list[str]:
    artifacts: list[str] = []
    for event in events:
        git = event.get("git") or {}
        for path in git.get("modified_files") or []:
            artifacts.append(str(path))
        for item in git.get("diff_name_status") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                artifacts.append(str(item[1]))
    return sorted(set(artifacts))


def scratch_artifacts(events: list[dict[str, Any]], checkpoint: Checkpoint) -> list[str]:
    final = set(changed_source_files_at_handoff(checkpoint))
    scratch = [
        path
        for path in observed_artifacts(events) + list(checkpoint.modified_source_files)
        if path not in final
    ]
    return sorted(set(scratch))


def validation_commands(events: list[dict[str, Any]], checkpoint: Checkpoint) -> list[str]:
    commands: list[str] = []
    if checkpoint.validation_command:
        commands.append(checkpoint.validation_command)
    command_prefixes = (
        "python ",
        "python3 ",
        "python -m ",
        "python3 -m ",
        "pytest ",
        "tox ",
        "ruff ",
        "mypy ",
        "source ",
        "cd ",
    )
    validation_markers = ("pytest", "unittest", "tox", "ruff", "mypy", "assert")
    for event in events:
        text = str(event.get("text") or "")
        for line in text.splitlines():
            lower = line.lower().strip()
            if lower.startswith(command_prefixes) and any(
                marker in lower for marker in validation_markers
            ):
                commands.append(line.strip())
    return _unique(commands)


def _unique(items: list[str]) -> list[str]:
    unique: list[str] = []
    for item in items:
        item = clip_text(item)
        if item and item not in unique:
            unique.append(item)
    return unique


def _event_timeline(
    events: list[dict[str, Any]],
    *,
    sanitize: bool = True,
) -> list[str]:
    lines: list[str] = []
    # Handoff summaries should emphasize the state immediately before takeover.
    for event in events[-MAX_EVENTS:]:
        raw_text = str(event.get("text") or "")
        text = sanitize_event_text(raw_text) if sanitize else clip_text(raw_text)
        if not text:
            continue
        step = int(event.get("step") or 0)
        source = event.get("source") or "unknown"
        event_type = event.get("event_type") or "event"
        lines.append(f"step {step} | {source} | {event_type}: {text}")
    return lines


def _bullets(items: list[str], *, empty: str = "NONE OBSERVED") -> str:
    if not items:
        items = [empty]
    return "\n".join(f"- {item}" for item in items)


def render_observable_checkpoint_state(
    checkpoint: Checkpoint,
    events: list[dict[str, Any]] | None = None,
) -> str:
    """Render rule-based handoff state that should not be overridden by summaries."""

    changed_source_files = changed_source_files_at_handoff(checkpoint)
    if events is None:
        scratch: list[str] = []
    else:
        scratch = scratch_artifacts(events, checkpoint)
    source_state = (
        "The current repository snapshot already contains predecessor source changes."
        if changed_source_files
        else "No predecessor source-file changes were observed in the current repository snapshot."
    )
    return f"""Observable checkpoint state:
- {source_state}
- Source files changed in the current repository snapshot:
{_bullets(changed_source_files)}
- Scratch or temporary artifacts observed before handoff:
{_bullets(scratch)}
"""


def build_allowed_evidence_packet(
    task: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
) -> str:
    events = events_until_checkpoint(events_path, checkpoint)
    changed_source_files = changed_source_files_at_handoff(checkpoint)
    scratch = scratch_artifacts(events, checkpoint)
    validations = validation_commands(events, checkpoint)

    return f"""Predecessor evidence packet for handoff generation

Original benchmark task:
<original_task>
{task.issue_text}
</original_task>

Repository state facts at handoff:
- Source files changed in the handoff workspace:
{_bullets(changed_source_files)}
- Scratch or temporary artifacts observed before handoff:
{_bullets(scratch)}
- Full git patch text: EXCLUDED

Validation or reproduction commands observed before handoff:
{_bullets(validations)}

Event timeline before handoff:
{_bullets(_event_timeline(events), empty="NONE OBSERVED")}

Packet boundaries:
- Events after handoff are not included.
- Full raw git patch text is not included.
- Checkpoint kind, step number, and internal checkpoint reason are not included.
"""


def build_summary_evidence_packet(
    task: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
) -> str:
    events = events_until_checkpoint(events_path, checkpoint)

    return f"""Previous agent work log

Original benchmark task:
<original_task>
{task.issue_text}
</original_task>

Previous agent event timeline:
{_bullets(_event_timeline(events, sanitize=False), empty="NONE OBSERVED")}
"""
