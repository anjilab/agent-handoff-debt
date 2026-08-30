"""Workspace git setup and checkpoint reconstruction helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from handoff_debt.io import read_jsonl
from handoff_debt.schemas import Checkpoint


def run_checked(cmd: list[str], cwd: Path) -> None:
    subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def run_git(workspace: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        command = " ".join(["git", *args])
        raise RuntimeError(
            f"{command} failed in {workspace} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def diff_from_base(workspace: Path, base_commit: str) -> str:
    return run_git(workspace, "diff", "--binary", base_commit)


def init_git_repo(workspace: Path) -> str:
    # The base commit anchors all later git diffs for checkpoints and scoring.
    run_checked(["git", "init"], workspace)
    run_checked(["git", "config", "user.email", "handoff@example.com"], workspace)
    run_checked(["git", "config", "user.name", "Handoff Debt"], workspace)
    run_checked(["git", "config", "core.filemode", "false"], workspace)
    configure_git_excludes(workspace)
    run_checked(["git", "add", "."], workspace)
    run_checked(["git", "commit", "-m", "base task state"], workspace)
    return run_git(workspace, "rev-parse", "HEAD")


def configure_git_excludes(workspace: Path) -> None:
    exclude_path = workspace / ".git" / "info" / "exclude"
    # Runtime caches should not become part of checkpoint or takeover diffs.
    patterns = [
        "",
        "# Handoff Debt runtime clutter",
        ".venv/",
        "venv/",
        "venv_*/",
        "__pycache__/",
        "*.pyc",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "build/",
        "dist/",
        "*.egg-info/",
    ]
    with exclude_path.open("a") as f:
        f.write("\n".join(patterns) + "\n")


RUNTIME_COPY_IGNORE = shutil.ignore_patterns(
    ".venv",
    "venv",
    "venv_*",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "*.egg-info",
    "result_images",
)


def make_workspace_writable(workspace: Path) -> None:
    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        _chmod_if_regular_path(root_path, 0o777)
        for name in dirs:
            path = root_path / name
            _chmod_if_regular_path(path, 0o777)
        for name in files:
            path = root_path / name
            _chmod_if_regular_path(path, 0o666)


def _chmod_if_regular_path(path: Path, mode_bits: int) -> None:
    """Make real files/dirs writable without following broken test symlinks."""

    try:
        if path.is_symlink():
            return
        path.chmod(path.stat().st_mode | mode_bits)
    except FileNotFoundError:
        return


def event_at_step(events_path: Path, step: int) -> dict[str, object]:
    for event in read_jsonl(events_path):
        if int(event["step"]) == step:
            return event
    raise ValueError(f"No event found at step {step} in {events_path}")


def create_checkpoint_workspace(
    *,
    run_dir: Path,
    source_workspace: Path | None = None,
    checkpoint: Checkpoint,
    base_commit: str,
) -> Checkpoint:
    events_path = run_dir / "events.jsonl"
    source_workspace = source_workspace or run_dir / "workspace"
    checkpoint_dir = run_dir / "checkpoints" / checkpoint.checkpoint_id
    checkpoint_workspace = checkpoint_dir / "workspace"

    if checkpoint_workspace.exists():
        shutil.rmtree(checkpoint_workspace)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    # Copy the live workspace first so untracked but relevant files are present.
    shutil.copytree(
        source_workspace,
        checkpoint_workspace,
        ignore=RUNTIME_COPY_IGNORE,
        symlinks=True,
    )

    # Rebuild from task base plus the exact diff recorded at the checkpoint.
    run_git(checkpoint_workspace, "reset", "--hard", base_commit)
    run_git(checkpoint_workspace, "clean", "-fd")

    event = event_at_step(events_path, checkpoint.step)
    git = event.get("git") or {}
    diff = str(git.get("diff") or "")
    if diff:
        if not diff.endswith("\n"):
            diff += "\n"
        run_git(checkpoint_workspace, "apply", "--binary", input_text=diff)

    make_workspace_writable(checkpoint_workspace)
    return Checkpoint(
        checkpoint_id=checkpoint.checkpoint_id,
        kind=checkpoint.kind,
        step=checkpoint.step,
        reason=checkpoint.reason,
        modified_source_files=checkpoint.modified_source_files,
        validation_command=checkpoint.validation_command,
        workspace_snapshot=str(checkpoint_workspace),
        metadata=checkpoint.metadata,
    )
