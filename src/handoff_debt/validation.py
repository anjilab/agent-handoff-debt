"""Task validation backends."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SETUP_FILES_TO_REMOVE = {"pyproject.toml", "tox.ini", "setup.py"}
GENERATED_ARTIFACT_DIRS_TO_REMOVE = {
    ".doctrees",
    "__pycache__",
    "_build",
    "build",
    "dist",
    "htmlcov",
}
GENERATED_ARTIFACT_FILES_TO_REMOVE = {
    ".coverage",
    "objects.inv",
}
GENERATED_ARTIFACT_SUFFIXES_TO_REMOVE = {
    ".a",
    ".class",
    ".dll",
    ".dylib",
    ".egg",
    ".gz",
    ".jar",
    ".mo",
    ".o",
    ".pdf",
    ".pickle",
    ".png",
    ".pyc",
    ".pyo",
    ".so",
    ".tar",
    ".whl",
    ".zip",
}
MAX_SWEBENCH_RUN_ID_LENGTH = 96
DEFAULT_VALIDATION_LOCK_PATH = "/tmp/handoff-debt-swebench-validation.lock"


def _print_stage(message: str) -> None:
    print(f"[handoff-debt] {message}", flush=True)


@contextmanager
def official_validation_lock():
    """Serialize official SWE-bench validation across parallel workers.

    The SWE-bench harness runs one instance at a time here, but its Docker
    cleanup/reporting code still lists and inspects daemon-wide images and
    containers. A process-wide file lock prevents concurrent validation jobs
    from racing on those global Docker resources.
    """

    lock_path = Path(
        os.getenv("HANDOFF_VALIDATION_LOCK_PATH", DEFAULT_VALIDATION_LOCK_PATH)
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        _print_stage(f"Waiting for official SWE-bench validation lock: {lock_path}")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _print_stage("Acquired official SWE-bench validation lock.")
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            _print_stage("Released official SWE-bench validation lock.")


def validation_python_executable() -> str:
    """Return a live Python executable for launching the official harness."""

    executable = Path(sys.executable)
    if executable.exists():
        return str(executable)
    fallback = shutil.which("python3") or shutil.which("python")
    if fallback:
        return fallback
    return sys.executable


def make_swebench_run_id(*, output_dir: Path, suffix: str = "official") -> str:
    """Build a Docker-safe SWE-bench run id unique to this validation output."""

    source = str(output_dir.parent.resolve())
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    parts = output_dir.parent.parts[-6:]
    stem = "__".join(parts)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._-")
    budget = MAX_SWEBENCH_RUN_ID_LENGTH - len(suffix) - len(digest) - 2
    safe = safe[: max(1, budget)].strip("._-")
    return f"{safe}_{suffix}_{digest}"


def _paths_from_diff_header(header: str) -> list[str]:
    if not header.startswith("diff --git "):
        return []
    parts = header.strip().split()
    paths: list[str] = []
    for token in parts[2:4]:
        if token.startswith("a/") or token.startswith("b/"):
            paths.append(token[2:])
    return paths


def _is_generated_or_scratch_path(path: str) -> bool:
    parts = Path(path).parts
    if not parts:
        return False

    top_level = parts[0]
    name = parts[-1]
    suffix = Path(name).suffix.lower()

    if name in SETUP_FILES_TO_REMOVE:
        return True
    if name in GENERATED_ARTIFACT_FILES_TO_REMOVE:
        return True
    if suffix in GENERATED_ARTIFACT_SUFFIXES_TO_REMOVE:
        return True
    if any(part in GENERATED_ARTIFACT_DIRS_TO_REMOVE for part in parts):
        return True
    if top_level.startswith(("repro", "reproduce")):
        return True
    return False


def remove_setup_file_diffs(patch: str) -> str:
    """Remove harness/setup and generated scratch artifacts from validation patches."""

    if not patch.strip():
        return patch

    chunks: list[list[str]] = []
    current: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                chunks.append(current)
            current = [line]
        elif current:
            current.append(line)
        else:
            current = [line]
    if current:
        chunks.append(current)

    kept: list[str] = []
    for chunk in chunks:
        header = chunk[0]
        if any(_is_generated_or_scratch_path(path) for path in _paths_from_diff_header(header)):
            continue
        kept.extend(chunk)
    return "".join(kept)


def collect_benchmark_patch(workspace: Path, base_commit: str) -> dict[str, Any]:
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if add.returncode != 0:
        return {
            "ok": False,
            "exit_code": add.returncode,
            "stdout": add.stdout,
            "stderr": add.stderr,
            "error": "failed_to_stage_workspace",
        }

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
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode != 0:
        return {
            "ok": False,
            "exit_code": diff.returncode,
            "stdout": diff.stdout.decode("utf-8", errors="replace"),
            "stderr": diff.stderr.decode("utf-8", errors="replace"),
            "error": "failed_to_collect_workspace_diff",
        }

    raw_patch = diff.stdout.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "raw_patch": raw_patch,
        "model_patch": remove_setup_file_diffs(raw_patch),
    }


def prediction_from_openhands_output(
    *,
    output_path: Path,
    instance_id: str,
    model_name: str,
) -> dict[str, Any]:
    for line in output_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("instance_id") != instance_id:
            continue
        git_patch = str((row.get("test_result") or {}).get("git_patch") or "")
        return {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": remove_setup_file_diffs(git_patch),
            "raw_patch": git_patch,
        }
    raise ValueError(f"No OpenHands output row found for {instance_id} in {output_path}")


def prediction_from_workspace(
    *,
    workspace: Path,
    instance_id: str,
    base_commit: str,
    model_name: str,
) -> dict[str, Any]:
    patch = collect_benchmark_patch(workspace, base_commit)
    if not patch["ok"]:
        return patch
    return {
        "ok": True,
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": patch["model_patch"],
        "raw_patch": patch["raw_patch"],
    }


def run_swebench_official_validation(
    *,
    workspace: Path,
    output_dir: Path,
    instance_id: str,
    base_commit: str,
    openhands_output_path: Path | None = None,
    dataset_name: str = "SWE-bench/SWE-bench_Verified",
    split: str = "test",
    model_name: str = "handoff-debt",
    max_workers: int = 1,
    timeout: int = 1800,
    cache_level: str = "env",
    clean: bool = False,
    namespace: str | None = "swebench",
) -> dict[str, Any]:
    """Evaluate the workspace diff with SWE-bench's official Docker harness."""

    workspace = workspace.resolve()
    output_dir = output_dir.resolve()
    try:
        if openhands_output_path is not None and openhands_output_path.exists():
            prediction = prediction_from_openhands_output(
                output_path=openhands_output_path,
                instance_id=instance_id,
                model_name=model_name,
            )
        else:
            prediction = prediction_from_workspace(
                workspace=workspace,
                instance_id=instance_id,
                base_commit=base_commit,
                model_name=model_name,
            )
    except Exception as exc:
        return {
            "backend": "swebench_official",
            "passed": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }

    if not prediction.get("ok", True):
        return {
            "backend": "swebench_official",
            "passed": False,
            "exit_code": prediction.get("exit_code"),
            "stdout": prediction.get("stdout", ""),
            "stderr": prediction.get("stderr", ""),
            "error": prediction["error"],
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = (output_dir / "predictions.jsonl").resolve()
    swebench_prediction = {
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": prediction["model_patch"],
    }
    predictions_path.write_text(json.dumps(swebench_prediction, sort_keys=True) + "\n")
    (output_dir / "raw_model.patch").write_text(str(prediction["raw_patch"]))
    (output_dir / "model.patch").write_text(str(prediction["model_patch"]))

    run_id = make_swebench_run_id(output_dir=output_dir)
    cmd = [
        validation_python_executable(),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        predictions_path.name,
        "--max_workers",
        str(max_workers),
        "--run_id",
        run_id,
        "--split",
        split,
        "--timeout",
        str(timeout),
    ]
    stdout_path = output_dir / "validation.live.log"
    _print_stage(
        "Running official SWE-bench validation "
        f"for {instance_id}; live logs are also saved under {output_dir}"
    )
    _print_stage("Command: " + " ".join(cmd))
    stdout_chunks: list[str] = []
    with official_validation_lock():
        with stdout_path.open("w") as stdout_file:
            try:
                process = subprocess.Popen(
                    cmd,
                    cwd=output_dir,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                message = f"{type(exc).__name__}: {exc}"
                stdout_file.write(message + "\n")
                stdout_file.flush()
                return {
                    "backend": "swebench_official",
                    "passed": False,
                    "predictions_path": str(predictions_path),
                    "stdout_path": str(stdout_path),
                    "stdout": message + "\n",
                    "stderr": "",
                    "exit_code": None,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
            assert process.stdout is not None
            for line in process.stdout:
                stdout_chunks.append(line)
                print(line, end="", flush=True)
                stdout_file.write(line)
                stdout_file.flush()
            returncode = process.wait()
    stdout = "".join(stdout_chunks)
    _print_stage(
        "Official SWE-bench validation finished "
        f"with exit_code={returncode}"
    )
    if returncode != 0:
        return {
            "backend": "swebench_official",
            "passed": False,
            "predictions_path": str(predictions_path),
            "stdout_path": str(stdout_path),
            "stdout": stdout,
            "stderr": "",
            "exit_code": returncode,
        }

    report_file = output_dir / f"{model_name}.{run_id}.json"
    report = (
        json.loads(report_file.read_text())
        if report_file and report_file.exists()
        else {}
    )
    return {
        "backend": "swebench_official",
        "passed": instance_id in set(report.get("resolved_ids", [])),
        "predictions_path": str(predictions_path),
        "raw_patch_path": str(output_dir / "raw_model.patch"),
        "model_patch_path": str(output_dir / "model.patch"),
        "stdout_path": str(stdout_path),
        "report_path": str(report_file) if report_file else None,
        "report": report,
        "stdout": stdout,
        "stderr": "",
    }


def run_task_validation(
    *,
    task: dict[str, str],
    workspace: Path,
    run_dir: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    backend = task.get("validation_backend")
    if backend == "swebench_official":
        return run_swebench_official_validation(
            workspace=workspace,
            output_dir=run_dir / "swebench_official",
            instance_id=task["swebench_instance_id"],
            base_commit=task["swebench_base_commit"],
            openhands_output_path=output_path,
            dataset_name=task.get("swebench_dataset", "SWE-bench/SWE-bench_Verified"),
            split=task.get("swebench_split", "test"),
            model_name=task.get("swebench_model_name", "handoff-debt"),
        )
    return {
        "backend": backend,
        "passed": False,
        "error": "unsupported_validation_backend",
    }
