"""OpenHands event callbacks that write trajectory JSONL files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from handoff_debt.schemas import GitSnapshot, TraceEvent
from handoff_debt.tracing.remote_git_snapshots import (
    RemoteCommandWorkspace,
    snapshot_remote_git_state,
)


def event_to_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json", exclude_none=True)
    if isinstance(event, dict):
        return event
    return {"repr": repr(event)}


def flatten_text(value: Any) -> str:
    # Pull searchable text out of nested OpenHands event payloads.
    parts: list[str] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            parts.append(node)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {
                    "action",
                    "arguments",
                    "command",
                    "content",
                    "message",
                    "observation",
                    "text",
                    "thought",
                    "tool_call",
                    "tool_name",
                }:
                    visit(item)
            return
        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return "\n".join(part for part in parts if part).strip()


def infer_event_type(raw: dict[str, Any]) -> str:
    return str(
        raw.get("event_type")
        or raw.get("type")
        or raw.get("kind")
        or raw.get("tool_name")
        or raw.get("action", {}).get("action")
        or "event"
    )


def is_finished_state_update(raw: dict[str, Any]) -> bool:
    value = raw.get("value")
    if not isinstance(value, dict):
        return False
    return str(value.get("execution_status", "")).lower() == "finished"


class RemoteEventRecorder:
    """OpenHands callback that records events plus container-side git snapshots."""

    def __init__(
        self,
        workspace: RemoteCommandWorkspace,
        remote_repo_path: str,
        events_path: Path,
    ):
        self.workspace = workspace
        self.remote_repo_path = remote_repo_path
        self.events_path = events_path
        self.step = 0
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Any) -> None:
        raw = event_to_dict(event)
        git = GitSnapshot(
            step=self.step,
            workspace=self.remote_repo_path,
            head=None,
            status_short="",
            diff="",
            modified_files=[],
            diff_name_status=[],
        )
        if not is_finished_state_update(raw):
            try:
                git = snapshot_remote_git_state(
                    self.workspace,
                    self.remote_repo_path,
                    self.step,
                )
            except Exception as exc:
                raw["git_snapshot_error"] = repr(exc)
        else:
            raw["git_snapshot_skipped"] = "conversation_finished"
        if raw.get("git_snapshot_error"):
            git = GitSnapshot(
                step=self.step,
                workspace=self.remote_repo_path,
                head=None,
                status_short="",
                diff="",
                modified_files=[],
                diff_name_status=[],
            )
        trace_event = TraceEvent(
            step=self.step,
            event_type=infer_event_type(raw),
            source=raw.get("source"),
            text=flatten_text(raw),
            raw=raw,
            git=git,
        )
        with self.events_path.open("a") as f:
            f.write(json.dumps(trace_event.to_dict(), sort_keys=True) + "\n")
        self.step += 1
