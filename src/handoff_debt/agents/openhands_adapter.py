"""Thin adapter between the harness and the OpenHands SDK."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openhands.sdk import Agent, Conversation, LLM
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.context import AgentContext
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.sdk.event import ActionEvent, MessageEvent
from openhands.sdk.skills.skill import load_public_skills
from openhands.sdk.tool.builtins.finish import FinishAction
from openhands.tools.preset.default import get_default_tools

from handoff_debt.agents.docker_workspace import HandoffDockerWorkspace
from handoff_debt.io import write_json
from handoff_debt.tracing.event_logger import RemoteEventRecorder
from handoff_debt.tracing.openhands_trajectory import (
    save_openhands_state,
    save_openhands_trajectory,
)

DOCKER_HOST_ALIAS = "host.docker.internal"
DOCKER_HOST_GATEWAY = "host-gateway"


def host_user_group() -> tuple[int, int] | None:
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return os.getuid(), os.getgid()
    return None


@dataclass(frozen=True)
class OpenHandsConfig:
    model: str
    api_key: str
    base_url: str | None = None
    max_iterations: int = 500
    conversation_timeout_seconds: int = 21600
    native_tool_calling: bool = True


@dataclass(frozen=True)
class DockerRuntimeConfig:
    host_workspace: Path
    remote_workspace: str = "/workspace/project"
    remote_repo_path: str | None = None
    server_image: str = "ghcr.io/openhands/agent-server:latest-python"
    platform: str = "linux/amd64"
    host_port: int | None = None
    network: str | None = None
    extra_ports: bool = False
    enable_gpu: bool = False
    detach_logs: bool = True
    forward_env: list[str] = field(default_factory=lambda: ["DEBUG"])
    extra_hosts: dict[str, str] = field(
        default_factory=lambda: {DOCKER_HOST_ALIAS: DOCKER_HOST_GATEWAY}
    )
    extra_volumes: list[str] = field(default_factory=list)
    setup_commands: list[str] = field(default_factory=list)
    base_commit: str | None = None
    instance_id: str | None = None


def build_default_agent(config: OpenHandsConfig) -> Any:
    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        native_tool_calling=config.native_tool_calling,
    )
    skills = load_public_skills()
    return Agent(
        llm=llm,
        tools=get_default_tools(enable_browser=False),
        system_prompt_kwargs={"cli_mode": True},
        condenser=LLMSummarizingCondenser(
            llm=llm.model_copy(update={"usage_id": "condenser"}),
            # It means OpenHands keeps the first 2 important opening events and starts summarizing once the conversation grows large enough.
            # 240 conversation events, not 240 tokens.
            max_size=240,
            keep_first=2,
        ),
        agent_context=AgentContext(skills=skills) if skills else None,
    )


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    return value


def remap_base_url_for_docker(base_url: str | None) -> str | None:
    if not base_url:
        return base_url

    parsed = urlsplit(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return base_url
    if parsed.port is None:
        return base_url

    netloc = f"{DOCKER_HOST_ALIAS}:{parsed.port}"
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        netloc = f"{auth}@{netloc}"

    # The LLM may run on the host while OpenHands runs inside Docker.
    # Docker containers need the host-gateway alias instead of localhost.
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def dockerized_openhands_config(config: OpenHandsConfig) -> OpenHandsConfig:
    return OpenHandsConfig(
        model=config.model,
        api_key=config.api_key,
        base_url=remap_base_url_for_docker(config.base_url),
        max_iterations=config.max_iterations,
        conversation_timeout_seconds=config.conversation_timeout_seconds,
        native_tool_calling=config.native_tool_calling,
    )


def fake_user_response(conversation: Conversation) -> str:
    msg = (
        "Please continue working on the task on whatever approach you think is suitable.\n"
        "When you think you have solved the question, please use the finish tool and "
        "include your final answer in the message parameter of the finish tool.\n"
        "IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP.\n"
    )
    user_messages = [
        event
        for event in conversation.state.events
        if isinstance(event, MessageEvent) and event.source == "user"
    ]
    if len(user_messages) >= 2:
        return (
            msg
            + 'If you want to give up, use the "finish" tool to finish the interaction.\n'
        )
    return msg


def _agent_finished_with_finish_action(events: list[Any]) -> bool:
    for event in reversed(events):
        if isinstance(event, ActionEvent):
            return event.action is not None and isinstance(event.action, FinishAction)
    return False


def _agent_sent_message(events: list[Any]) -> bool:
    for event in reversed(events):
        if isinstance(event, MessageEvent) and event.source == "agent":
            return True
        if isinstance(event, ActionEvent):
            return False
    return False


def run_with_fake_user_response(
    conversation: Conversation,
    *,
    timeout_seconds: int,
    max_fake_responses: int = 10,
) -> None:
    fake_response_count = 0
    total_timeout = int(os.getenv("CONVERSATION_TIMEOUT", timeout_seconds))
    started_at = time.monotonic()
    deadline = started_at + total_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"OpenHands conversation exceeded total timeout of {total_timeout} seconds"
            )

        conversation.run(timeout=max(1, int(remaining)))

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"OpenHands conversation exceeded total timeout of {total_timeout} seconds"
            )

        status = conversation.state.execution_status
        if status != ConversationExecutionStatus.FINISHED:
            break

        events = list(conversation.state.events)
        if _agent_finished_with_finish_action(events):
            break
        if not _agent_sent_message(events):
            break
        if fake_response_count >= max_fake_responses:
            break

        conversation.send_message(fake_user_response(conversation))
        fake_response_count += 1


def host_path_for_remote(runtime_config: DockerRuntimeConfig, remote_path: str) -> Path:
    remote_workspace = runtime_config.remote_workspace.rstrip("/")
    if remote_path == remote_workspace:
        return runtime_config.host_workspace.resolve()

    prefix = remote_workspace + "/"
    if remote_path.startswith(prefix):
        return runtime_config.host_workspace.resolve() / remote_path[len(prefix):]

    raise ValueError(
        f"Remote path {remote_path!r} is not under mounted workspace "
        f"{runtime_config.remote_workspace!r}"
    )


def collect_git_patch(*, repo_path: Path, base_commit: str) -> str:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    diff = subprocess.run(
        [
            "git",
            "-c",
            "core.fileMode=false",
            "--no-pager",
            "diff",
            "--no-color",
            "--binary",
            "--cached",
            base_commit,
        ],
        cwd=repo_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return diff.stdout


def repair_host_permissions(
    *,
    workspace: HandoffDockerWorkspace,
    runtime_config: DockerRuntimeConfig,
    error_path: Path,
) -> None:
    uid_gid = host_user_group()
    container_id = getattr(workspace, "_container_id", None)
    if uid_gid is None or not container_id:
        return

    uid, gid = uid_gid
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "0",
            container_id,
            "chown",
            "-R",
            f"{uid}:{gid}",
            runtime_config.remote_workspace,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        write_json(
            error_path,
            {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )


def run_docker_openhands_task(
    *,
    prompt: str,
    llm_config: OpenHandsConfig,
    runtime_config: DockerRuntimeConfig,
    events_path: Path,
    trajectory_path: Path | None = None,
    state_path: Path | None = None,
    output_path: Path | None = None,
) -> Any:
    """Run an OpenHands task in a Docker-backed workspace.

    The host workspace is mounted at `runtime_config.remote_workspace`. Git
    snapshots are collected by executing git commands inside the container.
    """

    host_workspace = runtime_config.host_workspace.resolve()
    if not host_workspace.exists():
        raise FileNotFoundError(f"Host workspace does not exist: {host_workspace}")

    volumes = [
        f"{host_workspace}:{runtime_config.remote_workspace}",
        *runtime_config.extra_volumes,
    ]
    workspace = HandoffDockerWorkspace(
        working_dir=runtime_config.remote_workspace,
        server_image=runtime_config.server_image,
        host_port=runtime_config.host_port,
        forward_env=runtime_config.forward_env,
        volumes=volumes,
        detach_logs=runtime_config.detach_logs,
        platform=runtime_config.platform,
        extra_ports=runtime_config.extra_ports,
        enable_gpu=runtime_config.enable_gpu,
        network=runtime_config.network,
        extra_hosts=runtime_config.extra_hosts,
    )
    conversation = None
    try:
        remote_repo_path = runtime_config.remote_repo_path or runtime_config.remote_workspace
        # Mounted repos can look unsafe to git because container uid/gid differ
        # from the host. Mark only the mounted task path as safe before setup.
        workspace.execute_command(
            "git config --global --add safe.directory "
            f"{shlex.quote(remote_repo_path)}",
            timeout=30,
        )
        for command in runtime_config.setup_commands:
            result = workspace.execute_command(command, timeout=120)
            exit_code = getattr(result, "exit_code", getattr(result, "returncode", 1))
            if exit_code != 0:
                stderr = getattr(result, "stderr", "") or getattr(result, "stdout", "")
                raise RuntimeError(f"Runtime setup command failed: {command}\n{stderr}")
        recorder = RemoteEventRecorder(
            workspace=workspace,
            remote_repo_path=remote_repo_path,
            events_path=events_path,
        )
        conversation = Conversation(
            agent=build_default_agent(dockerized_openhands_config(llm_config)),
            workspace=workspace,
            callbacks=[recorder],
            max_iteration_per_run=llm_config.max_iterations,
            delete_on_close=True,
        )
        conversation.send_message(prompt)
        run_with_fake_user_response(
            conversation,
            timeout_seconds=llm_config.conversation_timeout_seconds,
        )
        if (
            output_path is not None
            and runtime_config.base_commit is not None
            and runtime_config.instance_id is not None
        ):
            repair_host_permissions(
                workspace=workspace,
                runtime_config=runtime_config,
                error_path=events_path.parent / "permission_repair_error.json",
            )
            git_patch = collect_git_patch(
                repo_path=host_path_for_remote(runtime_config, remote_repo_path),
                base_commit=runtime_config.base_commit,
            )
            output_path.write_text(
                json.dumps(
                    {
                        "instance_id": runtime_config.instance_id,
                        "attempt": 0,
                        "test_result": {"git_patch": git_patch},
                        "instruction": prompt,
                        "error": None,
                        "history": [
                            to_jsonable(event)
                            for event in conversation.state.events
                        ],
                        "metrics": to_jsonable(
                            conversation.conversation_stats.get_combined_metrics()
                        ),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return conversation
    finally:
        if conversation is not None:
            try:
                if trajectory_path is not None:
                    save_openhands_trajectory(conversation, trajectory_path)
                if state_path is not None:
                    save_openhands_state(conversation, state_path)
            except Exception as exc:
                write_json(
                    events_path.parent / "trajectory_error.json",
                    {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
        try:
            repair_host_permissions(
                workspace=workspace,
                runtime_config=runtime_config,
                error_path=events_path.parent / "permission_repair_error.json",
            )
        except Exception as exc:
            write_json(
                events_path.parent / "permission_repair_error.json",
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        if conversation is not None and hasattr(conversation, "close"):
            conversation.close()
        workspace.cleanup()
