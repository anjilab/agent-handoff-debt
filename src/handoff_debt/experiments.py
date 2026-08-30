"""Experiment planning helpers for repeatable handoff studies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handoff_debt.io import write_json
from handoff_debt.swebench import load_swebench_instances, slugify_instance_id

CHECKPOINT_KINDS = [
    "first_meaningful_modification",
    "post_first_validation_result",
    "post_failed_repair_edit",
]
HANDOFF_VIEWS = [
    "repo_only",
    "raw_trace",
    "structured_notes",
    "summary_notes",
]


@dataclass(frozen=True)
class AgentRef:
    label: str
    config: str


@dataclass(frozen=True)
class ModelPair:
    initial: str
    takeover: str


def parse_agent_refs(values: list[str]) -> dict[str, AgentRef]:
    if not values:
        values = ["local=configs/agents/local_openai_compatible.toml"]

    agents: dict[str, AgentRef] = {}
    for value in values:
        label, config = _split_once(value, "=", "--agent values must be label=path")
        safe_label = safe_id(label)
        agents[safe_label] = AgentRef(label=safe_label, config=config)
    return agents


def parse_model_pairs(values: list[str], agents: dict[str, AgentRef]) -> list[ModelPair]:
    if not values:
        if "local" in agents:
            return [ModelPair(initial="local", takeover="local")]
        first = sorted(agents)[0]
        return [ModelPair(initial=first, takeover=first)]

    pairs: list[ModelPair] = []
    for value in values:
        separator = "->" if "->" in value else ":"
        initial, takeover = _split_once(
            value,
            separator,
            "--model-pair values must be initial->takeover or initial:takeover",
        )
        initial = safe_id(initial)
        takeover = safe_id(takeover)
        for label in (initial, takeover):
            if label not in agents:
                raise ValueError(f"Model pair references unknown agent label: {label}")
        pairs.append(ModelPair(initial=initial, takeover=takeover))
    return pairs


def write_experiment_plan(
    *,
    experiment_id: str,
    task_manifest: Path,
    output_dir: Path,
    task_configs_dir: Path,
    agents: dict[str, AgentRef],
    model_pairs: list[ModelPair],
    checkpoint_kinds: list[str],
    handoff_views: list[str],
    repeats: int,
    max_iterations: int,
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    instances = load_swebench_instances(task_manifest)
    experiment_dir = output_dir / safe_id(experiment_id)
    runs_dir = experiment_dir / "runs"
    rows = build_plan_rows(
        instances=instances,
        task_configs_dir=task_configs_dir,
        runs_dir=runs_dir,
        agents=agents,
        model_pairs=model_pairs,
        checkpoint_kinds=checkpoint_kinds,
        handoff_views=handoff_views,
        repeats=repeats,
        max_iterations=max_iterations,
    )

    experiment_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "experiment_id": safe_id(experiment_id),
        "task_manifest": str(task_manifest),
        "task_count": len(instances),
        "agents": {label: agent.config for label, agent in agents.items()},
        "model_pairs": [pair.__dict__ for pair in model_pairs],
        "checkpoint_kinds": checkpoint_kinds,
        "handoff_views": handoff_views,
        "repeats": repeats,
        "max_iterations": max_iterations,
        "runs_dir": str(runs_dir),
    }
    write_json(experiment_dir / "experiment.json", metadata)
    _write_jsonl(experiment_dir / "plan.jsonl", rows)
    (experiment_dir / "run_plan.sh").write_text(render_run_script(rows))
    (experiment_dir / "README.md").write_text(render_readme(metadata, rows))
    return metadata | {"planned_rows": len(rows), "experiment_dir": str(experiment_dir)}


def build_plan_rows(
    *,
    instances: list,
    task_configs_dir: Path,
    runs_dir: Path,
    agents: dict[str, AgentRef],
    model_pairs: list[ModelPair],
    checkpoint_kinds: list[str],
    handoff_views: list[str],
    repeats: int,
    max_iterations: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    initial_keys: list[tuple[Any, str]] = []
    seen_initial_keys: set[tuple[str, str]] = set()
    for instance in instances:
        for pair in model_pairs:
            key = (instance.instance_id, pair.initial)
            if key not in seen_initial_keys:
                seen_initial_keys.add(key)
                initial_keys.append((instance, pair.initial))
    for instance, initial_label in initial_keys:
        task_id = instance.instance_id
        task_slug = slugify_instance_id(task_id)
        run_id = f"{task_slug}/{initial_label}/initial"
        rows.append(
            {
                "stage": "initial",
                "task_id": task_id,
                "difficulty": instance.difficulty,
                "task_config": str(task_configs_dir / f"{task_slug}.toml"),
                "agent_label": initial_label,
                "agent_config": agents[initial_label].config,
                "runs_dir": str(runs_dir),
                "run_id": run_id,
                "max_iterations": max_iterations,
                "command": [
                    "uv",
                    "run",
                    "handoff-debt",
                    "run-task",
                    "--task-config",
                    str(task_configs_dir / f"{task_slug}.toml"),
                    "--agent-config",
                    agents[initial_label].config,
                    "--runs-dir",
                    str(runs_dir),
                    "--run-id",
                    run_id,
                    "--max-iterations",
                    str(max_iterations),
                ],
            }
        )

    for instance in instances:
        task_slug = slugify_instance_id(instance.instance_id)
        for pair in model_pairs:
            initial_run_id = f"{task_slug}/{pair.initial}/initial"
            initial_run = str(runs_dir / initial_run_id)
            for checkpoint_kind in checkpoint_kinds:
                for handoff_view in handoff_views:
                    for repeat in range(1, repeats + 1):
                        run_id = (
                            f"{task_slug}/{pair.initial}_to_{pair.takeover}/"
                            f"{checkpoint_kind}/{handoff_view}/r{repeat:02d}"
                        )
                        rows.append(
                            {
                                "stage": "takeover",
                                "task_id": instance.instance_id,
                                "difficulty": instance.difficulty,
                                "initial_agent_label": pair.initial,
                                "takeover_agent_label": pair.takeover,
                                "takeover_agent_config": agents[pair.takeover].config,
                                "initial_run": initial_run,
                                "checkpoint_kind": checkpoint_kind,
                                "handoff_view": handoff_view,
                                "repeat": repeat,
                                "runs_dir": str(runs_dir),
                                "run_id": run_id,
                                "max_iterations": max_iterations,
                                "missing_checkpoint_policy": "skip_and_report",
                                "command": [
                                    "uv",
                                    "run",
                                    "handoff-debt",
                                    "takeover",
                                    "--initial-run",
                                    initial_run,
                                    "--agent-config",
                                    agents[pair.takeover].config,
                                    "--handoff-view",
                                    handoff_view,
                                    "--checkpoint-kind",
                                    checkpoint_kind,
                                    "--runs-dir",
                                    str(runs_dir),
                                    "--run-id",
                                    run_id,
                                    "--max-iterations",
                                    str(max_iterations),
                                    "--skip-missing-checkpoint",
                                ],
                            }
                        )
    return rows


def render_run_script(rows: list[dict[str, Any]]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Generated plan. Run one line at a time while the protocol is still being debugged.",
        "",
    ]
    for row in rows:
        lines.append("# " + _row_label(row))
        lines.append(" ".join(_quote(part) for part in row["command"]))
        lines.append("")
    return "\n".join(lines)


def render_readme(metadata: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    initial_count = sum(1 for row in rows if row["stage"] == "initial")
    takeover_count = sum(1 for row in rows if row["stage"] == "takeover")
    return (
        f"# Experiment {metadata['experiment_id']}\n\n"
        f"- task manifest: `{metadata['task_manifest']}`\n"
        f"- runs dir: `{metadata['runs_dir']}`\n"
        f"- repeats per condition: {metadata['repeats']}\n"
        f"- initial runs: {initial_count}\n"
        f"- takeover runs: {takeover_count}\n"
        f"- missing checkpoint policy: skip and report, never fallback\n\n"
        "Run commands are in `run_plan.sh`; structured rows are in `plan.jsonl`.\n"
    )


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = value.strip("._-")
    if not value:
        raise ValueError("Identifier cannot be empty")
    return value


def _split_once(value: str, separator: str, message: str) -> tuple[str, str]:
    if separator not in value:
        raise ValueError(message)
    left, right = value.split(separator, 1)
    if not left.strip() or not right.strip():
        raise ValueError(message)
    return left.strip(), right.strip()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _row_label(row: dict[str, Any]) -> str:
    if row["stage"] == "initial":
        return f"initial {row['task_id']} model={row['agent_label']}"
    return (
        f"takeover {row['task_id']} {row['initial_agent_label']}->{row['takeover_agent_label']} "
        f"{row['checkpoint_kind']} {row['handoff_view']} repeat={row['repeat']}"
    )
