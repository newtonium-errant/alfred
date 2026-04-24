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


# Per-source-type extraction rules (c10, 2026-04-24). Ported from the
# legacy pipeline's ``_EXTRACTION_RULES`` (see ``pipeline.py``) — the
# text is the only piece of the legacy SKILL worth keeping; everything
# else in ``stage1_extract.md`` either duplicates schema contracts the
# Pydantic layer now owns, or pushes the ``cat > manifest_path`` file-
# write protocol that v2 is eliminating. Light cleanup only; the
# per-type framing (session vs. conversation vs. note vs. task vs.
# project) is the critical asset.
_V2_EXTRACTION_RULES: dict[str, str] = {
    "conversation": (
        "- Decisions: 'we agreed', 'let's go with', 'decided to', explicit choices.\n"
        "- Assumptions: 'we're assuming', 'should be fine', implicit beliefs about "
        "timelines or outcomes.\n"
        "- Constraints: 'we can't', 'regulation requires', 'budget limit', "
        "'deadline is'.\n"
        "- Contradictions: disagreements between participants, conflicting "
        "information."
    ),
    "session": (
        "- Decisions: check ## Outcome sections, action items that imply choices made.\n"
        "- Assumptions: Context sections revealing beliefs the team operates on.\n"
        "- Synthesis: patterns across multiple sessions about the same project."
    ),
    "note": (
        "- Assumptions: research notes revealing implicit beliefs.\n"
        "- Constraints: meeting notes mentioning limits, regulations, requirements.\n"
        "- Synthesis: ideas connecting multiple observations."
    ),
    "task": (
        "- Assumptions: Context fields revealing why a task exists.\n"
        "- Decisions: task outcomes that reflect choices made.\n"
        "- Constraints: blockers and dependencies revealing limits."
    ),
    "project": (
        "- Assumptions: `based_on` and `depends_on` fields revealing foundational "
        "beliefs.\n"
        "- Constraints: `blocked_by` revealing limits.\n"
        "- Decisions: project scope and approach choices."
    ),
}

_V2_EXTRACTION_RULES_GENERIC = (
    "- Examine the source for implicit beliefs, choices made, limits "
    "mentioned in passing, statements that conflict with each other, or "
    "patterns connecting multiple observations."
)


SYSTEM_PROMPT = """You are a knowledge extractor for an Obsidian vault of \
operational records. Your job is to identify latent knowledge that would be \
lost if not captured as its own record.

Look for five kinds of latent knowledge:

- **Assumptions** — beliefs the team is operating on, implicit or explicit \
("we're assuming X", budget lines that only work if Y holds, plans that \
presume Z).
- **Decisions** — choices made but not formally recorded elsewhere \
("we went with X", "chose A over B", resolutions that settled a question).
- **Constraints** — limits mentioned in passing (regulatory, budget, \
timeline, technical, contractual). Casual mentions count — a constraint \
named once in a meeting note is still a constraint.
- **Contradictions** — statements that conflict with each other inside \
this source, or against the existing learnings listed in the user prompt.
- **Syntheses** — patterns connecting multiple observations into a \
higher-order insight.

Return output as a JSON object with this exact shape:
{
  "learnings": [
    {
      "type": "assumption" | "decision" | "constraint" | "contradiction" | "synthesis",
      "title": "<5-150 char noun phrase, no leading verb>",
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

## Confidence & status calibration

Use all three confidence levels; do not default to "medium" out of caution.

| Signal                                                | confidence | status     |
|-------------------------------------------------------|------------|------------|
| Decision explicitly stated ("we decided")             | high       | final      |
| Decision implied by action taken                      | medium     | draft      |
| Assumption explicitly stated ("we're assuming")       | medium     | active     |
| Assumption implied by context                         | low        | active     |
| Constraint from regulation / contract                 | high       | active     |
| Constraint mentioned casually                         | low        | active     |
| Contradiction between explicit statements             | high       | unresolved |
| Contradiction between implicit positions              | medium     | unresolved |
| Synthesis from 3+ observations                        | medium     | draft      |
| Synthesis from 2 observations                         | low        | draft      |

Rules:
  - Return ONLY the JSON object. No prose, no code fences, no commentary.
  - If the source contains no actionable latent knowledge, return \
`{"learnings": []}`. Marketing emails, auto-replies, and sparse logs \
legitimately have nothing to extract — don't hallucinate a learning to \
fill the slot.
  - But: if the source has genuine operational content, prefer to extract \
at least one learning. A source rich in decision/assumption/constraint \
keywords but scored as "learnings: []" almost always means signal was \
missed, not that nothing was there.
  - Do not repeat learnings that already exist (see existing titles list).
  - title must be a noun phrase, not a full sentence; the claim carries \
the sentence. Every learning must trace to specific content in the source \
— never invent.
"""


def _render_user_prompt(
    source_body: str,
    source_frontmatter: dict[str, Any],
    existing_learn_titles: list[tuple[str, str]],
    signals: CandidateSignal,
    source_type: str | None = None,
) -> str:
    """Assemble the per-source user turn.

    ``source_type`` (c10, 2026-04-24) drives the per-type
    ``_V2_EXTRACTION_RULES`` block injected under "Extraction rules for
    this source type." Unknown/None falls back to a generic hint so
    unseen record types still get non-empty guidance.
    """
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

    rules_block = _V2_EXTRACTION_RULES.get(
        source_type or "", _V2_EXTRACTION_RULES_GENERIC,
    )
    rules_header = (
        f"Extraction rules for source type '{source_type}':"
        if source_type and source_type in _V2_EXTRACTION_RULES
        else "Extraction rules (generic):"
    )

    # Signal-driven nudge (c10). When the keyword pre-scan or section
    # markers suggest the source is likely to carry latent knowledge,
    # we remind the model to prefer extracting at least one learning
    # unless the signals are clearly spurious. The keyword regexes
    # (candidates.py::*_KEYWORDS) are crude — false positives happen —
    # so this is a *nudge*, not a command. The empty-is-fine clause in
    # SYSTEM_PROMPT still dominates when the source is genuinely sparse.
    has_signal = (
        signals.decision_keywords > 0
        or signals.assumption_keywords > 0
        or signals.constraint_keywords > 0
        or signals.contradiction_keywords > 0
        or signals.has_outcome
        or signals.has_context
    )
    signal_nudge = (
        "\n--- Signal-driven nudge ---\n"
        "The pre-scan flagged signals suggesting this source likely "
        "contains latent knowledge of the indicated types. Prefer to "
        "extract at least one learning unless the signals are clearly "
        "spurious (e.g. keywords inside quoted prose, boilerplate "
        "footer text).\n"
        if has_signal
        else ""
    )

    return (
        "Extract learnings from this source record.\n\n"
        f"--- {rules_header} ---\n{rules_block}\n\n"
        f"--- Source frontmatter ---\n{fm_json}\n\n"
        f"--- Source body ---\n{source_body}\n\n"
        f"--- Candidate signals (scoring hints) ---\n{signal_lines}\n"
        f"{signal_nudge}\n"
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
    source_type: str | None = None,
) -> ExtractionResult:
    """Non-agentic LLM extraction with Pydantic validation + one repair retry.

    Returns a validated ``ExtractionResult``. ``ExtractionResult(learnings=[])``
    is a valid success — the caller decides what to do when empty (the
    daemon just records ``candidates_processed`` and moves on).

    ``source_type`` (c10, 2026-04-24) — the vault record type of the
    source (``session``, ``note``, ``task``, ``project``,
    ``conversation``). Drives the per-type extraction-rules block in
    the user prompt. Optional for backward compat and ease of unit-test
    construction; unknown/None falls back to a generic nudge.

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
        source_type=source_type,
    )

    # First attempt
    raw, meta = await call_anthropic_no_tools(
        prompt=user_prompt,
        system=SYSTEM_PROMPT,
        model=config.anthropic.model,
        max_tokens=config.anthropic.max_tokens,
        api_key=config.anthropic.api_key or None,
    )
    cleaned = _strip_code_fences(raw)
    stop_reason = meta.get("stop_reason")

    try:
        result = ExtractionResult.model_validate_json(cleaned)
        # c9 (2026-04-24): on learnings=0 the extractor was indistinguishable
        # from a silent "nothing to extract" vs. a truncated / refused LLM.
        # We now log the raw preview + stop_reason when the first attempt
        # returns empty so the next iteration can tell the three apart.
        if len(result.learnings) == 0:
            log.info(
                "extractor.extract_empty",
                attempt=1,
                stop_reason=stop_reason,
                raw_preview=raw[:200],
            )
        log.info(
            "extractor.extract_complete",
            attempt=1,
            learnings=len(result.learnings),
            stop_reason=stop_reason,
        )
        return result
    except ValidationError as exc:
        first_error = str(exc)
        log.info(
            "extractor.validation_retry",
            error=first_error[:500],
            stop_reason=stop_reason,
        )

    # Repair retry — give the model its own output + the exact error.
    repair_prompt = _render_repair_prompt(raw, first_error)
    raw_repair, meta_repair = await call_anthropic_no_tools(
        prompt=repair_prompt,
        system=SYSTEM_PROMPT,
        model=config.anthropic.model,
        max_tokens=config.anthropic.max_tokens,
        api_key=config.anthropic.api_key or None,
    )
    cleaned_repair = _strip_code_fences(raw_repair)
    stop_reason_repair = meta_repair.get("stop_reason")

    try:
        result = ExtractionResult.model_validate_json(cleaned_repair)
        if len(result.learnings) == 0:
            log.info(
                "extractor.extract_empty",
                attempt=2,
                stop_reason=stop_reason_repair,
                raw_preview=raw_repair[:200],
            )
        log.info(
            "extractor.extract_complete",
            attempt=2,
            learnings=len(result.learnings),
            stop_reason=stop_reason_repair,
        )
        return result
    except ValidationError as exc:
        log.warning(
            "extractor.validation_failed",
            attempts=2,
            error=str(exc)[:500],
            raw_len=len(raw_repair),
            raw_preview=raw_repair[:500],
            stop_reason=stop_reason_repair,
        )
        return ExtractionResult(learnings=[])
