"""Container-side git snapshot helpers for Docker workspaces."""

from __future__ import annotations

from typing import Any, Protocol

from handoff_debt.schemas import GitSnapshot


class RemoteCommandWorkspace(Protocol):
    def execute_command(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> Any: ...


def _stdout(result: Any) -> str:
    return str(getattr(result, "stdout", "") or "").rstrip("\n")


def _returncode(result: Any) -> int:
    value = getattr(result, "exit_code", None)
    if value is None:
        value = getattr(result, "returncode", None)
    return int(value if value is not None else 1)


def modified_files_from_status(status_short: str) -> list[str]:
    files: list[str] = []
    for line in status_short.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        files.append(path)
    return sorted(set(files))


def _run(
    workspace: RemoteCommandWorkspace,
    remote_repo_path: str,
    command: str,
    timeout: float = 30.0,
) -> Any:
    return workspace.execute_command(command, cwd=remote_repo_path, timeout=timeout)


def _remote_stdout(
    workspace: RemoteCommandWorkspace,
    remote_repo_path: str,
    command: str,
) -> str:
    result = _run(workspace, remote_repo_path, command)
    if _returncode(result) != 0:
        return ""
    return _stdout(result)


def _diff_name_status(output: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status, path = line.split(maxsplit=1)
        if "\t" in path and status.startswith("R"):
            path = path.split("\t")[-1]
        pairs.append((status, path))
    return pairs


def snapshot_remote_git_state(
    workspace: RemoteCommandWorkspace,
    remote_repo_path: str,
    step: int,
) -> GitSnapshot:
    inside_repo = _remote_stdout(
        workspace,
        remote_repo_path,
        "git rev-parse --is-inside-work-tree",
    )
    if inside_repo.strip() != "true":
        return GitSnapshot(
            step=step,
            workspace=remote_repo_path,
            head=None,
            status_short="",
            diff="",
            modified_files=[],
            diff_name_status=[],
        )

    status = _remote_stdout(workspace, remote_repo_path, "git status --short")
    name_status = _remote_stdout(workspace, remote_repo_path, "git diff --name-status")
    return GitSnapshot(
        step=step,
        workspace=remote_repo_path,
        head=_remote_stdout(workspace, remote_repo_path, "git rev-parse HEAD") or None,
        status_short=status,
        diff=_remote_stdout(workspace, remote_repo_path, "git diff --binary"),
        modified_files=modified_files_from_status(status),
        diff_name_status=_diff_name_status(name_status),
    )
