"""#14 slice 14a — note-gen edit-diff capture: contract-first tests (§9).

Pins written from the schema/behavior (never code-derived): the PHI-free widening pin (a claim
string fails it), the diff-classification golden vectors (against the REAL render_soap output),
flag-survival correctness, and the side-effect-free + observable-dormancy captures. Regression pins
run UNCONDITIONALLY (no module-level importorskip).
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe import enroll_learning
from alfred.scribe import notegen_feedback as nf
from alfred.scribe.attest import attest
from alfred.scribe.grounding import GroundingResult, verify
from alfred.scribe.notegen import StructuredNote, render_soap
from alfred.scribe.transcript import Segment, Transcript
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie"}
_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for k in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(k, raising=False)


def _render(**sections) -> str:
    """A REAL render_soap body (clean, no flags) for a hand-built claim set — golden vectors go
    through the ACTUAL renderer so the parser is pinned against real output, not a paraphrase."""
    return render_soap(StructuredNote.from_dict(sections), title="E", grounding=GroundingResult())


def _row(draft, attested, *, flags=None):
    return nf.compute_notegen_edit_row(
        draft_original=draft, attested_body=attested, grounding_flags=flags,
        template_id=None, template_version=None, source_id="enc-x")


# ===========================================================================
# Parser — walked against the REAL render_soap output
# ===========================================================================

def test_parse_sections_roundtrip_real_renderer():
    # A note WITH a grounding flag (⚠) + cites + an empty "Not addressed" section + the REASONING line.
    s = StructuredNote.from_dict({
        "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
        "objective": [],
        "assessment": [{"claim": "Likely musculoskeletal", "source_spans": ["S1"]}],
        "plan": [{"claim": "Ibuprofen 400mg", "source_spans": ["S2"]}],
        "assessment_reasoning_stated": False,
    })
    t = Transcript(source_id="s", mode="synthetic", segments=[
        Segment(id="S1", start_s=0, end_s=5, text="chest pain for two days"),
        Segment(id="S2", start_s=5, end_s=10, text="ibuprofen 600mg"),   # 400 ≠ cite → number_mismatch ⚠
    ])
    body = render_soap(s, title="Encounter s", grounding=verify(s, t))
    parsed = nf._parse_sections(body)
    # flags + cites + "Not addressed" + REASONING line all stripped/excluded → clean claim text
    assert parsed["subjective"] == ["Chest pain for 2 days"]
    assert parsed["objective"] == []
    assert parsed["assessment"] == ["Likely musculoskeletal"]
    assert parsed["plan"] == ["Ibuprofen 400mg"]


def test_parse_defensive_on_malformed_markdown_never_crashes():
    # A clinician-restructured note: renamed heading, a deleted section, free text. Graceful — no crash,
    # bullets under an unrecognized heading are dropped (not mis-attributed).
    body = (
        "# E\n\n## Subjective\n- Chest pain\n\n## Plan & Follow-up\n- Ibuprofen\n\n"
        "Some free text the clinician typed.\n"
    )
    parsed = nf._parse_sections(body)
    assert parsed["subjective"] == ["Chest pain"]
    assert parsed["plan"] == []                              # "## Plan & Follow-up" not recognized → dropped
    assert parsed["objective"] == [] and parsed["assessment"] == []


# ===========================================================================
# Diff-classification golden vectors (exact counts)
# ===========================================================================

def test_verbatim_keep():
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    r = _row(body, body)
    sub = r["sections"]["subjective"]
    assert sub["claims_kept_verbatim"] == 1
    assert sub["claims_removed"] == 0 and sub["claims_added"] == 0 and sub["claims_modified"] == 0


def test_pure_cut_is_removed():
    draft = _render(plan=[{"claim": "Start ibuprofen 400mg", "source_spans": ["S1"]}])
    attested = _render(plan=[])
    r = _row(draft, attested)
    assert r["sections"]["plan"]["claims_removed"] == 1
    assert r["totals"]["claims_removed"] == 1 and r["totals"]["claims_added"] == 0


def test_pure_add_is_added():
    draft = _render(plan=[])
    attested = _render(plan=[{"claim": "Follow up in two weeks", "source_spans": ["S1"]}])
    r = _row(draft, attested)
    assert r["sections"]["plan"]["claims_added"] == 1
    assert r["totals"]["claims_removed"] == 0


def test_reword_is_modified():
    draft = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    attested = _render(subjective=[{"claim": "Chest pain for three days", "source_spans": ["S1"]}])
    assert difflib.SequenceMatcher(None, "chest pain for two days",
                                   "chest pain for three days").ratio() >= 0.6   # above threshold
    r = _row(draft, attested)
    sub = r["sections"]["subjective"]
    assert sub["claims_modified"] == 1 and sub["claims_removed"] == 0 and sub["claims_added"] == 0


def test_disjoint_is_removed_plus_added_not_modified():
    # A totally different claim (ratio < 0.6) → removed + added, NOT modified (the boundary's low side).
    draft = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    attested = _render(subjective=[{"claim": "Denies shortness of breath", "source_spans": ["S1"]}])
    assert difflib.SequenceMatcher(None, "chest pain for two days",
                                   "denies shortness of breath").ratio() < 0.6    # below threshold
    r = _row(draft, attested)
    sub = r["sections"]["subjective"]
    assert sub["claims_removed"] == 1 and sub["claims_added"] == 1 and sub["claims_modified"] == 0


def test_threshold_constant_is_060():
    # Pin the tunable constant + the gate direction (mutation-bind: raising it would flip
    # test_reword_is_modified to removed+added; lowering it would flip the disjoint case to modified).
    assert nf._MODIFIED_RATIO_THRESHOLD == 0.6


def test_mixed_keep_cut_add_modify():
    draft = _render(
        subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]},   # → modified
                    {"claim": "No fever reported", "source_spans": ["S1"]}],          # → kept
        plan=[{"claim": "Start ibuprofen 400mg", "source_spans": ["S2"]}])            # → removed
    attested = _render(
        subjective=[{"claim": "Chest pain for three days", "source_spans": ["S1"]},
                    {"claim": "No fever reported", "source_spans": ["S1"]}],
        plan=[{"claim": "Refer to physiotherapy", "source_spans": ["S2"]}])            # → added (plan)
    r = _row(draft, attested)
    assert r["sections"]["subjective"] == {
        "claims_draft": 2, "claims_attested": 2, "claims_removed": 0, "claims_added": 0,
        "claims_modified": 1, "claims_kept_verbatim": 1, "words_draft": 8, "words_attested": 8}
    assert r["sections"]["plan"]["claims_removed"] == 1 and r["sections"]["plan"]["claims_added"] == 1


def test_net_word_delta_sign():
    draft = _render(subjective=[{"claim": "The patient reports chest pain for two days", "source_spans": ["S1"]}])
    attested = _render(subjective=[{"claim": "Chest pain two days", "source_spans": ["S1"]}])
    r = _row(draft, attested)
    assert r["totals"]["net_word_delta"] < 0                 # clinician CUT → draft too verbose
    r2 = _row(attested, draft)
    assert r2["totals"]["net_word_delta"] > 0                # clinician ADDED → completeness gap


def test_high_modification_derivation():
    # 2 of 2 draft claims modified ⇒ ratio 1.0 ≥ 0.5 ⇒ True.
    draft = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]},
                                {"claim": "Headache since yesterday morning", "source_spans": ["S1"]}])
    attested = _render(subjective=[{"claim": "Chest pain for three days", "source_spans": ["S1"]},
                                   {"claim": "Headache since yesterday evening", "source_spans": ["S1"]}])
    assert _row(draft, attested)["high_modification"] is True
    # a clean note (all kept) ⇒ False
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    assert _row(body, body)["high_modification"] is False


def test_zero_edit_note_still_lands_a_row_ilb():
    # intentionally-left-blank: a zero-edit note is the healthy hit-rate signal, not silence.
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    r = _row(body, body)
    assert r["sections"]["subjective"]["claims_kept_verbatim"] == 1
    assert nf.phi_free_violations({"ts": "t", **r}) == []


# ===========================================================================
# Flag-survival — generalizes the two twins to ALL reasons
# ===========================================================================

def test_flag_survival_across_reasons():
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    flags = [
        {"reason": "number_mismatch", "claim": "Ibuprofen 400mg"},          # not in body → removed
        {"reason": "inferred_diagnosis", "claim": "Chest pain for two days"},  # in body → kept
        {"reason": "speaker_mismatch", "claim": "BP was 120 over 80"},       # not in body → removed
    ]
    fs = _row(body, body, flags=flags)["flag_survival"]
    assert fs["number_mismatch"] == {"removed": 1, "kept": 0}
    assert fs["inferred_diagnosis"] == {"removed": 0, "kept": 1}
    assert fs["speaker_mismatch"] == {"removed": 1, "kept": 0}


# ===========================================================================
# PHI-FREE widening pin (load-bearing)
# ===========================================================================

def test_compute_output_is_phi_free():
    r = _row(_render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}]),
             _render(subjective=[{"claim": "Chest pain three days", "source_spans": ["S1"]}]))
    assert nf.phi_free_violations({"ts": "t", **r}) == []


def test_widening_pin_rejects_a_claim_string_leak():
    r = {"ts": "t", **_row(_render(subjective=[]), _render(subjective=[]))}
    # a mutation that writes a claim string as a value MUST fail the pin
    leaked = dict(r, sections=dict(r["sections"], subjective=dict(
        r["sections"]["subjective"], claims_draft="No fever reported today")))
    assert nf.phi_free_violations(leaked)                    # non-empty → caught
    # an extra top-level field also fails
    assert nf.phi_free_violations(dict(r, phrasing="the patient denies"))
    # a flag_survival value that isn't {removed,kept} fails
    assert nf.phi_free_violations(dict(r, flag_survival={"x": {"note": "denies chest pain"}}))


# ===========================================================================
# Writer + side-effect-free + observable dormancy
# ===========================================================================

def _rows(enroll):
    p = enroll_learning._capture_path(enroll)
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()
            and json.loads(x).get("kind") == "notegen_edit"]


def test_writer_appends_to_shared_sink_with_ts(tmp_path):
    enroll = str(tmp_path / "enroll")
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    nf.record_notegen_edit_outcome(enrollment_dir=enroll, grounding_flags=None,
                                   draft_original=body, attested_body=body, source_id="enc-1")
    rows = _rows(enroll)
    assert len(rows) == 1
    assert rows[0]["kind"] == "notegen_edit" and rows[0]["ts"]
    assert rows[0]["template_id"] == "soap" and rows[0]["template_version"] == 0
    assert nf.phi_free_violations(rows[0]) == []             # the written row is PHI-free


def test_dormant_is_observable_one_time(tmp_path):
    import structlog
    nf._DORMANT["warned"] = False                            # reset the module latch for the test
    with structlog.testing.capture_logs() as cap:
        nf.record_notegen_edit_outcome(enrollment_dir="", grounding_flags=None,
                                       draft_original="x", attested_body="x", source_id="enc-1")
        nf.record_notegen_edit_outcome(enrollment_dir="", grounding_flags=None,
                                       draft_original="x", attested_body="x", source_id="enc-2")
    dormant = [e for e in cap if e.get("event") == "scribe.notegen_feedback.capture_dormant"]
    assert len(dormant) == 1                                 # ONE-TIME (latched), not per-attest spam
    assert not (tmp_path / "enroll").exists()                # dormant → no sink materialized


def test_capture_error_is_swallowed(tmp_path, monkeypatch):
    def _boom(**k):
        raise RuntimeError("compute exploded")
    monkeypatch.setattr(nf, "compute_notegen_edit_row", _boom)
    import structlog
    with structlog.testing.capture_logs() as cap:
        nf.record_notegen_edit_outcome(enrollment_dir=str(tmp_path / "enroll"), grounding_flags=None,
                                       draft_original="x", attested_body="x", source_id="enc-1")  # no raise
    assert [e for e in cap if e.get("event") == "scribe.notegen_feedback.capture_error"]


# ===========================================================================
# Integration through the real attest() — capture NEVER fails a valid attest
# ===========================================================================

def _make_draft(vault, *, draft_original, body):
    return vault_create(
        vault, "clinical_note", "Synthetic encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": "enc-abc0123456789d", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "encounter_completeness": {"protocol": 1, "complete": True},
            "grounding_flags": [], "draft_original": draft_original,
        },
        body=body, scope="stayc_clinical")["path"]


def test_attest_wires_the_capture(tmp_path):
    enroll = str(tmp_path / "enroll")
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    rel = _make_draft(tmp_path, draft_original=body, body=body)
    attest(tmp_path, rel, new_status="attested", attester="np_jamie", clinician_ids=_CLINICIANS,
           audit_path=tmp_path / "audit.jsonl", now=_NOW, enrollment_dir=enroll)
    assert len(_rows(enroll)) == 1                            # a notegen_edit row landed


def test_attest_capture_error_never_fails_the_attest(tmp_path, monkeypatch):
    def _boom(**k):
        raise RuntimeError("sink exploded")
    monkeypatch.setattr(nf, "record_notegen_edit_outcome", _boom)
    body = _render(subjective=[{"claim": "Chest pain for two days", "source_spans": ["S1"]}])
    rel = _make_draft(tmp_path, draft_original=body, body=body)
    result = attest(tmp_path, rel, new_status="attested", attester="np_jamie",
                    clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
                    enrollment_dir=str(tmp_path / "enroll"))
    assert result and frontmatter.load(str(tmp_path / rel))["status"] == "attested"
