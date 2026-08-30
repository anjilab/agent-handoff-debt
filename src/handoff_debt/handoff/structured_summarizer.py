"""Model-assisted structured handoff contract generation."""

from __future__ import annotations

import json
import re
from typing import Any

from handoff_debt.agents.openhands_adapter import OpenHandsConfig
from handoff_debt.handoff.summarizer_common import (
    completion_text,
    parse_json_object,
    validate_generated_handoff_text,
)

STRUCTURED_FIELDS = [
    "problem_understanding",
    "work_completed",
    "evidence_observed",
    "observed_failures_or_error_evidence",
    "remaining_uncertainty",
    "rollback_notes",
    "recommended_next_action",
]
OVERCONFIDENT_STATUS_VALUES = {"fixed", "done", "complete", "completed", "solved"}
STRUCTURED_NO_CHANGE_CLAIMS = (
    "no code changes have been made",
    "no code changes were made",
    "no changes have been made",
    "no changes were made",
    "no source changes have been made",
    "no source changes were made",
    "no code changes have been implemented",
    "no code changes were implemented",
    "no fix has been implemented",
    "no fix was implemented",
    "no fix implemented",
    "fix has not been implemented",
    "fix was not implemented",
    "not been implemented yet",
    "no code changes yet",
    "no changes yet",
)
STRUCTURED_SOURCE_CHANGE_CONTRADICTION_RE = re.compile(
    r"(?:"
    r"implementation\s+(?:and\s+)?(?:final\s+)?verification\s+remain|"
    r"fix\s+implementation\s+(?:and\s+)?(?:final\s+)?verification\s+remain|"
    r"implementation\s+remains|"
    r"fix\s+remains\s+to\s+be\s+(?:implemented|applied)|"
    r"remaining\s+work\s+involves\s+(?:applying|implementing|making)|"
    r"next\s+step\s+is\s+to\s+(?:apply|implement|make|modify|edit)|"
    r"recommended\s+next\s+action:\s*(?:apply|implement|make|modify|edit)"
    r")",
    re.IGNORECASE,
)


def generate_structured_fields(
    *,
    config: OpenHandsConfig,
    evidence_packet: str,
) -> dict[str, str]:
    has_source_changes = _evidence_has_source_changes(evidence_packet)
    for retry in range(2):
        source_change_rule = (
            "\n- The repository state facts list changed source files. work_completed "
            "must acknowledge predecessor source changes; do not say no fix or "
            "no code changes were implemented. Do not say the implementation "
            "remains; recommend verification of the current changes before any "
            "further edits."
            if has_source_changes
            else ""
        )
        retry_rule = (
            "\n- Previous output failed validation. Rewrite it so every field is "
            "supported by the evidence and consistent with repository state facts."
            if retry
            else ""
        )
        text = completion_text(
            config,
            [
                {
                    "role": "system",
                    "content": (
                        "You fill predecessor notes for a structured software-agent handoff. "
                        "Use only the provided predecessor evidence. Return only JSON. A "
                        "command invocation is not a result; only say a command passed "
                        "or failed when command output in the evidence shows that "
                        "result."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""Return one strict JSON object with exactly these string fields:
{json.dumps(STRUCTURED_FIELDS)}

Rules:
- Use UNKNOWN when the evidence does not support a field.
- Use NONE OBSERVED when the category was not observed.
- Distinguish historical failures from current failures.
- If source files are listed as changed in the repository state facts, do not
  claim that no code/source changes have been made.
- You may describe concrete edit intent or implementation findings when they are
  supported by predecessor evidence, but do not invent implementation details,
  validation results, or conclusions that are not supported by that evidence.
- Do not treat a file edit action by itself as proof that the task is fixed.
  If the exact patch is not visible in the evidence, say source changes are
  present rather than claiming the fix is implemented.
- recommended_next_action should be one concise next action, not a full plan.
- Do not include checkpoint kind, step number, or internal checkpoint reason.
- Do not copy these rules into any field.
- Do not claim a validation command passed or failed unless observed output says so.
- Keep each value concise; bullets inside a string are allowed.{source_change_rule}{retry_rule}

{evidence_packet}
""",
                },
            ],
            response_format={"type": "json_object"},
        )
        data = parse_json_object(text)
        missing = [field for field in STRUCTURED_FIELDS if field not in data]
        if missing:
            raise RuntimeError(
                f"Summarizer omitted required fields: {', '.join(missing)}"
            )
        fields = {
            field: _repair_structured_source_change_contradiction(
                _field_to_text(data[field]),
                has_source_changes,
            )
            for field in STRUCTURED_FIELDS
        }
        _validate_structured_fields(fields)
        try:
            combined = "\n".join(fields.values())
            validate_generated_handoff_text(combined)
            _validate_structured_source_change_consistency(combined, has_source_changes)
        except RuntimeError as exc:
            last_error = exc
            continue
        return fields
    assert last_error is not None
    raise last_error


def _field_to_text(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if not items:
            return "UNKNOWN"
        return "\n".join(f"- {item}" for item in items)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    text = str(value).strip()
    return text or "UNKNOWN"


def _validate_structured_fields(fields: dict[str, str]) -> None:
    combined = " ".join(fields.values()).strip().lower().rstrip(".")
    if combined in OVERCONFIDENT_STATUS_VALUES:
        raise RuntimeError(
            "Generated handoff is too overconfident: "
            f"{combined!r}"
        )


def _evidence_has_source_changes(evidence_packet: str) -> bool:
    marker = "Source files changed in the handoff workspace:"
    if marker not in evidence_packet:
        return False
    section = evidence_packet.split(marker, 1)[1].split(
        "Scratch or temporary artifacts observed before handoff:", 1
    )[0]
    return any(
        line.strip().startswith("- ") and line.strip() != "- NONE OBSERVED"
        for line in section.splitlines()
    )


def _validate_structured_source_change_consistency(
    text: str,
    has_source_changes: bool,
) -> None:
    if not has_source_changes:
        return
    lower = text.lower()
    for phrase in STRUCTURED_NO_CHANGE_CLAIMS:
        if phrase in lower:
            raise RuntimeError(
                "Generated handoff contradicts observed source changes: "
                f"{phrase!r}"
            )
    match = STRUCTURED_SOURCE_CHANGE_CONTRADICTION_RE.search(text)
    if match:
        raise RuntimeError(
            "Generated handoff treats an already-changed checkpoint as untouched: "
            f"{match.group(0)!r}"
        )


def _repair_structured_source_change_contradiction(
    text: str,
    has_source_changes: bool,
) -> str:
    if not has_source_changes:
        return text
    repaired = text
    replacement = "source changes are present in the handoff workspace"
    for phrase in STRUCTURED_NO_CHANGE_CLAIMS:
        repaired = re.sub(
            re.escape(phrase),
            replacement,
            repaired,
            flags=re.IGNORECASE,
        )
    repaired = STRUCTURED_SOURCE_CHANGE_CONTRADICTION_RE.sub(
        "verify the current source changes before deciding whether additional edits are needed",
        repaired,
    )
    return repaired
