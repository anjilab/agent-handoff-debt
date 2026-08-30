"""Compact summaries for initial and takeover run directories."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from csv import DictWriter
from io import StringIO
from pathlib import Path
from typing import Any

from handoff_debt.scoring import changed_files_from_diff, classify_changed_files

CHECKPOINT_TAKEOVER_PRIORITY = [
    "post_first_validation_result",
    "post_failed_repair_edit",
    "first_meaningful_modification",
]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _event_count(path: Path) -> int | None:
    if not path.exists():
        return None
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _token_usage(state: dict[str, Any]) -> dict[str, Any]:
    return (
        state.get("stats", {})
        .get("usage_to_metrics", {})
        .get("default", {})
        .get("accumulated_token_usage", {})
    )


def _rate(success: int | None, total: int | None) -> float | None:
    if success is None or total is None or total == 0:
        return None
    return round(success / total, 4)


def _official_instance_report_from_output_dir(output_dir: Path) -> dict[str, Any]:
    reports = sorted(
        (output_dir / "logs" / "run_evaluation").glob("*/*/*/report.json")
    )
    if not reports:
        return {}
    data = _read_json(reports[0])
    if not data:
        return {}
    instance = next(iter(data.values()))
    return instance if isinstance(instance, dict) else {}


def _official_instance_report(run_dir: Path) -> dict[str, Any]:
    return _official_instance_report_from_output_dir(run_dir / "swebench_official")


def _official_top_level_outcome(
    validation: dict[str, Any],
    *,
    instance_id: str | None = None,
) -> dict[str, Any]:
    report = validation.get("report") or {}
    if not isinstance(report, dict):
        return {}

    empty_patch_ids = set(report.get("empty_patch_ids") or [])
    error_ids = set(report.get("error_ids") or [])
    resolved_ids = set(report.get("resolved_ids") or [])
    unresolved_ids = set(report.get("unresolved_ids") or [])
    submitted_ids = set(report.get("submitted_ids") or [])
    target_id = instance_id
    if target_id is None and len(submitted_ids) == 1:
        target_id = next(iter(submitted_ids))
    if target_id is None:
        return {}

    if target_id in resolved_ids:
        return {
            "official_resolved": True,
            "official_empty_patch": False,
            "official_error": False,
        }
    if target_id in unresolved_ids:
        return {
            "official_resolved": False,
            "official_empty_patch": False,
            "official_error": False,
        }
    if target_id in empty_patch_ids:
        return {
            "official_resolved": False,
            "official_empty_patch": True,
            "official_error": False,
        }
    if target_id in error_ids:
        return {
            "official_resolved": None,
            "official_empty_patch": False,
            "official_error": True,
        }
    return {}


def _official_test_progress(report: dict[str, Any]) -> dict[str, Any]:
    tests_status = report.get("tests_status") or {}
    fail_to_pass = tests_status.get("FAIL_TO_PASS") or {}
    pass_to_pass = tests_status.get("PASS_TO_PASS") or {}

    ftp_success = len(fail_to_pass.get("success") or [])
    ftp_failure = len(fail_to_pass.get("failure") or [])
    ptp_success = len(pass_to_pass.get("success") or [])
    ptp_failure = len(pass_to_pass.get("failure") or [])
    ftp_total = ftp_success + ftp_failure
    ptp_total = ptp_success + ptp_failure

    return {
        "official_resolved": report.get("resolved"),
        "official_empty_patch": False,
        "official_error": None,
        "official_patch_applied": report.get("patch_successfully_applied"),
        "fail_to_pass_success": ftp_success,
        "fail_to_pass_failure": ftp_failure,
        "fail_to_pass_total": ftp_total,
        "fail_to_pass_rate": _rate(ftp_success, ftp_total),
        "pass_to_pass_success": ptp_success,
        "pass_to_pass_failure": ptp_failure,
        "pass_to_pass_total": ptp_total,
        "pass_to_pass_rate": _rate(ptp_success, ptp_total),
    }


def _is_scratch_file(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1] if parts else path
    return (
        name.startswith(("debug_", "reproduce_", "repro_", "scratch_", "tmp_"))
        or name in {"debug.py", "reproduce.py", "reproduction.py"}
        or (len(parts) <= 2 and name.startswith("test_"))
        or any(part.startswith(("test_", "debug_", "reproduce_")) for part in parts[:-1])
    )


def _patch_summary_from_path(patch_path: Path) -> dict[str, Any]:
    if not patch_path.exists():
        return {
            "patch_changed_file_count": None,
            "patch_source_files": [],
            "patch_test_files": [],
            "patch_setup_files": [],
            "patch_other_files": [],
            "patch_scratch_files": [],
            "patch_clean_source_only": None,
        }

    changed_files = changed_files_from_diff(patch_path.read_text())
    classified = classify_changed_files(changed_files)
    scratch_files = [path for path in changed_files if _is_scratch_file(path)]
    clean_source_only = bool(
        changed_files
        and not classified["test"]
        and not classified["setup"]
        and not classified["other"]
        and not scratch_files
    )
    return {
        "patch_changed_file_count": len(changed_files),
        "patch_source_files": classified["source"],
        "patch_test_files": classified["test"],
        "patch_setup_files": classified["setup"],
        "patch_other_files": classified["other"],
        "patch_scratch_files": scratch_files,
        "patch_clean_source_only": clean_source_only,
    }


def _patch_summary(run_dir: Path) -> dict[str, Any]:
    return _patch_summary_from_path(run_dir / "swebench_official" / "model.patch")


def _lifecycle_checkpoint_summaries(
    *,
    checkpoints: list[dict[str, Any]],
    validations: list[dict[str, Any]],
    timings: dict[str, Any],
) -> list[dict[str, Any]]:
    timing_by_id = {
        item.get("checkpoint_id"): item
        for item in timings.get("phases", {})
        .get("lifecycle_checkpoint_validation", {})
        .get("per_checkpoint", [])
        if item.get("checkpoint_id")
    }
    validation_by_id = {
        item.get("checkpoint_id"): item
        for item in validations
        if item.get("checkpoint_id")
    }

    summaries = []
    for checkpoint in checkpoints:
        checkpoint_id = checkpoint.get("checkpoint_id")
        item = dict(validation_by_id.get(checkpoint_id) or checkpoint)
        timing = timing_by_id.get(checkpoint_id) or {}
        if "validation_seconds" not in item and "duration_seconds" in timing:
            item["validation_seconds"] = timing.get("duration_seconds")
        if "validation_passed" not in item and "passed" in timing:
            item["validation_passed"] = timing.get("passed")
        summaries.append(item)

    known_ids = {item.get("checkpoint_id") for item in summaries}
    for validation in validations:
        checkpoint_id = validation.get("checkpoint_id")
        if checkpoint_id in known_ids:
            continue
        item = dict(validation)
        timing = timing_by_id.get(checkpoint_id) or {}
        if "validation_seconds" not in item and "duration_seconds" in timing:
            item["validation_seconds"] = timing.get("duration_seconds")
        summaries.append(item)

    return summaries


def summarize_official_validation_json(
    *,
    checkpoint: dict[str, Any] | None = None,
    output_dir: Path,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize one official validation artifact with the final-run metric schema."""

    validation = validation or {}
    report = _official_instance_report_from_output_dir(output_dir)
    summary = {
        "validation_passed": validation.get("passed"),
        "backend": validation.get("backend"),
        "validation_path": validation.get("validation_path"),
    }
    if checkpoint:
        summary.update(
            {
                "checkpoint_id": checkpoint.get("checkpoint_id"),
                "checkpoint_kind": checkpoint.get("kind"),
                "checkpoint_step": checkpoint.get("step"),
                "modified_source_files": checkpoint.get("modified_source_files", []),
            }
        )
    summary.update(_official_test_progress(report))
    summary.update(_official_top_level_outcome(validation))
    summary.update(_patch_summary_from_path(output_dir / "model.patch"))
    return summary


def summarize_run_json(run_dir: Path) -> dict[str, Any]:
    metadata = _read_json(run_dir / "metadata.json")
    validation = _read_json(run_dir / "validation.json")
    checkpoint_validations = _read_json(run_dir / "checkpoint_validations.json")
    score = _read_json(run_dir / "takeover_score.json")
    timings = _read_json(run_dir / "timings.json")
    state = _read_json(run_dir / "state.json")
    run_error = _read_json(run_dir / "run_error.json")
    usage = _token_usage(state)
    official_report = _official_instance_report(run_dir)
    patch_summary = _patch_summary(run_dir)
    has_takeover_score = bool(score)

    phases = timings.get("phases", {})
    lifecycle_checkpoints = _read_json(run_dir / "lifecycle_checkpoints.json") or []
    lifecycle_summaries = _lifecycle_checkpoint_summaries(
        checkpoints=lifecycle_checkpoints,
        validations=checkpoint_validations or [],
        timings=timings,
    )
    summary = {
        "run_dir": str(run_dir),
        "run_id": metadata.get("run_id"),
        "role": metadata.get("role", "initial"),
        "handoff_view": metadata.get("handoff_view"),
        "task_id": metadata.get("task_id"),
        "difficulty": metadata.get("difficulty"),
        "skipped": metadata.get("skipped") or validation.get("skipped"),
        "takeover_checkpoint_id": metadata.get("checkpoint_id"),
        "takeover_checkpoint_kind": metadata.get("checkpoint_kind"),
        "validation_passed": validation.get("passed"),
        "clean_continuation": score.get("clean_continuation"),
        "final_diff_matches_checkpoint": score.get("final_diff_matches_checkpoint"),
        "source_only": (
            score.get("source_only")
            if has_takeover_score
            else patch_summary.get("patch_clean_source_only")
        ),
        "modified_source_files": (
            score.get("modified_source_files", [])
            if has_takeover_score
            else patch_summary.get("patch_source_files", [])
        ),
        "modified_test_files": (
            score.get("modified_test_files", [])
            if has_takeover_score
            else patch_summary.get("patch_test_files", [])
        ),
        "modified_setup_files": (
            score.get("modified_setup_files", [])
            if has_takeover_score
            else patch_summary.get("patch_setup_files", [])
        ),
        "modified_other_files": (
            score.get("modified_other_files", [])
            if has_takeover_score
            else patch_summary.get("patch_other_files", [])
        ),
        "total_seconds": timings.get("total_seconds"),
        "agent_seconds": phases.get("openhands_agent", {}).get("duration_seconds"),
        "validation_seconds": phases.get("validation", {}).get("duration_seconds"),
        "lifecycle_checkpoint_validation_seconds": phases.get(
            "lifecycle_checkpoint_validation", {}
        ).get("duration_seconds"),
        "lifecycle_checkpoint_count": len(lifecycle_checkpoints),
        "lifecycle_checkpoint_validated_count": len(checkpoint_validations or []),
        "lifecycle_checkpoints": lifecycle_summaries,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "events": _event_count(run_dir / "events.jsonl"),
        "run_error": run_error or None,
    }
    summary.update(_official_test_progress(official_report))
    summary.update(
        _official_top_level_outcome(
            validation,
            instance_id=metadata.get("task_id"),
        )
    )
    summary.update(patch_summary)
    return summary


def summarize_run(run_dir: Path) -> str:
    summary = summarize_run_json(run_dir)
    score = _read_json(run_dir / "takeover_score.json")
    run_error = summary.get("run_error")

    lines = [
        f"run_dir: {run_dir}",
        f"role: {summary.get('role')}",
        f"handoff_view: {summary.get('handoff_view')}",
        f"validation_passed: {summary.get('validation_passed', 'missing')}",
        "fail_to_pass: "
        f"{summary.get('fail_to_pass_success')}/{summary.get('fail_to_pass_total')}",
        "pass_to_pass: "
        f"{summary.get('pass_to_pass_success')}/{summary.get('pass_to_pass_total')}",
    ]
    if summary.get("takeover_checkpoint_id"):
        lines.extend(
            [
                f"takeover_checkpoint_id: {summary.get('takeover_checkpoint_id')}",
                f"takeover_checkpoint_kind: {summary.get('takeover_checkpoint_kind')}",
            ]
        )
    if score:
        lines.extend(
            [
                "final_diff_matches_checkpoint: "
                f"{score.get('final_diff_matches_checkpoint')}",
                f"clean_continuation: {score.get('clean_continuation')}",
                f"checkpoint_diff_bytes: {score.get('checkpoint_diff_bytes')}",
                f"final_diff_bytes: {score.get('final_diff_bytes')}",
            ]
        )
    lines.extend(
        [
            f"total_seconds: {summary.get('total_seconds')}",
            f"agent_seconds: {summary.get('agent_seconds')}",
            f"prompt_tokens: {summary.get('prompt_tokens')}",
            f"completion_tokens: {summary.get('completion_tokens')}",
            f"events: {summary.get('events')}",
            f"patch_changed_file_count: {summary.get('patch_changed_file_count')}",
            f"patch_scratch_files: {summary.get('patch_scratch_files')}",
        ]
    )
    if run_error:
        lines.append(f"run_error: {run_error.get('type')}: {run_error.get('message')}")
    return "\n".join(lines)


REPORT_COLUMNS = [
    "run_id",
    "role",
    "handoff_view",
    "task_id",
    "takeover_checkpoint_id",
    "takeover_checkpoint_kind",
    "lifecycle_checkpoint_count",
    "lifecycle_checkpoint_validated_count",
    "validation_passed",
    "official_patch_applied",
    "fail_to_pass_success",
    "fail_to_pass_total",
    "fail_to_pass_rate",
    "pass_to_pass_success",
    "pass_to_pass_total",
    "pass_to_pass_rate",
    "skipped",
    "clean_continuation",
    "final_diff_matches_checkpoint",
    "source_only",
    "patch_clean_source_only",
    "patch_changed_file_count",
    "patch_scratch_files",
    "total_seconds",
    "agent_seconds",
    "validation_seconds",
    "lifecycle_checkpoint_validation_seconds",
    "prompt_tokens",
    "completion_tokens",
    "events",
    "modified_source_files",
    "modified_test_files",
    "modified_setup_files",
    "modified_other_files",
    "run_dir",
]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def classify_lifecycle_checkpoint(checkpoint: dict[str, Any]) -> str:
    """Assign a checkpoint to the analysis bucket used for takeover selection."""

    if checkpoint.get("official_patch_applied") is False:
        return "invalid"
    if checkpoint.get("patch_clean_source_only") is False:
        return "invalid"
    if checkpoint.get("validation_passed") is True:
        return "preservation_candidate"

    f2p_total = checkpoint.get("fail_to_pass_total")
    f2p_success = checkpoint.get("fail_to_pass_success")
    if f2p_total and f2p_success is not None and f2p_success < f2p_total:
        return "handoff_candidate"

    if (checkpoint.get("pass_to_pass_failure") or 0) > 0:
        return "regression_candidate"

    return "diagnostic_candidate"


def selected_takeover_checkpoint(
    summary: dict[str, Any],
    *,
    candidate_classes: set[str],
) -> dict[str, Any] | None:
    """Return the deterministic primary checkpoint for a run summary."""

    checkpoints = []
    for checkpoint in summary.get("lifecycle_checkpoints") or []:
        item = dict(checkpoint)
        item["checkpoint_class"] = classify_lifecycle_checkpoint(item)
        if item["checkpoint_class"] in candidate_classes:
            checkpoints.append(item)

    priority = {kind: index for index, kind in enumerate(CHECKPOINT_TAKEOVER_PRIORITY)}
    checkpoints.sort(
        key=lambda item: (
            priority.get(str(item.get("checkpoint_kind")), len(priority)),
            int(item.get("checkpoint_step") or 0),
        )
    )
    return checkpoints[0] if checkpoints else None


def summaries_csv(summaries: list[dict[str, Any]]) -> str:
    output = StringIO()
    writer = DictWriter(output, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for summary in summaries:
        writer.writerow({key: _cell(summary.get(key)) for key in REPORT_COLUMNS})
    return output.getvalue()


def summaries_markdown(summaries: list[dict[str, Any]]) -> str:
    columns = [
        "role",
        "handoff_view",
        "validation_passed",
        "fail_to_pass_success",
        "fail_to_pass_total",
        "pass_to_pass_success",
        "pass_to_pass_total",
        "patch_clean_source_only",
        "patch_scratch_files",
        "skipped",
        "clean_continuation",
        "final_diff_matches_checkpoint",
        "agent_seconds",
        "prompt_tokens",
        "events",
        "modified_test_files",
        "modified_setup_files",
    ]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for summary in summaries:
        lines.append(
            "| "
            + " | ".join(_cell(summary.get(column)) for column in columns)
            + " |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="python -m handoff_debt.reporting")
    parser.add_argument(
        "--format",
        choices=["text", "json", "csv", "markdown"],
        default="text",
        help="Output format for one or more run directories.",
    )
    parser.add_argument("run_dir", nargs="+")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    run_dirs = [Path(item) for item in args.run_dir]
    summaries = [summarize_run_json(run_dir) for run_dir in run_dirs]
    if args.format == "json":
        print(json.dumps(summaries, indent=2, sort_keys=True))
    elif args.format == "csv":
        print(summaries_csv(summaries), end="")
    elif args.format == "markdown":
        print(summaries_markdown(summaries))
    else:
        for index, run_dir in enumerate(run_dirs):
            if index:
                print()
            print(summarize_run(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
