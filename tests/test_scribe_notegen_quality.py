"""#14 slice 14c — post-note quality pass: contract-first tests.

Pins each check's exact fire condition (profile-required-aware, default-profile-degrading), the
clean-note ILB, the note-level render via the SAME flags_for banner path, the grounding/quality
frontmatter SEPARATION (prefix split) + the namespace-disjointness pin, never-mutates, the additive
_REASON_INLINE_LITERAL registration (grounding detector-frozen), and the DRAFT_EDIT_FIELDS widen.
Regression pins UNCONDITIONAL.
"""

from __future__ import annotations

import tempfile

import pytest

from alfred.scribe.config import load_from_unified
from alfred.scribe.grounding import (
    MECHANICAL_GROUNDING_REASONS,
    _REASON_INLINE_LITERAL,
    verify,
)
from alfred.scribe.inferred_dx import GROUNDING_REASONS as _INFERRED_DX_REASONS
from alfred.scribe.notegen import (
    QUALITY_ASSESSMENT_NO_PLAN,
    QUALITY_REQUIRED_SECTION_EMPTY,
    StructuredNote,
)
from alfred.scribe import notegen_quality as nq
from alfred.scribe.notegen_profile import DEFAULT_PROFILE, profile_from_dict
from alfred.scribe.pipeline import render_verified_note
from alfred.scribe.speaker_attribution import GROUNDING_REASONS as _SPEAKER_REASONS
from alfred.scribe.transcript import Segment, Transcript
from alfred.vault.scope import STAYC_CLINICAL_DRAFT_EDIT_FIELDS


def _s(**sections):
    sections.setdefault("assessment_reasoning_stated", True)
    return StructuredNote.from_dict(sections)


def _claim(text, spans=("S1",)):
    return {"claim": text, "source_spans": list(spans)}


def _reasons(structured, profile=DEFAULT_PROFILE):
    return [f.reason for f in nq.check_note_quality(structured, profile)]


def _profile(**required):
    """A profile with the given per-section required flags (defaults from DEFAULT_PROFILE)."""
    d = DEFAULT_PROFILE.to_dict()
    for sec in d["sections"]:
        if sec["key"] in required:
            sec["required"] = required[sec["key"]]
    d["profile_version"] = 1
    return profile_from_dict(d)


# ===========================================================================
# Each check — exact fire condition
# ===========================================================================

def test_required_section_empty_fires_only_for_required_empty():
    # plan required + empty → fires; objective NOT required + empty → does NOT fire.
    r = _reasons(_s(subjective=[_claim("a")], objective=[], assessment=[_claim("b")], plan=[]))
    assert "quality_required_section_empty" in r
    # exactly the required-empty sections (subjective+assessment filled, plan empty; objective optional)
    empties = [f.detail for f in nq.check_note_quality(
        _s(subjective=[_claim("a")], objective=[], assessment=[_claim("b")], plan=[]), DEFAULT_PROFILE)
        if f.reason == "quality_required_section_empty"]
    assert len(empties) == 1 and "'plan'" in empties[0]              # only plan (objective is optional)


def test_required_section_empty_profile_aware():
    # marking OBJECTIVE required → an empty objective now fires (profile-driven).
    prof = _profile(objective=True)
    r = _reasons(_s(subjective=[_claim("a")], objective=[], assessment=[_claim("b")], plan=[_claim("c")]), prof)
    assert "quality_required_section_empty" in r
    # with the DEFAULT (objective optional) the same note is clean on that axis
    assert "quality_required_section_empty" not in _reasons(
        _s(subjective=[_claim("a")], objective=[], assessment=[_claim("b")], plan=[_claim("c")]))


def test_assessment_no_plan_fires():
    assert "quality_assessment_no_plan" in _reasons(
        _s(subjective=[_claim("a")], assessment=[_claim("b")], plan=[]))
    # both filled → does NOT fire
    assert "quality_assessment_no_plan" not in _reasons(
        _s(subjective=[_claim("a")], assessment=[_claim("b")], plan=[_claim("c")]))
    # assessment empty → does NOT fire (nothing to plan for)
    assert "quality_assessment_no_plan" not in _reasons(_s(subjective=[_claim("a")], assessment=[], plan=[]))


def test_verbose_fires_over_target_advisory():
    # 1 claim of 30 words → 30 words / 1 claim = 30 > target 25 → fires (averaged over ALL claims).
    verbose = _s(subjective=[_claim(" ".join(["word"] * 30))])
    assert "quality_verbose" in _reasons(verbose)
    # a tight note (few words/claim) → does NOT fire
    tight = _s(subjective=[_claim("chest pain")], assessment=[_claim("mskl")], plan=[_claim("nsaids")])
    assert "quality_verbose" not in _reasons(tight)


def test_verbose_no_div_by_zero_on_empty_note():
    # zero claims → the verbose check is skipped (no ZeroDivisionError); only required-empty flags.
    r = _reasons(_s())                                              # all sections empty
    assert "quality_verbose" not in r and r.count("quality_required_section_empty") == 3  # S/A/P required


def test_verbose_target_from_profile():
    # a profile with a tighter target (10) fires where the default (25) would not.
    note = _s(subjective=[_claim(" ".join(["w"] * 15))], assessment=[_claim("a")], plan=[_claim("b")])
    assert "quality_verbose" not in _reasons(note)                 # 17w/3c ≈ 5.7 < 25 default
    tight_profile = profile_from_dict({**DEFAULT_PROFILE.to_dict(),
                                       "succinctness_target_words_per_claim": 3, "profile_version": 1})
    assert "quality_verbose" in _reasons(note, tight_profile)      # 5.7 > 3


# ===========================================================================
# ILB + degrades-on-DEFAULT + never-mutates
# ===========================================================================

def test_clean_note_yields_no_flags():
    clean = _s(subjective=[_claim("chest pain")], objective=[_claim("bp 120")],
               assessment=[_claim("mskl")], plan=[_claim("nsaids")])
    assert nq.check_note_quality(clean, DEFAULT_PROFILE) == []      # ran, nothing to flag (ILB verdict)


def test_degrades_on_default_profile():
    # DEFAULT_PROFILE (soap/0) must drive the pass without a configured file.
    assert nq.check_note_quality(_s(assessment=[_claim("a")], plan=[]), DEFAULT_PROFILE)


def test_never_mutates_structured():
    s = _s(assessment=[_claim("a")], plan=[])
    before = (len(s.section("assessment")), len(s.section("plan")), s.section("assessment")[0].claim)
    nq.check_note_quality(s, DEFAULT_PROFILE)
    assert (len(s.section("assessment")), len(s.section("plan")), s.section("assessment")[0].claim) == before


def test_flags_are_note_level():
    for f in nq.check_note_quality(_s(assessment=[_claim("a")], plan=[]), DEFAULT_PROFILE):
        assert f.section == "note" and f.claim_index == -1 and f.claim == "" and f.source_spans == []


# ===========================================================================
# Render + separation (through render_verified_note) + additive dispatch
# ===========================================================================

def _cfg():
    return load_from_unified({"scribe": {"mode": "clinical", "encounter_salt": "S",
                                         "input_dir": str(tempfile.mkdtemp())}})


def test_render_splits_quality_out_of_grounding_flags():
    s = _s(subjective=[_claim("chest pain")], assessment=[_claim("likely mskl")], plan=[])
    t = Transcript(source_id="s", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0, end_s=5, text="chest pain likely mskl")])
    vnote = render_verified_note(s, t, config=_cfg(), title="E")
    q = {f["reason"] for f in vnote.quality_flags}
    assert "quality_assessment_no_plan" in q and "quality_required_section_empty" in q
    # SEPARATION: NO quality_ reason leaks into the medico-legal grounding_flags list
    assert not any(f["reason"].startswith("quality_") for f in vnote.grounding_flags)
    # and the note-level QUALITY banner renders inline via the existing flags_for path
    assert QUALITY_ASSESSMENT_NO_PLAN in vnote.body and QUALITY_REQUIRED_SECTION_EMPTY in vnote.body


def test_render_emits_quality_verdict_log_ilb():
    # ILB (confirm #6): the pass ALWAYS logs its verdict — flagged=0 ⇒ "clean", so idle is
    # distinguishable from broken. Log-emission pin (discipline #9), both paths.
    import structlog
    clean_s = _s(subjective=[_claim("chest pain")], objective=[_claim("bp 120")],
                 assessment=[_claim("mskl")], plan=[_claim("nsaids")])
    t = Transcript(source_id="s", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0, end_s=5, text="chest pain bp 120 mskl nsaids")])
    with structlog.testing.capture_logs() as cap:
        render_verified_note(clean_s, t, config=_cfg(), title="E")
    v = [e for e in cap if e.get("event") == "scribe.notegen_quality.verdict"]
    assert len(v) == 1 and v[0]["flagged"] == 0 and "clean" in v[0]["detail"]
    # flagged path
    flagged_s = _s(subjective=[_claim("a")], assessment=[_claim("b")], plan=[])
    with structlog.testing.capture_logs() as cap:
        render_verified_note(flagged_s, t, config=_cfg(), title="E")
    v = [e for e in cap if e.get("event") == "scribe.notegen_quality.verdict"]
    assert len(v) == 1 and v[0]["flagged"] >= 1


def test_quality_reasons_registered_additively():
    # grounding.py touch is PURELY ADDITIVE to the render dispatch — the 3 quality reasons map to
    # their literals; the pre-existing grounding/inferred/speaker entries are untouched.
    assert _REASON_INLINE_LITERAL["quality_required_section_empty"] == QUALITY_REQUIRED_SECTION_EMPTY
    assert _REASON_INLINE_LITERAL["quality_assessment_no_plan"] == QUALITY_ASSESSMENT_NO_PLAN
    assert _REASON_INLINE_LITERAL["inferred_diagnosis"]           # existing entry still present


def test_grounding_detector_unchanged_on_a_clean_note():
    # grounding.verify DETECTION is byte-frozen — a clean note grounds clean (the quality pass is a
    # SEPARATE post-pass, not part of verify).
    s = _s(subjective=[_claim("chest pain for two days")])
    t = Transcript(source_id="s", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0, end_s=5, text="chest pain for two days")])
    assert verify(s, t).clean is True                              # verify() untouched by 14c


# ===========================================================================
# Namespace disjointness + DRAFT_EDIT_FIELDS widen (the 3 ratified conditions)
# ===========================================================================

# SELF-MAINTAINING grounding-reason set — DERIVED from the three LIVE per-module registries, NOT a
# hand-maintained copy. A grounding reason added to any minting module's ``GROUNDING_REASONS`` /
# ``MECHANICAL_GROUNDING_REASONS`` frozenset auto-enters the disjointness guard below, so a future
# grounding reason cannot silently escape it (the #14c hand-list drift the go-live hardening closed).
# CEILING (be honest for the next reader): the guard covers reasons that are REGISTERED in these
# frozensets. A brand-new INLINE reason literal minted in code WITHOUT being added to its module's
# registry is not auto-detected (that needs AST/corpus analysis) — but each module's registry sits
# co-located with its reason constants + mint sites, so registering is the local, obvious step.
_LIVE_GROUNDING_REASONS = MECHANICAL_GROUNDING_REASONS | _INFERRED_DX_REASONS | _SPEAKER_REASONS


def test_quality_namespace_disjoint_from_grounding():
    # the split keys on the quality_ prefix — a grounding reason must NEVER start with it (else it'd
    # leak into the advisory list, diluting the medico-legal signal), and every quality reason MUST.
    # Run over the DERIVED live set so the guard follows the registries, not a stale hand-copy.
    assert _LIVE_GROUNDING_REASONS, "live grounding-reason registries must be non-empty"
    assert all(r.startswith(nq.QUALITY_REASON_PREFIX) for r in nq.QUALITY_REASONS)
    assert not any(r.startswith(nq.QUALITY_REASON_PREFIX) for r in _LIVE_GROUNDING_REASONS)
    assert nq.QUALITY_REASONS.isdisjoint(_LIVE_GROUNDING_REASONS)


def test_quality_flags_is_a_draft_edit_field():
    # #14c widen — quality_flags is refreshed each regen like grounding_flags (writable while ai_draft,
    # sealed at attest). The exact-set pin lives in test_stayc_clinical_scope.py (lockstep).
    assert "quality_flags" in STAYC_CLINICAL_DRAFT_EDIT_FIELDS
    assert "grounding_flags" in STAYC_CLINICAL_DRAFT_EDIT_FIELDS   # unchanged sibling
