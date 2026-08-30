"""Model-assisted summary handoff notes construction."""

from __future__ import annotations

from pathlib import Path

from handoff_debt.agents.openhands_adapter import OpenHandsConfig
from handoff_debt.handoff.evidence_packet import build_summary_evidence_packet
from handoff_debt.handoff.prompt_parts import (
    build_original_task_prompt,
    build_takeover_context_prompt,
)
from handoff_debt.handoff.summary_summarizer import generate_summary_notes
from handoff_debt.schemas import Checkpoint, HandoffPackage, TaskSpec


def build_summary_notes_handoff(
    task: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
    *,
    summarizer_config: OpenHandsConfig,
    artifact_dir: Path | None = None,
) -> HandoffPackage:
    evidence_packet = build_summary_evidence_packet(task, checkpoint, events_path)
    summary = generate_summary_notes(
        config=summarizer_config,
        evidence_packet=evidence_packet,
    )
    handoff_note = f"""=== PREVIOUS AGENT SUMMARY NOTES BEGINS ===

Natural-language summary of the previous agent work log:

{summary}

=== PREVIOUS AGENT SUMMARY NOTES ENDS ===
"""

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "summary_evidence_packet.md").write_text(evidence_packet)
        (artifact_dir / "summary_notes.md").write_text(handoff_note)

    prompt = f"""{build_takeover_context_prompt()}
{handoff_note}
{build_original_task_prompt(task)}"""
    return HandoffPackage(
        task_id=task.task_id,
        checkpoint_id=checkpoint.checkpoint_id,
        view="summary_notes",
        prompt=prompt,
        workspace_path=checkpoint.workspace_snapshot,
    )
