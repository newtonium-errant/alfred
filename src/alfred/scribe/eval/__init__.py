"""Regulator-benchmarked eval suite (task #16) — score STAY-C on the Ontario AG
AI-Scribe accuracy axes vs the published commercial failure rates.

Public surface:
  * :mod:`.corpus` — the synthetic two-party encounter cases + ground truth.
  * :mod:`.scoring` — deterministic per-axis scoring → :class:`~.scoring.CaseScore`.
  * :mod:`.scorecard` — AG baselines + aggregation + markdown render.
  * :mod:`.harness` — the fixture/real note-gen seam + :func:`~.harness.run_suite`.

Run it: ``alfred scribe eval`` (fixture mode, CI-safe) or
``alfred scribe eval --mode real`` (on-box, live qwen).
"""

from __future__ import annotations

from alfred.scribe.eval.corpus import (
    AG_AXES,
    AXIS_FABRICATION,
    AXIS_MISSED_MH,
    AXIS_WRONG_DRUG,
    CORPUS_VERSION,
    EvalCase,
    GroundTruth,
    all_cases,
    build_transcript,
    case_by_id,
    cases_for_axis,
)
from alfred.scribe.eval.harness import (
    FixtureNoteGenSeam,
    RealNoteGenSeam,
    run_suite,
)
from alfred.scribe.eval.scorecard import (
    AG_BASELINES,
    Scorecard,
    aggregate,
    render_scorecard_md,
)
from alfred.scribe.eval.scoring import CaseScore, score_case

__all__ = [
    "AG_AXES",
    "AG_BASELINES",
    "AXIS_FABRICATION",
    "AXIS_MISSED_MH",
    "AXIS_WRONG_DRUG",
    "CORPUS_VERSION",
    "CaseScore",
    "EvalCase",
    "FixtureNoteGenSeam",
    "GroundTruth",
    "RealNoteGenSeam",
    "Scorecard",
    "aggregate",
    "all_cases",
    "build_transcript",
    "case_by_id",
    "cases_for_axis",
    "render_scorecard_md",
    "run_suite",
    "score_case",
]
