"""Aggregate per-case scores into the STAY-C-vs-market scorecard (task #16).

Turns a list of :class:`~alfred.scribe.eval.scoring.CaseScore` into:
  * per-axis STAY-C rates directly comparable to the Ontario AG's published
    commercial failure rates, and
  * a committed, human-readable markdown scorecard (the repeatable artifact).

The AG baseline numbers are the primary-source figures (Special Report 2026,
page 24 / Figure 7 — see :mod:`alfred.scribe.eval.corpus`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from alfred.scribe.eval.corpus import (
    AXIS_FABRICATION,
    AXIS_MISSED_MH,
    AXIS_WRONG_DRUG,
    CORPUS_VERSION,
)
from alfred.scribe.eval.scoring import CaseScore


@dataclass(frozen=True)
class AGBaseline:
    """One AG-published commercial failure rate (the market bar STAY-C is scored
    against). ``rate`` is the FAILURE fraction — higher is worse."""

    axis: str
    label: str
    rate: float          # fraction of the 20 AG-approved vendors that failed this axis
    numerator: int
    denominator: int
    source: str


# The Ontario AG Special Report 2026 figures (primary source — Figure 7, p.24).
AG_BASELINES: dict[str, AGBaseline] = {
    AXIS_FABRICATION: AGBaseline(
        AXIS_FABRICATION, "Fabrication (AG: Hallucinations)",
        9 / 20, 9, 20, "Ontario AG Special Report 2026, Figure 7 (p.24)"),
    AXIS_WRONG_DRUG: AGBaseline(
        AXIS_WRONG_DRUG, "Wrong drug captured (AG: Incorrect information)",
        12 / 20, 12, 20, "Ontario AG Special Report 2026, Figure 7 (p.24)"),
    AXIS_MISSED_MH: AGBaseline(
        AXIS_MISSED_MH, "Missed key mental-health detail (AG: Missing/incomplete)",
        17 / 20, 17, 20, "Ontario AG Special Report 2026, Figure 7 (p.24)"),
}

# The AG's headline "≥1 inaccuracy type" figure — all 20 vendors.
AG_ANY_INACCURACY = AGBaseline(
    "any", "Any inaccuracy type (≥1)", 20 / 20, 20, 20,
    "Ontario AG Special Report 2026, §4.3.2 (p.23-24)")

# The Suki-affiliated peer-reviewed hallucination baseline (context, not an AG axis).
SUKI_AMBIENT_HALLUCINATION_RATE = 0.31   # 31% of ambient notes
SUKI_PHYSICIAN_HALLUCINATION_RATE = 0.20  # 20% of physician-written notes
SUKI_SOURCE = "Suki-affiliated PDQI-9 study, PMC12586549"


@dataclass
class AxisRollup:
    """STAY-C's rate on one axis + the AG comparator."""

    axis: str
    label: str
    failures: int
    scored: int                # cases that scored this axis
    ag: AGBaseline

    @property
    def rate(self) -> float:
        return self.failures / self.scored if self.scored else 0.0

    @property
    def delta_vs_ag(self) -> float:
        """STAY-C rate minus AG rate (negative = STAY-C better)."""
        return self.rate - self.ag.rate


@dataclass
class Scorecard:
    """The aggregated result — the repeatable STAY-C-vs-market artifact."""

    mode: str                              # "fixture" | "real"
    model: str                             # the note-gen model (or "fixture")
    generated_at: str
    corpus_version: str
    case_scores: list[CaseScore]
    axis_rollups: dict[str, AxisRollup] = field(default_factory=dict)
    any_inaccuracy_failures: int = 0
    any_inaccuracy_scored: int = 0
    # STAY-C-unique aggregate observability -------------------------------------
    total_grounding_flags: int = 0
    total_speaker_flags: int = 0
    # grounding-detector reason histogram (grounding + #48 inferred-dx; the speaker
    # reasons are counted in total_speaker_flags, not here) — lets the render split
    # negation_mismatch FALSE POSITIVES (task #24) from genuine ungrounded catches.
    grounding_flag_reasons: dict[str, int] = field(default_factory=dict)
    mean_word_count: float = 0.0
    mean_claim_count: float = 0.0

    @property
    def any_inaccuracy_rate(self) -> float:
        return (self.any_inaccuracy_failures / self.any_inaccuracy_scored
                if self.any_inaccuracy_scored else 0.0)


def aggregate(
    case_scores: list[CaseScore], *, mode: str, model: str,
) -> Scorecard:
    """Roll per-case scores up into a :class:`Scorecard`."""
    rollups: dict[str, AxisRollup] = {}
    for axis, ag in AG_BASELINES.items():
        scored = [cs for cs in case_scores if _axis_of(cs, axis).scored]
        failures = sum(1 for cs in scored if not _axis_of(cs, axis).passed)
        rollups[axis] = AxisRollup(
            axis=axis, label=ag.label, failures=failures, scored=len(scored), ag=ag,
        )

    any_scored = [cs for cs in case_scores if cs.scored_axes]
    any_failures = sum(1 for cs in any_scored if cs.any_inaccuracy)

    # Grounding-detector reason totals (exclude speaker reasons — counted separately).
    from alfred.scribe.eval.scoring import _SPEAKER_REASONS
    reason_totals: dict[str, int] = {}
    for cs in case_scores:
        for reason, count in cs.flag_reasons.items():
            if reason in _SPEAKER_REASONS:
                continue
            reason_totals[reason] = reason_totals.get(reason, 0) + count

    n = len(case_scores) or 1
    return Scorecard(
        mode=mode,
        model=model,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        corpus_version=CORPUS_VERSION,
        case_scores=case_scores,
        axis_rollups=rollups,
        any_inaccuracy_failures=any_failures,
        any_inaccuracy_scored=len(any_scored),
        total_grounding_flags=sum(cs.grounding_flag_count for cs in case_scores),
        total_speaker_flags=sum(cs.speaker_flag_count for cs in case_scores),
        grounding_flag_reasons=reason_totals,
        mean_word_count=sum(cs.word_count for cs in case_scores) / n,
        mean_claim_count=sum(cs.claim_count for cs in case_scores) / n,
    )


def _axis_of(cs: CaseScore, axis: str):
    return {
        AXIS_FABRICATION: cs.fabrication,
        AXIS_WRONG_DRUG: cs.wrong_drug,
        AXIS_MISSED_MH: cs.missed_mh,
    }[axis]


# --- markdown render --------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _grounding_flag_caveat(sc: "Scorecard") -> str:
    """Honest read of what the grounding flags on THIS run actually are.

    The flag COUNT demonstrates the review surface exists; it must not be sold as
    N real catches. ``negation_mismatch`` is a KNOWN false-positive class (the
    grounding negation check over-flags faithful paraphrases — pre-existing
    grounding.py limitation, tracked as task #24), so the caveat splits it from
    genuine ungrounded-claim catches, accurately for BOTH fixture and real runs."""
    tot = sc.total_grounding_flags
    if tot == 0:
        return ("_None fired on this run — the reference notes are clean (the "
                "detector fires on ungrounded claims; the adversarial tests exercise "
                "it)._")
    neg = sc.grounding_flag_reasons.get("negation_mismatch", 0)
    real = tot - neg
    reason_bits = ", ".join(f"{r}×{c}" for r, c in sorted(sc.grounding_flag_reasons.items()))
    breakdown = f"By reason: {reason_bits}. "
    if neg == tot:
        return (breakdown + f"**Caveat: all {tot} are `negation_mismatch` FALSE "
                "POSITIVES** — the negation check over-flags faithful paraphrases "
                "(e.g. \"denies a plan\" vs \"I wouldn't do anything\"), a known "
                "pre-existing grounding.py limitation tracked as task #24. The count "
                "is shown to demonstrate the review surface EXISTS, not to claim "
                "these are real catches.")
    if neg > 0:
        return (breakdown + f"**Caveat: {neg} of {tot} are `negation_mismatch` FALSE "
                "POSITIVES** (a known over-flagging class, task #24); the remaining "
                f"{real} are genuine ungrounded-claim catches.")
    return (breakdown + f"All {tot} are genuine ungrounded-claim catches (no "
            "`negation_mismatch` false positives on this run).")


def _verdict(rollup: AxisRollup) -> str:
    if rollup.scored == 0:
        return "—"
    if rollup.rate < rollup.ag.rate:
        return f"✅ better ({_pct(-rollup.delta_vs_ag)} lower)"
    if rollup.rate == rollup.ag.rate:
        return "➖ same"
    return f"⚠️ worse ({_pct(rollup.delta_vs_ag)} higher)"


def render_scorecard_md(sc: Scorecard) -> str:
    """Render the human-readable scorecard markdown (the committed artifact)."""
    lines: list[str] = []
    lines.append("# STAY-C vs the Market — Regulator-Benchmarked Scorecard")
    lines.append("")
    lines.append(
        "Scores STAY-C on the same accuracy axes the Ontario Auditor General used "
        "to test the 20 procurement-approved commercial AI scribes (Special Report "
        "2026, §4.3.2 / Figure 7). Generated by `alfred scribe eval` — regenerate "
        "per release so quality claims stay current.")
    lines.append("")
    lines.append(f"- **Run mode:** `{sc.mode}`  ·  **Note-gen:** `{sc.model}`")
    lines.append(f"- **Generated:** {sc.generated_at}  ·  **Corpus version:** {sc.corpus_version}")
    lines.append(f"- **Cases scored:** {len(sc.case_scores)}")
    lines.append("")

    # honesty banner — what this run does and does NOT certify
    lines.append("> **What this is / isn't.** This is a *directional, repeatable* "
                 "STAY-C-vs-market scorecard, NOT a certified head-to-head. It "
                 "replicates the AG's *taxonomy* on *analogous synthetic* encounters "
                 "(the AG's own two test transcripts were never published). "
                 "Fabrication / drug / mental-health axes are scored on synthetic "
                 "text transcripts today; the wrong-drug axis measures NOTE-GEN "
                 "faithfulness (STT mis-hearing needs the on-box audio leg), and "
                 "speaker attribution uses AUTHORED roles (real diarized attribution "
                 "waits on task #17). See the methodology-divergence notes below.")
    lines.append("")

    # --- headline comparison table ---
    lines.append("## AG axes — STAY-C vs the 20 approved commercial vendors")
    lines.append("")
    lines.append("| Axis | AG market failure rate | STAY-C failure rate | Verdict |")
    lines.append("|---|---|---|---|")
    for axis in (AXIS_FABRICATION, AXIS_WRONG_DRUG, AXIS_MISSED_MH):
        r = sc.axis_rollups[axis]
        ag_cell = f"{_pct(r.ag.rate)} ({r.ag.numerator}/{r.ag.denominator})"
        stayc_cell = (f"{_pct(r.rate)} ({r.failures}/{r.scored})"
                      if r.scored else "no cases")
        lines.append(f"| {r.label} | {ag_cell} | {stayc_cell} | {_verdict(r)} |")
    # any-inaccuracy row
    any_cell = (f"{_pct(sc.any_inaccuracy_rate)} "
                f"({sc.any_inaccuracy_failures}/{sc.any_inaccuracy_scored})"
                if sc.any_inaccuracy_scored else "no cases")
    lines.append(f"| **{AG_ANY_INACCURACY.label}** | "
                 f"**{_pct(AG_ANY_INACCURACY.rate)} "
                 f"({AG_ANY_INACCURACY.numerator}/{AG_ANY_INACCURACY.denominator})** | "
                 f"**{any_cell}** | |")
    lines.append("")
    lines.append(f"*AG source: {AG_BASELINES[AXIS_FABRICATION].source}.*")
    lines.append("")

    # --- Suki context ---
    lines.append("## Suki peer-reviewed hallucination baseline (context)")
    lines.append("")
    lines.append(
        f"The Suki-affiliated PDQI-9 study found hallucinations in "
        f"**{_pct(SUKI_AMBIENT_HALLUCINATION_RATE)}** of ambient-scribe notes vs "
        f"**{_pct(SUKI_PHYSICIAN_HALLUCINATION_RATE)}** of physician-written notes — "
        "ambient scribes hallucinated *more* than physicians, and were noted as "
        "thorough but **verbose**. STAY-C's fabrication rate above is the "
        "comparator; its verbosity metrics below are the #14 succinctness target.")
    lines.append(f"*Source: {SUKI_SOURCE}.*")
    lines.append("")

    # --- STAY-C-unique axes ---
    lines.append("## Axes the AG did not test — STAY-C's differentiators")
    lines.append("")
    lines.append(
        f"- **Grounding flags** (ungrounded-claim detector — no commercial vendor "
        f"ships this): **{sc.total_grounding_flags}** across the corpus. Every "
        "flagged claim carries an inline ⚠ + a `grounding_flags` frontmatter entry "
        "for clinician review — the IT-enforced review surface the AG's Rec 6 says "
        "the market lacks. " + _grounding_flag_caveat(sc))
    lines.append(
        f"- **Speaker-attribution flags** (P4-2 mis-attribution net): "
        f"**{sc.total_speaker_flags}** across the corpus (authored roles today; "
        "real diarized attribution is task #17).")
    lines.append(
        f"- **Verbosity (Suki succinctness gap):** mean "
        f"**{sc.mean_word_count:.0f}** words / **{sc.mean_claim_count:.1f}** atomic "
        "claims per note — the baseline #14's note-quality loop tunes against.")
    lines.append("")

    # --- per-case detail ---
    lines.append("## Per-case detail")
    lines.append("")
    lines.append("| Case | Primary axis | Fabrication | Wrong-drug | Missed-MH | "
                 "Ground.flags | Speaker.flags | Words |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cs in sc.case_scores:
        lines.append(
            f"| `{cs.case_id}` | {cs.primary_axis} | {_cell(cs.fabrication)} | "
            f"{_cell(cs.wrong_drug)} | {_cell(cs.missed_mh)} | "
            f"{cs.grounding_flag_count} | {cs.speaker_flag_count} | {cs.word_count} |")
    lines.append("")

    # --- failing-case explanations (intentionally-left-blank: say so if none) ---
    lines.append("### Inaccuracies found")
    lines.append("")
    failing = [
        (cs, a) for cs in sc.case_scores for a in cs.scored_axes if not a.passed
    ]
    if not failing:
        lines.append("_No inaccuracies detected across the corpus in this run._")
    else:
        for cs, a in failing:
            lines.append(f"- `{cs.case_id}` — **{a.axis}**: {a.detail}")
    lines.append("")

    # --- methodology divergences (honesty section) ---
    lines.append("## Methodology divergences from the AG instrument")
    lines.append("")
    lines.append(
        "1. **Not the AG's transcripts.** The AG never published its two test "
        "recordings — these are *analogous* cases built to the same taxonomy. "
        "Directional + repeatable, not a certified head-to-head.")
    lines.append(
        "2. **More cases per axis.** The AG ran 2 encounters/vendor; a 2-case "
        "sample only yields 0/50/100% rates. This corpus runs multiple cases per "
        "axis for finer-grained rates — so STAY-C's denominator differs from the "
        "AG's 20-vendor denominator (each rate names its own N).")
    lines.append(
        "3. **Text transcripts bypass STT.** Cases feed text straight to note-gen, "
        "so the wrong-drug axis scores whether NOTE-GEN preserves a correctly-"
        "transcribed drug — NOT whether STT mishears one. The real end-to-end "
        "wrong-drug rate needs the audio→STT leg (an on-box run).")
    lines.append(
        "4. **Authored speaker roles.** Diarized transcripts carry ground-truth "
        "roles, so speaker attribution scores note-gen's SECTION routing under "
        "known roles — not real diarization from audio (task #17 / P4-5c).")
    lines.append(
        f"5. **{'Fixture' if sc.mode == 'fixture' else 'Real-model'} run.** "
        + ("This run scored committed reference/captured note fixtures (CI-safe, "
           "LLM-free) — it pins the scoring logic and demonstrates the scorecard "
           "shape. The live-model numbers come from a `--mode real` run on the box "
           "(Ollama + qwen2.5-14b)."
           if sc.mode == "fixture" else
           "This run scored LIVE note-gen output from the on-box sovereign model."))
    lines.append("")
    return "\n".join(lines) + "\n"


def _cell(a) -> str:
    if not a.scored:
        return "—"
    if a.axis == AXIS_MISSED_MH and a.total:
        base = f"{a.captured}/{a.total}"
        return f"✅ {base}" if a.passed else f"⚠️ {base}"
    return "✅ pass" if a.passed else "⚠️ FAIL"
