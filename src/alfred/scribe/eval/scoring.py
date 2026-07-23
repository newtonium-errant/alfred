"""Deterministic per-axis scoring for the regulator-benchmarked eval (task #16).

Scores a rendered STAY-C note (a :class:`~alfred.scribe.pipeline.VerifiedNote`)
against a case's :class:`~alfred.scribe.eval.corpus.GroundTruth`, producing a
:class:`CaseScore` whose per-axis verdicts aggregate into the AG-comparable
scorecard rates.

The scored axes map 1:1 to the Ontario AG taxonomy:
  * fabrication  ← AG "Hallucinations" (45% market rate)
  * wrong_drug   ← AG "Incorrect information" (60%)
  * missed_mh    ← AG "Missing/incomplete information" (85%)

Plus the axes the AG did NOT test but STAY-C uniquely records:
  * grounding-flag counts (STAY-C's native ungrounded-claim detector)
  * speaker-attribution flag counts (P4-2 mis-attribution safety net)
  * verbosity (word / claim count — the Suki succinctness gap, #14's target)

All scoring is DETERMINISTIC substring/structural matching on the rendered body +
the parsed structured note. A human rubric pass overlays this for the on-box
real-model run (the deterministic score is the auto-computed spine; the rubric is
the clinician-evaluator analogue of the AG's OntarioMD reviewers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from alfred.scribe.eval.corpus import (
    AXIS_FABRICATION,
    AXIS_MISSED_MH,
    AXIS_WRONG_DRUG,
    EvalCase,
    GroundTruth,
)
from alfred.scribe.grounding import GroundingResult
from alfred.scribe.inferred_dx import INFERRED_DIAGNOSIS_REASON
from alfred.scribe.notegen import SOAP_SECTIONS, StructuredNote
from alfred.scribe.pipeline import VerifiedNote
from alfred.scribe.speaker_attribution import (
    ATTRIBUTION_UNVERIFIED_REASON,
    COLLATERAL_ATTRIBUTION_REASON,
    SPEAKER_MISMATCH_REASON,
    SPEAKER_UNVERIFIED_REASON,
)

# The reasons the speaker-attribution pass emits (everything else on a flag is a
# grounding or #48 inferred-dx reason). Used to split the flag histogram.
_SPEAKER_REASONS = frozenset({
    SPEAKER_MISMATCH_REASON,
    SPEAKER_UNVERIFIED_REASON,
    COLLATERAL_ATTRIBUTION_REASON,
    ATTRIBUTION_UNVERIFIED_REASON,
})


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + glue ``<n> <unit>`` → ``<n><unit>`` so a
    note's ``5 mg`` matches a ground-truth ``5mg`` (mirrors grounding._normalize's
    dose intent without importing its private helper)."""
    t = " ".join(text.lower().split())
    return re.sub(r"(\d+(?:\.\d+)?)\s+(mg|mcg|g|ml|units?)\b", r"\1\2", t)


def _contains(token: str, body_norm: str) -> bool:
    """Leading-word-boundary match of ``token`` against the already-normalized
    body. Anchors at a word START — so it kills interior false-matches
    (``therapy`` inside ``physiotherapy``, ``refer`` inside ``prefer``) — while
    leaving the token OPEN on the right so a STEM prefix still matches inflections
    (``prescrib``→``prescribed``, ``depress``→``depression``). Without this the
    substring checks would false-score a faithful paraphrase on the live-model run.
    ``token`` is normalized the same way as the body."""
    return re.search(r"\b" + re.escape(_normalize(token)), body_norm) is not None


@dataclass
class AxisScore:
    """One AG axis's verdict for one case. ``scored`` False ⇒ the case carries no
    ground truth for this axis (it doesn't count toward that axis's rate)."""

    axis: str
    scored: bool
    passed: bool                       # True ⇒ NO inaccuracy of this type
    detail: str                        # human explanation for the scorecard
    captured: int = 0                  # missed_mh: # key details captured
    total: int = 0                     # missed_mh: # key details required


@dataclass
class CaseScore:
    """The full per-case result: AG-axis verdicts + STAY-C-unique recorded axes."""

    case_id: str
    title: str
    primary_axis: str
    fabrication: AxisScore
    wrong_drug: AxisScore
    missed_mh: AxisScore
    # STAY-C-unique recorded axes (the AG didn't test these) --------------------
    grounding_flag_count: int
    speaker_flag_count: int
    flag_reasons: dict[str, int] = field(default_factory=dict)
    # verbosity (the Suki succinctness gap — #14's tuning target) ----------------
    word_count: int = 0
    claim_count: int = 0

    @property
    def words_per_claim(self) -> float:
        """#14d — the DENSITY metric (words / atomic claim). 0.0 for a degenerate zero-claim note
        (empty / parse-failed) — such cases are EXCLUDED from the corpus distribution aggregates so
        their 0.0 can't understate verbosity (they surface as an explicit excluded-count instead)."""
        return self.word_count / self.claim_count if self.claim_count else 0.0

    @property
    def scored_axes(self) -> list[AxisScore]:
        return [a for a in (self.fabrication, self.wrong_drug, self.missed_mh) if a.scored]

    @property
    def any_inaccuracy(self) -> bool:
        """The AG "≥1 inaccuracy type" verdict: any SCORED axis failed."""
        return any(not a.passed for a in self.scored_axes)


# --- per-axis scorers -------------------------------------------------------

def score_fabrication(
    body_norm: str, structured: StructuredNote, gt: GroundTruth,
) -> AxisScore:
    """AG "Hallucinations": the note invented content not in the transcript.

    Two failure modes: (1) a forbidden bait substring appears (an un-discussed
    field/finding/plan the model filled in); (2) the clinician stated NO
    assessment but the note emitted a real Assessment claim (``forbid_invented_
    assessment``)."""
    scored = bool(gt.forbidden_content) or gt.forbid_invented_assessment
    if not scored:
        return AxisScore(AXIS_FABRICATION, scored=False, passed=True, detail="not scored")

    hits = [s for s in gt.forbidden_content if _contains(s, body_norm)]
    invented_assessment = gt.forbid_invented_assessment and len(structured.assessment) > 0

    fabricated = bool(hits) or invented_assessment
    if not fabricated:
        return AxisScore(AXIS_FABRICATION, scored=True, passed=True,
                         detail="no fabricated content")
    parts = []
    if hits:
        parts.append("invented: " + ", ".join(sorted(hits)))
    if invented_assessment:
        parts.append("emitted an assessment the clinician did not state")
    return AxisScore(AXIS_FABRICATION, scored=True, passed=False, detail="; ".join(parts))


def score_wrong_drug(body_norm: str, gt: GroundTruth) -> AxisScore:
    """AG "Incorrect information": the note captured a DIFFERENT drug (or dose)
    than prescribed. Passes iff every correct drug (name + dose) is present AND no
    confusable/sound-alike drug appears."""
    scored = bool(gt.correct_drugs)
    if not scored:
        return AxisScore(AXIS_WRONG_DRUG, scored=False, passed=True, detail="not scored")

    missing_names = [d.name for d in gt.correct_drugs if not _contains(d.name, body_norm)]
    missing_doses = [
        f"{d.name} {d.dose}" for d in gt.correct_drugs
        if d.dose and not _contains(d.dose, body_norm)
    ]
    wrong = [c for c in gt.confusable_drugs if _contains(c, body_norm)]

    if not missing_names and not missing_doses and not wrong:
        return AxisScore(AXIS_WRONG_DRUG, scored=True, passed=True,
                         detail="prescribed drug(s) captured exactly")
    parts = []
    if missing_names:
        parts.append("missing drug: " + ", ".join(missing_names))
    if missing_doses:
        parts.append("wrong/missing dose: " + ", ".join(missing_doses))
    if wrong:
        parts.append("captured a different drug: " + ", ".join(wrong))
    return AxisScore(AXIS_WRONG_DRUG, scored=True, passed=False, detail="; ".join(parts))


def score_missed_mh(body_norm: str, gt: GroundTruth) -> AxisScore:
    """AG "Missing/incomplete information": the note missed key mental-health
    details that WERE raised. Passes iff every required detail is captured (any of
    its synonyms present). A single missed detail fails the axis (the AG's "missed
    key details in ≥1 test")."""
    scored = bool(gt.required_details)
    if not scored:
        return AxisScore(AXIS_MISSED_MH, scored=False, passed=True, detail="not scored", total=0)

    captured_labels, missed_labels = [], []
    for detail in gt.required_details:
        if any(_contains(syn, body_norm) for syn in detail.any_of):
            captured_labels.append(detail.label)
        else:
            missed_labels.append(detail.label)

    total = len(gt.required_details)
    captured = len(captured_labels)
    if not missed_labels:
        return AxisScore(AXIS_MISSED_MH, scored=True, passed=True,
                         detail=f"captured all {total} key detail(s)",
                         captured=captured, total=total)
    return AxisScore(AXIS_MISSED_MH, scored=True, passed=False,
                     detail="missed: " + ", ".join(missed_labels),
                     captured=captured, total=total)


def _flag_histogram(grounding_flags: list[dict]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for f in grounding_flags:
        reason = str(f.get("reason", "unknown"))
        hist[reason] = hist.get(reason, 0) + 1
    return hist


def score_case(case: EvalCase, note: VerifiedNote) -> CaseScore:
    """Score one rendered note against its case's ground truth across every axis."""
    gt = case.ground_truth
    # Score the CLINICAL BODY only — drop the H1 title line (``# ...``). The title
    # is not clinical content and can carry axis keywords (a case_id like
    # ``fab_noplan_therapy`` or a descriptive title) that would false-trip the
    # substring checks against the note's own heading. H2 section headings
    # (``## ...``) are kept — they carry no bait.
    clinical_body = "\n".join(
        ln for ln in note.body.splitlines() if not ln.startswith("# ")
    )
    body_norm = _normalize(clinical_body)
    structured = note.structured if note.structured is not None else StructuredNote()

    hist = _flag_histogram(note.grounding_flags)
    speaker_count = sum(n for r, n in hist.items() if r in _SPEAKER_REASONS)
    inferred_count = hist.get(INFERRED_DIAGNOSIS_REASON, 0)
    grounding_count = sum(
        n for r, n in hist.items() if r not in _SPEAKER_REASONS and r != INFERRED_DIAGNOSIS_REASON
    )
    # Inferred-diagnosis flags are a fabrication-adjacent signal → fold into the
    # grounding-detector count (both are STAY-C catching an un-grounded claim).
    grounding_count += inferred_count

    claim_count = sum(len(structured.section(s)) for s in SOAP_SECTIONS)

    return CaseScore(
        case_id=case.case_id,
        title=case.title,
        primary_axis=case.axis,
        fabrication=score_fabrication(body_norm, structured, gt),
        wrong_drug=score_wrong_drug(body_norm, gt),
        missed_mh=score_missed_mh(body_norm, gt),
        grounding_flag_count=grounding_count,
        speaker_flag_count=speaker_count,
        flag_reasons=hist,
        word_count=len(note.body.split()),
        claim_count=claim_count,
    )
