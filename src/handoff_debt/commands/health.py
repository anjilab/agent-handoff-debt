"""Runtime health checks for Docker and model endpoint wiring."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from handoff_debt.agents.docker_workspace import HandoffDockerWorkspace
from handoff_debt.agents.openhands_adapter import DockerRuntimeConfig
from handoff_debt.config import dockerized_openhands_config, load_openhands_config
from handoff_debt.tracing.remote_git_snapshots import snapshot_remote_git_state
from handoff_debt.workspaces import run_checked


def cmd_doctor(args: argparse.Namespace) -> int:
    docker = subprocess.run(
        ["docker", "version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    docker_ok = docker.returncode == 0
    print(f"docker: {'ok' if docker_ok else 'missing'}")

    if args.image:
        print(f"agent_server_image: {args.image}")
    if args.agent_config:
        llm_config = load_openhands_config(Path(args.agent_config))
        docker_config = dockerized_openhands_config(llm_config)
        print(f"agent_model: {llm_config.model}")
        print(f"agent_base_url: {llm_config.base_url}")
        print(f"docker_agent_base_url: {docker_config.base_url}")

    if not docker_ok:
        stderr = docker.stderr.strip()
        if stderr:
            print(f"docker_error: {stderr}")
        return 1
    return 0


def cmd_model_smoke(args: argparse.Namespace) -> int:
    llm_config = load_openhands_config(Path(args.agent_config))
    if llm_config.base_url is None:
        raise SystemExit("Agent config has no base_url to check")

    endpoint = llm_config.base_url.rstrip("/") + "/models"
    with urllib.request.urlopen(endpoint, timeout=args.timeout) as response:
        body = response.read().decode("utf-8")

    print(f"agent_model: {llm_config.model}")
    print(f"agent_base_url: {llm_config.base_url}")
    print(f"models_endpoint: ok ({endpoint})")
    if args.show_body:
        print(body)
    return 0


def cmd_docker_smoke(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="handoff-docker-smoke-") as temp_dir:
        repo = Path(temp_dir) / "repo"
        repo.mkdir()
        run_checked(["git", "init"], repo)
        run_checked(["git", "config", "user.email", "smoke@example.com"], repo)
        run_checked(["git", "config", "user.name", "Smoke Test"], repo)
        (repo / "README.md").write_text("smoke\n")
        run_checked(["git", "add", "README.md"], repo)
        run_checked(["git", "commit", "-m", "init"], repo)

        runtime = DockerRuntimeConfig(
            host_workspace=repo,
            server_image=args.image,
            detach_logs=args.detach_logs,
        )
        workspace = HandoffDockerWorkspace(
            working_dir=runtime.remote_workspace,
            volumes=[f"{runtime.host_workspace}:{runtime.remote_workspace}"],
            server_image=runtime.server_image,
            detach_logs=runtime.detach_logs,
            platform=runtime.platform,
            extra_hosts=runtime.extra_hosts,
        )
        try:
            workspace.execute_command(
                f"git config --global --add safe.directory {runtime.remote_workspace}",
                timeout=30,
            )
            snapshot = snapshot_remote_git_state(
                workspace,
                runtime.remote_workspace,
                step=0,
            )
            if snapshot.head is None:
                raise SystemExit("Docker git snapshot failed: no HEAD detected")
            print("docker_workspace: ok")
            print(f"remote_workspace: {runtime.remote_workspace}")
            print(f"head: {snapshot.head}")
            if args.agent_config:
                llm_config = load_openhands_config(Path(args.agent_config))
                docker_config = dockerized_openhands_config(llm_config)
                if docker_config.base_url is None:
                    raise SystemExit("Agent config has no base_url to check")
                result = workspace.execute_command(
                    (
                        "python -c \"import urllib.request; "
                        f"r=urllib.request.urlopen('{docker_config.base_url}/models', timeout=5); "
                        "print(r.status)\""
                    ),
                    cwd=runtime.remote_workspace,
                    timeout=10,
                )
                if getattr(result, "exit_code", 1) != 0:
                    raise SystemExit(
                        "Docker cannot reach configured model endpoint: "
                        f"{getattr(result, 'stderr', '')}"
                    )
                print(f"docker_model_endpoint: ok ({docker_config.base_url})")
            return 0
        finally:
            workspace.cleanup()
