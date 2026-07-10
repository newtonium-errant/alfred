"""Tests for the scribe pipeline per-source state (scribe P2-d)."""

from __future__ import annotations

import json

from alfred.scribe.state import (
    MAX_ATTEMPTS,
    STATE_DRAFTED,
    STATE_FAILED,
    STATE_RECORDED,
    STATE_REFUSED,
    STATE_STRUCTURING,
    ScribeState,
    SourceState,
)


def test_source_state_from_dict_schema_tolerant():
    st = SourceState.from_dict({
        "source_id": "sha256:abc", "state": "drafted", "note_path": "x.md",
        "attempts": 2, "last_error_class": "STTError", "updated_at": "t",
        "future_field": "ignored",  # unknown key dropped
    })
    assert st.source_id == "sha256:abc" and st.state == "drafted"
    assert st.attempts == 2
    # defaults on missing keys
    d = SourceState.from_dict({"source_id": "s"})
    assert d.state == STATE_RECORDED and d.attempts == 0 and d.note_path == ""


def test_state_save_load_round_trip_atomic(tmp_path):
    p = tmp_path / "scribe_state.json"
    s = ScribeState(p)
    s.set("s1", state=STATE_DRAFTED, note_path="clinical_note/enc.md")
    s.set("s2", state=STATE_FAILED, attempts=1, last_error_class="STTError")
    # persisted + atomic (no .tmp left behind)
    assert p.exists()
    assert not (tmp_path / "scribe_state.json.tmp").exists()
    # reload
    s2 = ScribeState(p)
    s2.load()
    assert s2.get("s1").state == STATE_DRAFTED
    assert s2.get("s1").note_path == "clinical_note/enc.md"
    assert s2.get("s2").state == STATE_FAILED and s2.get("s2").attempts == 1


def test_state_load_tolerates_unknown_and_missing(tmp_path):
    p = tmp_path / "scribe_state.json"
    p.write_text(json.dumps({
        "version": 99,
        "sources": {"s1": {"source_id": "s1", "state": "drafted", "legacy": 1}},
    }), encoding="utf-8")
    s = ScribeState(p)
    s.load()
    assert s.get("s1").state == "drafted"  # unknown 'legacy' dropped


def test_is_done_gate():
    p = "/tmp/does-not-matter"
    s = ScribeState(p)
    # absent → not done
    assert s.is_done("nope") is False
    # drafted / attested / refused → done (never reprocess)
    s.sources["d"] = SourceState("d", state=STATE_DRAFTED)
    s.sources["r"] = SourceState("r", state=STATE_REFUSED)
    assert s.is_done("d") is True
    assert s.is_done("r") is True
    # intermediate → NOT done (resume re-runs)
    s.sources["mid"] = SourceState("mid", state=STATE_STRUCTURING)
    assert s.is_done("mid") is False
    # failed under the cap → retriable; at the cap → terminal
    s.sources["f1"] = SourceState("f1", state=STATE_FAILED, attempts=MAX_ATTEMPTS - 1)
    s.sources["f2"] = SourceState("f2", state=STATE_FAILED, attempts=MAX_ATTEMPTS)
    assert s.is_done("f1") is False
    assert s.is_done("f2") is True


def test_state_field_is_phi_free_by_shape():
    # The SourceState shape carries NO free-text — only ids, a state enum, a
    # derived path, an int, and an exception CLASS name. NOTE-4 by construction.
    fields = set(SourceState.__dataclass_fields__)
    assert fields == {
        "source_id", "state", "note_path", "attempts", "last_error_class", "updated_at",
    }
