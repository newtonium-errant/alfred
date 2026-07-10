"""Tests for the structural attestation orchestrator (scribe P2-a, #41).

The ONLY sanctioned path to flip a clinical_note's attest triad. These pins
prove the primitives are now STRUCTURAL: the orchestrator enforces
authorize_attestation, writes under the privileged scope, and appends a durable
PHI-free audit — while the agent scope can never raw-flip the triad (pinned in
test_stayc_clinical_scope.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY, source_id_for
from alfred.scribe.attest import attest
from alfred.scribe.attestation import AttestationError
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie", "dr_synthetic"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def _make_ai_draft(tmp_path, *, source_id="sha256:abc0123456789def", drafted_by=SCRIBE_DRAFTER_IDENTITY):
    """Create a born-ai_draft clinical_note under the agent scope; return rel_path."""
    result = vault_create(
        tmp_path, "clinical_note", "Synthetic encounter chest pain",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": source_id, "drafted_by": drafted_by,
        },
        body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope="stayc_clinical",
    )
    return result["path"]


# ---------------------------------------------------------------------------
# source_id — opaque hash in clinical mode
# ---------------------------------------------------------------------------

def test_source_id_hash_in_clinical_mode():
    sid = source_id_for(mode="clinical", filename="patient_jane_doe_2026-07-09.wav")
    assert sid.startswith("sha256:")
    assert "jane" not in sid and "doe" not in sid  # NO PHI-bearing filename leaks
    assert len(sid) == len("sha256:") + 16


def test_source_id_hash_prefers_bytes_in_clinical_mode():
    sid = source_id_for(mode="clinical", filename="x.wav", audio_bytes=b"synthetic-audio")
    assert sid.startswith("sha256:")


def test_source_id_filename_in_synthetic_mode():
    assert source_id_for(mode="synthetic", filename="synth1.wav") == "synth1.wav"
    assert source_id_for(mode="synthetic", filename=None) == "synthetic"


# ---------------------------------------------------------------------------
# Happy path — distinct clinician attests an ai_draft
# ---------------------------------------------------------------------------

def test_attest_happy_path_writes_triad_and_audit(tmp_path):
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    result = attest(
        tmp_path, rel, new_status="attested", attester="np_jamie",
        clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW,
    )
    assert result["path"] == rel
    post = frontmatter.load(str(tmp_path / rel))
    assert post["status"] == "attested"
    assert post["attested_by"] == "np_jamie"
    assert post["attested_at"] == _NOW.isoformat()
    # body unchanged (frozen)
    assert "chest pain" in post.content
    # durable audit written
    assert audit.exists()
    entry = json.loads(audit.read_text().strip())
    assert entry["op"] == "attest"
    assert entry["attester"] == "np_jamie"
    assert entry["from_status"] == "ai_draft"
    assert entry["to_status"] == "attested"


def test_attest_audit_is_phi_free(tmp_path):
    rel = _make_ai_draft(tmp_path, source_id="sha256:deadbeefdeadbeef")
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    raw = audit.read_text()
    # The audit carries the OPAQUE source_id (a hash), clinician id, statuses —
    # NEVER the note body / transcript / patient content.
    assert "sha256:deadbeefdeadbeef" in raw
    assert "chest pain" not in raw
    assert "Subjective" not in raw


# ---------------------------------------------------------------------------
# Self-attest refused — even via the orchestrator (defense-in-depth)
# ---------------------------------------------------------------------------

def test_scribe_self_attest_refused_via_orchestrator(tmp_path):
    # THE self-attest pin (mutation: allow self-attest => fails). The scribe
    # drafter cannot attest its own draft even by calling the orchestrator —
    # authorize_attestation refuses the drafter identity.
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested",
               attester=SCRIBE_DRAFTER_IDENTITY,
               clinician_ids=_CLINICIANS | {SCRIBE_DRAFTER_IDENTITY},
               audit_path=audit, now=_NOW)
    assert exc.value.reason == "scribe_self_attest"
    # note untouched, no audit line
    assert frontmatter.load(str(tmp_path / rel))["status"] == "ai_draft"
    assert not audit.exists()


def test_creator_self_attest_refused(tmp_path):
    # A clinician who was ALSO the drafter (creator) can't self-attest.
    rel = _make_ai_draft(tmp_path, drafted_by="np_jamie")
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "self_attest"


# ---------------------------------------------------------------------------
# Fail-closed refusals
# ---------------------------------------------------------------------------

def test_empty_clinicians_no_valid_attester(tmp_path):
    rel = _make_ai_draft(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=set(), audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "attester_not_clinician"


def test_non_clinician_attester_refused(tmp_path):
    rel = _make_ai_draft(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="random_user",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "attester_not_clinician"


def test_forward_only_no_un_attest_via_orchestrator(tmp_path):
    # Attest to 'attested', then a revert to 'ai_draft' is refused (forward-only).
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="ai_draft", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    assert exc.value.reason == "illegal_status_transition"


def test_attest_refuses_non_clinical_note(tmp_path):
    # A non-clinical_note record can never be attested (unscoped create of a note).
    result = vault_create(tmp_path, "note", "Just a note", set_fields={}, scope=None)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, result["path"], new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "not_clinical_note"
