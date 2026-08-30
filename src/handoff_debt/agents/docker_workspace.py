"""OpenHands Docker workspace extension for host model access."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from typing import Any

from openhands.sdk.logger import get_logger
from openhands.sdk.utils.command import execute_command
from openhands.workspace import DockerWorkspace
from openhands.workspace.docker.workspace import (
    check_port_available,
    find_available_tcp_port,
)
from pydantic import Field

logger = get_logger(__name__)


class HandoffDockerWorkspace(DockerWorkspace):
    """DockerWorkspace with host aliases for host-side local model servers."""

    extra_hosts: dict[str, str] = Field(default_factory=dict)

    def _start_container(self, image: str, context: Any) -> None:
        self._image_name = image

        if self.host_port is None:
            self.host_port = find_available_tcp_port()
        else:
            self.host_port = int(self.host_port)

        if not check_port_available(self.host_port):
            raise RuntimeError(f"Port {self.host_port} is not available")

        if self.extra_ports:
            if not check_port_available(self.host_port + 1):
                raise RuntimeError(
                    f"Port {self.host_port + 1} is not available for VSCode"
                )
            if not check_port_available(self.host_port + 2):
                raise RuntimeError(
                    f"Port {self.host_port + 2} is not available for VNC"
                )

        docker_ver = execute_command(["docker", "version"]).returncode
        if docker_ver != 0:
            raise RuntimeError(
                "Docker is not available. Please install and start "
                "Docker Desktop/daemon."
            )

        flags: list[str] = []
        for key in self.forward_env:
            if key in os.environ:
                flags += ["-e", f"{key}={os.environ[key]}"]

        for volume in self.volumes:
            flags += ["-v", volume]
            logger.info("Adding volume mount: %s", volume)

        for host_name, host_value in self.extra_hosts.items():
            flags += ["--add-host", f"{host_name}:{host_value}"]

        # OpenHands agent-server listens on 8000 inside the container.
        ports = ["-p", f"{self.host_port}:8000"]
        if self.extra_ports:
            ports += [
                "-p",
                f"{self.host_port + 1}:8001",
                "-p",
                f"{self.host_port + 2}:8002",
            ]
        flags += ports

        if self.enable_gpu:
            flags += ["--gpus", "all"]

        if self.network:
            flags += ["--network", self.network]

        run_cmd = [
            "docker",
            "run",
            "-d",
            "--platform",
            self.platform,
            "--rm",
            "--ulimit",
            "nofile=65536:65536",
            "--name",
            f"agent-server-{uuid.uuid4()}",
            *flags,
            image,
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
        proc = execute_command(run_cmd)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to run docker container: {proc.stderr}")

        self._container_id = proc.stdout.strip()
        logger.info("Started container: %s", self._container_id)

        if self.detach_logs:
            self._logs_thread = threading.Thread(
                target=self._stream_docker_logs, daemon=True
            )
            self._logs_thread.start()

        if not self.host:
            object.__setattr__(self, "host", f"http://127.0.0.1:{self.host_port}")
        object.__setattr__(self, "api_key", None)

        self._wait_for_health(timeout=self.health_check_timeout)
        logger.info("Docker workspace is ready at %s", self.host)
        super(DockerWorkspace, self).model_post_init(context)

    def _stream_docker_logs(self) -> None:
        if not self._container_id:
            return
        try:
            proc = subprocess.Popen(
                ["docker", "logs", "-f", self._container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if proc.stdout is None:
                return
            for line in iter(proc.stdout.readline, ""):
                if self._stop_logs.is_set():
                    break
                if line:
                    sys.stdout.write(f"[DOCKER] {line}")
                    sys.stdout.flush()
        except Exception as exc:
            sys.stderr.write(f"Error streaming docker logs: {exc}\n")
        finally:
            self._stop_logs.set()
