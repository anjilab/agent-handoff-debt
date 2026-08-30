"""Model-assisted natural-language handoff summary baseline."""

from __future__ import annotations

from handoff_debt.agents.openhands_adapter import OpenHandsConfig
from handoff_debt.handoff.summarizer_common import completion_text


def generate_summary_notes(
    *,
    config: OpenHandsConfig,
    evidence_packet: str,
) -> str:
    text = completion_text(
        config,
        [
            {
                "role": "system",
                "content": (
                    "You write concise handoff notes for software agents. Use only "
                    "the provided predecessor evidence. Do not add facts or "
                    "validation results that are not present in that evidence."
                ),
            },
            {
                "role": "user",
                "content": f"""Summarize the previous agent work log for a successor agent taking over the task.
Include useful context from the predecessor's investigation, edits, validation
attempts, uncertainty, and possible next steps when those are visible in the
evidence.

Keep the summary concise but useful: 5-10 bullets or short paragraphs is ideal,
aiming for about 200-500 words when enough evidence is available.
Return only the handoff summary text. Do not wrap it in JSON, Markdown fences,
or any other container. Do not mention this prompt or these instructions.

{evidence_packet}
""",
            },
        ],
    )
    summary = text.strip()
    if not summary:
        raise RuntimeError("Summary notes summarizer returned empty summary")
    return summary
