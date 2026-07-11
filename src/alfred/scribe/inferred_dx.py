"""#48 — deterministic inferred-diagnosis post-check + self-correcting capture.

qwen-14b at temp=0 deterministically writes an INFERRED diagnosis label (e.g.
"major depressive disorder" from low mood + PHQ-9 + an SSRI) that grounding is
BLIND to (a pure-qualitative fabrication — a claim citing a real segment but
inventing a LABEL from it — has no number/negation token to check) and rule-6
cannot stop (the model disobeys the maximally-explicit prompt). This is the CODE
lever: a deterministic post-check that FLAGS (never removes — mirrors grounding)
a claim naming a lexicon diagnosis label that is absent from the claim's CITED
source segments.

CITED-SPAN, not whole-transcript (locked by review): the check compares the
claim's named diagnosis against ONLY the text of the segments the claim CITES
(``source_spans``, joined by grounding's ``_cited_text``). Whole-transcript would
false-CLEAR an inferred CURRENT diagnosis whenever the label appears verbatim in
family-history / ruled-out / differential / past-resolved context elsewhere — a
high-harm false-NEGATIVE on the common primary-care case. The mis-cite
false-POSITIVE (the dx stated in a DIFFERENT cited segment than the claim
references) is low-harm + rare, and a co-citation (``["S1","S5"]``) clears it
because ``_cited_text`` JOINS all cited spans.

ALL FOUR SECTIONS (not assessment-only): the inferred dx smuggles into the PLAN
as an indication ("Start sertraline for major depressive disorder" citing "start
sertraline", no dx in the cite). The lexicon match is section-agnostic; cited-span
keeps the false-positive rate low everywhere (a stated "history of MDD" whose
claim cites the segment carrying the label clears).

SELF-CORRECTING SEAM (Part 1 — capture; feedback_self_correcting_design_standard):
:func:`record_inferred_dx_attest_outcome` emits a READ-ONLY observability signal at
attest — per ``inferred_diagnosis`` flag, whether the flagged label SURVIVED into
the attested body (kept ⇒ likely a false-positive; removed ⇒ the check was right)
— for morning-review. TWO GUARDRAILS: (1) SIDE-EFFECT-FREE w.r.t. attestation — a
pure log that NEVER blocks/alters/gates an attest (medico-legal path); it is also
internally exception-swallowing so a capture bug can never fail a valid attest.
(2) kept-vs-removed via label word-boundary presence is a HEURISTIC / coarse
signal — it drives NO auto-action. Parts 2-3 (auto-grow the lexicon / tune from
operator-approved overrides) are a documented later increment.
"""

from __future__ import annotations

from typing import Any

import structlog

from alfred.scribe.diagnosis_lexicon import diagnoses_named_in, entry_present
from alfred.scribe.grounding import GroundingFlag, _cited_text
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

# The single reason literal these flags carry (dispatched to the INFERRED_DIAGNOSIS
# inline literal by GroundingResult.flag_for).
INFERRED_DIAGNOSIS_REASON = "inferred_diagnosis"


def check_inferred_diagnoses(
    structured: StructuredNote, transcript: Transcript,
) -> list[GroundingFlag]:
    """Return one :class:`GroundingFlag` (``reason='inferred_diagnosis'``) per
    claim (across ALL four SOAP sections) that NAMES a lexicon diagnosis absent
    from its CITED source segments — the deterministic inferred-dx post-check.

    FLAG, do not remove — the flags are extended onto the existing
    ``GroundingResult.flags`` so they ride the SAME render (``flag_for`` →
    inline ⚠) + ``grounding_flags`` frontmatter path grounding uses. Deterministic
    string ops; no LLM, no mutation of the claim objects."""
    seg_by_id = {s.id: s for s in transcript.segments}
    flags: list[GroundingFlag] = []
    for section, idx, claim in structured.all_claims():
        named = diagnoses_named_in(claim.claim)
        if not named:
            continue  # low-FP posture: only NAMED lexicon labels, never bare symptoms
        cited = _cited_text(claim.source_spans, seg_by_id)  # grounding's JOIN of all cited spans
        inferred = [e for e in named if not entry_present(e, cited)]
        if not inferred:
            continue  # every named dx appears verbatim in the cited segment(s) → stated, clean
        labels = [e.canonical for e in inferred]
        flags.append(
            GroundingFlag(
                section=section,
                claim_index=idx,
                reason=INFERRED_DIAGNOSIS_REASON,
                detail=(
                    "inferred_diagnosis: "
                    + ", ".join(labels)
                    + " named in the claim but absent from the cited segment(s) — "
                    "the model may have INFERRED this diagnosis from grounded "
                    "symptoms; clinician to confirm or remove"
                ),
                claim=claim.claim,
                source_spans=list(claim.source_spans),
            )
        )
    return flags


def record_inferred_dx_attest_outcome(
    *,
    grounding_flags: Any,
    draft_original: str,
    attested_body: str,
    source_id: str,
) -> None:
    """Self-correcting Part-1 CAPTURE — a READ-ONLY signal at attest.

    For each ``inferred_diagnosis`` entry in the note's ``grounding_flags``
    frontmatter, log whether the flagged diagnosis label SURVIVED into the
    attested body (``kept``) or was removed (``kept=False``) relative to the
    AI-generated ``draft_original`` (P3-b3). Emits
    ``scribe.inferred_dx.attest_outcome`` for morning-review.

    SIDE-EFFECT-FREE + fail-silent BY CONSTRUCTION: the entire body is wrapped so
    an exception here can NEVER propagate to the attest path (a capture bug must
    not fail a valid, medico-legal attestation). ``kept`` via word-boundary label
    presence is a HEURISTIC/coarse signal — drives NO auto-action.
    """
    try:
        if not isinstance(grounding_flags, list):
            return
        for flag in grounding_flags:
            if not isinstance(flag, dict) or flag.get("reason") != INFERRED_DIAGNOSIS_REASON:
                continue
            claim_text = str(flag.get("claim", ""))
            for entry in diagnoses_named_in(claim_text):
                was_in_draft = entry_present(entry, draft_original)
                kept = entry_present(entry, attested_body)
                # Only a label the AI actually drafted is a meaningful correction
                # signal (the flagged claim's label WAS in draft_original by
                # construction; the guard is defensive against checkpoint churn).
                if not was_in_draft:
                    continue
                log.info(
                    "scribe.inferred_dx.attest_outcome",
                    source_id=source_id,
                    diagnosis=entry.canonical,   # a lexicon canonical — NOT PHI
                    kept=kept,                   # True ⇒ likely FP (dx legit); False ⇒ TP (removed)
                    section=flag.get("section", ""),
                    detail=(
                        "self-correcting Part-1 capture (heuristic, read-only): "
                        "the flagged inferred diagnosis "
                        + ("SURVIVED into" if kept else "was REMOVED from")
                        + " the attested body — surface for morning-review"
                    ),
                )
    except Exception:  # noqa: BLE001 — SIDE-EFFECT-FREE guarantee: never fail an attest
        log.warning(
            "scribe.inferred_dx.attest_capture_error",
            source_id=source_id,
            detail="inferred-dx attest capture failed — SWALLOWED (attestation unaffected)",
        )
