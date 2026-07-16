"""P4-5a capture writers — diarize_stats + attest_outcome + audit (PHI-free, fail-silent)."""

from __future__ import annotations

import json
import os
import stat

import structlog

from alfred.scribe import enroll_learning as el


def _read(path):
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_diarize_stats_appends_phi_free_row(tmp_path):
    el.record_diarize_stats(
        tmp_path, source_id="enc-abc", chunk_seq=1, user="np_jamie", preset_id="pst-x",
        centroid_version=1, engine_fingerprint={"engine_version": "fake-1"},
        n_segments=4, role_counts={"clinician": 2, "patient": 1, "unknown": 1},
        best_cosine=0.82, separation=0.2, min_purity=0.9, fail_closed_demotions=1,
    )
    rows = _read(tmp_path / "learning" / "attest_capture.jsonl")
    assert len(rows) == 1 and rows[0]["kind"] == "diarize_stats"
    assert rows[0]["preset_id"] == "pst-x" and rows[0]["role_counts"]["clinician"] == 2
    # PHI-free: no name/label/text/transcript keys anywhere in the row
    blob = json.dumps(rows[0]).lower()
    assert "name" not in rows[0] and "label" not in rows[0] and "transcript" not in blob


def test_no_preset_row_still_lands(tmp_path):
    # intentionally-left-blank: a no-preset encounter still records a row (preset_id null).
    el.record_diarize_stats(
        tmp_path, source_id="enc-x", chunk_seq=None, user=None, preset_id=None,
        centroid_version=None, engine_fingerprint=None, n_segments=3,
        role_counts={"unknown": 3}, best_cosine=None, separation=None,
        min_purity=None, fail_closed_demotions=0,
    )
    rows = _read(tmp_path / "learning" / "attest_capture.jsonl")
    assert len(rows) == 1 and rows[0]["preset_id"] is None and rows[0]["user"] is None


def test_diarize_stats_row_carries_extractor_marker(tmp_path):
    # P4-5c 5b DISCRIMINATOR: a real-extraction row stamps the extractor version marker so
    # the health filter can tell it from a pre-P4-5c placeholder-era all-unknown row (both
    # can carry best_cosine=0.0 / diarized=true / a real fingerprint — only the marker
    # separates 'extractor wired' from 'placeholder era').
    el.record_diarize_stats(
        tmp_path, source_id="enc-jamie", chunk_seq=1, user="np_jamie", preset_id="pst-x",
        centroid_version=1, engine_fingerprint={"embedding_model": "pyannote/wespeaker"},
        n_segments=2, role_counts={"clinician": 1, "unknown": 1},
        best_cosine=0.0, separation=0.0, min_purity=None, fail_closed_demotions=1,
        extractor="p4-5c", diarized=True,
    )
    rows = _read(tmp_path / "learning" / "attest_capture.jsonl")
    assert len(rows) == 1 and rows[0]["extractor"] == "p4-5c"
    # even a real NO-MATCH (best_cosine 0.0) carries the marker — it means 'extractor ran',
    # NOT 'match succeeded'.
    assert rows[0]["best_cosine"] == 0.0 and rows[0]["diarized"] is True


def test_diarize_stats_placeholder_era_row_has_null_extractor(tmp_path):
    # A row written WITHOUT the marker (a pre-P4-5c placeholder-era row, or any caller not
    # passing it) records extractor=None — the absent-marker POISON signal the 5b filter
    # keys on. Pins the schema field is always present (JSONL forward-tolerance) + defaults null.
    el.record_diarize_stats(
        tmp_path, source_id="enc-old", chunk_seq=1, user="np_jamie", preset_id="pst-x",
        centroid_version=1, engine_fingerprint={"embedding_model": "pyannote/wespeaker"},
        n_segments=2, role_counts={"unknown": 2}, best_cosine=0.0, separation=0.0,
        min_purity=None, fail_closed_demotions=2, diarized=True,
    )
    rows = _read(tmp_path / "learning" / "attest_capture.jsonl")
    assert len(rows) == 1 and "extractor" in rows[0] and rows[0]["extractor"] is None


def test_attest_outcome_appends(tmp_path):
    el.record_attest_outcome(tmp_path, source_id="enc-a", user="np_jamie",
                             preset_id="pst-y", centroid_version=2,
                             reason="speaker_mismatch", kept=False)
    el.record_attest_outcome(tmp_path, source_id="enc-a", user="np_jamie",
                             preset_id="pst-y", centroid_version=2,
                             reason="attribution_unverified", kept=True, is_banner=True)
    rows = _read(tmp_path / "learning" / "attest_capture.jsonl")
    assert len(rows) == 2                                    # append-only
    assert rows[0]["kind"] == "attest_outcome" and rows[0]["kept"] is False
    assert rows[1]["is_banner"] is True


def test_audit_is_preset_id_only(tmp_path):
    el.audit(tmp_path, "preset_created", preset_id="pst-z", user="np_jamie")
    rows = _read(tmp_path / "audit.log")
    assert len(rows) == 1 and rows[0]["event"] == "preset_created"
    assert rows[0]["preset_id"] == "pst-z"
    assert "name" not in rows[0]                             # NEVER a name/label


def test_capture_and_audit_are_separate_files(tmp_path):
    el.record_diarize_stats(tmp_path, source_id="e", chunk_seq=1, user=None, preset_id=None,
                            centroid_version=None, engine_fingerprint=None, n_segments=1,
                            role_counts={}, best_cosine=None, separation=None,
                            min_purity=None, fail_closed_demotions=0)
    el.audit(tmp_path, "preset_selected", preset_id="pst-1")
    assert (tmp_path / "learning" / "attest_capture.jsonl").is_file()
    assert (tmp_path / "audit.log").is_file()
    assert stat.S_IMODE(os.stat(tmp_path / "audit.log").st_mode) == 0o600


def test_capture_fail_silent(tmp_path):
    # enrollment_dir is a FILE → the learning subpath can't be created → the writer
    # must SWALLOW (never propagate to the pipeline), logging a capture_error.
    bad = tmp_path / "afile"
    bad.write_text("x")
    with structlog.testing.capture_logs() as caps:
        el.record_diarize_stats(bad, source_id="e", chunk_seq=1, user=None, preset_id=None,
                                centroid_version=None, engine_fingerprint=None, n_segments=1,
                                role_counts={}, best_cosine=None, separation=None,
                                min_purity=None, fail_closed_demotions=0)   # must NOT raise
    assert any(c.get("event") == "scribe.enroll_learning.capture_error" for c in caps)
