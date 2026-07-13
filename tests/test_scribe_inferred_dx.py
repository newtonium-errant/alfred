"""Tests for the #48 deterministic inferred-diagnosis post-check.

Deterministic (NO LLM judge). The 7 base + 4 added = 11 A/B fixtures, the
mutation-bind (remove the check → the #48 case NOT flagged → RED), the H2 flags_for
reason-dispatch, the inline ⚠ render + grounding_flags metadata, the lexicon
abbreviation-exclusion policy, and the self-correcting Part-1 attest capture
(fires kept/removed AND is side-effect-free w.r.t. attestation).

AUDIT BATCH 2 additions (see the ``# audit batch 2`` section at the bottom):
FIX 1 same-span hedge-aware clear-check (the core FN); FIX 2 CKD-EPI / GERD-Q
tool-name collisions; FIX 3 adjectival stated-dx clears; FIX 4 unicode-apostrophe
+ hyphen↔space + reordered-diabetes normalise; FIX 5 capture restricted to the
flag's actually-inferred labels (multi-dx conflation).
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
    record_inferred_dx_attest_outcome,
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
# H2 — flags_for reason dispatch
# ---------------------------------------------------------------------------

def test_flags_for_dispatches_on_reason():
    g = GroundingResult(flags=[
        GroundingFlag("assessment", 0, "number_mismatch", "d", "c", ["S1"]),
        GroundingFlag("plan", 0, "inferred_diagnosis", "d", "c", ["S2"]),
    ])
    assert g.flags_for("assessment", 0) == [GROUNDING_UNVERIFIED]   # grounding reason → default
    assert g.flags_for("plan", 0) == [INFERRED_DIAGNOSIS]           # #48 reason → distinct literal
    assert g.flags_for("subjective", 0) == []                      # clean → []


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
    # These abbrevs stay DROPPED — their collision reaches BEYOND the screening-
    # instrument namespace (ms=morphine sulfate; gad ⊂ the LAB "anti-GAD/GAD
    # antibodies", not just GAD-7), so the pre-fold instrument blocklist can't
    # protect them; only the full label is matchable. (ckd/gerd/ptsd/adhd/ibs/osa/
    # bph are KEPT — instrument-only collisions closed by the blocklist, batch 2.)
    all_forms = {f for e in DIAGNOSIS_LEXICON for f in e.forms}
    assert ambiguous not in all_forms
    # but the FULL labels ARE present.
    assert diagnoses_named_in("patient with multiple sclerosis")
    assert diagnoses_named_in("history of myocardial infarction")
    assert diagnoses_named_in("assessment: generalized anxiety disorder")


def test_gad_dropped_abbrev_names_no_dx_at_matcher_level():
    # gad stays DROPPED (beyond-instrument lab collision), so at the pure MATCHER
    # level a cited GAD-7 / PHQ score names NO lexicon diagnosis. (For the KEPT
    # abbrevs ckd/gerd the matcher DOES return them — the instrument false-CLEAR is
    # closed at the CLEAR-check level by the pre-fold mask, see the blocklist tests.)
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
            # #58 — a COMPLETE encounter so attest passes the completeness gate
            # (these tests target the inferred-dx attest capture, not completeness).
            "encounter_completeness": {"protocol": 1, "complete": True},
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
            "encounter_completeness": {"protocol": 1, "complete": True},  # #58 complete
        },
        body="## Assessment\n- Something.\n", scope="stayc_clinical",
    )["path"]
    r2 = attest(tmp_path, bad, new_status="attested", attester="np_jamie",
                clinician_ids=_CLINICIANS, audit_path=tmp_path / "attest_audit.jsonl", now=_NOW)
    assert frontmatter.load(tmp_path / bad)["status"] == "attested"   # attest unaffected
    assert r2["path"] == bad


# ===========================================================================
# AUDIT BATCH 2 — FP/FN correctness fixes (FIX 1-5)
# ===========================================================================

# --- FIX 1 (#5): SAME-SPAN hedge false-CLEAR — the core FN -------------------

@pytest.mark.parametrize("cited_span", [
    "family history of major depressive disorder, mother had it",  # attribution
    "we can rule out major depressive disorder",                   # ruled-out
    "no evidence of major depressive disorder",                    # negated
    "major depressive disorder vs adjustment disorder",            # differential (trailing)
    "screening for major depressive disorder",                     # screening
    "?major depressive disorder",                                  # differential (leading ?)
])
def test_fix1_same_span_hedge_does_not_clear_inferred_dx(cited_span):
    # A CURRENT-assessment MDD claim whose ONLY cited support is a HEDGED same-span
    # mention is a fabrication → MUST be FLAGGED (pre-fix: entry_present saw the
    # label verbatim in the cite and CLEARED it — the wide-open same-span FN).
    tx = _tx(_seg(1, cited_span))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    flags = _flagged(note, tx)
    assert len(flags) == 1 and flags[0].reason == INFERRED_DIAGNOSIS_REASON


def test_fix1_mutation_bind_family_history_flagged():
    # MUTATION-BIND for FIX 1: the family-history same-span case is flagged ONLY
    # because the clear-check is hedge-aware. Revert _stated_current → entry_present
    # and this CLEARS again → RED. (Done + reverted manually in the ship report.)
    tx = _tx(_seg(1, "family history of major depressive disorder, mother had it"))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    assert len(_flagged(note, tx)) == 1


@pytest.mark.parametrize("cited_span", [
    "patient has major depressive disorder",
    "assessment: major depressive disorder, start sertraline",
    "diagnosis of major depressive disorder confirmed today",
    "major depressive disorder, moderate severity",
])
def test_fix1_current_assertion_still_clears(cited_span):
    # The hedge-aware check MUST NOT over-flag a genuinely STATED current dx.
    tx = _tx(_seg(1, cited_span))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    assert _flagged(note, tx) == []


def test_fix1_bare_history_of_pmh_still_clears():
    # A bare "history of MDD" / PMH dx is a CURRENT active problem, NOT a hedge —
    # it must still clear (guards against over-tightening past-resolved detection).
    tx = _tx(_seg(1, "past medical history: major depressive disorder, hypertension."))
    note = _note(assessment=[Claim(claim="Major depressive disorder", source_spans=["S1"])])
    assert _flagged(note, tx) == []


# --- FIX 2 (#7): CKD-EPI / GERD-Q tool-name collisions ----------------------

def test_fix2_ckd_epi_does_not_clear_inferred_ckd():
    # CKD-EPI is the eGFR estimating equation — naming it is NOT stating CKD. The
    # dropped "ckd" abbrev means an inferred CKD citing only the equation is FLAGGED.
    tx = _tx(_seg(1, "renal function per CKD-EPI eGFR is 55."))
    note = _note(assessment=[Claim(claim="Chronic kidney disease", source_spans=["S1"])])
    assert len(_flagged(note, tx)) == 1
    # the FULL label still clears.
    tx_stated = _tx(_seg(1, "assessment: chronic kidney disease stage 3."))
    assert _flagged(_note(assessment=[Claim(claim="Chronic kidney disease", source_spans=["S1"])]),
                    tx_stated) == []


def test_fix2_gerd_q_does_not_clear_inferred_gerd():
    tx = _tx(_seg(1, "GERD-Q score is 9."))
    note = _note(assessment=[Claim(claim="Gastroesophageal reflux disease", source_spans=["S1"])])
    assert len(_flagged(note, tx)) == 1
    tx_stated = _tx(_seg(1, "assessment: gastroesophageal reflux disease."))
    assert _flagged(_note(assessment=[Claim(claim="Gastroesophageal reflux disease", source_spans=["S1"])]),
                    tx_stated) == []


# --- FIX 3 (#6): adjectival stated-dx must CLEAR (alarm-fatigue FP) ----------

@pytest.mark.parametrize("dx_claim, cited", [
    ("Hypertension", "patient is hypertensive"),
    ("Type 2 diabetes", "known diabetic on metformin"),
    ("Asthma", "asthmatic since childhood"),
    ("Anemia", "appears anemic on exam"),
    ("Gout", "acute gouty flare of the first toe"),
    ("Hypothyroidism", "clinically hypothyroid, on levothyroxine"),
    ("Obesity", "patient is obese, bmi 34"),
])
def test_fix3_adjectival_stated_dx_clears(dx_claim, cited):
    # The note writes the NOUN dx; the clinician dictated it ADJECTIVALLY in the
    # cite. Noun-only lexicon false-FLAGGED this stated dx as inferred — now CLEAN.
    tx = _tx(_seg(1, cited))
    note = _note(assessment=[Claim(claim=dx_claim, source_spans=["S1"])])
    assert _flagged(note, tx) == []


# --- FIX 4 (#8): normalize_text unicode-apostrophe + hyphen + reorder --------

def test_fix4_curly_apostrophe_matches_lexicon():
    # STT/LLM emit the U+2019 curly apostrophe by default; without the fold
    # "Parkinson's"/"Alzheimer's" never match the straight-apostrophe forms.
    assert diagnoses_named_in("history of Parkinson’s disease")
    assert diagnoses_named_in("early Alzheimer’s disease")


def test_fix4_hyphen_space_and_reorder_variants_match():
    assert diagnoses_named_in("assessment: post traumatic stress disorder")  # space, not hyphen
    assert diagnoses_named_in("diabetes type 2, on metformin")               # reordered


# --- FIX 5 (#9): capture restricted to the flag's ACTUALLY-inferred labels ---

def test_fix5_capture_only_the_flag_inferred_labels_not_whole_claim():
    # A multi-dx claim NAMES a STATED dx (hypertension) alongside the one INFERRED
    # dx (MDD) that was actually flagged. The flag detail encodes only MDD → the
    # capture must emit ONLY the MDD outcome, NOT a spurious hypertension row.
    # (Pre-fix: diagnoses_named_in(whole claim) emitted BOTH → conflation.)
    flag = {
        "section": "assessment", "claim_index": 0, "reason": "inferred_diagnosis",
        "detail": ("inferred_diagnosis: major depressive disorder named in the claim "
                   "but absent from the cited segment(s) — clinician to confirm or remove"),
        "claim": "Major depressive disorder and stable hypertension", "source_spans": ["S1"],
    }
    draft = "## Assessment\n- Major depressive disorder and stable hypertension [S1]\n"
    with structlog.testing.capture_logs() as caps:
        record_inferred_dx_attest_outcome(
            grounding_flags=[flag], draft_original=draft, attested_body=draft, source_id=_SID,
        )
    ev = [c for c in caps if c.get("event") == "scribe.inferred_dx.attest_outcome"]
    assert len(ev) == 1
    assert ev[0]["diagnosis"] == "major depressive disorder"
    assert all(e["diagnosis"] != "hypertension" for e in ev)


# ===========================================================================
# AUDIT BATCH 2 — BLOCK-FIX: screening-INSTRUMENT blocklist (pre-fold)
# Closes the 5 instrument false-CLEARs the reviewer confirmed, KEEPING the abbrev
# forms (recall preserved). The instrument mask runs on RAW text before FIX-4's
# hyphen→space fold.
# ===========================================================================

@pytest.mark.parametrize("dx_claim, instrument_cite", [
    ("Post-traumatic stress disorder", "PC-PTSD-5 score is 12"),        # ptsd ⊂ PC-PTSD-5
    ("Attention deficit hyperactivity disorder", "ADHD-RS score is 30"),  # adhd ⊂ ADHD-RS
    ("Irritable bowel syndrome", "IBS-SSS is 240"),                     # ibs ⊂ IBS-SSS
    ("Obstructive sleep apnea", "OSA-18 quality-of-life score 60"),     # osa ⊂ OSA-18
    ("Benign prostatic hyperplasia", "BPH-II impact index is 4"),      # bph ⊂ BPH-II
    ("Chronic kidney disease", "renal function per CKD-EPI eGFR is 55"),  # ckd ⊂ CKD-EPI
    ("Gastroesophageal reflux disease", "GERD-Q score is 9"),          # gerd ⊂ GERD-Q
    ("Generalized anxiety disorder", "GAD-7 score is 15"),             # gad (dropped) ⊂ GAD-7
])
def test_blockfix_instrument_only_cite_flags_inferred_dx(dx_claim, instrument_cite):
    # A full-label dx claim whose ONLY cited support is the INSTRUMENT token is a
    # fabrication → FLAGGED. Pre-fix the KEPT abbrev matched inside the folded
    # instrument ("pc ptsd 5") and CLEARED it. (MUTATION-BIND: drop the mask from
    # _stated_current → all 8 CLEAR again → RED. Done + reverted in the ship report.)
    tx = _tx(_seg(1, instrument_cite))
    note = _note(assessment=[Claim(claim=dx_claim, source_spans=["S1"])])
    flags = _flagged(note, tx)
    assert len(flags) == 1 and flags[0].reason == INFERRED_DIAGNOSIS_REASON


@pytest.mark.parametrize("dx_claim, cited", [
    ("Post-traumatic stress disorder", "assessment: PTSD, start prazosin"),
    ("Attention deficit hyperactivity disorder", "assessment: ADHD, start methylphenidate"),
    ("Irritable bowel syndrome", "diagnosis: IBS, dietary advice given"),
    ("Chronic kidney disease", "assessment: CKD stage 3, monitor eGFR"),
    ("Gastroesophageal reflux disease", "assessment: GERD, start ppi"),
])
def test_blockfix_bare_abbrev_stated_still_clears(dx_claim, cited):
    # RECALL PROOF — the abbrev forms are KEPT, so a bare abbrev STATED as a current
    # dx (no instrument wrapper) still CLEARS. This is what dropping the abbrev would
    # have destroyed; the blocklist preserves it.
    tx = _tx(_seg(1, cited))
    note = _note(assessment=[Claim(claim=dx_claim, source_spans=["S1"])])
    assert _flagged(note, tx) == []


def test_blockfix_prefold_ordering_mask_before_fold():
    # The mask MUST run on RAW text: the fold turns "pc-ptsd-5" → "pc ptsd 5", which
    # exposes a standalone "ptsd". Prove the ordering directly.
    from alfred.scribe.diagnosis_lexicon import normalize_text
    from alfred.scribe.inferred_dx import _SCREENING_INSTRUMENT_RE, _mask_screening_instruments
    raw = "PC-PTSD-5 score is 12"
    # raw carries the instrument signature; the post-fold text does NOT.
    assert _SCREENING_INSTRUMENT_RE.search(raw)
    assert "ptsd" in normalize_text(raw)                       # fold would expose standalone ptsd
    assert "ptsd" not in normalize_text(_mask_screening_instruments(raw))  # mask-then-fold removes it


def test_blockfix_both_present_standalone_clears_instrument_only_flags():
    # If the label appears BOTH standalone (current assertion) AND inside an
    # instrument in the same span → the standalone occurrence STATES it → CLEAR.
    tx_both = _tx(_seg(1, "assessment: post-traumatic stress disorder; PC-PTSD-5 was 12"))
    assert _flagged(_note(assessment=[Claim(claim="Post-traumatic stress disorder", source_spans=["S1"])]),
                    tx_both) == []
    # only-in-instrument → FLAG (the standalone leg removed).
    tx_only = _tx(_seg(1, "PC-PTSD-5 was 12"))
    assert len(_flagged(_note(assessment=[Claim(claim="Post-traumatic stress disorder", source_spans=["S1"])]),
                        tx_only)) == 1


def test_blockfix_instrument_mask_composes_with_hedge():
    # A standalone PTSD that is ALSO hedged (family history) still FLAGS — the mask
    # and the hedge check compose (stated = current-assertion AND not-instrument).
    tx = _tx(_seg(1, "family history of PTSD; PC-PTSD-5 not done"))
    note = _note(assessment=[Claim(claim="Post-traumatic stress disorder", source_spans=["S1"])])
    assert len(_flagged(note, tx)) == 1
