"""#14 item-13 slice 14c — the deterministic post-note QUALITY pass.

A CODE (no-LLM) pass run in the pipeline AFTER grounding, BEFORE render — the completeness/style
sibling of grounding's faithfulness check. It reports the 3 MVP quality signals as NOTE-LEVEL
``quality_*`` flags. Like grounding it NEVER auto-mutates (anti-spoliation): it FLAGS, the clinician
decides. ADVISORY — it never gates note-gen (the eval never gates either).

The flags are a SEPARATE frontmatter list from ``grounding_flags`` (grounding = faithfulness-to-
transcript = medico-legal; quality = completeness/style-vs-profile = advisory) — the pipeline splits
by the ``quality_`` reason prefix so an advisory nudge can never dilute the grounding signal's weight
(§4.1). They RENDER via the SAME ``GroundingResult.flags_for`` dispatch (registered in
``grounding._REASON_INLINE_LITERAL``): each is a NOTE-LEVEL banner (``("note", -1)``) rendered at the
top of the note by the existing note-level path — NO ``render_soap`` change.

PROFILE-AWARE + degrades on DEFAULT: reads the active profile's ``required`` sections + succinctness
target via the TOTAL ``resolve_active_profile`` (DEFAULT_PROFILE — required S/A/P, target 25 — when no
profile is init'd or one is corrupt), so the pass NEVER crashes.

The 3 checks:
  * ``quality_required_section_empty`` — a profile-``required`` SOAP section has no claims ("Not
    addressed"). One flag per empty required section.
  * ``quality_verbose`` (ADVISORY) — total words / total claims > the profile target. Skipped when
    there are no claims (no div-by-zero).
  * ``quality_assessment_no_plan`` — assessment has ≥1 claim AND plan is empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from alfred.scribe.grounding import GroundingFlag
from alfred.scribe.notegen import SOAP_SECTIONS

if TYPE_CHECKING:
    from alfred.scribe.notegen import StructuredNote
    from alfred.scribe.notegen_profile import NoteProfile

log = structlog.get_logger(__name__)

# The ``quality_`` reason prefix is the SPLIT key: the pipeline routes every flag whose reason starts
# with it into the advisory ``quality_flags`` list, and everything else into ``grounding_flags``. A
# disjointness pin guarantees no grounding/inferred/speaker reason ever starts with this prefix.
QUALITY_REASON_PREFIX = "quality_"

QUALITY_REQUIRED_SECTION_EMPTY_REASON = "quality_required_section_empty"
QUALITY_VERBOSE_REASON = "quality_verbose"
QUALITY_ASSESSMENT_NO_PLAN_REASON = "quality_assessment_no_plan"

QUALITY_REASONS = frozenset({
    QUALITY_REQUIRED_SECTION_EMPTY_REASON,
    QUALITY_VERBOSE_REASON,
    QUALITY_ASSESSMENT_NO_PLAN_REASON,
})


def _note_flag(reason: str, detail: str) -> GroundingFlag:
    """A NOTE-LEVEL quality flag (``("note", -1)``) — renders as a top banner via the existing
    note-level ``flags_for("note", -1)`` path. No claim / cite (a whole-note advisory signal)."""
    return GroundingFlag(
        section="note", claim_index=-1, reason=reason, detail=detail, claim="", source_spans=[])


def check_note_quality(structured: "StructuredNote", profile: "NoteProfile") -> list[GroundingFlag]:
    """Return the NOTE-LEVEL ``quality_*`` flags for ``structured`` against ``profile`` (deterministic,
    no LLM, NEVER mutates the note). PROFILE-AWARE: only sections the profile marks ``required`` fire
    the empty-section check; the verbose check uses the profile target. Degrades safely on
    DEFAULT_PROFILE (the caller resolves it via the TOTAL resolver)."""
    flags: list[GroundingFlag] = []

    # (1) required-section-empty — only for a profile-required SOAP section that rendered no claims.
    # Guard on SOAP_SECTIONS so a hand-authored profile with a non-SOAP key can never crash the pass.
    for sec in profile.sections:
        if sec.key in SOAP_SECTIONS and sec.required and not structured.section(sec.key):
            flags.append(_note_flag(
                QUALITY_REQUIRED_SECTION_EMPTY_REASON,
                f"quality_required_section_empty: the '{sec.key}' section is marked REQUIRED by "
                f"note_profile v{profile.profile_version} but has no claims (advisory)"))

    # (3) assessment-without-plan — findings but no plan of action.
    if structured.section("assessment") and not structured.section("plan"):
        flags.append(_note_flag(
            QUALITY_ASSESSMENT_NO_PLAN_REASON,
            "quality_assessment_no_plan: the assessment has findings but the plan is empty "
            "(advisory — add a plan or confirm intentional)"))

    # (2) verbose (ADVISORY) — words/claim over the profile target. Skipped when no claims exist.
    total_claims = sum(len(structured.section(s)) for s in SOAP_SECTIONS)
    if total_claims > 0:
        total_words = sum(
            len(c.claim.split()) for s in SOAP_SECTIONS for c in structured.section(s))
        target = profile.succinctness_target_words_per_claim
        words_per_claim = total_words / total_claims
        if words_per_claim > target:
            flags.append(_note_flag(
                QUALITY_VERBOSE_REASON,
                f"quality_verbose: {total_words} words / {total_claims} claims = "
                f"{words_per_claim:.1f} words/claim exceeds the succinctness target {target} "
                f"(ADVISORY — never gates note-gen)"))

    return flags
