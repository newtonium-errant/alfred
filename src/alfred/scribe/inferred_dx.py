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

HEDGE-AWARE clear (FIX 1, audit batch 2 #5): cited-span alone closed only the
DIFFERENT-span hole. The SAME-span hole — the label present in the cited span but
wrapped in a hedge there ("family history of MDD", "rule out MDD", "no evidence of
MDD", "MDD vs adjustment disorder") — was still false-CLEARED. The clear-check
(:func:`_stated_current`) now requires a CURRENT-assertion occurrence: a label
present ONLY inside a negation / family-history / ruled-out / differential /
screening / resolved frame does NOT clear. A bare "history of MDD" / "PMH: HTN"
is a current active problem (not a hedge) and still clears.

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

import re
from typing import Any

import structlog

from alfred.scribe.diagnosis_lexicon import (
    DiagnosisEntry,
    diagnoses_named_in,
    entry_present,
    form_spans_in,
    normalize_text,
)
from alfred.scribe.grounding import _NEGATION_RE, GroundingFlag, _cited_text
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

# The single reason literal these flags carry (dispatched to the INFERRED_DIAGNOSIS
# inline literal by GroundingResult.flags_for).
INFERRED_DIAGNOSIS_REASON = "inferred_diagnosis"


# --- FIX 1 (audit batch 2 #5): SAME-SPAN hedge-aware clear-check ------------
# A cited lexicon label clears an inferred-dx flag ONLY when it is a CURRENT
# assertion. The original clear-check (``entry_present(e, cited)``) was blind to
# a negation / family-history / ruled-out / differential / screening / resolved
# frame WRAPPING the label in the SAME cited span — so a current-assessment dx
# claim whose only support is a HEDGED mention ("family history of MDD", "rule out
# MDD", "no evidence of MDD", "MDD vs adjustment disorder") was false-CLEARED (a
# real false-NEGATIVE in the check's own high-harm class). We now inspect the
# clause immediately around each label occurrence and treat a hedged-only label as
# NOT stated. Reuses grounding's clinical negation vocabulary (``_NEGATION_RE``)
# and adds the non-negation hedge phrases below.
#
# NOTE (deliberate non-hedges): a bare "history of MDD" / "PMH: hypertension" is a
# CURRENT active problem, not a hedge — so ``history of`` is NOT a hedge marker
# (keeps the PMH-stated fixtures clean). Only an explicit ``resolved`` / ``resolving``
# framing past-hedges. POST-POSITIVE negation ("MDD was ruled out") is a known
# residual gap, the same class grounding documents (grounding.py "(e)").

# Non-negation hedge phrases in the clause BEFORE a matched label (operate on
# ``normalize_text`` output — hyphens already folded to spaces, so no hyphen
# variants needed). ``\?`` = a differential "?MDD".
_EXTRA_PRECEDING_HEDGE_RE = re.compile(
    r"\bfamily (?:history|hx) of\b|\bfh of\b|\bfhx\b"
    r"|\b(?:mother|father|sibling|brother|sister|parent|maternal|paternal|son|daughter)\b"
    r"|\brule[ds]? out\b|\bruling out\b|\br/o\b"
    r"|\bversus\b|\bvs\b|\bdifferential\b|\bddx\b|\?"
    r"|\bscreen(?:ing)? for\b"
)
# Hedge markers in the clause AFTER a matched label: differential ("MDD vs …",
# "MDD?") + explicit past-resolved framing.
_FOLLOWING_HEDGE_RE = re.compile(
    r"^(?:versus|vs|resolved|resolving)\b|^\?"
)
# "non-diabetic" / "non diabetic" immediately before the label (after the hyphen
# fold both end "... non "). "non" is DELIBERATELY out of grounding's global
# _NEGATION_RE (it false-registers inside "non-productive"); here it is clause-
# anchored immediately before a dx label, where it is a genuine negation.
_NON_PREFIX_RE = re.compile(r"\bnon\s*$")


# --- FIX 2 (audit batch 2 #7, CORRECTED): screening-INSTRUMENT blocklist -----
# A dx abbrev is a word-boundary SUBSTRING of many clinical instrument tokens (the
# hyphen is a word boundary): ptsd ⊂ PC-PTSD-5, adhd ⊂ ADHD-RS, ibs ⊂ IBS-SSS, osa
# ⊂ OSA-18, bph ⊂ BPH-II, ckd ⊂ CKD-EPI, gerd ⊂ GERD-Q (and gad ⊂ GAD-7/GAD-2).
# NAMING the instrument is NOT the clinician stating the DIAGNOSIS — so a cited
# "PC-PTSD-5 score is 12" must NOT clear an inferred PTSD (a high-harm false-CLEAR
# the self-correcting loop is BLIND to: a false-CLEAR emits NO flag → NO capture
# row → the loop can never grow to fix it, so it MUST be closed here, not deferred).
#
# The FIX (keeps the abbrev forms → keeps recall, unlike dropping them): MASK these
# instrument tokens out of the cited text. This MUST run on the RAW text BEFORE the
# FIX-4 hyphen→space fold — the fold turns "pc-ptsd-5" into "pc ptsd 5" and destroys
# the signature, so detection has to precede it. Flexible ``[-\s]?`` separators so a
# pre-spaced transcript variant ("pc ptsd 5") is caught too. A label whose ONLY
# cited occurrence was inside a masked instrument then has no span → not "stated" →
# the dx FLAGS. Composes with the hedge check (an occurrence counts as stated only
# if current-assertion AND not instrument-embedded).
#
# NOT via a dropped abbrev EXCEPT where the collision reaches BEYOND the instrument
# namespace (the mask cannot reach a LAB/drug token): ``gad`` stays dropped (⊂ the
# lab "anti-GAD / GAD antibodies", not only GAD-7); ms/mi/ra/pd/ad stay dropped
# (drug / ambiguous). See the lexicon curation-policy docstring.
_SCREENING_INSTRUMENT_RE = re.compile(
    r"\b(?:"
    r"pc[-\s]?ptsd[-\s]?5|ptsd[-\s]?8"          # PTSD screens (⊃ ptsd)
    r"|adhd[-\s]?rs(?:[-\s]?iv)?"                # ADHD Rating Scale, +/- -IV (⊃ adhd)
    r"|ibs[-\s]?(?:sss|qol)"                     # IBS severity / QoL (⊃ ibs)
    r"|osa[-\s]?18"                              # OSA-18 QoL (⊃ osa)
    r"|bph[-\s]?ii"                              # BPH Impact Index (⊃ bph)
    r"|ckd[-\s]?epi"                             # CKD-EPI eGFR equation (⊃ ckd)
    r"|gerd[-\s]?q"                              # GERD-Q questionnaire (⊃ gerd)
    r"|gad[-\s]?[27]"                            # GAD-7/GAD-2 (belt: gad also dropped)
    r"|phq[-\s]?(?:9|2|15)"                      # PHQ-* (no dx abbrev embedded; documents)
    r")\b",
    re.IGNORECASE,
)


def _mask_screening_instruments(raw_cited: str) -> str:
    """Blank out screening-instrument / scale / equation tokens on the RAW cited
    text (pre-fold) so a dx abbrev embedded in one cannot register as a stated dx.
    Replaced with a space to preserve the token boundary. MUST precede the
    ``normalize_text`` hyphen→space fold (which destroys the instrument signature)."""
    return _SCREENING_INSTRUMENT_RE.sub(" ", raw_cited)


def _occurrence_is_hedged(norm: str, start: int, end: int) -> bool:
    """True iff the label occurrence at ``[start, end)`` in ``norm`` (a
    ``normalize_text`` string) sits inside a negation / hedge / attribution /
    differential / screening / resolved frame — so it does NOT count as a current
    assertion. Windows are cut at clause punctuation so a hedge governing a
    DIFFERENT clause's label does not leak (over-flag is the SAFE direction; a
    leaked hedge would over-flag, never false-CLEAR)."""
    # Preceding context, limited to the label's own clause.
    before_clause = re.split(r"[.;,:()]", norm[:start])[-1]
    if _NEGATION_RE.search(before_clause):            # reuse grounding's negation vocab
        return True
    if _EXTRA_PRECEDING_HEDGE_RE.search(before_clause):
        return True
    if _NON_PREFIX_RE.search(before_clause):
        return True
    # Following context: differential / resolved may TRAIL the label. Comma does
    # NOT cut here — "MDD, resolved" / "MDD, vs …" post-modify the same label.
    after_clause = re.split(r"[.;:()]", norm[end:])[0].lstrip(" ,")
    if _FOLLOWING_HEDGE_RE.search(after_clause):
        return True
    return False


def _stated_current(entry: DiagnosisEntry, cited: str) -> bool:
    """True iff AT LEAST ONE occurrence of any of ``entry``'s forms appears in
    ``cited`` as a CURRENT assertion — i.e. NOT hedge-wrapped AND NOT embedded in a
    screening-instrument token. If EVERY occurrence is hedged or instrument-embedded,
    the dx is not "stated" and must not clear an inferred-dx flag.

    ORDER IS LOAD-BEARING: mask instruments on the RAW ``cited`` FIRST, then fold —
    ``normalize_text`` turns "pc-ptsd-5" into "pc ptsd 5" and destroys the signature."""
    norm = normalize_text(_mask_screening_instruments(cited))
    for start, end in form_spans_in(entry, norm):
        if not _occurrence_is_hedged(norm, start, end):
            return True
    return False


def check_inferred_diagnoses(
    structured: StructuredNote, transcript: Transcript,
) -> list[GroundingFlag]:
    """Return one :class:`GroundingFlag` (``reason='inferred_diagnosis'``) per
    claim (across ALL four SOAP sections) that NAMES a lexicon diagnosis absent
    from its CITED source segments — the deterministic inferred-dx post-check.

    FLAG, do not remove — the flags are extended onto the existing
    ``GroundingResult.flags`` so they ride the SAME render (``flags_for`` →
    inline ⚠) + ``grounding_flags`` frontmatter path grounding uses. Deterministic
    string ops; no LLM, no mutation of the claim objects."""
    seg_by_id = {s.id: s for s in transcript.segments}
    flags: list[GroundingFlag] = []
    for section, idx, claim in structured.all_claims():
        named = diagnoses_named_in(claim.claim)
        if not named:
            continue  # low-FP posture: only NAMED lexicon labels, never bare symptoms
        cited = _cited_text(claim.source_spans, seg_by_id)  # grounding's JOIN of all cited spans
        # HEDGE-AWARE clear (FIX 1): a named dx clears ONLY if it appears in the
        # cited segment(s) as a CURRENT assertion — NOT if its every occurrence is
        # wrapped in a family-history / ruled-out / differential / screening /
        # resolved frame in the SAME span (that is a fabrication, not a stated dx).
        inferred = [e for e in named if not _stated_current(e, cited)]
        if not inferred:
            continue  # every named dx is stated (current) in the cited segment(s) → clean
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


def _inferred_entries_from_detail(detail: str) -> list[DiagnosisEntry]:
    """The lexicon entries a flag ACTUALLY inferred, recovered from its ``detail``.

    ``check_inferred_diagnoses`` builds ``detail`` as
    ``"inferred_diagnosis: <canonical>, <canonical> named in the claim but ..."`` —
    the ONLY lexicon labels in that string are the inferred ones, so matching the
    lexicon over the label portion recovers EXACTLY the flagged set (and NOT the
    stated dxs elsewhere in a multi-dx claim). Robust to a truncated ``detail``
    (older/hand-authored records): if the ``" named in the claim"`` marker is
    absent it falls back to the whole post-prefix string, still matching only the
    canonical labels present there."""
    prefix = INFERRED_DIAGNOSIS_REASON + ":"
    body = detail.split(prefix, 1)[1] if prefix in detail else detail
    label_portion = body.split(" named in the claim", 1)[0]
    return diagnoses_named_in(label_portion)


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
            # Capture ONLY the labels this flag ACTUALLY inferred (FIX 5, audit
            # batch 2 #9) — NOT every dx named in the whole claim. A multi-dx claim
            # ("MDD and stable hypertension", only MDD inferred) names STATED dxs
            # too, which were never flagged; re-deriving over the claim would emit
            # spurious capture rows for them and skew the self-correcting signal.
            # The flag ``detail`` lists exactly the inferred canonical labels.
            for entry in _inferred_entries_from_detail(str(flag.get("detail", ""))):
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
