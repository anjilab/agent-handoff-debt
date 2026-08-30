"""Command-line parser wiring for the Handoff Debt harness."""

from __future__ import annotations

import argparse

from handoff_debt.commands.experiments import (
    cmd_select_takeover_candidates,
    cmd_setup_experiment,
)
from handoff_debt.commands.health import cmd_docker_smoke, cmd_doctor, cmd_model_smoke
from handoff_debt.commands.runs import (
    cmd_finalize_run,
    cmd_precompute_handoffs,
    cmd_render_handoff_prompts,
    cmd_run_task,
    cmd_takeover,
)
from handoff_debt.commands.swebench import (
    cmd_fetch_swebench_verified,
    cmd_prepare_swebench,
    cmd_select_swebench,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff-debt")
    sub = parser.add_subparsers(required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument(
        "--image",
        default="ghcr.io/openhands/agent-server:latest-python",
        help="Docker image intended for OpenHands agent-server runs.",
    )
    doctor.add_argument("--agent-config", help="Path to an agent TOML config.")
    doctor.set_defaults(func=cmd_doctor)

    model_smoke = sub.add_parser("model-smoke")
    model_smoke.add_argument("--agent-config", required=True)
    model_smoke.add_argument("--timeout", type=int, default=10)
    model_smoke.add_argument(
        "--show-body",
        action="store_true",
        help="Print the raw /models response body.",
    )
    model_smoke.set_defaults(func=cmd_model_smoke)

    docker_smoke = sub.add_parser("docker-smoke")
    docker_smoke.add_argument(
        "--image",
        default="ghcr.io/openhands/agent-server:latest-python",
        help="Docker image for the OpenHands agent-server container.",
    )
    docker_smoke.add_argument(
        "--detach-logs",
        action="store_true",
        help="Stream Docker logs in a background thread.",
    )
    docker_smoke.add_argument(
        "--agent-config",
        help="Also verify that Docker can reach the configured model endpoint.",
    )
    docker_smoke.set_defaults(func=cmd_docker_smoke)

    fetch_swebench = sub.add_parser("fetch-swebench-verified")
    fetch_swebench.add_argument(
        "--out",
        default="data/swebench/verified_5.jsonl",
        help="Output JSONL manifest path.",
    )
    fetch_swebench.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of official SWE-bench Verified rows to download.",
    )
    fetch_swebench.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Starting row offset in the official SWE-bench Verified test split.",
    )
    fetch_swebench.set_defaults(func=cmd_fetch_swebench_verified)

    select_swebench = sub.add_parser("select-swebench")
    select_swebench.add_argument(
        "--manifest",
        required=True,
        help="Path to a SWE-bench JSON, JSONL, or TOML manifest.",
    )
    select_swebench.add_argument(
        "--output-dir",
        default="data/swebench/selection_seed20260430",
        help="Directory for ordered and prefix-stable selected manifests.",
    )
    select_swebench.add_argument(
        "--seed",
        type=int,
        default=20260430,
        help="Deterministic shuffle seed within repo/difficulty strata.",
    )
    select_swebench.add_argument(
        "--size",
        type=int,
        action="append",
        default=None,
        help="Prefix size to write. May be passed repeatedly.",
    )
    select_swebench.set_defaults(func=cmd_select_swebench)

    prepare_swebench = sub.add_parser("prepare-swebench")
    prepare_swebench.add_argument(
        "--manifest",
        required=True,
        help="Path to a SWE-bench JSON, JSONL, or TOML manifest.",
    )
    prepare_swebench.add_argument(
        "--instance-id",
        action="append",
        help="Specific SWE-bench instance id to prepare. May be passed repeatedly.",
    )
    prepare_swebench.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Prepare the first N instances when --instance-id is not provided.",
    )
    prepare_swebench.add_argument(
        "--tasks-dir",
        default="tasks/swebench_verified",
        help="Directory where generated task prompts will be written.",
    )
    prepare_swebench.add_argument(
        "--configs-dir",
        default="configs/tasks/swebench_verified",
        help="Directory where generated task config TOML files will be written.",
    )
    prepare_swebench.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing generated SWE-bench task directories and configs.",
    )
    prepare_swebench.set_defaults(func=cmd_prepare_swebench)

    run_task = sub.add_parser("run-task")
    run_task.add_argument("--task-config", required=True)
    run_task.add_argument("--agent-config", required=True)
    run_task.add_argument("--runs-dir", default="data/runs")
    run_task.add_argument("--run-id")
    run_task.add_argument("--max-iterations", type=int)
    run_task.add_argument(
        "--server-image",
        help=(
            "OpenHands SWE-bench agent-server image. This should be built from "
            "the task's official SWE-bench /testbed image."
        ),
    )
    run_task.add_argument(
        "--detach-logs",
        action="store_true",
        help="Stream Docker logs in a background thread.",
    )
    run_task.set_defaults(func=cmd_run_task)

    finalize_run = sub.add_parser("finalize-run")
    finalize_run.add_argument("--run-dir", required=True)
    finalize_run.add_argument(
        "--agent-config",
        help=(
            "Generator model config for precomputing handoff artifacts. Defaults "
            "to the initial run's agent_config metadata."
        ),
    )
    finalize_run.add_argument(
        "--force-validation",
        action="store_true",
        help="Rerun official validation even if validation.json already exists.",
    )
    finalize_run.add_argument(
        "--force-handoffs",
        action="store_true",
        help="Regenerate precomputed handoff artifacts even if they already exist.",
    )
    finalize_run.set_defaults(func=cmd_finalize_run)

    takeover = sub.add_parser("takeover")
    takeover.add_argument("--initial-run", required=True)
    takeover.add_argument("--agent-config", required=True)
    takeover.add_argument(
        "--handoff-view",
        choices=["repo_only", "raw_trace", "structured_notes", "summary_notes"],
        default="repo_only",
    )
    takeover.add_argument("--runs-dir", default="data/runs")
    takeover.add_argument("--run-id")
    takeover.add_argument(
        "--checkpoint-kind",
        help=(
            "Lifecycle checkpoint kind to take over from. Required because initial "
            "runs can expose multiple lifecycle checkpoints."
        ),
    )
    takeover.add_argument(
        "--checkpoint-id",
        help="Exact lifecycle checkpoint id to take over from.",
    )
    takeover.add_argument("--max-iterations", type=int)
    takeover.add_argument(
        "--server-image",
        help="OpenHands SWE-bench agent-server image for takeover runs.",
    )
    takeover.add_argument(
        "--detach-logs",
        action="store_true",
        help="Stream Docker logs in a background thread.",
    )
    takeover.add_argument(
        "--skip-missing-checkpoint",
        action="store_true",
        help="Write a skipped run artifact instead of failing when checkpoint kind is absent.",
    )
    takeover.set_defaults(func=cmd_takeover)

    render_prompts = sub.add_parser("render-handoff-prompts")
    render_prompts.add_argument("--initial-run", required=True)
    render_prompts.add_argument("--agent-config", required=True)
    render_prompts.add_argument("--output-dir", required=True)
    render_prompts.add_argument(
        "--checkpoint-kind",
        help=(
            "Lifecycle checkpoint kind to render prompts for. Required because initial "
            "runs can expose multiple lifecycle checkpoints."
        ),
    )
    render_prompts.add_argument(
        "--checkpoint-id",
        help="Exact lifecycle checkpoint id to render prompts for.",
    )
    render_prompts.add_argument(
        "--views",
        default="repo_only,raw_trace,structured_notes,summary_notes",
        help="Comma-separated handoff views to render.",
    )
    render_prompts.set_defaults(func=cmd_render_handoff_prompts)

    precompute_handoffs = sub.add_parser("precompute-handoffs")
    precompute_handoffs.add_argument("--initial-run", required=True)
    precompute_handoffs.add_argument(
        "--agent-config",
        help=(
            "Predecessor/generator model config. Defaults to the initial run's "
            "agent_config metadata."
        ),
    )
    precompute_handoffs.add_argument(
        "--checkpoint-kind",
        help="Lifecycle checkpoint kind to precompute. Defaults to all checkpoints.",
    )
    precompute_handoffs.add_argument(
        "--checkpoint-id",
        help="Exact lifecycle checkpoint id to precompute. Defaults to all checkpoints.",
    )
    precompute_handoffs.add_argument(
        "--views",
        default="repo_only,raw_trace,structured_notes,summary_notes",
        help="Comma-separated handoff views to precompute.",
    )
    precompute_handoffs.add_argument(
        "--force",
        action="store_true",
        help="Regenerate artifacts even if they already exist.",
    )
    precompute_handoffs.set_defaults(func=cmd_precompute_handoffs)

    setup_experiment = sub.add_parser("setup-experiment")
    setup_experiment.add_argument("--experiment-id", required=True)
    setup_experiment.add_argument(
        "--task-manifest",
        required=True,
        help="Selected SWE-bench manifest, typically selected_005.jsonl.",
    )
    setup_experiment.add_argument(
        "--output-dir",
        default="data/experiments",
        help="Root directory for experiment plans and runs.",
    )
    setup_experiment.add_argument(
        "--task-configs-dir",
        default="configs/tasks/swebench_verified",
        help="Directory containing prepared SWE-bench task TOML files.",
    )
    setup_experiment.add_argument(
        "--agent",
        action="append",
        help="Agent label/config pair, e.g. qwen=configs/agents/qwen.toml.",
    )
    setup_experiment.add_argument(
        "--model-pair",
        action="append",
        help=(
            "Initial/takeover model pair, e.g. qwen:gemma or 'qwen->gemma'. "
            "Defaults to local:local."
        ),
    )
    setup_experiment.add_argument(
        "--checkpoint-kind",
        action="append",
        choices=[
            "first_meaningful_modification",
            "post_first_validation_result",
            "post_failed_repair_edit",
        ],
        help="Checkpoint kind to include. Defaults to all permanent checkpoint kinds.",
    )
    setup_experiment.add_argument(
        "--handoff-view",
        action="append",
        choices=["repo_only", "raw_trace", "structured_notes", "summary_notes"],
        help="Handoff view to include. Defaults to all views.",
    )
    setup_experiment.add_argument("--repeats", type=int, default=1)
    setup_experiment.add_argument("--max-iterations", type=int, default=500)
    setup_experiment.set_defaults(func=cmd_setup_experiment)

    select_takeover = sub.add_parser("select-takeover-candidates")
    select_takeover.add_argument(
        "--runs-dir",
        required=True,
        help="Experiment runs directory containing <task>/<agent>/initial summaries.",
    )
    select_takeover.add_argument(
        "--out",
        required=True,
        help="JSONL file to write selected takeover candidates.",
    )
    select_takeover.add_argument(
        "--task-manifest",
        help="Optional selected task .txt or .jsonl file used to preserve task order.",
    )
    select_takeover.add_argument(
        "--initial-agent",
        help="Only select candidates produced by this initial agent label.",
    )
    select_takeover.add_argument(
        "--candidate-class",
        action="append",
        default=None,
        choices=[
            "handoff_candidate",
            "preservation_candidate",
            "regression_candidate",
            "diagnostic_candidate",
        ],
        help="Checkpoint class to include. May be passed repeatedly.",
    )
    select_takeover.add_argument(
        "--limit",
        type=int,
        help="Maximum number of selected candidates to write.",
    )
    select_takeover.add_argument(
        "--require-final-passed",
        action="store_true",
        help="Only select checkpoints from initial runs whose final official validation passed.",
    )
    select_takeover.set_defaults(func=cmd_select_takeover_candidates)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
