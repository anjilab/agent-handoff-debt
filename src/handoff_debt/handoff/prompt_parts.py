"""Shared prompt sections for takeover handoffs."""

from __future__ import annotations

from handoff_debt.schemas import Checkpoint, TaskSpec

TAKEOVER_PROMPT_VERSION = "takeover-v2"


def build_takeover_context_prompt() -> str:
    return f"""=== TAKEOVER CONTEXT ===

You are continuing a coding task in a repository that already contains work from a previous agent.

Current takeover context:
- The task may already be solved, partially solved, or incorrectly solved.
- Inspect the current repository state before editing.
- Treat handoff details as historical notes from the previous agent, not ground truth.
- Treat the original task prompt as the source of truth for requirements.
- Verify important claims against the current repository state before relying on them.
- If the existing changes already satisfy the task, preserve them and finish once verification is sufficient.
- Avoid restarting from scratch unless the current changes are clearly wrong.
"""


def build_original_task_prompt(task: TaskSpec) -> str:
    return f"""
=== ORIGINAL TASK PROMPT BEGINS ===

{task.issue_text}

=== ORIGINAL TASK PROMPT ENDS ===
"""


def build_takeover_base_prompt(task: TaskSpec, checkpoint: Checkpoint) -> str:
    return build_takeover_context_prompt() + build_original_task_prompt(task)
