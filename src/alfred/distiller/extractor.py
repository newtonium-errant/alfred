"""Non-agentic extractor — LLM returns JSON, Pydantic validates it.

Week 1 MVP of the distiller rebuild (see memory
``project_distiller_rebuild.md``). Replaces the manifest-file sidecar
protocol (`pipeline.manifest_parse_failed` — 1194 events since
2026-04-15) with a direct Messages-API call whose output is gated by
the Pydantic contracts in ``contracts.py``.

Flow per call:
  1. Render a prompt with the source body + frontmatter + existing
     learn titles (dedup context) + candidate signals (scoring hints).
  2. Call :func:`call_anthropic_no_tools` — no tools, no subprocess.
  3. ``ExtractionResult.model_validate_json`` on the raw text.
  4. On ``ValidationError``: build a repair prompt with the error +
     raw output and call once more. Validate again.
  5. On second failure: log ``extractor.validation_failed`` with the
     raw text + error, return ``ExtractionResult(learnings=[])``.
     Empty is a valid success — the writer just skips the batch.

No file I/O. No tool access. Pure function. All write-side concerns
live in ``writer.py``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .backends.anthropic_sdk import call_anthropic_no_tools
from .candidates import CandidateSignal
from .config import DistillerConfig
from .contracts import ExtractionResult
from .utils import get_logger

log = get_logger(__name__)


# --- Prompt templates --------------------------------------------------------
#
# Kept as module constants for Week 1 — keeps the surface small while the
# contract is still being validated. If prompt iteration volume spikes in
# Week 2+, move these to ``src/alfred/distiller/prompts/extract_v2.md``.

SYSTEM_PROMPT = """You are a knowledge extractor for an Obsidian vault of \
operational records. Your job is to extract latent "learnings" from a \
source record — assumptions, decisions, constraints, contradictions, or \
syntheses that are implicit in the source but not yet captured as their \
own records.

Return output as a JSON object with this exact shape:
{
  "learnings": [
    {
      "type": "assumption" | "decision" | "constraint" | "contradiction" | "synthesis",
      "title": "<5-150 char title, no leading verb>",
      "confidence": "low" | "medium" | "high",
      "status": "<valid status for the type>",
      "claim": "<1-3 sentence claim, 20+ chars>",
      "evidence_excerpt": "<short quote from source showing the signal>",
      "source_links": ["[[source/Source Name]]"],
      "entity_links": ["[[project/Some Project]]", "[[person/Someone]]"],
      "project": "Optional project name or null"
    }
  ]
}

Valid statuses per type:
  - assumption: active, challenged, invalidated, confirmed
  - decision: draft, final, superseded, reversed
  - constraint: active, expired, waived, superseded
  - contradiction: unresolved, resolved, accepted
  - synthesis: draft, active, superseded

Rules:
  - Return ONLY the JSON object. No prose, no code fences, no commentary.
  - If the source doesn't warrant any new learnings, return {"learnings": []}.
  - Do not repeat learnings that already exist (see existing titles list).
  - confidence="high" only when the source is explicit; err toward "medium".
  - title must be a noun phrase, not a full sentence; claim carries the sentence.
"""


def _render_user_prompt(
    source_body: str,
    source_frontmatter: dict[str, Any],
    existing_learn_titles: list[tuple[str, str]],
    signals: CandidateSignal,
) -> str:
    """Assemble the per-source user turn."""
    fm_json = json.dumps(source_frontmatter, default=str, indent=2)

    if existing_learn_titles:
        existing_lines = "\n".join(
            f"  - [{lt}] {title}" for title, lt in existing_learn_titles
        )
    else:
        existing_lines = "  (none)"

    signal_lines = (
        f"  body_length: {signals.body_length}\n"
        f"  has_outcome: {signals.has_outcome}\n"
        f"  has_context: {signals.has_context}\n"
        f"  decision_keywords: {signals.decision_keywords}\n"
        f"  assumption_keywords: {signals.assumption_keywords}\n"
        f"  constraint_keywords: {signals.constraint_keywords}\n"
        f"  contradiction_keywords: {signals.contradiction_keywords}\n"
        f"  link_density: {signals.link_density}"
    )

    return (
        "Extract learnings from this source record.\n\n"
        f"--- Source frontmatter ---\n{fm_json}\n\n"
        f"--- Source body ---\n{source_body}\n\n"
        f"--- Candidate signals (scoring hints) ---\n{signal_lines}\n\n"
        f"--- Existing learnings (do not duplicate) ---\n{existing_lines}\n\n"
        "Return the JSON object."
    )


def _render_repair_prompt(raw: str, error: str) -> str:
    """User turn for the one repair retry.

    Strict constraint: return a JSON object matching the schema and
    fix the specified validation error. No commentary.
    """
    return (
        "Your previous response failed validation. Fix the errors and "
        "return a corrected JSON object with the same schema.\n\n"
        f"--- Your previous response ---\n{raw}\n\n"
        f"--- Validation error ---\n{error}\n\n"
        "Return ONLY the corrected JSON. No prose, no code fences."
    )


def _strip_code_fences(text: str) -> str:
    """Strip ``` or ```json fences if the model ignored the no-fence rule."""
    t = text.strip()
    if t.startswith("```"):
        # Remove opening fence (optionally with language tag)
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1 :]
        # Remove trailing fence
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


async def extract(
    source_body: str,
    source_frontmatter: dict[str, Any],
    existing_learn_titles: list[tuple[str, str]],
    signals: CandidateSignal,
    config: DistillerConfig,
) -> ExtractionResult:
    """Non-agentic LLM extraction with Pydantic validation + one repair retry.

    Returns a validated ``ExtractionResult``. ``ExtractionResult(learnings=[])``
    is a valid success — the caller decides what to do when empty (the
    daemon just records ``candidates_processed`` and moves on).

    Two failure modes are silent-by-design:
      - LLM can't produce valid JSON twice → log ``extractor.validation_failed``,
        return empty result. Surfaces in logs; doesn't crash the daemon.
      - SDK error (timeout, rate-limit, etc.) → propagates to the caller.
        The daemon's top-level ``try`` catches it per-batch.
    """
    user_prompt = _render_user_prompt(
        source_body=source_body,
        source_frontmatter=source_frontmatter,
        existing_learn_titles=existing_learn_titles,
        signals=signals,
    )

    # First attempt
    raw = await call_anthropic_no_tools(
        prompt=user_prompt,
        system=SYSTEM_PROMPT,
        model=config.anthropic.model,
        max_tokens=config.anthropic.max_tokens,
        api_key=config.anthropic.api_key or None,
    )
    cleaned = _strip_code_fences(raw)

    try:
        result = ExtractionResult.model_validate_json(cleaned)
        log.info(
            "extractor.extract_complete",
            attempt=1,
            learnings=len(result.learnings),
        )
        return result
    except ValidationError as exc:
        first_error = str(exc)
        log.info(
            "extractor.validation_retry",
            error=first_error[:500],
        )

    # Repair retry — give the model its own output + the exact error.
    repair_prompt = _render_repair_prompt(raw, first_error)
    raw_repair = await call_anthropic_no_tools(
        prompt=repair_prompt,
        system=SYSTEM_PROMPT,
        model=config.anthropic.model,
        max_tokens=config.anthropic.max_tokens,
        api_key=config.anthropic.api_key or None,
    )
    cleaned_repair = _strip_code_fences(raw_repair)

    try:
        result = ExtractionResult.model_validate_json(cleaned_repair)
        log.info(
            "extractor.extract_complete",
            attempt=2,
            learnings=len(result.learnings),
        )
        return result
    except ValidationError as exc:
        log.warning(
            "extractor.validation_failed",
            attempts=2,
            error=str(exc)[:500],
            raw_len=len(raw_repair),
            raw_preview=raw_repair[:500],
        )
        return ExtractionResult(learnings=[])
