"""Shared source-file and validation-signal heuristics for lifecycle checkpoints."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

VALIDATION_RE = re.compile(
    r"\b(pytest|tox|nox|unittest|python\s+-m\s+pytest|python\s+-m\s+unittest|"
    r"ruff|mypy|npm\s+test|pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test|"
    r"make\s+test|gradle\s+test|mvn\s+test|python(?:3)?\s+\S*(?:repro|reproduce|"
    r"check|verify|validate|test)\S*\.py)\b",
    flags=re.IGNORECASE,
)

SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".scala",
    ".kt",
}

TEST_PATH_RE = re.compile(r"(^|/)(test|tests|testing|spec|specs)(/|_)|(_test|_spec)\.")
SCRATCH_NAME_PREFIXES = ("reproduce", "repro", "scratch", "tmp", "debug")
PASS_RE = re.compile(
    r"\b(all tests passed|all tests pass|tests passed|test passed|pass:\s*true|"
    r"passed\s+\d+|\d+\s+passed|ok\b|successfully fixed|verified)\b",
    flags=re.IGNORECASE,
)
FAIL_RE = re.compile(
    r"\b(assertionerror|traceback|failed|fail:\s*true|pass:\s*false|"
    r"\d+\s+failed|error:|exception)\b",
    flags=re.IGNORECASE,
)


def is_source_file(path: str) -> bool:
    path_obj = Path(path)
    if path_obj.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    if "/" not in path.replace("\\", "/") and path_obj.stem.lower().startswith(
        SCRATCH_NAME_PREFIXES
    ):
        return False
    return TEST_PATH_RE.search(path.replace("\\", "/")) is None


def validation_command(text: str) -> str | None:
    match = VALIDATION_RE.search(text)
    if not match:
        return None
    line = next((line for line in text.splitlines() if match.group(0) in line), text)
    return line.strip()


def validation_result(text: str) -> str | None:
    if FAIL_RE.search(text):
        return "failed"
    if PASS_RE.search(text):
        return "passed"
    return None


def event_tool_name(event: dict[str, Any]) -> str:
    raw = event.get("raw") or {}
    return str(raw.get("tool_name") or raw.get("tool_call", {}).get("name") or "")


def is_terminal_event(event: dict[str, Any]) -> bool:
    if event_tool_name(event) == "terminal":
        return True
    return str(event.get("text") or "").startswith("terminal")


def validation_command_from_event(event: dict[str, Any]) -> str | None:
    """Return a validation command only from terminal actions/observations."""

    if not is_terminal_event(event):
        return None

    raw = event.get("raw") or {}
    command = (
        (raw.get("action") or {}).get("command")
        or (raw.get("observation") or {}).get("command")
        or ""
    )
    return validation_command(str(command)) or validation_command(str(event.get("text") or ""))


def validation_result_from_event(event: dict[str, Any]) -> str | None:
    """Return validation outcome text only from terminal observations."""

    if event.get("source") != "environment" or not is_terminal_event(event):
        return None
    return validation_result(str(event.get("text") or ""))


def modified_source_files(event: dict[str, Any]) -> list[str]:
    git = event.get("git") or {}
    files = git.get("modified_files") or []
    return sorted(path for path in files if is_source_file(path))
