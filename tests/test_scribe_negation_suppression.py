"""#26 negation-paraphrase self-correcting loop — Phase 1 CAPTURE contract tests.

Phase 1 ships ONLY the writers (mirrors enroll_learning). The capture is SPLIT:

  * RENDER-time (1a) — the PHI-BEARING concept-pair spool, re-derived from grounding's
    (B) path so grounding.py stays byte-identical.
  * ATTEST-time (1b) — the PHI-FREE ``kept`` boolean, the THIRD twin beside the
    inferred-dx / speaker attest captures.

These pins fix the Phase-1 contract: the render pair lands for a lexically-disjoint
paraphrase; the attest kept boolean lands side-effect-free; grounding is UNCHANGED (the
empagliflozin fixture STILL flags — Phase 1 adds NO suppression); the two sinks are
PHI-posture-correct, share join keys, and carry the retention keys (source_id + ts).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import frontmatter
import pytest
import structlog

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY, load_from_unified
from alfred.scribe import negation_suppression as ns
from alfred.scribe.attest import attest
from alfred.scribe.grounding import verify as verify_grounding
from alfred.scribe.negation_suppression import (
    KIND_ATTEST_OUTCOME,
    KIND_CANDIDATE,
    NEGATION_MISMATCH_REASON,
    _attest_outcomes_file,
    _candidates_file,
    capture_render_candidates,
    record_negation_attest_outcome,
    resolve_candidates_dir,
)
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.transcript import Segment, Transcript
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie"}
_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


# --- harness (mirrors tests/test_scribe_notegen.py) -------------------------

def _transcript(*texts, source_id="synth-1"):
    segs = [
        Segment(id=f"S{i+1}", start_s=float(i * 5), end_s=float(i * 5 + 5), text=t)
        for i, t in enumerate(texts)
    ]
    return Transcript(source_id=source_id, mode="synthetic", segments=segs)


def _structured(**sections):
    return StructuredNote.from_dict(sections)


def _rows(path):
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _candidate_rows(candidates_dir):
    return _rows(_candidates_file(candidates_dir))


def _outcome_rows(candidates_dir):
    return _rows(_attest_outcomes_file(candidates_dir))


# The #26 canonical case — reproduced verbatim from the eval fixture
# drug_switch_empagliflozin (case 2) and the pin at
# tests/test_scribe_notegen.py:356-370.
_EMPAGLIFLOZIN_CITE = "Your sugars haven't come down the way I'd hoped on the metformin."
_EMPAGLIFLOZIN_CLAIM = "Blood sugars not adequately controlled on metformin"


# ===========================================================================
# RENDER-time capture (1a) — the PHI-bearing concept-pair
# ===========================================================================

def test_render_captures_disjoint_paraphrase_pair(tmp_path):
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-empa")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-empa")

    rows = _candidate_rows(d)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == KIND_CANDIDATE
    assert r["source_id"] == "enc-empa"
    assert r["section"] == "objective" and r["claim_index"] == 0
    assert r["reason"] == NEGATION_MISMATCH_REASON
    assert r["disposition"] == "pending"
    # The concept-SET pair — exactly grounding's (B)-path derivation.
    assert len(r["claim_concepts"]) == 1
    assert set(r["claim_concepts"][0]) == {"adequately", "controlled", "metformin"}
    assert len(r["cite_concepts"]) == 1
    assert set(r["cite_concepts"][0]) == {"come", "down", "way", "i'd", "hoped", "metformin"}


def test_render_capture_aligns_with_verify_flag(tmp_path):
    # The capture fires EXACTLY when verify() mints a negation_mismatch flag on the
    # same input — grounding is UNCHANGED, the capture just re-derives its (B) path.
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-a")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == NEGATION_MISMATCH_REASON  # grounding flags
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-a")
    assert len(_candidate_rows(d)) == 1                                    # capture fires


def test_render_no_capture_when_no_negation(tmp_path):
    d = tmp_path / "scribe"
    t = _transcript("Blood pressure was 120 over 80 today.", source_id="enc-b")
    s = _structured(objective=[{"claim": "BP 120/80", "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-b")
    assert _candidate_rows(d) == []                    # no negation → no candidate


def test_render_no_capture_when_negation_grounded(tmp_path):
    # A faithful paraphrase whose concept IS a subset of the cite negation → verify
    # clears it → NO ungrounded negation → NO candidate (nothing to learn).
    d = tmp_path / "scribe"
    t = _transcript(
        "But my weight's the same and I haven't noticed any neck swelling.",
        source_id="enc-c",
    )
    s = _structured(subjective=[{"claim": "No neck swelling", "source_spans": ["S1"]}])
    assert verify_grounding(s, t).clean is True         # grounding clears it
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-c")
    assert _candidate_rows(d) == []


def test_render_no_capture_when_cite_has_no_negation(tmp_path):
    # An INVENTED negation (the cite negates nothing) — verify STILL flags, but there
    # is no cite concept to pair against, so NO candidate is spooled (suppressing it
    # would be wrong; that flag is not a paraphrase, it's a real fabrication).
    d = tmp_path / "scribe"
    t = _transcript("Patient reports chest pain radiating to the arm.", source_id="enc-d")
    s = _structured(subjective=[{"claim": "Denies chest pain", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == NEGATION_MISMATCH_REASON  # verify flags
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-d")
    assert _candidate_rows(d) == []                     # but no pair → no candidate


def test_render_capture_holds_concept_sets_never_raw_text(tmp_path):
    # PHI posture: the spool holds concept-SETS, NEVER the raw claim / cite sentences.
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-phi")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-phi")
    blob = _candidates_file(d).read_text(encoding="utf-8")
    assert _EMPAGLIFLOZIN_CLAIM not in blob             # no raw claim sentence
    assert "come down the way" not in blob              # no raw cite sentence
    r = _candidate_rows(d)[0]
    assert "claim" not in r and "cite" not in r         # no free-text claim/cite fields


def test_render_capture_sink_perms_0600_dir_0700(tmp_path):
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-perm")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-perm")
    assert (os.stat(_candidates_file(d)).st_mode & 0o777) == 0o600
    assert (os.stat(d).st_mode & 0o777) == 0o700


def test_render_capture_dormant_dir_no_write(tmp_path):
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-dorm")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir="", source_id="enc-dorm")
    assert not (tmp_path / "scribe").exists()           # dormant → nothing materialized


def test_render_capture_fail_silent(tmp_path, monkeypatch):
    # A capture bug must NEVER affect the rendered note. Force the sink writer to raise
    # → the call SWALLOWS it (no propagation) and emits the SWALLOWED warning.
    def _boom(*a, **k):
        raise RuntimeError("sink exploded")
    monkeypatch.setattr(ns, "_append_row", _boom)
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-fail")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    with structlog.testing.capture_logs() as cap:
        capture_render_candidates(s, t, candidates_dir=d, source_id="enc-fail")  # no raise
    errs = [e for e in cap if e.get("event") == "scribe.negation_suppression.render_capture_error"]
    assert len(errs) == 1 and errs[0]["source_id"] == "enc-fail"


def test_render_capture_emits_count_log(tmp_path):
    # Log-emission pin (standing discipline #9): the capture MUST drive the grep-able
    # candidates_captured signal with the true count.
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-log")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    with structlog.testing.capture_logs() as cap:
        capture_render_candidates(s, t, candidates_dir=d, source_id="enc-log")
    hits = [e for e in cap if e.get("event") == "scribe.negation_suppression.candidates_captured"]
    assert len(hits) == 1
    assert hits[0]["count"] == 1 and hits[0]["source_id"] == "enc-log"


def test_render_capture_no_log_when_nothing_captured(tmp_path):
    # Symmetric to the count pin: a note with no paraphrase candidate emits NO
    # candidates_captured line (the every-render heartbeat is flags_finalized, not this).
    d = tmp_path / "scribe"
    t = _transcript("Blood pressure was 120 over 80.", source_id="enc-q")
    s = _structured(objective=[{"claim": "BP 120/80", "source_spans": ["S1"]}])
    with structlog.testing.capture_logs() as cap:
        capture_render_candidates(s, t, candidates_dir=d, source_id="enc-q")
    assert [e for e in cap if e.get("event") == "scribe.negation_suppression.candidates_captured"] == []


# ===========================================================================
# grounding UNCHANGED — the Phase-1 regression pin (adds NO suppression)
# ===========================================================================

def test_grounding_unchanged_empagliflozin_still_flags():
    # Phase 1 adds ZERO suppression: the lexically-disjoint paraphrase STILL flags
    # (the mechanism flips only in Phase 2+, on an operator-approved pair). Mirrors the
    # canonical pin at tests/test_scribe_notegen.py:356-370, self-contained here as the
    # "capture-only, no behavior change" guard.
    t = _transcript(_EMPAGLIFLOZIN_CITE)
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == NEGATION_MISMATCH_REASON


# ===========================================================================
# ATTEST-time capture (1b) — the PHI-free kept boolean
# ===========================================================================

def test_attest_outcome_kept_true_when_claim_survives(tmp_path):
    d = tmp_path / "scribe"
    record_negation_attest_outcome(
        d,
        grounding_flags=[{
            "reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
            "section": "objective", "claim_index": 0,
        }],
        attested_body="## Objective\n- " + _EMPAGLIFLOZIN_CLAIM + " [S1]\n",
        source_id="enc-keep",
    )
    rows = _outcome_rows(d)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == KIND_ATTEST_OUTCOME
    assert r["kept"] is True                              # claim survived → implicit 'faithful'
    assert r["reason"] == NEGATION_MISMATCH_REASON
    assert r["source_id"] == "enc-keep"
    assert r["section"] == "objective" and r["claim_index"] == 0


def test_attest_outcome_kept_false_when_claim_removed(tmp_path):
    d = tmp_path / "scribe"
    record_negation_attest_outcome(
        d,
        grounding_flags=[{
            "reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
            "section": "objective", "claim_index": 0,
        }],
        attested_body="## Objective\n- Sugars improving on metformin [S1]\n",
        source_id="enc-drop",
    )
    assert _outcome_rows(d)[0]["kept"] is False           # clinician edited it out → flag was right


def test_attest_outcome_is_phi_free(tmp_path):
    d = tmp_path / "scribe"
    record_negation_attest_outcome(
        d,
        grounding_flags=[{
            "reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
            "section": "objective", "claim_index": 0,
        }],
        attested_body="- " + _EMPAGLIFLOZIN_CLAIM, source_id="enc-free",
    )
    blob = _attest_outcomes_file(d).read_text(encoding="utf-8")
    assert _EMPAGLIFLOZIN_CLAIM not in blob               # no raw claim text
    r = _outcome_rows(d)[0]
    assert "claim" not in r and "claim_concepts" not in r and "cite_concepts" not in r


def test_attest_outcome_only_negation_flags(tmp_path):
    d = tmp_path / "scribe"
    record_negation_attest_outcome(
        d,
        grounding_flags=[
            {"reason": NEGATION_MISMATCH_REASON, "claim": "x", "section": "s", "claim_index": 0},
            {"reason": "inferred_diagnosis", "claim": "has MDD", "section": "a", "claim_index": 1},
            {"reason": "number_mismatch", "claim": "5mg", "section": "p", "claim_index": 2},
        ],
        attested_body="x", source_id="enc-mix",
    )
    rows = _outcome_rows(d)
    assert len(rows) == 1 and rows[0]["reason"] == NEGATION_MISMATCH_REASON


def test_attest_outcome_non_list_flags_noop(tmp_path):
    d = tmp_path / "scribe"
    record_negation_attest_outcome(d, grounding_flags=None, attested_body="x", source_id="enc-n")
    assert _outcome_rows(d) == []


def test_attest_outcome_dormant_dir_no_write(tmp_path):
    record_negation_attest_outcome(
        "",
        grounding_flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": "x", "section": "s", "claim_index": 0}],
        attested_body="x", source_id="enc-dorm",
    )
    assert not (tmp_path / "scribe").exists()


def test_attest_outcome_fail_silent(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("sink exploded")
    monkeypatch.setattr(ns, "_append_row", _boom)
    d = tmp_path / "scribe"
    with structlog.testing.capture_logs() as cap:
        record_negation_attest_outcome(
            d,
            grounding_flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": "x", "section": "s", "claim_index": 0}],
            attested_body="x", source_id="enc-fs",
        )  # no raise
    errs = [e for e in cap if e.get("event") == "scribe.negation_suppression.attest_capture_error"]
    assert len(errs) == 1


# ===========================================================================
# join keys + retention shape (design the sinks for Phase 3)
# ===========================================================================

def test_two_sinks_share_join_keys(tmp_path):
    # The Phase-3 join is candidate ⋈ attest_outcome on (source_id, section, claim_index).
    # Both sides must emit those keys for the SAME claim.
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-join")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-join")
    record_negation_attest_outcome(
        d,
        grounding_flags=[{
            "reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
            "section": "objective", "claim_index": 0,
        }],
        attested_body="- " + _EMPAGLIFLOZIN_CLAIM, source_id="enc-join",
    )
    c = _candidate_rows(d)[0]
    o = _outcome_rows(d)[0]
    key = lambda r: (r["source_id"], r["section"], r["claim_index"])
    assert key(c) == key(o) == ("enc-join", "objective", 0)


def test_every_row_carries_retention_keys(tmp_path):
    # Retention shape (Phase 3 wires the prune): destroy-with-encounter needs source_id;
    # the unreviewed age-cap needs ts. Every row on both sinks must carry both.
    d = tmp_path / "scribe"
    t = _transcript(_EMPAGLIFLOZIN_CITE, source_id="enc-ret")
    s = _structured(objective=[{"claim": _EMPAGLIFLOZIN_CLAIM, "source_spans": ["S1"]}])
    capture_render_candidates(s, t, candidates_dir=d, source_id="enc-ret")
    record_negation_attest_outcome(
        d,
        grounding_flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
                          "section": "objective", "claim_index": 0}],
        attested_body="- " + _EMPAGLIFLOZIN_CLAIM, source_id="enc-ret",
    )
    for row in _candidate_rows(d) + _outcome_rows(d):
        assert row.get("source_id") and row.get("ts")


def test_resolve_candidates_dir_derives_from_input_dir(tmp_path):
    cfg = load_from_unified({"scribe": {"input_dir": str(tmp_path / "inbox")}})
    assert resolve_candidates_dir(cfg) == tmp_path / "scribe"


# ===========================================================================
# integration through the real attest() path + CLI
# ===========================================================================

@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _make_draft(vault, *, flags, body):
    return vault_create(
        vault, "clinical_note", "Synthetic encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": "enc-abc0123456789d", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "encounter_completeness": {"protocol": 1, "complete": True},
            "grounding_flags": flags,
        },
        body=body, scope="stayc_clinical",
    )["path"]


def test_attest_wires_the_negation_capture(tmp_path):
    d = tmp_path / "scribe"
    rel = _make_draft(
        tmp_path,
        flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
                "section": "objective", "claim_index": 0}],
        body="## Objective\n- " + _EMPAGLIFLOZIN_CLAIM + " [S1]\n",
    )
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
           negation_candidates_dir=d)
    rows = _outcome_rows(d)
    assert len(rows) == 1 and rows[0]["reason"] == NEGATION_MISMATCH_REASON
    assert rows[0]["kept"] is True                        # the flagged claim survived into the signed body


def test_attest_capture_error_never_fails_the_attest(tmp_path, monkeypatch):
    # A capture bug must NEVER fail a valid attest (medico-legal path). Force the twin to
    # raise → attest STILL succeeds + writes the triad.
    def _boom(*a, **k):
        raise RuntimeError("sink exploded")
    monkeypatch.setattr(ns, "record_negation_attest_outcome", _boom)
    d = tmp_path / "scribe"
    rel = _make_draft(
        tmp_path,
        flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": "x", "section": "s", "claim_index": 0}],
        body="## Subjective\n- x [S1]\n",
    )
    result = attest(tmp_path, rel, new_status="attested", attester="np_jamie",
                    clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
                    negation_candidates_dir=d)
    assert result
    assert frontmatter.load(str(tmp_path / rel))["status"] == "attested"


def test_attest_no_dir_no_capture(tmp_path):
    rel = _make_draft(
        tmp_path,
        flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": "x", "section": "s", "claim_index": 0}],
        body="## Subjective\n- x [S1]\n",
    )
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
           negation_candidates_dir="")
    assert not (tmp_path / "scribe").exists()             # dormant → no sink materialized


def test_attest_CLI_threads_negation_candidates_dir(tmp_path):
    # cmd_scribe is the ONLY production caller of attest(); the whole loop rides a single
    # negation_candidates_dir kwarg. Because the capture is fail-silent BY DESIGN, a dropped
    # kwarg produces ZERO runtime signal — the loop just stops accumulating. Drive the REAL
    # CLI path and assert a row lands.
    import yaml
    from alfred.cli import build_parser, cmd_scribe

    vault = tmp_path / "vault"
    rel = _make_draft(
        vault,
        flags=[{"reason": NEGATION_MISMATCH_REASON, "claim": _EMPAGLIFLOZIN_CLAIM,
                "section": "objective", "claim_index": 0}],
        body="## Objective\n- " + _EMPAGLIFLOZIN_CLAIM + " [S1]\n",
    )
    cfg = {
        "vault": {"path": str(vault)},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "input_dir": str(tmp_path / "inbox"),   # → spool dir = <tmp>/scribe/
            "encounter_salt": "DUMMY_SCRIBE_TEST_SALT",
            "stt": {"provider": "fake"},
            "clinicians": ["np_jamie"],
            "diarize": {"provider": "fake"},
        },
    }
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    args = build_parser().parse_args(
        ["--config", str(config), "scribe", "attest", rel, "--attester", "np_jamie"])
    cmd_scribe(args)

    rows = _outcome_rows(tmp_path / "scribe")
    assert len(rows) == 1 and rows[0]["reason"] == NEGATION_MISMATCH_REASON
