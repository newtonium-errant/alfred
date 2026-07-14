"""P4-5 attest_outcome capture hook — contract tests (self-correcting Part-1, speaker).

The TWIN of the inferred-dx attest capture: at attest, record per speaker-attribution
flag whether the flagged claim SURVIVED into the attested body (kept = normalized
substring — the correction vehicle). READ-ONLY + fail-silent: a capture bug must NEVER
fail a valid, medico-legal attestation. PHI-free (preset_id/enum/bool only).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe import enroll_learning
from alfred.scribe.attest import _capture_speaker_attest_outcome, attest
from alfred.scribe.inferred_dx import INFERRED_DIAGNOSIS_REASON
from alfred.scribe.speaker_attribution import (
    ATTRIBUTION_UNVERIFIED_REASON,
    SPEAKER_MISMATCH_REASON,
    SPEAKER_UNVERIFIED_REASON,
)
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
_PROV = {
    "user": "np_jamie", "preset_id": "pst-1720000000000-0123456789abcdef",
    "centroid_version": 1, "engine_fingerprint": {"embedding_model": "fake-embed-v1"},
}


def _attest_rows(enroll_dir):
    p = enroll_learning._capture_path(enroll_dir)
    if not p.is_file():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("kind") == "attest_outcome"]


# --- direct unit tests of the capture helper --------------------------------

def test_captures_speaker_flags_only_with_provenance(tmp_path):
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll,
        grounding_flags=[
            {"reason": SPEAKER_MISMATCH_REASON, "claim": "BP was 120 over 80", "section": "objective"},
            {"reason": INFERRED_DIAGNOSIS_REASON, "claim": "has MDD", "detail": "mdd"},  # NON-speaker → skip
        ],
        diarize_provenance=_PROV,
        attested_body="## Objective\nBP was 120 over 80 today.\n", source_id="enc-x",
    )
    rows = _attest_rows(enroll)
    assert len(rows) == 1                                  # only the speaker flag captured
    r = rows[0]
    assert r["reason"] == SPEAKER_MISMATCH_REASON
    assert r["preset_id"] == _PROV["preset_id"] and r["user"] == "np_jamie"
    assert r["centroid_version"] == 1
    assert r["kept"] is True and r["is_banner"] is False   # claim present in the body


def test_kept_false_when_claim_removed(tmp_path):
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll,
        grounding_flags=[{"reason": SPEAKER_UNVERIFIED_REASON, "claim": "radiating to the left arm"}],
        diarize_provenance=_PROV, attested_body="## Objective\nNormal exam.\n", source_id="enc-y",
    )
    assert _attest_rows(enroll)[0]["kept"] is False        # claim absent → clinician removed it


def test_banner_row_flagged(tmp_path):
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll,
        grounding_flags=[{"reason": ATTRIBUTION_UNVERIFIED_REASON, "claim": "", "section": "note"}],
        diarize_provenance=_PROV, attested_body="anything", source_id="enc-z",
    )
    r = _attest_rows(enroll)[0]
    assert r["is_banner"] is True and r["reason"] == ATTRIBUTION_UNVERIFIED_REASON


def test_null_provenance_still_records_the_flag(tmp_path):
    # A speaker flag can fire on an UN-anchored encounter (no preset) — still capture the
    # reason, with null preset provenance.
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll,
        grounding_flags=[{"reason": SPEAKER_UNVERIFIED_REASON, "claim": "x"}],
        diarize_provenance=None, attested_body="x", source_id="enc-p",
    )
    r = _attest_rows(enroll)[0]
    assert r["preset_id"] is None and r["reason"] == SPEAKER_UNVERIFIED_REASON


def test_non_list_flags_is_noop(tmp_path):
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll, grounding_flags=None, diarize_provenance=_PROV,
        attested_body="x", source_id="enc-n",
    )
    assert _attest_rows(enroll) == []


def test_no_speaker_flags_lands_no_rows(tmp_path):
    enroll = str(tmp_path / "enroll")
    _capture_speaker_attest_outcome(
        enrollment_dir=enroll,
        grounding_flags=[{"reason": INFERRED_DIAGNOSIS_REASON, "claim": "x", "detail": "y"}],
        diarize_provenance=_PROV, attested_body="x", source_id="enc-none",
    )
    assert _attest_rows(enroll) == []                      # intentionally-left-blank: no speaker flags


# --- integration through the real attest() path -----------------------------

@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _make_draft(vault, *, flags, provenance):
    return vault_create(
        vault, "clinical_note", "Synthetic encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": "enc-abc0123456789d", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "encounter_completeness": {"protocol": 1, "complete": True},
            "grounding_flags": flags, "diarize_provenance": provenance,
        },
        body="## Objective\nBP was 120 over 80.\n", scope="stayc_clinical",
    )["path"]


def test_attest_wires_the_capture(tmp_path):
    enroll = str(tmp_path / "enroll")
    rel = _make_draft(
        tmp_path,
        flags=[{"reason": SPEAKER_MISMATCH_REASON, "claim": "BP was 120 over 80", "section": "objective"}],
        provenance=_PROV,
    )
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
           enrollment_dir=enroll)
    rows = _attest_rows(enroll)
    assert len(rows) == 1 and rows[0]["reason"] == SPEAKER_MISMATCH_REASON
    assert rows[0]["kept"] is True                         # the BP claim survived into the signed body


def test_attest_capture_error_never_fails_the_attest(tmp_path, monkeypatch):
    # A capture bug must NEVER fail a valid attest (medico-legal path). Force the sink
    # writer to raise → attest STILL succeeds + writes the triad.
    def _boom(*a, **k):
        raise RuntimeError("sink exploded")
    monkeypatch.setattr(enroll_learning, "record_attest_outcome", _boom)
    enroll = str(tmp_path / "enroll")
    rel = _make_draft(
        tmp_path, flags=[{"reason": SPEAKER_MISMATCH_REASON, "claim": "x"}], provenance=_PROV,
    )
    result = attest(tmp_path, rel, new_status="attested", attester="np_jamie",
                    clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
                    enrollment_dir=enroll)
    assert result                                          # attest succeeded despite the crash
    assert frontmatter.load(str(tmp_path / rel))["status"] == "attested"


def test_attest_no_enrollment_dir_no_capture(tmp_path):
    rel = _make_draft(
        tmp_path, flags=[{"reason": SPEAKER_MISMATCH_REASON, "claim": "x"}], provenance=_PROV,
    )
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "audit.jsonl", now=_NOW,
           enrollment_dir="")
    assert not (tmp_path / "enroll").exists()              # dormant → no sink materialized
