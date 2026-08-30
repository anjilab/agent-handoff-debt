"""Initial-run and takeover-run lifecycle commands."""

from __future__ import annotations

import argparse
import subprocess
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from handoff_debt.agents.openhands_adapter import (
    DockerRuntimeConfig,
    run_docker_openhands_task,
)
from handoff_debt.config import load_openhands_config
from handoff_debt.handoff.summary_notes import build_summary_notes_handoff
from handoff_debt.handoff.prompt_parts import TAKEOVER_PROMPT_VERSION
from handoff_debt.handoff.raw_trace import build_raw_trace_handoff
from handoff_debt.handoff.repo_only import build_repo_only_handoff
from handoff_debt.handoff.structured_notes import build_structured_notes_handoff
from handoff_debt.io import read_json, write_json
from handoff_debt.reporting import (
    summarize_official_validation_json,
    summarize_run_json,
)
from handoff_debt.scoring import build_takeover_score
from handoff_debt.schemas import Checkpoint, HandoffPackage, TaskSpec
from handoff_debt.task_config import load_task_config
from handoff_debt.tracing.checkpoints import (
    detect_lifecycle_from_jsonl,
)
from handoff_debt.validation import run_swebench_official_validation, run_task_validation
from handoff_debt.workspaces import (
    create_checkpoint_workspace,
    diff_from_base,
    make_workspace_writable,
    run_git,
)


def make_run_id(task_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{task_id}_{stamp}"


def log_stage(message: str) -> None:
    print(f"[handoff-debt] {message}", flush=True)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(start: float) -> float:
    return round(time.perf_counter() - start, 3)


def finish_timings(timings: dict, total_start: float) -> dict:
    timings["ended_at"] = now_utc_iso()
    timings["total_seconds"] = elapsed_seconds(total_start)
    return timings


def load_llm_config_with_override(agent_config: str, max_iterations: int | None):
    llm_config = load_openhands_config(Path(agent_config))
    if max_iterations is None:
        return llm_config
    return type(llm_config)(
        model=llm_config.model,
        api_key=llm_config.api_key,
        base_url=llm_config.base_url,
        max_iterations=max_iterations,
        conversation_timeout_seconds=llm_config.conversation_timeout_seconds,
        native_tool_calling=llm_config.native_tool_calling,
    )


def materialize_testbed_from_image(
    *, image: str, workspace: Path, platform: str
) -> None:
    """Copy the official SWE-bench /testbed repo into a persistent host workspace."""

    workspace.mkdir(parents=True, exist_ok=True)
    create = subprocess.run(
        ["docker", "create", "--platform", platform, image],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create image container: {create.stderr}")

    container_id = create.stdout.strip()
    try:
        copy = subprocess.run(
            ["docker", "cp", f"{container_id}:/testbed/.", str(workspace)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if copy.returncode != 0:
            raise RuntimeError(f"Failed to copy /testbed from image: {copy.stderr}")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def build_swebench_image_runtime_config(
    *,
    task: dict[str, str],
    run_dir: Path,
    server_image: str | None,
    detach_logs: bool,
) -> tuple[DockerRuntimeConfig, Path, str]:
    image = server_image or task.get("openhands_server_image")
    if not image:
        raise SystemExit(
            "OpenHands SWE-bench image runtime requires --server-image or "
            "openhands_server_image in the task config."
        )

    repo_path = task["remote_repo_path"]
    repo_dir = task.get("repo_dir") or Path(repo_path).name
    workspace = run_dir / repo_dir
    materialize_testbed_from_image(
        image=image,
        workspace=workspace,
        platform=DockerRuntimeConfig.platform,
    )
    make_workspace_writable(workspace)
    quoted_repo_path = shlex.quote(repo_path)
    setup_commands = [
        f"cd {quoted_repo_path} && git reset --hard",
        f"cd {quoted_repo_path} && git config core.filemode false",
    ]
    return (
        DockerRuntimeConfig(
            host_workspace=workspace,
            remote_workspace=repo_path,
            remote_repo_path=repo_path,
            server_image=image,
            detach_logs=detach_logs,
            setup_commands=setup_commands,
            base_commit=str(task["swebench_base_commit"]),
            instance_id=str(task["swebench_instance_id"]),
        ),
        workspace,
        repo_path,
    )


def run_checkpoint_official_validation(
    *,
    task: dict[str, str],
    checkpoint: Checkpoint,
    run_dir: Path,
) -> dict:
    if checkpoint.workspace_snapshot is None:
        return {
            "backend": task.get("validation_backend"),
            "passed": False,
            "error": "checkpoint_workspace_not_created",
        }
    if task.get("validation_backend") != "swebench_official":
        return {
            "backend": task.get("validation_backend"),
            "passed": False,
            "error": "unsupported_validation_backend",
        }

    checkpoint_dir = run_dir / "checkpoints" / checkpoint.checkpoint_id
    return run_swebench_official_validation(
        workspace=Path(checkpoint.workspace_snapshot),
        output_dir=checkpoint_dir / "swebench_official",
        instance_id=task["swebench_instance_id"],
        base_commit=task["swebench_base_commit"],
        openhands_output_path=None,
        dataset_name=task.get("swebench_dataset", "SWE-bench/SWE-bench_Verified"),
        split=task.get("swebench_split", "test"),
        model_name=f"{task.get('swebench_model_name', 'handoff-debt')}.checkpoint",
    )


def run_lifecycle_official_validations(
    *,
    task: dict[str, str],
    checkpoints: list[Checkpoint],
    run_dir: Path,
    force: bool = False,
) -> tuple[list[dict], dict]:
    """Run official SWE-bench validation for every standard lifecycle checkpoint."""

    summaries: list[dict] = []
    timing = {
        "duration_seconds": 0.0,
        "checkpoint_count": len(checkpoints),
        "validated_count": 0,
        "per_checkpoint": [],
    }
    phase_start = time.perf_counter()
    for checkpoint in checkpoints:
        checkpoint_dir = run_dir / "checkpoints" / checkpoint.checkpoint_id
        validation_path = checkpoint_dir / "validation.json"
        output_dir = checkpoint_dir / "swebench_official"

        item_start = time.perf_counter()
        if validation_path.exists() and not force:
            validation = read_json(validation_path)
            log_stage(
                "Reusing official SWE-bench validation for checkpoint "
                f"{checkpoint.checkpoint_id}."
            )
        else:
            log_stage(
                "Running official SWE-bench validation for checkpoint "
                f"{checkpoint.checkpoint_id}..."
            )
            validation = run_checkpoint_official_validation(
                task=task,
                checkpoint=checkpoint,
                run_dir=run_dir,
            )
            write_json(validation_path, validation)

        summary = summarize_official_validation_json(
            checkpoint=checkpoint.to_dict(),
            output_dir=output_dir,
            validation={**validation, "validation_path": str(validation_path)},
        )
        summaries.append(summary)
        timing["validated_count"] += 1
        timing["per_checkpoint"].append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "kind": checkpoint.kind,
                "duration_seconds": elapsed_seconds(item_start),
                "passed": validation.get("passed"),
            }
        )
        log_stage(
            "Checkpoint official validation finished "
            f"for {checkpoint.checkpoint_id}: passed={validation.get('passed')}"
        )

    timing["duration_seconds"] = elapsed_seconds(phase_start)
    write_json(run_dir / "checkpoint_validations.json", summaries)
    return summaries, timing


HANDOFF_VIEWS = ("repo_only", "raw_trace", "structured_notes", "summary_notes")


def precomputed_handoff_dir(
    initial_run_dir: Path,
    checkpoint_id: str,
    handoff_view: str,
) -> Path:
    return initial_run_dir / "precomputed_handoffs" / checkpoint_id / handoff_view


def precomputed_handoff_path(
    initial_run_dir: Path,
    checkpoint_id: str,
    handoff_view: str,
) -> Path:
    return precomputed_handoff_dir(initial_run_dir, checkpoint_id, handoff_view) / (
        f"handoff_{handoff_view}.json"
    )


def write_precomputed_handoff(
    *,
    initial_run_dir: Path,
    task_spec: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
    handoff_view: str,
    generator_config,
    generator_config_path: str,
    force: bool = False,
) -> dict:
    output_dir = precomputed_handoff_dir(
        initial_run_dir,
        checkpoint.checkpoint_id,
        handoff_view,
    )
    handoff_path = precomputed_handoff_path(
        initial_run_dir,
        checkpoint.checkpoint_id,
        handoff_view,
    )
    metadata_path = output_dir / "metadata.json"
    if handoff_path.exists() and (output_dir / "prompt.md").exists() and not force:
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_kind": checkpoint.kind,
            "handoff_view": handoff_view,
            "path": str(output_dir),
            "skipped": "already_exists",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    handoff = build_handoff(
        handoff_view,
        task_spec,
        checkpoint,
        events_path,
        summarizer_config=generator_config,
        artifact_dir=output_dir / "artifacts",
    )
    (output_dir / "prompt.md").write_text(handoff.prompt)
    write_json(handoff_path, handoff.__dict__)
    write_json(
        metadata_path,
        {
            "prompt_version": TAKEOVER_PROMPT_VERSION,
            "initial_run": str(initial_run_dir),
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_kind": checkpoint.kind,
            "handoff_view": handoff_view,
            "generator_agent_config": generator_config_path,
            "generator_model": getattr(generator_config, "model", None),
        },
    )
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_kind": checkpoint.kind,
        "handoff_view": handoff_view,
        "path": str(output_dir),
        "skipped": None,
    }


def merge_precomputed_handoff_manifest(
    initial_run_dir: Path,
    results: list[dict],
) -> list[dict]:
    manifest_path = initial_run_dir / "precomputed_handoffs.json"
    existing: list[dict] = []
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if isinstance(manifest, list):
            existing = [item for item in manifest if isinstance(item, dict)]

    merged: dict[tuple[str, str], dict] = {}
    for item in [*existing, *results]:
        checkpoint_id = item.get("checkpoint_id")
        handoff_view = item.get("handoff_view")
        if not checkpoint_id or not handoff_view:
            continue
        if handoff_view not in HANDOFF_VIEWS:
            continue
        merged[(checkpoint_id, handoff_view)] = item

    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("checkpoint_id", "")),
            str(item.get("handoff_view", "")),
        ),
    )


def precompute_handoffs(
    *,
    initial_run_dir: Path,
    task_spec: TaskSpec,
    checkpoints: list[Checkpoint],
    events_path: Path,
    generator_config,
    generator_config_path: str,
    views: list[str],
    force: bool = False,
) -> tuple[list[dict], dict]:
    phase_start = time.perf_counter()
    results: list[dict] = []
    for checkpoint in checkpoints:
        for handoff_view in views:
            log_stage(
                "Precomputing handoff "
                f"{checkpoint.checkpoint_id}/{handoff_view}..."
            )
            results.append(
                write_precomputed_handoff(
                    initial_run_dir=initial_run_dir,
                    task_spec=task_spec,
                    checkpoint=checkpoint,
                    events_path=events_path,
                    handoff_view=handoff_view,
                    generator_config=generator_config,
                    generator_config_path=generator_config_path,
                    force=force,
                )
            )

    timing = {
        "duration_seconds": elapsed_seconds(phase_start),
        "checkpoint_count": len(checkpoints),
        "view_count": len(views),
        "artifact_count": len(results),
    }
    manifest = merge_precomputed_handoff_manifest(initial_run_dir, results)
    write_json(initial_run_dir / "precomputed_handoffs.json", manifest)
    return manifest, timing


def write_no_checkpoint_handoff_manifest(initial_run_dir: Path) -> dict:
    manifest = {
        "status": "skipped_no_checkpoints",
        "initial_run": str(initial_run_dir),
        "reason": "No lifecycle checkpoints found",
    }
    write_json(initial_run_dir / "precomputed_handoffs.json", manifest)
    return {
        "duration_seconds": 0.0,
        "checkpoint_count": 0,
        "view_count": 0,
        "artifact_count": 0,
        "skipped": "no_lifecycle_checkpoints",
    }


def load_precomputed_handoff(
    *,
    initial_run_dir: Path,
    checkpoint: Checkpoint,
    handoff_view: str,
) -> tuple[HandoffPackage, Path, dict]:
    handoff_path = precomputed_handoff_path(
        initial_run_dir,
        checkpoint.checkpoint_id,
        handoff_view,
    )
    if not handoff_path.exists():
        raise SystemExit(
            "Missing precomputed handoff artifact for takeover: "
            f"{handoff_path}. Run `handoff-debt precompute-handoffs` for the "
            "initial run using the predecessor model before takeover."
        )
    data = read_json(handoff_path)
    metadata_path = handoff_path.parent / "metadata.json"
    metadata = read_json(metadata_path) if metadata_path.exists() else {}
    return HandoffPackage(**data), handoff_path.parent, metadata


def _run_workspace_for_initial(run_dir: Path, task: dict[str, str]) -> Path:
    repo_dir = task.get("repo_dir") or Path(str(task["remote_repo_path"])).name
    workspace = run_dir / repo_dir
    if workspace.exists():
        return workspace
    return run_dir / "workspace"


def cmd_finalize_run(args: argparse.Namespace) -> int:
    total_start = time.perf_counter()
    run_dir = Path(args.run_dir)
    metadata = read_json(run_dir / "metadata.json")
    if not metadata:
        raise SystemExit(f"Missing metadata.json in {run_dir}")
    if metadata.get("role", "initial") != "initial":
        raise SystemExit("finalize-run currently supports initial runs only")

    task = load_task_config(Path(str(metadata["task_config"])))
    run_id = str(metadata.get("run_id") or run_dir.name)
    base_commit = str(metadata["base_commit"])
    workspace = _run_workspace_for_initial(run_dir, task)
    events_path = Path(str(metadata.get("events_path") or run_dir / "events.jsonl"))
    output_path = Path(str(metadata.get("output_path") or run_dir / "output.jsonl"))
    timings_path = run_dir / "timings.json"
    timings = read_json(timings_path) if timings_path.exists() else {
        "role": "initial",
        "started_at": now_utc_iso(),
        "phases": {},
    }
    phases = timings.setdefault("phases", {})

    generator_config_path = args.agent_config or str(metadata["agent_config"])
    llm_config = load_openhands_config(Path(generator_config_path))

    if not workspace.exists():
        write_json(
            run_dir / "run_error.json",
            {"type": "WorkspaceMissing", "message": f"Missing workspace: {workspace}"},
        )
        write_json(timings_path, finish_timings(timings, total_start))
        write_json(run_dir / "summary.json", summarize_run_json(run_dir))
        return 1

    try:
        make_workspace_writable(workspace)

        checkpoint = None
        if events_path.exists():
            log_stage("Detecting checkpoint from recorded events...")
            phase_start = time.perf_counter()
            lifecycle_checkpoints = detect_lifecycle_from_jsonl(events_path)
            lifecycle_by_id: dict[str, Checkpoint] = {}
            if lifecycle_checkpoints:
                log_stage(
                    "Detected lifecycle checkpoints: "
                    + ", ".join(item.kind for item in lifecycle_checkpoints)
                )
                for lifecycle_checkpoint in lifecycle_checkpoints:
                    lifecycle_by_id[lifecycle_checkpoint.checkpoint_id] = (
                        create_checkpoint_workspace(
                            run_dir=run_dir,
                            source_workspace=workspace,
                            checkpoint=lifecycle_checkpoint,
                            base_commit=base_commit,
                        )
                    )
                write_json(
                    run_dir / "lifecycle_checkpoints.json",
                    [item.to_dict() for item in lifecycle_by_id.values()],
                )
                if (
                    args.force_validation
                    or not (run_dir / "checkpoint_validations.json").exists()
                ):
                    checkpoint_summaries, checkpoint_timing = (
                        run_lifecycle_official_validations(
                            task=task,
                            checkpoints=list(lifecycle_by_id.values()),
                            run_dir=run_dir,
                            force=args.force_validation,
                        )
                    )
                    phases["lifecycle_checkpoint_validation"] = checkpoint_timing
                else:
                    checkpoint_summaries = read_json(
                        run_dir / "checkpoint_validations.json"
                    )
                    phases.setdefault(
                        "lifecycle_checkpoint_validation",
                        {
                            "duration_seconds": 0.0,
                            "checkpoint_count": len(lifecycle_checkpoints),
                            "validated_count": len(checkpoint_summaries),
                            "skipped": "already_exists",
                        },
                    )
                task_spec = _task_spec_from_initial_run(run_dir, metadata, task)
                _, handoff_timing = precompute_handoffs(
                    initial_run_dir=run_dir,
                    task_spec=task_spec,
                    checkpoints=list(lifecycle_by_id.values()),
                    events_path=events_path,
                    generator_config=llm_config,
                    generator_config_path=generator_config_path,
                    views=list(HANDOFF_VIEWS),
                    force=args.force_handoffs,
                )
                phases["handoff_precompute"] = handoff_timing
            else:
                checkpoint_summaries = []
                phases["handoff_precompute"] = write_no_checkpoint_handoff_manifest(
                    run_dir
                )

            phases["checkpoint_detection"] = {
                "duration_seconds": elapsed_seconds(phase_start),
                "lifecycle_checkpoint_count": len(lifecycle_checkpoints),
            }
        else:
            phases["checkpoint_detection"] = {
                "duration_seconds": 0.0,
                "skipped": "missing_events",
            }

        if not (run_dir / "validation.json").exists() or args.force_validation:
            log_stage("Running task validation...")
            phase_start = time.perf_counter()
            validation = run_task_validation(
                task=task,
                workspace=workspace,
                run_dir=run_dir,
                output_path=output_path if output_path.exists() else None,
            )
            write_json(run_dir / "validation.json", validation)
            phases["validation"] = {
                "duration_seconds": elapsed_seconds(phase_start),
                "passed": validation.get("passed"),
            }

        run_error_path = run_dir / "run_error.json"
        if run_error_path.exists():
            run_error = read_json(run_error_path)
            if run_error.get("type") != "ConversationRunError":
                run_error_path.unlink()
        write_json(timings_path, finish_timings(timings, total_start))
        write_json(run_dir / "summary.json", summarize_run_json(run_dir))
        summary = summarize_run_json(run_dir)
        print(f"run_id: {run_id}")
        print(f"run_dir: {run_dir}")
        print(f"validation_passed: {summary.get('validation_passed')}")
        print(
            "lifecycle_checkpoints: "
            f"{summary.get('lifecycle_checkpoint_count') or 0}"
        )
        return 0 if summary.get("validation_passed") else 1
    except Exception as exc:
        phases["finalization"] = {
            "duration_seconds": elapsed_seconds(total_start),
            "failed": True,
        }
        write_json(
            run_dir / "run_error.json",
            {"type": type(exc).__name__, "message": str(exc)},
        )
        write_json(timings_path, finish_timings(timings, total_start))
        write_json(run_dir / "summary.json", summarize_run_json(run_dir))
        raise


def cmd_run_task(args: argparse.Namespace) -> int:
    total_start = time.perf_counter()
    timings: dict = {
        "role": "initial",
        "started_at": now_utc_iso(),
        "phases": {},
    }
    task_config_path = Path(args.task_config)
    task = load_task_config(task_config_path)
    task_id = task["swebench_instance_id"]
    run_id = args.run_id or make_run_id(task_id)
    run_dir = Path(args.runs_dir) / run_id
    workspace = run_dir / "workspace"
    events_path = run_dir / "events.jsonl"
    trajectory_path = run_dir / "trajectory.json"
    state_path = run_dir / "state.json"
    output_path = run_dir / "output.jsonl"
    timings_path = run_dir / "timings.json"

    if run_dir.exists():
        raise SystemExit(f"Run directory already exists: {run_dir}")

    prompt_path = Path(task["prompt"])
    if not prompt_path.exists():
        raise SystemExit(f"Task prompt does not exist: {prompt_path}")

    run_dir.mkdir(parents=True)
    make_workspace_writable(run_dir)
    prompt = prompt_path.read_text()
    base_commit = task["swebench_base_commit"]
    runtime_config, workspace, remote_repo_path = build_swebench_image_runtime_config(
        task=task,
        run_dir=run_dir,
        server_image=args.server_image,
        detach_logs=args.detach_logs,
    )

    llm_config = load_llm_config_with_override(args.agent_config, args.max_iterations)

    write_json(
        run_dir / "metadata.json",
        {
            "run_id": run_id,
            "task_config": str(task_config_path),
            "task_id": task_id,
            "difficulty": task.get("difficulty", "unknown"),
            "agent_config": args.agent_config,
            "base_commit": base_commit,
            "openhands_runtime": "swebench_image",
            "server_image": runtime_config.server_image,
            "remote_repo_path": remote_repo_path,
            "events_path": str(events_path),
            "trajectory_path": str(trajectory_path),
            "state_path": str(state_path),
            "output_path": str(output_path),
        },
    )
    (run_dir / "prompt.md").write_text(prompt)

    run_error = None
    try:
        log_stage("Starting OpenHands agent run...")
        phase_start = time.perf_counter()
        run_docker_openhands_task(
            prompt=prompt,
            llm_config=llm_config,
            runtime_config=runtime_config,
            events_path=events_path,
            trajectory_path=trajectory_path,
            state_path=state_path,
            output_path=output_path,
        )
        timings["phases"]["openhands_agent"] = {
            "duration_seconds": elapsed_seconds(phase_start)
        }
        log_stage(
            "OpenHands agent run finished "
            f"in {timings['phases']['openhands_agent']['duration_seconds']}s."
        )
    except Exception as exc:
        timings["phases"]["openhands_agent"] = {
            "duration_seconds": elapsed_seconds(phase_start),
            "failed": True,
        }
        write_json(timings_path, finish_timings(timings, total_start))
        run_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        write_json(run_dir / "run_error.json", run_error)
        write_json(run_dir / "summary.json", summarize_run_json(run_dir))
        log_stage(f"OpenHands agent run failed: {type(exc).__name__}")
        print(f"run_id: {run_id}")
        print(f"run_dir: {run_dir}")
        print(f"total_seconds: {timings['total_seconds']}")
        print(f"agent_seconds: {timings['phases']['openhands_agent']['duration_seconds']}")
        print(f"run_error: {run_error['type']}")
        return 1

    if workspace.exists():
        make_workspace_writable(workspace)

    log_stage("Detecting checkpoint from recorded events...")
    checkpoint_detection_start = time.perf_counter()
    lifecycle_checkpoints = detect_lifecycle_from_jsonl(events_path)
    lifecycle_by_id: dict[str, Checkpoint] = {}
    checkpoint_summaries: list[dict] = []
    if lifecycle_checkpoints:
        log_stage(
            "Detected lifecycle checkpoints: "
            + ", ".join(checkpoint.kind for checkpoint in lifecycle_checkpoints)
        )
        for lifecycle_checkpoint in lifecycle_checkpoints:
            lifecycle_by_id[lifecycle_checkpoint.checkpoint_id] = (
                create_checkpoint_workspace(
                    run_dir=run_dir,
                    source_workspace=workspace,
                    checkpoint=lifecycle_checkpoint,
                    base_commit=base_commit,
                )
            )
        write_json(
            run_dir / "lifecycle_checkpoints.json",
            [checkpoint.to_dict() for checkpoint in lifecycle_by_id.values()],
        )
        checkpoint_summaries, checkpoint_timing = run_lifecycle_official_validations(
            task=task,
            checkpoints=list(lifecycle_by_id.values()),
            run_dir=run_dir,
        )
        timings["phases"]["lifecycle_checkpoint_validation"] = checkpoint_timing
        task_spec = _task_spec_from_initial_run(run_dir, read_json(run_dir / "metadata.json"), task)
        _, handoff_timing = precompute_handoffs(
            initial_run_dir=run_dir,
            task_spec=task_spec,
            checkpoints=list(lifecycle_by_id.values()),
            events_path=events_path,
            generator_config=llm_config,
            generator_config_path=args.agent_config,
            views=list(HANDOFF_VIEWS),
        )
        timings["phases"]["handoff_precompute"] = handoff_timing
    else:
        timings["phases"]["handoff_precompute"] = write_no_checkpoint_handoff_manifest(
            run_dir
        )
    timings["phases"]["checkpoint_detection"] = {
        "duration_seconds": elapsed_seconds(checkpoint_detection_start),
        "lifecycle_checkpoint_count": len(lifecycle_checkpoints),
    }
    if not lifecycle_checkpoints:
        log_stage("No lifecycle checkpoints detected.")
    timings["phases"].setdefault(
        "lifecycle_checkpoint_validation",
        {
            "duration_seconds": 0.0,
            "checkpoint_count": len(lifecycle_checkpoints),
            "validated_count": len(checkpoint_summaries),
        },
    )

    if workspace.exists():
        log_stage("Running task validation...")
        phase_start = time.perf_counter()
        validation = run_task_validation(
            task=task,
            workspace=workspace,
            run_dir=run_dir,
            output_path=output_path,
        )
        timings["phases"]["validation"] = {
            "duration_seconds": elapsed_seconds(phase_start),
            "passed": validation.get("passed"),
        }
        log_stage(
            "Task validation finished "
            f"in {timings['phases']['validation']['duration_seconds']}s: "
            f"passed={validation.get('passed')}"
        )
    else:
        validation = {
            "backend": task.get("validation_backend"),
            "passed": False,
            "error": "workspace_not_created",
        }
        timings["phases"]["validation"] = {
            "duration_seconds": 0.0,
            "passed": False,
            "skipped": "workspace_not_created",
        }
    write_json(run_dir / "validation.json", validation)
    write_json(timings_path, finish_timings(timings, total_start))
    write_json(run_dir / "summary.json", summarize_run_json(run_dir))

    print(f"run_id: {run_id}")
    print(f"run_dir: {run_dir}")
    print(f"total_seconds: {timings['total_seconds']}")
    print(f"agent_seconds: {timings['phases']['openhands_agent']['duration_seconds']}")
    print(
        "lifecycle_checkpoint_validation_seconds: "
        f"{timings['phases']['lifecycle_checkpoint_validation']['duration_seconds']}"
    )
    print(
        "lifecycle_checkpoint_validated_count: "
        f"{timings['phases']['lifecycle_checkpoint_validation']['validated_count']}"
    )
    print(f"validation_seconds: {timings['phases']['validation']['duration_seconds']}")
    print(f"validation_passed: {validation.get('passed')}")
    print(f"lifecycle_checkpoints: {len(lifecycle_checkpoints)}")
    if run_error:
        print(f"run_error: {run_error['type']}")
    return 0 if validation.get("passed") else 1


def build_handoff(
    view: str,
    task_spec: TaskSpec,
    checkpoint: Checkpoint,
    events_path: Path,
    *,
    summarizer_config=None,
    artifact_dir: Path | None = None,
):
    if view == "repo_only":
        return build_repo_only_handoff(task_spec, checkpoint)
    if view == "raw_trace":
        return build_raw_trace_handoff(task_spec, checkpoint, events_path)
    if view == "structured_notes":
        if summarizer_config is None:
            raise ValueError("structured_notes requires summarizer_config")
        return build_structured_notes_handoff(
            task_spec,
            checkpoint,
            events_path,
            summarizer_config=summarizer_config,
            artifact_dir=artifact_dir,
        )
    if view == "summary_notes":
        if summarizer_config is None:
            raise ValueError("summary_notes requires summarizer_config")
        return build_summary_notes_handoff(
            task_spec,
            checkpoint,
            events_path,
            summarizer_config=summarizer_config,
            artifact_dir=artifact_dir,
        )
    raise ValueError(f"Unsupported handoff view: {view}")


def _task_spec_from_initial_run(initial_run_dir: Path, metadata: dict, task: dict) -> TaskSpec:
    prompt = Path(task["prompt"]).read_text()
    return TaskSpec(
        task_id=str(metadata["task_id"]),
        repo=str(metadata["task_id"]),
        base_commit=str(metadata["base_commit"]),
        issue_text=prompt,
        test_command=task.get("validation_backend"),
    )


def _load_checkpoint(
    initial_run_dir: Path,
    checkpoint_kind: str | None,
    checkpoint_id: str | None = None,
) -> Checkpoint:
    if checkpoint_kind is None and checkpoint_id is None:
        raise SystemExit(
            "Takeover requires --checkpoint-kind or --checkpoint-id. Initial runs "
            "now expose multiple lifecycle checkpoints instead of a default checkpoint."
        )

    lifecycle_path = initial_run_dir / "lifecycle_checkpoints.json"
    if lifecycle_path.exists():
        lifecycle_checkpoints = [
            Checkpoint(**checkpoint) for checkpoint in read_json(lifecycle_path)
        ]
    else:
        lifecycle_checkpoints = detect_lifecycle_from_jsonl(
            initial_run_dir / "events.jsonl"
        )
    if checkpoint_id is not None:
        for checkpoint in lifecycle_checkpoints:
            if checkpoint.checkpoint_id == checkpoint_id:
                if checkpoint_kind is not None and checkpoint.kind != checkpoint_kind:
                    raise SystemExit(
                        "Checkpoint id/kind mismatch: "
                        f"{checkpoint_id} is {checkpoint.kind}, not {checkpoint_kind}."
                    )
                return checkpoint

        available_ids = ", ".join(
            checkpoint.checkpoint_id for checkpoint in lifecycle_checkpoints
        )
        raise SystemExit(
            f"Checkpoint id not found: {checkpoint_id}. "
            f"Available: {available_ids or 'none'}"
        )

    for checkpoint in lifecycle_checkpoints:
        if checkpoint.kind == checkpoint_kind:
            return checkpoint

    available = ", ".join(checkpoint.kind for checkpoint in lifecycle_checkpoints)
    raise SystemExit(
        f"Checkpoint kind not found: {checkpoint_kind}. Available: {available or 'none'}"
    )


def cmd_render_handoff_prompts(args: argparse.Namespace) -> int:
    initial_run_dir = Path(args.initial_run)
    metadata = read_json(initial_run_dir / "metadata.json")
    checkpoint = _load_checkpoint(
        initial_run_dir,
        args.checkpoint_kind,
        args.checkpoint_id,
    )
    task = load_task_config(Path(str(metadata["task_config"])))
    task_spec = _task_spec_from_initial_run(initial_run_dir, metadata, task)
    llm_config = load_openhands_config(Path(args.agent_config))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    views = [view.strip() for view in args.views.split(",") if view.strip()]
    for view in views:
        artifact_dir = output_dir / f"{view}_artifacts"
        handoff = build_handoff(
            view,
            task_spec,
            checkpoint,
            initial_run_dir / "events.jsonl",
            summarizer_config=llm_config,
            artifact_dir=artifact_dir,
        )
        prompt_path = output_dir / f"{view}_prompt.md"
        prompt_path.write_text(handoff.prompt)
        write_json(output_dir / f"{view}_handoff.json", handoff.__dict__)
        log_stage(f"Wrote {view} prompt: {prompt_path}")

    write_json(
        output_dir / "metadata.json",
        {
            "initial_run": str(initial_run_dir),
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_kind": checkpoint.kind,
            "views": views,
            "agent_config": args.agent_config,
        },
    )
    print(f"prompt_dir: {output_dir}")
    return 0


def _selected_checkpoints_for_precompute(
    initial_run_dir: Path,
    checkpoint_kind: str | None,
    checkpoint_id: str | None,
) -> list[Checkpoint]:
    if checkpoint_kind or checkpoint_id:
        return [_load_checkpoint(initial_run_dir, checkpoint_kind, checkpoint_id)]

    lifecycle_path = initial_run_dir / "lifecycle_checkpoints.json"
    if lifecycle_path.exists():
        return [Checkpoint(**checkpoint) for checkpoint in read_json(lifecycle_path)]
    return detect_lifecycle_from_jsonl(initial_run_dir / "events.jsonl")


def cmd_precompute_handoffs(args: argparse.Namespace) -> int:
    initial_run_dir = Path(args.initial_run)
    metadata = read_json(initial_run_dir / "metadata.json")
    task = load_task_config(Path(str(metadata["task_config"])))
    task_spec = _task_spec_from_initial_run(initial_run_dir, metadata, task)
    checkpoints = _selected_checkpoints_for_precompute(
        initial_run_dir,
        args.checkpoint_kind,
        args.checkpoint_id,
    )
    if not checkpoints:
        write_no_checkpoint_handoff_manifest(initial_run_dir)
        print(f"initial_run: {initial_run_dir}")
        print("checkpoints: 0")
        print("handoff_artifacts: 0")
        print("status: skipped_no_checkpoints")
        return 0

    views = [view.strip() for view in args.views.split(",") if view.strip()]
    invalid = sorted(set(views) - set(HANDOFF_VIEWS))
    if invalid:
        raise SystemExit(f"Unsupported handoff views: {', '.join(invalid)}")

    generator_config_path = args.agent_config or str(metadata["agent_config"])
    generator_config = load_openhands_config(Path(generator_config_path))
    results, timing = precompute_handoffs(
        initial_run_dir=initial_run_dir,
        task_spec=task_spec,
        checkpoints=checkpoints,
        events_path=initial_run_dir / "events.jsonl",
        generator_config=generator_config,
        generator_config_path=generator_config_path,
        views=views,
        force=args.force,
    )
    print(f"initial_run: {initial_run_dir}")
    print(f"checkpoints: {len(checkpoints)}")
    print(f"handoff_artifacts: {len(results)}")
    print(f"duration_seconds: {timing['duration_seconds']}")
    return 0


def cmd_takeover(args: argparse.Namespace) -> int:
    total_start = time.perf_counter()
    timings: dict = {
        "role": "takeover",
        "started_at": now_utc_iso(),
        "phases": {},
    }
    initial_run_dir = Path(args.initial_run)
    metadata = read_json(initial_run_dir / "metadata.json")
    task_config_path = Path(str(metadata["task_config"]))
    task = load_task_config(task_config_path)
    handoff_view = args.handoff_view
    run_id = args.run_id or f"{metadata['run_id']}__takeover_{handoff_view}"
    takeover_dir = Path(args.runs_dir) / run_id
    repo_dir = task.get("repo_dir") or Path(str(task["remote_repo_path"])).name
    remote_repo_path = str(task["remote_repo_path"])
    takeover_workspace = takeover_dir / repo_dir
    events_path = takeover_dir / "events.jsonl"
    trajectory_path = takeover_dir / "trajectory.json"
    state_path = takeover_dir / "state.json"
    timings_path = takeover_dir / "timings.json"

    if takeover_dir.exists():
        raise SystemExit(f"Takeover run directory already exists: {takeover_dir}")
    takeover_dir.mkdir(parents=True)
    make_workspace_writable(takeover_dir)

    try:
        checkpoint = _load_checkpoint(
            initial_run_dir,
            args.checkpoint_kind,
            args.checkpoint_id,
        )
    except SystemExit as exc:
        if not args.skip_missing_checkpoint:
            raise
        timings["phases"]["openhands_agent"] = {
            "duration_seconds": 0.0,
            "skipped": "missing_checkpoint",
        }
        timings["phases"]["validation"] = {
            "duration_seconds": 0.0,
            "skipped": "missing_checkpoint",
        }
        write_json(
            takeover_dir / "metadata.json",
            {
                "run_id": run_id,
                "role": "takeover",
                "handoff_view": handoff_view,
                "task_id": metadata.get("task_id"),
                "difficulty": metadata.get("difficulty", "unknown"),
                "initial_run": str(initial_run_dir),
                "checkpoint_kind": args.checkpoint_kind,
                "checkpoint_id": args.checkpoint_id,
                "agent_config": args.agent_config,
                "skipped": "missing_checkpoint",
                "skip_reason": str(exc),
            },
        )
        write_json(
            takeover_dir / "validation.json",
            {"passed": None, "skipped": "missing_checkpoint"},
        )
        write_json(timings_path, finish_timings(timings, total_start))
        write_json(takeover_dir / "summary.json", summarize_run_json(takeover_dir))
        print(f"run_id: {run_id}")
        print(f"run_dir: {takeover_dir}")
        print("skipped: missing_checkpoint")
        return 0

    if checkpoint.workspace_snapshot is None:
        checkpoint = create_checkpoint_workspace(
            run_dir=initial_run_dir,
            checkpoint=checkpoint,
            base_commit=str(metadata["base_commit"]),
        )

    llm_config = load_llm_config_with_override(args.agent_config, args.max_iterations)
    try:
        handoff, precomputed_dir, precomputed_metadata = load_precomputed_handoff(
            initial_run_dir=initial_run_dir,
            checkpoint=checkpoint,
            handoff_view=handoff_view,
        )
    except SystemExit:
        shutil.rmtree(takeover_dir, ignore_errors=True)
        raise

    # Takeover agents start from the reconstructed checkpoint state.
    shutil.copytree(Path(checkpoint.workspace_snapshot), takeover_workspace, symlinks=True)
    make_workspace_writable(takeover_workspace)
    (takeover_dir / "prompt.md").write_text(handoff.prompt)
    write_json(takeover_dir / f"handoff_{handoff_view}.json", handoff.__dict__)
    shutil.copytree(
        precomputed_dir,
        takeover_dir / "handoff_artifacts",
        dirs_exist_ok=True,
    )
    write_json(
        takeover_dir / "metadata.json",
        {
            "run_id": run_id,
            "role": "takeover",
            "prompt_version": TAKEOVER_PROMPT_VERSION,
            "handoff_view": handoff_view,
            "task_id": metadata.get("task_id"),
            "difficulty": metadata.get("difficulty", "unknown"),
            "initial_run": str(initial_run_dir),
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_workspace": checkpoint.workspace_snapshot,
            "agent_config": args.agent_config,
            "handoff_precomputed_dir": str(precomputed_dir),
            "handoff_generator_agent_config": precomputed_metadata.get(
                "generator_agent_config"
            ),
            "handoff_generator_model": precomputed_metadata.get("generator_model"),
            "remote_repo_path": remote_repo_path,
            "events_path": str(events_path),
            "trajectory_path": str(trajectory_path),
            "state_path": str(state_path),
        },
    )
    make_workspace_writable(takeover_dir)

    run_error = None
    try:
        log_stage("Starting OpenHands takeover run...")
        phase_start = time.perf_counter()
        run_docker_openhands_task(
            prompt=handoff.prompt,
            llm_config=llm_config,
            runtime_config=DockerRuntimeConfig(
                host_workspace=takeover_workspace,
                remote_workspace=remote_repo_path,
                remote_repo_path=remote_repo_path,
                server_image=args.server_image
                or metadata.get("server_image")
                or DockerRuntimeConfig.server_image,
                detach_logs=args.detach_logs,
                setup_commands=[
                    f"cd {shlex.quote(remote_repo_path)} && git config core.filemode false"
                ],
            ),
            events_path=events_path,
            trajectory_path=trajectory_path,
            state_path=state_path,
        )
        timings["phases"]["openhands_agent"] = {
            "duration_seconds": elapsed_seconds(phase_start)
        }
        log_stage(
            "OpenHands takeover run finished "
            f"in {timings['phases']['openhands_agent']['duration_seconds']}s."
        )
    except Exception as exc:
        timings["phases"]["openhands_agent"] = {
            "duration_seconds": elapsed_seconds(phase_start),
            "failed": True,
        }
        write_json(timings_path, finish_timings(timings, total_start))
        run_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        write_json(takeover_dir / "run_error.json", run_error)
        write_json(takeover_dir / "summary.json", summarize_run_json(takeover_dir))
        log_stage(f"OpenHands takeover run failed: {type(exc).__name__}")
        print(f"run_id: {run_id}")
        print(f"run_dir: {takeover_dir}")
        print(f"total_seconds: {timings['total_seconds']}")
        print(f"agent_seconds: {timings['phases']['openhands_agent']['duration_seconds']}")
        print(f"run_error: {run_error['type']}")
        return 1

    # This score asks whether takeover preserved the checkpoint patch exactly.
    phase_start = time.perf_counter()
    checkpoint_diff = diff_from_base(
        Path(checkpoint.workspace_snapshot),
        str(metadata["base_commit"]),
    )
    final_diff = diff_from_base(takeover_workspace, str(metadata["base_commit"]))
    timings["phases"]["diff_scoring"] = {
        "duration_seconds": elapsed_seconds(phase_start),
        "final_diff_matches_checkpoint": final_diff == checkpoint_diff,
    }

    log_stage("Running takeover validation...")
    phase_start = time.perf_counter()
    validation = run_task_validation(
        task=task,
        workspace=takeover_workspace,
        run_dir=takeover_dir,
        output_path=None,
    )
    write_json(takeover_dir / "validation.json", validation)
    timings["phases"]["validation"] = {
        "duration_seconds": elapsed_seconds(phase_start),
        "passed": validation.get("passed"),
    }
    log_stage(
        "Takeover validation finished "
        f"in {timings['phases']['validation']['duration_seconds']}s: "
        f"passed={validation.get('passed')}"
    )

    takeover_score = build_takeover_score(
        checkpoint_diff=checkpoint_diff,
        final_diff=final_diff,
        validation_passed=validation.get("passed"),
    )
    write_json(takeover_dir / "takeover_score.json", takeover_score)
    write_json(timings_path, finish_timings(timings, total_start))
    write_json(takeover_dir / "summary.json", summarize_run_json(takeover_dir))

    print(f"run_id: {run_id}")
    print(f"run_dir: {takeover_dir}")
    print(f"total_seconds: {timings['total_seconds']}")
    print(f"agent_seconds: {timings['phases']['openhands_agent']['duration_seconds']}")
    print(f"validation_seconds: {timings['phases']['validation']['duration_seconds']}")
    print(f"validation_passed: {validation.get('passed')}")
    print(
        "final_diff_matches_checkpoint: "
        f"{takeover_score['final_diff_matches_checkpoint']}"
    )
    print(f"clean_continuation: {takeover_score['clean_continuation']}")
    if run_error:
        print(f"run_error: {run_error['type']}")
    return 0 if validation.get("passed") else 1
