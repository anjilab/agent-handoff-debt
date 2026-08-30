"""Model-assisted structured handoff record construction."""

from __future__ import annotations

import re
from pathlib import Path

from handoff_debt.agents.openhands_adapter import OpenHandsConfig
from handoff_debt.handoff.evidence_packet import (
    build_allowed_evidence_packet,
    changed_source_files_at_handoff,
    scratch_artifacts,
    validation_commands,
    events_until_checkpoint,
)
from handoff_debt.handoff.prompt_parts import (
    build_original_task_prompt,
    build_takeover_context_prompt,
)
from handoff_debt.handoff.structured_summarizer import generate_structured_fields
from handoff_debt.schemas import Checkpoint, HandoffPackage, TaskSpec


def _bullets(
    items: list[str],
    *,
    empty: str = "NONE OBSERVED",
    indent: str = "",
) -> str:
    if not items:
        items = [empty]
    return "\n".join(f"{indent}- {item}" for item in items)


def _event_step(event: dict) -> int:
    return int(event.get("step") or 0)


def _event_text(event: dict) -> str:
    return str(event.get("text") or "")


def _latest_source_edit_step(events: list[dict], checkpoint: Checkpoint) -> int | None:
    del events
    if not checkpoint.modified_source_files:
        return None
    if checkpoint.kind == "post_first_validation_result":
        match = re.search(r"source modification at step (\d+)", checkpoint.reason)
        if match:
            return int(match.group(1))
    return checkpoint.step


def _validation_events_after_latest_edit(
    events: list[dict],
    checkpoint: Checkpoint,
) -> list[dict]:
    latest_edit = _latest_source_edit_step(events, checkpoint)
    if latest_edit is None:
        return []
    validation_text_markers = ("pytest", "unittest", "tox", "ruff", "mypy", "assert")
    result_markers = (
        " passed",
        " failed",
        " error",
        "traceback",
        "assertionerror",
        "no tests ran",
        "collected ",
        "====",
    )
    observed: list[dict] = []
    for event in events:
        if _event_step(event) < latest_edit:
            continue
        text = _event_text(event).lower()
        if any(marker in text for marker in validation_text_markers + result_markers):
            observed.append(event)
    return observed


def _last_validation_command_after_latest_edit(
    events: list[dict],
    checkpoint: Checkpoint,
) -> str:
    commands = validation_commands(
        _validation_events_after_latest_edit(events, checkpoint),
        checkpoint,
    )
    return commands[-1] if commands else "NONE OBSERVED"


def _validation_outcome_after_latest_edit(
    events: list[dict],
    checkpoint: Checkpoint,
) -> tuple[str, str]:
    validation_events = _validation_events_after_latest_edit(events, checkpoint)
    if not validation_events:
        return "none", "NONE OBSERVED"
    combined = "\n".join(_event_text(event).lower() for event in validation_events[-4:])
    failure_markers = (
        " failed",
        " failures",
        " error",
        " errors",
        "traceback",
        "assertionerror",
        "no tests ran",
        "exit code 1",
    )
    pass_markers = (
        " passed",
        " no failures",
        "exit code 0",
    )
    signature = _failure_signature(validation_events)
    if any(marker in combined for marker in failure_markers):
        return "failed", signature
    if any(marker in combined for marker in pass_markers):
        return "passed", signature
    return "unknown", signature


def _failure_signature(events: list[dict]) -> str:
    interesting = (
        "assertionerror",
        "traceback",
        "failed",
        "error",
        "no tests ran",
        "module not found",
    )
    for event in reversed(events):
        for line in _event_text(event).splitlines():
            stripped = line.strip()
            if any(marker in stripped.lower() for marker in interesting):
                return stripped[:240]
    return "NONE OBSERVED"


def _continuation_state_label(events: list[dict], checkpoint: Checkpoint) -> str:
    source = changed_source_files_at_handoff(checkpoint)
    outcome, _ = _validation_outcome_after_latest_edit(events, checkpoint)
    if source and outcome == "failed":
        return "source-change-with-failing-validation"
    if source and outcome == "passed":
        return "source-change-validated-by-predecessor"
    if source:
        return "source-change-needs-verification"
    if events:
        return "no-source-change-work-observed"
    return "no-source-change-observed"


def _continuation_state(checkpoint: Checkpoint, events: list[dict]) -> str:
    source_files = changed_source_files_at_handoff(checkpoint)
    scratch = scratch_artifacts(events, checkpoint)
    outcome, signature = _validation_outcome_after_latest_edit(events, checkpoint)
    repository_change_state = (
        "source changes present" if source_files else "no source changes observed"
    )
    return f"""Continuation state:
- Repository change state: {repository_change_state}
- Changed source files:
{_bullets(source_files, indent="  ")}
- Non-source artifacts observed:
{_bullets(scratch, indent="  ")}
- Validation after latest source change: {outcome}
- Latest predecessor validation command:
{_bullets([_last_validation_command_after_latest_edit(events, checkpoint)], indent="  ")}
- Latest predecessor validation evidence:
{_bullets([signature], indent="  ")}
- Continuation state label: {_continuation_state_label(events, checkpoint)}
"""


def build_structured_notes_handoff(
    task: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
    *,
    summarizer_config: OpenHandsConfig,
    artifact_dir: Path | None = None,
) -> HandoffPackage:
    evidence_packet = build_allowed_evidence_packet(task, checkpoint, events_path)
    events = events_until_checkpoint(events_path, checkpoint)
    fields = generate_structured_fields(
        config=summarizer_config,
        evidence_packet=evidence_packet,
    )

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "allowed_evidence_packet.md").write_text(evidence_packet)
        (artifact_dir / "structured_handoff_record.md").write_text(
            _render_record(checkpoint, events, fields)
        )

    prompt = f"""{build_takeover_context_prompt()}
{_render_record(checkpoint, events, fields)}
{build_original_task_prompt(task)}"""
    return HandoffPackage(
        task_id=task.task_id,
        checkpoint_id=checkpoint.checkpoint_id,
        view="structured_notes",
        prompt=prompt,
        workspace_path=checkpoint.workspace_snapshot,
    )


def _render_record(
    checkpoint: Checkpoint,
    events: list[dict],
    fields: dict[str, str],
) -> str:
    return f"""=== PREVIOUS AGENT STRUCTURED HANDOFF NOTES BEGINS ===

Structured handoff prepared from previous-agent evidence:

{_continuation_state(checkpoint, events)}

Previous agent notes:

Problem understanding:
{fields["problem_understanding"]}

Work completed:
{fields["work_completed"]}

Evidence observed:
{fields["evidence_observed"]}

Observed failures or error evidence:
{fields["observed_failures_or_error_evidence"]}

Remaining uncertainty:
{fields["remaining_uncertainty"]}

Rollback notes:
{fields["rollback_notes"]}

Recommended next action:
{fields["recommended_next_action"]}

=== PREVIOUS AGENT STRUCTURED HANDOFF NOTES ENDS ===
"""
