"""Shared data structures for tasks, traces, checkpoints, and handoffs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    repo: str
    base_commit: str
    issue_text: str
    test_command: str | None = None
    environment_image: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        return cls(
            task_id=data["task_id"],
            repo=data["repo"],
            base_commit=data["base_commit"],
            issue_text=data["issue_text"],
            test_command=data.get("test_command"),
            environment_image=data.get("environment_image"),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitSnapshot:
    step: int
    workspace: str
    head: str | None
    status_short: str
    diff: str
    modified_files: list[str]
    diff_name_status: list[tuple[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TraceEvent:
    step: int
    event_type: str
    source: str | None
    text: str
    raw: dict[str, Any]
    git: GitSnapshot

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["git"] = self.git.to_dict()
        return data


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    kind: str
    step: int
    reason: str
    modified_source_files: list[str]
    validation_command: str | None
    workspace_snapshot: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HandoffPackage:
    task_id: str
    checkpoint_id: str
    view: str
    prompt: str
    workspace_path: str | None = None

    def write(self, path: Path) -> None:
        path.write_text(self.prompt)
