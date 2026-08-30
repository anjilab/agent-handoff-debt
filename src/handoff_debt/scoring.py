"""Scoring helpers for takeover cleanliness and changed-file summaries."""

from __future__ import annotations

from pathlib import PurePosixPath

SETUP_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini"}


def changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        files.append(path)
    return files


def is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    return (
        "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or "/test/" in path
    )


def is_setup_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in SETUP_FILES


def classify_changed_files(paths: list[str]) -> dict[str, list[str]]:
    classified = {
        "source": [],
        "test": [],
        "setup": [],
        "other": [],
    }
    for path in paths:
        if is_test_file(path):
            classified["test"].append(path)
        elif is_setup_file(path):
            classified["setup"].append(path)
        elif path.endswith(".py"):
            classified["source"].append(path)
        else:
            classified["other"].append(path)
    return classified


def build_takeover_score(
    *,
    checkpoint_diff: str,
    final_diff: str,
    validation_passed: bool | None,
) -> dict:
    changed_files = changed_files_from_diff(final_diff)
    classified = classify_changed_files(changed_files)
    final_diff_matches_checkpoint = final_diff == checkpoint_diff
    source_only = not classified["test"] and not classified["setup"] and not classified["other"]
    clean_continuation = bool(
        validation_passed and final_diff_matches_checkpoint and source_only
    )

    return {
        "validation_passed": validation_passed,
        "final_diff_matches_checkpoint": final_diff_matches_checkpoint,
        "checkpoint_diff_bytes": len(checkpoint_diff.encode()),
        "final_diff_bytes": len(final_diff.encode()),
        "changed_files": changed_files,
        "modified_source_files": classified["source"],
        "modified_test_files": classified["test"],
        "modified_setup_files": classified["setup"],
        "modified_other_files": classified["other"],
        "source_only": source_only,
        "clean_continuation": clean_continuation,
    }
