"""CLI handlers for experiment planning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from handoff_debt.experiments import (
    CHECKPOINT_KINDS,
    HANDOFF_VIEWS,
    parse_agent_refs,
    parse_model_pairs,
    safe_id,
    write_experiment_plan,
)
from handoff_debt.reporting import selected_takeover_checkpoint, summarize_run_json


def _read_task_order(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    order: dict[str, int] = {}
    for index, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        if path.suffix == ".jsonl":
            task_id = json.loads(line)["instance_id"]
        else:
            task_id = line.strip()
        order[task_id] = index
    return order


def cmd_setup_experiment(args: argparse.Namespace) -> int:
    agents = parse_agent_refs(args.agent or [])
    pairs = parse_model_pairs(args.model_pair or [], agents)
    checkpoint_kinds = args.checkpoint_kind or CHECKPOINT_KINDS
    handoff_views = args.handoff_view or HANDOFF_VIEWS
    metadata = write_experiment_plan(
        experiment_id=safe_id(args.experiment_id),
        task_manifest=Path(args.task_manifest),
        output_dir=Path(args.output_dir),
        task_configs_dir=Path(args.task_configs_dir),
        agents=agents,
        model_pairs=pairs,
        checkpoint_kinds=checkpoint_kinds,
        handoff_views=handoff_views,
        repeats=args.repeats,
        max_iterations=args.max_iterations,
    )
    print(f"experiment_dir: {metadata['experiment_dir']}")
    print(f"planned_rows: {metadata['planned_rows']}")
    print(f"repeats: {args.repeats}")
    return 0


def cmd_select_takeover_candidates(args: argparse.Namespace) -> int:
    candidate_classes = set(args.candidate_class or ["handoff_candidate"])
    task_order = _read_task_order(Path(args.task_manifest) if args.task_manifest else None)
    summaries = []
    for summary_path in sorted(Path(args.runs_dir).glob("*/*/initial/summary.json")):
        summary = summarize_run_json(summary_path.parent)
        if args.initial_agent and summary_path.parts[-3] != args.initial_agent:
            continue
        if args.require_final_passed and summary.get("validation_passed") is not True:
            continue
        checkpoint = selected_takeover_checkpoint(
            summary,
            candidate_classes=candidate_classes,
        )
        if checkpoint is None:
            continue
        task_id = str(summary.get("task_id"))
        summaries.append(
            {
                "task_id": task_id,
                "difficulty": summary.get("difficulty"),
                "initial_agent": summary_path.parts[-3],
                "initial_run": str(summary_path.parent),
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "checkpoint_kind": checkpoint.get("checkpoint_kind"),
                "checkpoint_class": checkpoint.get("checkpoint_class"),
                "checkpoint_step": checkpoint.get("checkpoint_step"),
                "checkpoint_fail_to_pass_success": checkpoint.get(
                    "fail_to_pass_success"
                ),
                "checkpoint_fail_to_pass_total": checkpoint.get("fail_to_pass_total"),
                "checkpoint_pass_to_pass_success": checkpoint.get(
                    "pass_to_pass_success"
                ),
                "checkpoint_pass_to_pass_total": checkpoint.get("pass_to_pass_total"),
                "final_validation_passed": summary.get("validation_passed"),
                "final_fail_to_pass_success": summary.get("fail_to_pass_success"),
                "final_fail_to_pass_total": summary.get("fail_to_pass_total"),
                "final_pass_to_pass_success": summary.get("pass_to_pass_success"),
                "final_pass_to_pass_total": summary.get("pass_to_pass_total"),
            }
        )

    summaries.sort(
        key=lambda item: (
            task_order.get(str(item["task_id"]), 10**9),
            str(item["task_id"]),
            str(item["checkpoint_kind"]),
        )
    )
    if args.limit is not None:
        summaries = summaries[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in summaries))
    print(f"candidate_file: {out}")
    print(f"candidate_count: {len(summaries)}")
    print(f"candidate_classes: {','.join(sorted(candidate_classes))}")
    return 0
