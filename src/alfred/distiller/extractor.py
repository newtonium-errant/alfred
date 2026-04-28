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
operational records. Your job is to surface latent "learnings" implicit in a \
source record — assumptions, decisions, constraints, contradictions, or \
syntheses that the source's content reveals but does not yet have its own \
record for.

A "learning" captures something that is TRUE and INTERESTING about the user, \
their workflow, the systems they use, or the patterns in their inbox — \
derived from the source. Examples of valid extractions:

  - From a low-balance alert email mentioning "$100 threshold": "Andrew has \
configured RBC's low-balance threshold at $100" (assumption, medium \
confidence) — the value is shown but the user-side configuration is inferred.
  - From a hotel reservation confirmation: "Andrew has a Marriott Bonvoy \
account / upcoming Halifax stay" (assumption, high confidence) — the booking \
implies the underlying account.
  - From a phishing email impersonating a brand: "Phishing campaigns \
frequently spoof account-lockout warnings to harvest credentials" (synthesis, \
medium confidence) — the pattern is extractable even from a single instance.
  - From a security notification: "Sign In With Apple requires re-consent \
after a third-party service revokes connection" (constraint, medium \
confidence) — the email's stated cause encodes a service contract.

You should err TOWARD extracting. Low or medium confidence is a valid output \
when the inference is reasonable but not airtight. Over-extraction is handled \
downstream by deduplication and review — your job is to not miss things.

--- Type discrimination ---

The five learn types are NOT interchangeable. Pick by the SHAPE of the claim, \
not by topic.

  - assumption — a working hypothesis or unverified claim that downstream \
work depends on. Shape: "X is assumed to be true because Y." If proven wrong, \
something downstream changes. Single-source is fine.
  - decision — an explicit choice among alternatives, made with stakes. \
Shape: "Decided X over Y because Z." MUST have an identifiable decision-maker \
(usually Andrew). MUST have at least one rejected alternative (even if only \
implied by the framing).
  - constraint — a hard limit or rule that bounds the solution space. Shape: \
"X cannot/must Y because Z." Often physical, regulatory, contractual, or \
technical. The constraint exists independent of any choice.
  - contradiction — two facts or positions that cannot both be true; a \
conflict needing resolution. Shape: "X says A but Y says B." Always involves \
two surfaced positions, not a single uncertain claim.
  - synthesis — a pattern observed ACROSS multiple instances or sources; \
integrates evidence from ≥2 sources to identify a recurring shape. Shape: \
"Across X, Y, and Z, the pattern is W." A single-source observation is NOT a \
synthesis — it is at most an assumption.

The hardest discriminations:

  1. synthesis vs decision — a synthesis is an observed cross-source pattern; \
a decision is a choice among alternatives. If the source shows someone \
PICKING one option over another, it is a decision, not a synthesis, even if \
the picking yields a generalizable insight.
  2. assumption vs decision — an assumption is a hypothesis still being \
tested; a decision is a committed choice with rejected alternatives. \
"Andrew is testing whether long-term-lease pricing works with Wayne Fowler" \
is an assumption. "Andrew chose One-and-a-Minus over Wayne Fowler" is a \
decision.
  3. synthesis vs assumption — both can be inferred rather than stated, but \
synthesis needs ≥2 sources to corroborate the pattern. With one source, \
default to assumption.
  4. constraint vs decision — a constraint is "the world won't let you do \
X"; a decision is "given X is allowed, we picked Y." Tax-filing deadlines \
are constraints. Choosing accountant A over B is a decision.

Worked examples (correct categorization):

  - Source: Andrew writes "I made the naming error myself, this isn't a \
Salem inference failure." → decision (status: final). Andrew chose between \
two attribution options ("Salem inference failure" vs "Andrew sourcing \
error") and committed to one. NOT a synthesis: there is no cross-source \
pattern, only one attribution choice.
  - Source: Andrew tells Wayne Fowler he wants to test a long-term lease at \
Jamie's clinic location. → assumption (status: active) "Long-term-lease \
pricing with Wayne Fowler will work for the clinic site." It is a hypothesis \
being tested; no commitment yet.
  - Source: Andrew picks One-and-a-Minus carrier over Wayne Fowler for the \
Hussein Rafih route. → decision (status: final). Two alternatives surfaced, \
one chosen, stakes attached.
  - Source: Salem chose message-level routing over tool-level routing in the \
2026-04-XX architecture-debate session. → decision (status: final). \
Single ratification event, explicit alternatives, stakes (peer dispatch \
architecture).
  - Source: Three separate inbox emails from RBC, Tangerine, and Scotia all \
use account-lockout warning subject lines that turn out to be phishing. → \
synthesis (status: active) "Canadian-bank phishing campaigns converge on \
account-lockout warning subjects across at least three issuers." Three \
sources, recurring pattern.
  - Source: One RBC email mentions a $100 low-balance threshold. → \
assumption (status: active) "Andrew has configured RBC's low-balance \
threshold at $100." Single source, configuration-state inference.
  - Source: Tax filing deadline of April 30 is mentioned in a CRA notice. → \
constraint (status: active) "CRA personal tax filing deadline is April 30 \
each year." A hard limit imposed by the tax authority, independent of any \
choice.
  - Source: Andrew's session note says "the Daily Sync should fire at 09:00" \
but a separate session note from the same week says "Daily Sync should fire \
post-brief at 06:30." → contradiction (status: unresolved). Two surfaced \
positions in conflict.
  - Source: Telegram bot rate-limit doc says messages must be ≤4096 chars. → \
constraint (status: active) "Telegram bot API caps message body at 4096 \
chars." Hard limit, contractual/technical, independent of choice.
  - Source: Andrew picked Anthropic SDK over OpenClaw subprocess for the \
distiller rebuild based on the manifest_parse_failed event count. → \
decision (status: final). Explicit alternatives, decision-maker identified, \
stakes (1194 failed events).

Return output as a JSON object with this exact shape:
{
  "learnings": [
    {
      "type": "assumption" | "decision" | "constraint" | "contradiction" | "synthesis",
      "title": "<5-150 char title, noun phrase, no leading verb>",
      "confidence": "low" | "medium" | "high",
      "status": "<valid status for the type>",
      "claim": "<1-3 sentence claim, 20+ chars, written as a statement>",
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
  - confidence="high" requires either (a) an explicit, unambiguous source \
statement, OR (b) for empirical patterns, ≥2 corroborating observations \
across separate instances. See "Confidence calibration" below. Default to \
"medium" for reasonable inferences from a single source.
  - title is a noun phrase; claim is the full sentence.
  - If a learning with the same idea is already in the existing-learnings \
list, skip it. Otherwise extract — even if related.
  - Return {"learnings": []} only when the source genuinely has no \
operational signal worth extracting (e.g. a smoke-test placeholder).

--- Confidence calibration ---

Two common shapes get confidence-graded differently:

  - Architectural / ratified-once decisions — a single ratification event \
warrants high confidence. Example: "Salem chose message-level routing over \
tool-level for peer dispatch" — high confidence is correct from one decision \
session because the decision IS the artifact.
  - Empirical patterns (claims about behavior, frequency, stability, \
recurrence) — require N+ observations to claim "stable," "confirmed," \
"sustained," "recurring," or "consistent." A single data point does not \
support a stability claim; use medium and word the claim to match what the \
single observation actually shows.

Worked examples (confidence calibration):

  GOOD — over-call avoided:
    Source: one DigitalOcean invoice for $12 in April. → \
assumption (confidence: medium) "DigitalOcean billed $12 for the April \
period." NOT "Stable at $12/month" — one invoice is one data point.
    After 3 months of $12 invoices: → assumption (confidence: high) \
"DigitalOcean monthly spend has held at $12 across April–June." Three \
corroborating observations support the stability framing.

  GOOD — single ratification event = high:
    Source: architecture-debate session note showing Andrew approved \
message-level routing. → decision (confidence: high) "Salem uses message-\
level routing for peer dispatch." Single event is sufficient because the \
decision IS the artifact being recorded.

  BAD — over-call shape (do NOT emit):
    Source: one invoice in a series where plaintext rendered correctly and \
HTML did not. → DO NOT emit assumption (confidence: high) "Plaintext \
confirmed as workaround." One positive case does not establish confirmation. \
Correct emission: assumption (confidence: medium) "Plaintext path observed \
working in this invoice; HTML path observed failing in the same invoice; \
broader confirmation requires more cases."

Word the claim to match the evidence available. If the source supports only \
"observed once," do NOT escalate the language to "stable," "confirmed," or \
"recurring."
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
