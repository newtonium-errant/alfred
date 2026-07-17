"""Tests for the regulator-benchmarked eval suite (task #16).

Covers: corpus integrity, each per-axis scorer FIRING on adversarial input (not
just passing clean notes), the committed reference-fixture regression pin, the AG
primary-source baseline numbers, scorecard render, and the drift pin proving the
fixture seam runs the EXACT production composition (``render_verified_note`` ≡ the
post-generation half of ``generate_verified_note``).

All synthetic. LLM-free — the whole suite runs with NO Ollama / torch / network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from alfred.scribe.eval import (
    AG_BASELINES,
    AXIS_FABRICATION,
    AXIS_MISSED_MH,
    AXIS_WRONG_DRUG,
    FixtureNoteGenSeam,
    aggregate,
    all_cases,
    build_transcript,
    cases_for_axis,
    render_scorecard_md,
    run_suite,
    score_case,
)
from alfred.scribe.eval.harness import FixtureMissing, _default_config, fixture_path
from alfred.scribe.eval.scorecard import AG_ANY_INACCURACY
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.pipeline import render_verified_note


# --- helpers ----------------------------------------------------------------

def _note(case, structured_dict, *, config=None):
    """Render a StructuredNote (dict) through the production composition for a
    case, returning the VerifiedNote — the seam-independent way to build an
    adversarial or clean note for scoring."""
    cfg = config or _default_config()
    t = build_transcript(case)
    s = StructuredNote.from_dict(structured_dict)
    return render_verified_note(s, t, config=cfg, title="Clinical Note (test)")


def _case(case_id):
    return next(c for c in all_cases() if c.case_id == case_id)


# --- corpus integrity -------------------------------------------------------

def test_every_case_has_a_fixture_with_valid_spans():
    for case in all_cases():
        path = fixture_path(case.case_id)
        assert path.is_file(), f"missing fixture for {case.case_id}"
        data = json.loads(path.read_text(encoding="utf-8"))
        note = StructuredNote.from_dict(data)
        valid_ids = {f"S{i+1}" for i in range(len(case.turns))}
        for _sec, _idx, claim in note.all_claims():
            for span in claim.source_spans:
                assert span in valid_ids, (
                    f"{case.case_id}: claim {claim.claim!r} cites {span} "
                    f"(valid: {sorted(valid_ids)})"
                )


def test_corpus_covers_each_ag_axis():
    for axis in (AXIS_FABRICATION, AXIS_WRONG_DRUG, AXIS_MISSED_MH):
        assert cases_for_axis(axis), f"no cases for axis {axis}"


def test_build_transcript_sets_roles_and_diarized():
    ambient = _case("mh_passive_si")
    t = build_transcript(ambient)
    assert t.diarized is True
    assert all(s.speaker in ("clinician", "patient") for s in t.segments)
    dictation = _case("t2_dtc_lbp_dictation")
    td = build_transcript(dictation)
    assert td.diarized is False
    assert all(s.speaker is None for s in td.segments)


# --- the committed reference fixtures score CLEAN (regression pin) -----------

def test_reference_fixtures_score_clean():
    sc = asyncio.run(run_suite(FixtureNoteGenSeam()))
    # STAY-C reference notes: zero failures on every AG axis.
    assert sc.axis_rollups[AXIS_FABRICATION].failures == 0
    assert sc.axis_rollups[AXIS_WRONG_DRUG].failures == 0
    assert sc.axis_rollups[AXIS_MISSED_MH].failures == 0
    assert sc.any_inaccuracy_failures == 0
    # denominators are the axis-tagged case counts (pins corpus size per axis).
    assert sc.axis_rollups[AXIS_FABRICATION].scored == 7
    assert sc.axis_rollups[AXIS_WRONG_DRUG].scored == 4
    assert sc.axis_rollups[AXIS_MISSED_MH].scored == 3
    assert sc.any_inaccuracy_scored == 13
    # the grounding-flag differentiator is exercised (not trivially zero).
    assert sc.total_grounding_flags >= 1


# --- each scorer FIRES on adversarial input (the load-bearing coverage) ------

def test_fabrication_scorer_fires_on_invented_plan():
    # AG "Hallucinations": a note that invents a referral/bloodwork on a visit
    # where none was discussed (fab_noplan_therapy's bait).
    case = _case("fab_noplan_therapy")
    bad = _note(case, {
        "subjective": [{"claim": "Headache for two weeks", "source_spans": ["S2"]}],
        "plan": [{"claim": "Refer for therapy and order blood tests", "source_spans": ["S5"]}],
    })
    score = score_case(case, bad)
    assert score.fabrication.scored and not score.fabrication.passed
    assert "therapy" in score.fabrication.detail


def test_fabrication_scorer_fires_on_invented_assessment():
    # t3-style: the clinician stated NO impression; a note emitting an Assessment
    # claim is a fabrication (forbid_invented_assessment).
    case = _case("t3_fatigue_nodx_dictation")
    bad = _note(case, {
        "subjective": [{"claim": "Fatigue for one month", "source_spans": ["S1"]}],
        "assessment": [{"claim": "Iron deficiency", "source_spans": ["S1"]}],
    })
    score = score_case(case, bad)
    assert score.fabrication.scored and not score.fabrication.passed


def test_wrong_drug_scorer_fires_on_confusable():
    # AG "Incorrect information": the note captured a DIFFERENT drug (amiloride
    # instead of amlodipine).
    case = _case("drug_amlodipine")
    bad = _note(case, {
        "plan": [{"claim": "Start amiloride 5 mg once daily", "source_spans": ["S3"]}],
    })
    score = score_case(case, bad)
    assert score.wrong_drug.scored and not score.wrong_drug.passed
    assert "amiloride" in score.wrong_drug.detail


def test_wrong_drug_scorer_fires_on_missing_dose():
    case = _case("drug_amlodipine")
    bad = _note(case, {
        "plan": [{"claim": "Start amlodipine once daily", "source_spans": ["S3"]}],  # no dose
    })
    score = score_case(case, bad)
    assert score.wrong_drug.scored and not score.wrong_drug.passed
    assert "dose" in score.wrong_drug.detail


def test_missed_mh_scorer_fires_on_dropped_detail():
    # AG "Missing/incomplete": a note for the passive-SI case that captures the
    # low mood but DROPS the suicidal ideation.
    case = _case("mh_passive_si")
    bad = _note(case, {
        "subjective": [{"claim": "Reports feeling really low", "source_spans": ["S2"]}],
    })
    score = score_case(case, bad)
    assert score.missed_mh.scored and not score.missed_mh.passed
    assert "passive suicidal ideation" in score.missed_mh.detail
    assert score.missed_mh.captured == 1 and score.missed_mh.total == 2


def test_missed_mh_scorer_passes_when_all_captured():
    case = _case("mh_anxiety_panic")
    good = _note(case, {
        "subjective": [
            {"claim": "Reports anxiety and panic attacks", "source_spans": ["S4"]},
        ],
    })
    score = score_case(case, good)
    assert score.missed_mh.passed and score.missed_mh.captured == 2


def test_grounding_flag_recorded_on_ungrounded_claim():
    # A claim citing a non-existent segment → grounding flags it → the score
    # records it in grounding_flag_count (the STAY-C-unique detector axis).
    case = _case("base_complete_visit")
    note = _note(case, {
        "assessment": [{"claim": "Sepsis", "source_spans": ["S99"]}],  # S99 not real
    })
    score = score_case(case, note)
    assert score.grounding_flag_count >= 1


# --- AG baselines are the PRIMARY-SOURCE numbers ----------------------------

def test_ag_baselines_match_primary_source():
    assert (AG_BASELINES[AXIS_FABRICATION].numerator,
            AG_BASELINES[AXIS_FABRICATION].denominator) == (9, 20)
    assert (AG_BASELINES[AXIS_WRONG_DRUG].numerator,
            AG_BASELINES[AXIS_WRONG_DRUG].denominator) == (12, 20)
    assert (AG_BASELINES[AXIS_MISSED_MH].numerator,
            AG_BASELINES[AXIS_MISSED_MH].denominator) == (17, 20)
    assert (AG_ANY_INACCURACY.numerator, AG_ANY_INACCURACY.denominator) == (20, 20)


# --- scorecard render -------------------------------------------------------

def test_scorecard_render_has_axes_and_ilb_empty_signal():
    sc = asyncio.run(run_suite(FixtureNoteGenSeam()))
    md = render_scorecard_md(sc)
    assert "STAY-C vs the Market" in md
    assert "45% (9/20)" in md and "60% (12/20)" in md and "85% (17/20)" in md
    assert "Methodology divergences" in md
    # intentionally-left-blank: the clean run explicitly says "no inaccuracies".
    assert "No inaccuracies detected" in md


def test_scorecard_reports_failures_when_present():
    # feed one adversarial case into the aggregate → the render lists it (idle vs
    # broken distinguishable).
    case = _case("drug_amlodipine")
    bad = _note(case, {"plan": [{"claim": "Start amiloride 5 mg", "source_spans": ["S3"]}]})
    sc = aggregate([score_case(case, bad)], mode="fixture", model="test")
    md = render_scorecard_md(sc)
    assert "Inaccuracies found" in md
    assert "drug_amlodipine" in md
    assert "No inaccuracies detected" not in md


# --- fixture seam fails loud on a missing fixture ---------------------------

def test_fixture_missing_fails_loud(tmp_path):
    seam = FixtureNoteGenSeam(fixtures_dir=tmp_path)  # empty dir
    case = all_cases()[0]
    t = build_transcript(case)
    with pytest.raises(FixtureMissing):
        asyncio.run(seam.note_for(case, t))


# --- DRIFT PIN: render_verified_note ≡ generate_verified_note's remainder ----

def test_render_verified_note_matches_generate_verified_note(monkeypatch):
    """The eval's fixture seam runs ``render_verified_note`` on a fixture
    StructuredNote; production runs ``generate_verified_note`` (which calls
    ``generate_structured`` then the SAME ``render_verified_note``). Pin that the
    two produce byte-identical bodies + flags for the same structured note, so the
    scorecard never drifts from what ships."""
    from alfred.scribe.pipeline import generate_verified_note

    canned = {
        "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
        "objective": [], "assessment": [], "plan": [],
        "assessment_reasoning_stated": False,
    }

    async def _fake_ollama(prompt, system=None, model="", endpoint="", **kw):
        return json.dumps(canned), {"stop_reason": "stop", "prompt_eval_count": 300}

    import alfred.distiller.backends.ollama as ollama_mod
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama)

    case = _case("base_complete_visit")
    cfg = _default_config()
    t = build_transcript(case)

    produced = asyncio.run(generate_verified_note(t, config=cfg, title="X"))
    direct = render_verified_note(StructuredNote.from_dict(canned), t, config=cfg, title="X")

    assert produced.body == direct.body
    assert produced.grounding_flags == direct.grounding_flags
    assert produced.flag_count == direct.flag_count
