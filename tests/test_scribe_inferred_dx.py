"""Tests for the #48 deterministic inferred-diagnosis post-check.

Deterministic (NO LLM judge). The 7 base + 4 added = 11 A/B fixtures, the
mutation-bind (remove the check → the #48 case NOT flagged → RED), the H2 flag_for
reason-dispatch, the inline ⚠ render + grounding_flags metadata, the lexicon
abbreviation-exclusion policy, and the self-correcting Part-1 attest capture
(fires kept/removed AND is side-effect-free w.r.t. attestation).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest
import structlog

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe.attest import attest
from alfred.scribe.diagnosis_lexicon import DIAGNOSIS_LEXICON, diagnoses_named_in, entry_present
from alfred.scribe.grounding import GroundingFlag, GroundingResult, verify
from alfred.scribe.inferred_dx import (
    INFERRED_DIAGNOSIS_REASON,
    check_inferred_diagnoses,
)
from alfred.scribe.notegen import (
    GROUNDING_UNVERIFIED,
    INFERRED_DIAGNOSIS,
    Claim,
    StructuredNote,
    render_soap,
)
from alfred.scribe.transcript import Segment, Transcript
from alfred.vault.ops import vault_create


def _seg(i: int, text: str) -> Segment:
    return Segment(id=f"S{i}", start_s=float(i), end_s=float(i) + 1, text=text, speaker=None)


def _tx(*segs: Segment) -> Transcript:
    return Transcript(source_id="s", mode="synthetic", segments=list(segs))


def _note(**sections) -> StructuredNote:
    return StructuredNote(
        subjective=sections.get("subjective", []),
        objective=sections.get("objective", []),
        assessment=sections.get("assessment", []),
        plan=sections.get("plan", []),
    )


def _flagged(structured, transcript) -> list[GroundingFlag]:
    return check_inferred_diagnoses(structured, transcript)


# ---------------------------------------------------------------------------
# The 7 base A/B fixtures
# ---------------------------------------------------------------------------

def test_1_stated_mdd_cited_clean():
    tx = _tx(_seg(1, "Patient has a documented history of major depressive disorder."))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    assert _flagged(note, tx) == []                       # stated + cited → CLEAN


def test_2_empty_assessment_clean():
    tx = _tx(_seg(1, "Patient reports low mood."))
    note = _note(assessment=[])                           # clinician declined a diagnosis
    assert _flagged(note, tx) == []


def test_3_inferred_mdd_the_48_case_flagged():
    # THE #48 case — inferred from low mood + PHQ-9 + sertraline (all grounded),
    # NO 'MDD'/'major depression' in ANY cited segment.
    tx = _tx(
        _seg(1, "Patient reports low mood for three weeks."),
        _seg(2, "PHQ-9 score is 18."),
        _seg(3, "Started sertraline 50mg daily."),
    )
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1", "S2", "S3"])])
    flags = _flagged(note, tx)
    assert len(flags) == 1 and flags[0].reason == INFERRED_DIAGNOSIS_REASON
    assert flags[0].section == "assessment"


def test_3b_inferred_gad_screening_tool_collision_flagged():
    # REVIEW BLOCK — the GAD ← GAD-7 tool-name collision (same false-CLEAR class as
    # the MS/MI abbrev exclusions). An inferred GAD assessment citing a "GAD-7
    # score is 15" segment (no dx LABEL) MUST be FLAGGED. Pre-fix, "gad" matched
    # INSIDE "GAD-7" → the score segment read as the dx stated → spuriously CLEARED
    # (the fabrication slipped). Dropping the "gad" abbrev form fixes it. Mirrors
    # the #48 MDD/PHQ-9 case (which never collided — PHQ-9 ∌ "mdd").
    tx = _tx(
        _seg(1, "Patient reports excessive worry most days for six months."),
        _seg(2, "GAD-7 score is 15."),
        _seg(3, "Started sertraline 25mg."),
    )
    note = _note(assessment=[Claim(claim="Generalized anxiety disorder", source_spans=["S1", "S2", "S3"])])
    flags = _flagged(note, tx)
    assert len(flags) == 1 and flags[0].reason == INFERRED_DIAGNOSIS_REASON
    # the STATED case STILL clears — a cited segment naming the full dx label.
    tx_stated = _tx(_seg(1, "Assessment: generalized anxiety disorder; GAD-7 was 15."))
    assert _flagged(
        _note(assessment=[Claim(claim="Generalized anxiety disorder", source_spans=["S1"])]), tx_stated
    ) == []


def test_4_inferred_invented_label_flagged():
    tx = _tx(_seg(1, "Mood swings and started on a mood stabiliser."))
    note = _note(assessment=[Claim(claim="Bipolar disorder", source_spans=["S1"])])
    assert len(_flagged(note, tx)) == 1                   # invented label absent from cite → FLAG


def test_5_history_of_dx_cited_clean():
    tx = _tx(_seg(1, "Past medical history: hypertension, on amlodipine."))
    note = _note(assessment=[Claim(claim="Hypertension", source_spans=["S1"])])
    assert _flagged(note, tx) == []                       # label IS in the cited segment → CLEAN


def test_6_symptom_description_no_label_clean():
    tx = _tx(_seg(1, "Patient looks low, tired, poor sleep."))
    note = _note(assessment=[Claim(claim="Low mood with fatigue and insomnia", source_spans=["S1"])])
    assert _flagged(note, tx) == []                       # bare symptoms, no lexicon LABEL → CLEAN


def test_7_synonym_stated_clean_and_abbrev_inferred_flagged():
    # model writes the abbrev "MDD"; the cited segment states the full synonym → CLEAN.
    tx_stated = _tx(_seg(1, "Assessment: major depressive disorder, moderate."))
    assert _flagged(_note(assessment=[Claim(claim="MDD", source_spans=["S1"])]), tx_stated) == []
    # model writes "MDD" with no cited mention → FLAGGED.
    tx_inferred = _tx(_seg(1, "Low mood and anhedonia for a month."))
    assert len(_flagged(_note(assessment=[Claim(claim="MDD", source_spans=["S1"])]), tx_inferred)) == 1


# ---------------------------------------------------------------------------
# The 4 REQUIRED added fixtures
# ---------------------------------------------------------------------------

def test_a_miscite_flag_and_cocite_clean_proves_the_join():
    # dx STATED in S5, claim cites S1 only → FLAGGED (cited-span, not whole-transcript).
    tx = _tx(_seg(1, "Patient reports fatigue and headaches."),
             _seg(5, "Clinician: the diagnosis here is hypertension."))
    assert len(_flagged(_note(assessment=[Claim(claim="Hypertension", source_spans=["S1"])]), tx)) == 1
    # co-cite ["S1","S5"] → CLEAN (grounding's _cited_text JOINS all cited spans).
    assert _flagged(_note(assessment=[Claim(claim="Hypertension", source_spans=["S1", "S5"])]), tx) == []


def test_b_family_history_cited_span_justifies_the_design():
    # 'mother has T2DM' in S1; the patient-T2DM assessment cites S3 (no label) →
    # FLAGGED. Whole-transcript would FALSE-CLEAR this (label present in S1) — the
    # exact high-harm false-negative cited-span prevents.
    tx = _tx(_seg(1, "Mother has type 2 diabetes."),
             _seg(2, "Fasting glucose 9.1 mmol/L."),
             _seg(3, "Reports increased thirst and urination."))
    note = _note(assessment=[Claim(claim="Type 2 diabetes", source_spans=["S3"])])
    assert len(_flagged(note, tx)) == 1


def test_c_plan_smuggle_flagged_all_sections():
    # H1 — the inferred dx smuggles into the PLAN as an indication (cited segment
    # says only "start sertraline", no dx label). Must FLAG (section-agnostic).
    tx = _tx(_seg(2, "Start sertraline 50mg daily."))
    note = _note(plan=[Claim(claim="Start sertraline 50mg for major depressive disorder", source_spans=["S2"])])
    flags = _flagged(note, tx)
    assert len(flags) == 1 and flags[0].section == "plan"


def test_d_abbreviation_collision_must_not_false_clear():
    # cited "MS" = morphine sulfate; inferred "multiple sclerosis". Because the
    # lexicon EXCLUDES "MS" as a form, the cited "MS" does NOT clear the inferred
    # "multiple sclerosis" → FLAGGED. (If "MS" were a matchable form, cited "MS"
    # would false-CLEAR the fabrication — the dangerous direction.)
    tx = _tx(_seg(4, "Given MS 4mg IV for pain."))
    note = _note(assessment=[Claim(claim="Multiple sclerosis", source_spans=["S4"])])
    assert len(_flagged(note, tx)) == 1


# ---------------------------------------------------------------------------
# MUTATION-BIND — the #48 case flagged is bound to the check existing
# ---------------------------------------------------------------------------

def test_mutation_bind_48_case_flagged_by_the_check():
    # Direct pin: the #48 assessment is flagged ONLY because check_inferred_diagnoses
    # ran. (Manual mutation — stub the check to return [] — turns test_3 RED; done
    # + reverted in the ship report.)
    tx = _tx(_seg(1, "Low mood for weeks."), _seg(2, "PHQ-9 is 18."), _seg(3, "On sertraline."))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1", "S2", "S3"])])
    # grounding ALONE is blind (no number/negation token on the label) → 0 flags.
    assert verify(note, tx).flags == []
    # the post-check is what catches it.
    assert len(check_inferred_diagnoses(note, tx)) == 1


# ---------------------------------------------------------------------------
# H2 — flag_for reason dispatch
# ---------------------------------------------------------------------------

def test_flag_for_dispatches_on_reason():
    g = GroundingResult(flags=[
        GroundingFlag("assessment", 0, "number_mismatch", "d", "c", ["S1"]),
        GroundingFlag("plan", 0, "inferred_diagnosis", "d", "c", ["S2"]),
    ])
    assert g.flag_for("assessment", 0) == GROUNDING_UNVERIFIED   # grounding reason → default
    assert g.flag_for("plan", 0) == INFERRED_DIAGNOSIS           # #48 reason → distinct literal
    assert g.flag_for("subjective", 0) is None                  # clean → None


def test_inferred_flag_renders_inline_and_lands_in_metadata():
    tx = _tx(_seg(1, "Low mood, PHQ-9 20, on an SSRI."))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    g = verify(note, tx)
    g.flags.extend(check_inferred_diagnoses(note, tx))
    body = render_soap(note, title="Enc", grounding=g)
    assert INFERRED_DIAGNOSIS in body                            # ⚠ renders inline
    # rides the grounding_flags frontmatter metadata.
    assert any(m["reason"] == "inferred_diagnosis" for m in g.metadata)


# ---------------------------------------------------------------------------
# Lexicon curation policy — ambiguous abbreviations are NOT matchable forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ambiguous", ["ms", "mi", "ra", "pd", "ad", "gad"])
def test_lexicon_excludes_ambiguous_abbreviations(ambiguous):
    # None of the dangerous abbreviations is a matchable form (false-CLEAR guard).
    # "gad" is EXCLUDED for the screening-tool-name collision (GAD ⊂ GAD-7/GAD-2).
    all_forms = {f for e in DIAGNOSIS_LEXICON for f in e.forms}
    assert ambiguous not in all_forms
    # but the FULL labels ARE present.
    assert diagnoses_named_in("patient with multiple sclerosis")
    assert diagnoses_named_in("history of myocardial infarction")
    assert diagnoses_named_in("assessment: generalized anxiety disorder")


def test_screening_tool_name_does_not_clear_inferred_dx():
    # The fix, at the matcher level: a cited screening-tool NAME must not register
    # the DIAGNOSIS. "GAD-7 score is 15" names NO lexicon diagnosis.
    assert diagnoses_named_in("GAD-7 score is 15") == []
    assert diagnoses_named_in("PHQ-9 score is 18") == []      # never collided (kept as a pin)


def test_lexicon_word_boundary_not_substring():
    # 'depression' (a bare symptom) must NOT match inside 'major depression'
    # backwards: a claim saying only "depression" names NO lexicon entry.
    assert diagnoses_named_in("patient reports depression") == []
    # 'htn' must not match inside a longer token.
    assert diagnoses_named_in("brightness and lightning") == []


# ---------------------------------------------------------------------------
# Self-correcting Part-1 attest capture — fires kept/removed + SIDE-EFFECT-FREE
# ---------------------------------------------------------------------------

_CLINICIANS = {"np_jamie", "dr_synthetic"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
_SID = "enc-abc0123456789d"


def _make_flagged_draft(tmp_path, *, body: str):
    """A born-ai_draft clinical_note carrying an inferred_diagnosis grounding flag
    (claim names MDD) + a draft_original that contains the label."""
    flag = {
        "section": "assessment", "claim_index": 0, "reason": "inferred_diagnosis",
        "detail": "inferred_diagnosis: major depressive disorder ...",
        "claim": "Major depressive disorder", "source_spans": ["S1"],
    }
    result = vault_create(
        tmp_path, "clinical_note", "Synthetic depression encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": _SID, "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "grounding_flags": [flag],
            "draft_original": "## Assessment\n- Major depressive disorder [S1]\n",
        },
        body=body,
        scope="stayc_clinical",
    )
    return result["path"]


def _attest(tmp_path, rel_path):
    return attest(
        tmp_path, rel_path, new_status="attested", attester="np_jamie",
        clinician_ids=_CLINICIANS, audit_path=tmp_path / "attest_audit.jsonl", now=_NOW,
    )


def test_attest_capture_fires_kept_when_label_survives(tmp_path):
    # clinician KEPT the flagged dx in the attested body → kept=True (likely FP).
    rel = _make_flagged_draft(tmp_path, body="## Assessment\n- Major depressive disorder [S1]\n")
    with structlog.testing.capture_logs() as caps:
        _attest(tmp_path, rel)
    ev = [c for c in caps if c.get("event") == "scribe.inferred_dx.attest_outcome"]
    assert len(ev) == 1 and ev[0]["kept"] is True and ev[0]["diagnosis"] == "major depressive disorder"


def test_attest_capture_fires_removed_when_label_deleted(tmp_path):
    # clinician REMOVED the flagged dx → kept=False (the check was a true-positive).
    rel = _make_flagged_draft(tmp_path, body="## Assessment\n- Low mood; declined to diagnose.\n")
    with structlog.testing.capture_logs() as caps:
        _attest(tmp_path, rel)
    ev = [c for c in caps if c.get("event") == "scribe.inferred_dx.attest_outcome"]
    assert len(ev) == 1 and ev[0]["kept"] is False


def test_attest_capture_is_side_effect_free(tmp_path):
    # The capture NEVER alters the attestation: the triad is written + the audit
    # appended IDENTICALLY whether or not there are inferred flags, and a MALFORMED
    # grounding_flags does not break the attest (swallowed).
    rel = _make_flagged_draft(tmp_path, body="## Assessment\n- Major depressive disorder [S1]\n")
    result = _attest(tmp_path, rel)
    rec = frontmatter.load(tmp_path / rel)
    assert rec["status"] == "attested" and rec["attested_by"] == "np_jamie"  # triad written
    audit = (tmp_path / "attest_audit.jsonl").read_text().strip().splitlines()
    assert len(audit) == 1 and json.loads(audit[0])["to_status"] == "attested"  # audit appended
    assert result["path"] == rel

    # malformed grounding_flags → capture swallows, attest still succeeds.
    bad = vault_create(
        tmp_path, "clinical_note", "Synthetic malformed flags encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": "enc-def0123456789a", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "grounding_flags": "not-a-list", "draft_original": "x",
        },
        body="## Assessment\n- Something.\n", scope="stayc_clinical",
    )["path"]
    r2 = attest(tmp_path, bad, new_status="attested", attester="np_jamie",
                clinician_ids=_CLINICIANS, audit_path=tmp_path / "attest_audit.jsonl", now=_NOW)
    assert frontmatter.load(tmp_path / bad)["status"] == "attested"   # attest unaffected
    assert r2["path"] == bad
