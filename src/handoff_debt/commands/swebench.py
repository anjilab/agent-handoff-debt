"""CLI command handlers for SWE-bench task preparation."""

from __future__ import annotations

import argparse
from pathlib import Path

from handoff_debt.swebench import (
    download_swebench_verified_jsonl,
    load_swebench_instances,
    materialize_swebench_task,
    ordered_instances,
    select_instances,
    write_selected_manifests,
)


def cmd_prepare_swebench(args: argparse.Namespace) -> int:
    instances = load_swebench_instances(Path(args.manifest))
    selected = select_instances(
        instances,
        instance_ids=args.instance_id or [],
        limit=args.limit,
    )
    if not selected:
        raise SystemExit("No SWE-bench instances selected")

    for instance in selected:
        config_path, task_path = materialize_swebench_task(
            instance,
            tasks_dir=Path(args.tasks_dir),
            configs_dir=Path(args.configs_dir),
            overwrite=args.overwrite,
        )
        print(f"{instance.instance_id}")
        print(f"  config: {config_path}")
        print(f"  task: {task_path}")
    return 0


def cmd_fetch_swebench_verified(args: argparse.Namespace) -> int:
    out_path = Path(args.out)
    download_swebench_verified_jsonl(
        out_path,
        limit=args.limit,
        offset=args.offset,
    )
    print(f"manifest: {out_path}")
    print(f"rows: {args.limit}")
    return 0


def cmd_select_swebench(args: argparse.Namespace) -> int:
    instances = load_swebench_instances(Path(args.manifest))
    ordered = ordered_instances(instances, seed=args.seed)
    sizes = args.size or [25, 50, 100, 200]
    write_selected_manifests(
        ordered,
        output_dir=Path(args.output_dir),
        sizes=sizes,
    )
    print(f"selection_dir: {args.output_dir}")
    print(f"seed: {args.seed}")
    print(f"rows: {len(ordered)}")
    print("sizes: " + ", ".join(str(size) for size in sizes))
    return 0
