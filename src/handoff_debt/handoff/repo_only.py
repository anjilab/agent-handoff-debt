"""Repo-only takeover prompt construction."""

from __future__ import annotations

from handoff_debt.handoff.prompt_parts import build_takeover_base_prompt
from handoff_debt.schemas import Checkpoint, HandoffPackage, TaskSpec


def build_repo_only_handoff(task: TaskSpec, checkpoint: Checkpoint) -> HandoffPackage:
    prompt = build_takeover_base_prompt(task, checkpoint)
    return HandoffPackage(
        task_id=task.task_id,
        checkpoint_id=checkpoint.checkpoint_id,
        view="repo_only",
        prompt=prompt,
        workspace_path=checkpoint.workspace_snapshot,
    )
