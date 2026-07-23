"""#14 slice 14e-ii — quality-flag SURVIVAL capture (§4.3 self-correcting tie-in).

At attest, re-check each draft-time quality_* flag against the ATTESTED body: STILL fires ⇒ ignored
(tune-down candidate); STOPPED ⇒ acted (check was useful). Extends the notegen_edit row with
quality_survival {reason:{acted,ignored}}. Pins: the widening-pin lockstep (a claim-string leak into
quality_survival fails), the re-check golden vectors (profile-independent + target-dependent), the
side-effect-free guarantee (a raising re-check never fails the attest), and the readout consumer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe import enroll_learning as el
from alfred.scribe import notegen_feedback as nf
from alfred.scribe.attest import attest
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie"}
_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
_REQUIRED = frozenset({"subjective", "assessment", "plan"})


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for k in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(k, raising=False)


def _qflag(reason):
    return {"reason": reason, "section": "note", "claim_index": -1, "detail": reason}


def _survival(qflags, attested, *, target=25, required=_REQUIRED):
    return nf._quality_survival(qflags, attested, target, required)


# ===========================================================================
# Re-check golden vectors — acted vs ignored
# ===========================================================================

def test_required_section_empty_acted_when_filled():
    q = [_qflag("quality_required_section_empty")]
    filled = "## Subjective\n- a\n\n## Assessment\n- b\n\n## Plan\n- c\n"
    assert _survival(q, filled)["quality_required_section_empty"] == {"acted": 1, "ignored": 0}
    empty_plan = "## Subjective\n- a\n\n## Assessment\n- b\n\n## Plan\nNot addressed\n"
    assert _survival(q, empty_plan)["quality_required_section_empty"] == {"acted": 0, "ignored": 1}


def test_assessment_no_plan_acted_when_plan_added():
    q = [_qflag("quality_assessment_no_plan")]
    with_plan = "## Assessment\n- b\n\n## Plan\n- c\n"
    assert _survival(q, with_plan)["quality_assessment_no_plan"]["acted"] == 1
    still = "## Assessment\n- b\n\n## Plan\nNot addressed\n"
    assert _survival(q, still)["quality_assessment_no_plan"]["ignored"] == 1


def test_verbose_target_dependent():
    q = [_qflag("quality_verbose")]
    tight = "## Subjective\n- chest pain\n"                         # 2 words / 1 claim = 2
    assert _survival(q, tight, target=25)["quality_verbose"]["acted"] == 1     # under → acted
    verbose = "## Subjective\n- " + " ".join(["word"] * 30) + "\n"  # 30 w / 1 claim
    assert _survival(q, verbose, target=25)["quality_verbose"]["ignored"] == 1  # over → ignored


def test_no_draft_quality_flags_is_empty():
    assert nf._quality_survival([], "## Subjective\n- a\n", 25, _REQUIRED) == {}
    assert nf._quality_survival(None, "x", 25, _REQUIRED) == {}


def test_non_quality_flags_ignored():
    # only quality_ reasons are surveyed — a grounding reason in the list is skipped.
    q = [{"reason": "negation_mismatch", "claim": "x"}, _qflag("quality_assessment_no_plan")]
    surv = _survival(q, "## Assessment\n- b\n\n## Plan\nNot addressed\n")
    assert set(surv) == {"quality_assessment_no_plan"}


# ===========================================================================
# Widening-pin lockstep (PHI-free)
# ===========================================================================

def test_row_fields_include_quality_survival():
    assert "quality_survival" in nf._ROW_FIELDS


def test_compute_output_with_quality_survival_is_phi_free():
    row = nf.compute_notegen_edit_row(
        draft_original="## Plan\nNot addressed\n", attested_body="## Assessment\n- b\n\n## Plan\nNot addressed\n",
        grounding_flags=[], template_id="soap", template_version=1, source_id="enc-a",
        quality_flags=[_qflag("quality_assessment_no_plan")], succinctness_target=25, required_sections=_REQUIRED)
    assert row["quality_survival"]["quality_assessment_no_plan"]["ignored"] == 1
    assert nf.phi_free_violations({"ts": "t", **row}) == []


def test_widening_pin_rejects_claim_string_in_quality_survival():
    row = nf.compute_notegen_edit_row(
        draft_original="x", attested_body="x", grounding_flags=[], template_id="soap",
        template_version=1, source_id="enc-a")
    base = {"ts": "t", **row}
    # a claim-text leak into quality_survival (as a value) MUST fail the pin
    leak = dict(base, quality_survival={"quality_verbose": {"note": "the patient denies chest pain"}})
    assert nf.phi_free_violations(leak)
    # a non-{acted,ignored} shape fails
    assert nf.phi_free_violations(dict(base, quality_survival={"x": {"kept": 1}}))


# ===========================================================================
# Side-effect-free through the real attest() + wiring
# ===========================================================================

def _rows(enroll):
    p = el._capture_path(enroll)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip() and json.loads(x).get("kind") == "notegen_edit"] if p.is_file() else []


def _make_draft(vault, *, quality_flags, body):
    return vault_create(vault, "clinical_note", "Enc", set_fields={
        "ai_draft": True, "synthetic": True, "status": "ai_draft", "source_id": "enc-abc0123456789d",
        "drafted_by": SCRIBE_DRAFTER_IDENTITY, "encounter_completeness": {"protocol": 1, "complete": True},
        "grounding_flags": [], "quality_flags": quality_flags,
        "draft_original": "## Assessment\n- b\n\n## Plan\nNot addressed\n",
    }, body=body, scope="stayc_clinical")["path"]


def test_attest_captures_quality_survival(tmp_path):
    enroll = str(tmp_path / "enroll")
    # draft flagged assessment_no_plan; the clinician ADDED a plan in the attested body → ACTED.
    rel = _make_draft(tmp_path, quality_flags=[_qflag("quality_assessment_no_plan")],
                      body="## Assessment\n- b [S1]\n\n## Plan\n- ibuprofen [S1]\n")
    attest(tmp_path, rel, new_status="attested", attester="np_jamie", clinician_ids=_CLINICIANS,
           audit_path=tmp_path / "audit.jsonl", now=_NOW, enrollment_dir=enroll,
           quality_succinctness_target=25, quality_required_sections=_REQUIRED)
    rows = _rows(enroll)
    assert len(rows) == 1
    assert rows[0]["quality_survival"]["quality_assessment_no_plan"]["acted"] == 1   # clinician acted


def test_attest_quality_survival_capture_never_fails_attest(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("recheck exploded")
    monkeypatch.setattr(nf, "_quality_survival", _boom)
    rel = _make_draft(tmp_path, quality_flags=[_qflag("quality_assessment_no_plan")],
                      body="## Assessment\n- b [S1]\n\n## Plan\n- c [S1]\n")
    result = attest(tmp_path, rel, new_status="attested", attester="np_jamie", clinician_ids=_CLINICIANS,
                    audit_path=tmp_path / "audit.jsonl", now=_NOW, enrollment_dir=str(tmp_path / "enroll"),
                    quality_succinctness_target=25, quality_required_sections=_REQUIRED)
    assert result and frontmatter.load(str(tmp_path / rel))["status"] == "attested"   # attest UNAFFECTED


# ===========================================================================
# Readout consumer — the quality-check tune-down ranking
# ===========================================================================

def test_aggregate_quality_ranking_by_ignored_rate():
    rows = [
        {"kind": "notegen_edit", "source_id": "a",
         "quality_survival": {"quality_verbose": {"acted": 0, "ignored": 1},
                              "quality_assessment_no_plan": {"acted": 1, "ignored": 0}}},
        {"kind": "notegen_edit", "source_id": "b",
         "quality_survival": {"quality_verbose": {"acted": 0, "ignored": 1}}},
    ]
    agg = nf.aggregate_feedback(rows)
    top = agg["quality_ranking"][0]
    assert top["reason"] == "quality_verbose" and top["ignored"] == 2 and top["ignored_rate"] == 1.0
    assert agg["quality_survival"]["quality_assessment_no_plan"] == {"acted": 1, "ignored": 0}


def test_aggregate_quality_ranking_empty_ilb():
    assert nf.aggregate_feedback([{"kind": "notegen_edit", "source_id": "a"}])["quality_ranking"] == []
