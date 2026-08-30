"""Raw-trace takeover prompt construction."""

from __future__ import annotations

from pathlib import Path

from handoff_debt.handoff.prompt_parts import (
    build_original_task_prompt,
    build_takeover_context_prompt,
)
from handoff_debt.io import read_jsonl
from handoff_debt.schemas import Checkpoint, HandoffPackage, TaskSpec


def _format_trace_prefix(events_path: Path, checkpoint: Checkpoint) -> str:
    # Keep only events available to the takeover point.
    lines: list[str] = []
    for event in read_jsonl(events_path):
        step = int(event["step"])
        if step > checkpoint.step:
            break
        source = event.get("source") or "unknown"
        event_type = event.get("event_type") or "event"
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[step {step} | {source} | {event_type}]\n{text}")
    return "\n\n".join(lines)


def build_raw_trace_handoff(
    task: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
) -> HandoffPackage:
    trace = _format_trace_prefix(events_path, checkpoint)
    prompt = f"""{build_takeover_context_prompt()}
=== PREVIOUS AGENT RAW TRACE BEGINS ===

The following raw trace is historical context from the previous agent.
Use it to understand what has been tried and what remains. Continue from the
current repository state.

{trace}

=== PREVIOUS AGENT RAW TRACE ENDS ===

{build_original_task_prompt(task)}"""
    return HandoffPackage(
        task_id=task.task_id,
        checkpoint_id=checkpoint.checkpoint_id,
        view="raw_trace",
        prompt=prompt,
        workspace_path=checkpoint.workspace_snapshot,
    )
