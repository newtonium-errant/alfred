"""Checkpoint-accumulator foundation tests (scribe P3-b1).

Deterministic — the fake STT backend + pure primitives (append / verify /
identity / ledger) mean NO qwen, NO real model. Covers the six frozen
adversarial-design decisions:

  1. salted, opaque encounter_id (raw label never in id / logs / ledger name;
     salt-missing → fail-loud);
  2. input convention + sweep walk (subdirs AND legacy flat; integer seq order,
     not lexicographic; seq-gap freeze);
  3. transcript ledger (persist + schema-tolerant resume round-trip);
  4. append_chunk fold (continuity, idempotent-on-hash, global-monotonic
     offset, provenance, + the invariant-assert MUTATION-BIND);
  5. grounding dup-id refusal (the MUTATION-BIND against silent mis-grounding);
  6. settle-gate (holds an unsettled file).
"""

from __future__ import annotations

import json

import pytest
import structlog

import alfred.scribe.transcript as tmod
from alfred.scribe import (
    AccumResult,
    EncounterIdentityError,
    GroundingIntegrityError,
    Segment,
    SegmentInvariantError,
    Transcript,
    accumulate_encounter,
    compute_encounter_id,
    is_chunk_settled,
    ledger_path,
    load_ledger,
    parse_structured_json,
    save_ledger,
    verify_grounding,
)
from alfred.scribe.config import load_from_unified

# Obviously-fake test salt (NOT a real-provider-shaped secret).
_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(mode="synthetic", salt=_SALT):
    return load_from_unified({"scribe": {
        "mode": mode,
        "encounter_salt": salt,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _write_chunk(enc_dir, seq, lines, *, meta=True, ext=".wav", pad=3):
    """Write ``chunk_<seq>.<ext>`` + fake-STT ``.txt`` sidecar + optional
    ``.meta.json`` commit marker. Distinct audio bytes per seq so content-hashes
    differ (else two chunks would collapse as an idempotent no-op)."""
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:0{pad}d}" if pad else f"chunk_{seq}"
    (enc_dir / f"{name}{ext}").write_bytes(f"audio-bytes-seq-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if meta:
        (enc_dir / f"{name}.meta.json").write_text(
            json.dumps({"synthetic": True, "seq": seq}), encoding="utf-8"
        )
    return enc_dir / f"{name}{ext}"


# ---------------------------------------------------------------------------
# 1. Salted, opaque encounter_id
# ---------------------------------------------------------------------------

def test_encounter_id_salted_opaque_no_label_leak():
    eid = compute_encounter_id("patient_jane_doe", salt=_SALT)
    assert eid.startswith("enc-") and len(eid) == len("enc-") + 16
    assert "jane" not in eid and "doe" not in eid   # the raw label never appears
    assert ":" not in eid                            # colon-free (no filename bug)


def test_encounter_id_deterministic_and_salt_sensitive():
    a = compute_encounter_id("enc-A", salt=_SALT)
    assert a == compute_encounter_id("enc-A", salt=_SALT)          # stable
    assert a != compute_encounter_id("enc-A", salt="OTHER_SALT")   # salt is the point
    assert a != compute_encounter_id("enc-B", salt=_SALT)          # label-sensitive


@pytest.mark.parametrize("bad_salt", ["", "   ", None])
def test_encounter_id_fail_loud_on_missing_salt(bad_salt):
    with pytest.raises(EncounterIdentityError):
        compute_encounter_id("enc-A", salt=bad_salt)


def test_encounter_id_fail_loud_on_empty_label():
    with pytest.raises(EncounterIdentityError):
        compute_encounter_id("  ", salt=_SALT)


def test_accumulate_logs_and_ledger_are_opaque(tmp_path):
    # A PHI-ish encounter dir name must NOT leak into logs OR the ledger filename.
    enc = tmp_path / "inbox" / "patient_jane_doe"
    _write_chunk(enc, 1, ["Reports chest pain."])
    with structlog.testing.capture_logs() as caps:
        accumulate_encounter(enc, config=_config())
    eid = compute_encounter_id("patient_jane_doe", salt=_SALT)
    assert ledger_path(enc, eid).is_file()                 # named by the opaque id
    assert not any("jane" in str(v) for c in caps for v in c.values())
    folded = [c for c in caps if c.get("event") == "scribe.accumulator.folded"]
    assert len(folded) == 1 and folded[0]["encounter_id"] == eid


# ---------------------------------------------------------------------------
# 4. append_chunk fold — continuity / idempotent / offset / provenance
# ---------------------------------------------------------------------------

def test_append_chunk_continuity_offset_provenance():
    t = Transcript(source_id="enc-x", mode="synthetic")
    c1 = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=5.0, text="a"),
        Segment(id="S2", start_s=5.0, end_s=10.0, text="b"),
    ])
    c2 = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=5.0, text="c"),  # chunk-local id DISCARDED
    ])
    assert t.append_chunk(c1, audio_offset_s=0.0, chunk_key="h1", seq=1) is True
    assert t.append_chunk(c2, audio_offset_s=10.0, chunk_key="h2", seq=2) is True
    # chunk-2's id CONTINUES chunk-1 (S3, not a reset to S1); timestamps offset.
    assert [s.id for s in t.segments] == ["S1", "S2", "S3"]
    assert t.segments[2].start_s == 10.0 and t.segments[2].end_s == 15.0
    assert t.segments[2].text == "c"
    # provenance records chunk_key + seq + id-range.
    assert t.chunk_provenance[1] == {
        "chunk_key": "h2", "seq": 2,
        "first_id": "S3", "last_id": "S3", "n_segments": 1,
    }


def test_append_chunk_idempotent_on_chunk_key():
    t = Transcript(source_id="enc-x", mode="synthetic")
    c = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=5.0, text="a"),
    ])
    assert t.append_chunk(c, audio_offset_s=0.0, chunk_key="h1", seq=1) is True
    # replay of the SAME content-hash → NO-OP (not a second append).
    assert t.append_chunk(c, audio_offset_s=0.0, chunk_key="h1", seq=1) is False
    assert len(t.segments) == 1 and len(t.chunk_provenance) == 1


def test_prior_segments_immutable_across_appends():
    t = Transcript(source_id="enc-x", mode="synthetic")
    t.append_chunk(
        Transcript(source_id="enc-x", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="first")]),
        audio_offset_s=0.0, chunk_key="h1", seq=1,
    )
    snapshot = t.segments[0].to_dict()
    t.append_chunk(
        Transcript(source_id="enc-x", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="second")]),
        audio_offset_s=5.0, chunk_key="h2", seq=2,
    )
    assert t.segments[0].to_dict() == snapshot   # prior chunk never re-numbered/re-STT'd


# --- the invariant-assert MUTATION-BIND ------------------------------------

def test_assert_segment_ids_monotonic_rejects_dupes_and_nonincreasing():
    with pytest.raises(SegmentInvariantError):
        tmod._assert_segment_ids_monotonic(
            [Segment(id="S1", start_s=0, end_s=1, text="a"),
             Segment(id="S1", start_s=1, end_s=2, text="b")]
        )
    with pytest.raises(SegmentInvariantError):
        tmod._assert_segment_ids_monotonic(
            [Segment(id="S2", start_s=0, end_s=1, text="a"),
             Segment(id="S1", start_s=1, end_s=2, text="b")]
        )


def test_append_chunk_invariant_forced_collision_fail_closed(monkeypatch):
    # MUTATION-BIND: force the id-minter to collide → append must fail CLOSED.
    # Remove ``_assert_segment_ids_monotonic`` in append_chunk and the dup ids
    # land silently (no error) → this test goes RED.
    monkeypatch.setattr(tmod, "make_segment_id", lambda _i: "S1")
    t = Transcript(source_id="enc-x", mode="synthetic")
    chunk = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="A", start_s=0.0, end_s=5.0, text="a"),
        Segment(id="B", start_s=5.0, end_s=10.0, text="b"),
    ])
    with pytest.raises(SegmentInvariantError):
        t.append_chunk(chunk, audio_offset_s=0.0, chunk_key="h", seq=1)
    # rolled back — no corrupt half-append persists.
    assert t.segments == [] and t.chunk_provenance == []


# ---------------------------------------------------------------------------
# 2. Input convention + sweep walk
# ---------------------------------------------------------------------------

def test_discover_chunks_integer_seq_order_not_lexicographic(tmp_path):
    from alfred.scribe.pipeline import _discover_chunks
    enc = tmp_path / "enc"
    # UN-padded names so lexicographic ("chunk_10" < "chunk_2") would MIS-order.
    for seq in (1, 2, 10):
        _write_chunk(enc, seq, [f"line {seq}"], pad=0)
    seqs = [seq for _p, seq in _discover_chunks(enc)]
    assert seqs == [1, 2, 10]                    # integer order (the fix)
    assert seqs != [1, 10, 2]                    # NOT the lexicographic bug


def test_accumulate_folds_contiguous_chunks_in_seq_order(tmp_path):
    enc = tmp_path / "inbox" / "enc-A"
    _write_chunk(enc, 1, ["seg one", "seg two"])
    _write_chunk(enc, 2, ["seg three"])
    r = accumulate_encounter(enc, config=_config())
    assert isinstance(r, AccumResult)
    assert r.folded == 2 and r.frozen is False and r.segments == 3
    tx = load_ledger(ledger_path(enc, r.encounter_id))
    assert [s.text for s in tx.segments] == ["seg one", "seg two", "seg three"]
    assert [s.id for s in tx.segments] == ["S1", "S2", "S3"]


def test_accumulate_seq_gap_freezes_and_logs(tmp_path):
    enc = tmp_path / "inbox" / "enc-gap"
    _write_chunk(enc, 1, ["one"])
    _write_chunk(enc, 2, ["two"])
    _write_chunk(enc, 4, ["four"])   # seq 3 is a HOLE
    with structlog.testing.capture_logs() as caps:
        r = accumulate_encounter(enc, config=_config())
    assert r.frozen is True
    assert r.folded == 2 and r.segments == 2      # folded 1,2 — NOT 4 (never over a hole)
    gaps = [c for c in caps if c.get("event") == "scribe.accumulator.seq_gap"]
    assert len(gaps) == 1 and gaps[0]["expected_seq"] == 3 and gaps[0]["found_seq"] == 4


def test_accumulate_idempotent_across_sweeps(tmp_path):
    enc = tmp_path / "inbox" / "enc-A"
    _write_chunk(enc, 1, ["one"])
    _write_chunk(enc, 2, ["two"])
    r1 = accumulate_encounter(enc, config=_config())
    assert r1.folded == 2
    # second sweep, nothing new settled → folds 0, ledger unchanged (no dup).
    r2 = accumulate_encounter(enc, config=_config())
    assert r2.folded == 0 and r2.segments == 2
    tx = load_ledger(ledger_path(enc, r2.encounter_id))
    assert len(tx.segments) == 2 and len(tx.chunk_provenance) == 2


def test_accumulate_closed_sentinel_finalizes(tmp_path):
    enc = tmp_path / "inbox" / "enc-A"
    _write_chunk(enc, 1, ["one"])
    (enc / "_CLOSED").write_text("", encoding="utf-8")
    r = accumulate_encounter(enc, config=_config())
    assert r.closed is True
    assert load_ledger(ledger_path(enc, r.encounter_id)).closed is True


def test_run_sweep_walks_subdirs_and_legacy_flat(tmp_path, monkeypatch):
    import alfred.distiller.backends.ollama as ollama_mod
    from alfred.scribe import ScribeState, run_sweep

    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (json.dumps({"subjective": [], "objective": [], "assessment": [],
                            "plan": [], "assessment_reasoning_stated": False}),
                {"stop_reason": "stop"})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)

    import asyncio
    input_dir = tmp_path / "inbox"
    input_dir.mkdir(parents=True)
    # legacy FLAT single-chunk (P2 back-comp).
    (input_dir / "flat.wav").write_bytes(b"flat-audio")
    (input_dir / "flat.txt").write_text("A flat one-shot line.\n", encoding="utf-8")
    (input_dir / "flat.meta.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    # a SUBDIR encounter (the P2 iterdir()+is_file() would have SILENTLY SKIPPED it).
    _write_chunk(input_dir / "enc-sub", 1, ["Subdir chunk line."])

    cfg = _config()
    cfg.input_dir = str(input_dir)
    state = ScribeState(tmp_path / "state.json")
    counts = asyncio.run(run_sweep(cfg, state, tmp_path / "vault"))
    assert counts["scanned"] == 1 and counts["drafted"] == 1       # flat drafted
    assert counts["encounters"] == 1 and counts["chunks_folded"] == 1  # subdir folded
    eid = compute_encounter_id("enc-sub", salt=_SALT)
    assert ledger_path(input_dir / "enc-sub", eid).is_file()


# ---------------------------------------------------------------------------
# 3. Ledger — persist + schema-tolerant resume round-trip
# ---------------------------------------------------------------------------

def test_ledger_round_trip_schema_tolerant(tmp_path):
    enc = tmp_path / "enc"
    enc.mkdir()
    eid = compute_encounter_id("enc", salt=_SALT)
    t = Transcript(source_id=eid, mode="synthetic")
    t.append_chunk(
        Transcript(source_id=eid, mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="a")]),
        audio_offset_s=0.0, chunk_key="h1", seq=1,
    )
    t.closed = True
    lp = ledger_path(enc, eid)
    save_ledger(lp, t)
    # inject an UNKNOWN key (a newer scribe version) → must load, drop the key.
    raw = json.loads(lp.read_text(encoding="utf-8"))
    raw["future_field"] = {"unknown": True}
    lp.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_ledger(lp)
    assert loaded.source_id == eid and loaded.closed is True
    assert [s.text for s in loaded.segments] == ["a"]
    assert loaded.chunk_provenance[0]["chunk_key"] == "h1"
    assert not hasattr(loaded, "future_field")


def test_ledger_missing_returns_none(tmp_path):
    assert load_ledger(tmp_path / "nope.transcript.json") is None


def test_ledger_unreadable_returns_none_and_logs(tmp_path):
    # A corrupt ledger must NOT crash the sweep — return None (fold starts fresh)
    # + emit scribe.ledger.unreadable (pre-commit #9: the log line is pinned).
    lp = tmp_path / "enc-x.transcript.json"
    lp.write_text("{ not valid json", encoding="utf-8")
    with structlog.testing.capture_logs() as caps:
        assert load_ledger(lp) is None
    warns = [c for c in caps if c.get("event") == "scribe.ledger.unreadable"]
    assert len(warns) == 1
    assert warns[0]["error_class"] == "JSONDecodeError"
    assert warns[0]["path"] == str(lp)


def test_ledger_resume_continues_ids(tmp_path):
    # Fold chunk 1, persist; a fresh accumulate (loads the ledger) folds chunk 2
    # continuing the ids from the PERSISTED segment count (crash-resume).
    enc = tmp_path / "inbox" / "enc-A"
    _write_chunk(enc, 1, ["one", "two"])
    accumulate_encounter(enc, config=_config())
    _write_chunk(enc, 2, ["three"])
    r = accumulate_encounter(enc, config=_config())
    tx = load_ledger(ledger_path(enc, r.encounter_id))
    assert [s.id for s in tx.segments] == ["S1", "S2", "S3"]   # resumed, not reset


# ---------------------------------------------------------------------------
# 5. Grounding dup-id refusal — the MUTATION-BIND
# ---------------------------------------------------------------------------

def test_grounding_refuses_duplicate_segment_ids():
    # MUTATION-BIND: a transcript with two S1 segments would (without the gate)
    # last-wins overwrite so the claim grounds against the WRONG S1 and passes
    # clean. Remove the dup-id check in verify() → no error → this test RED.
    structured = parse_structured_json(json.dumps({
        "subjective": [{"claim": "Reports chest pain", "source_spans": ["S1"]}],
        "objective": [], "assessment": [], "plan": [],
        "assessment_reasoning_stated": False,
    }))
    corrupt = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=5.0, text="chest pain"),
        Segment(id="S1", start_s=5.0, end_s=10.0, text="no chest pain"),  # collision
    ])
    with pytest.raises(GroundingIntegrityError) as ei:
        verify_grounding(structured, corrupt)
    assert "S1" in str(ei.value)


def test_grounding_ok_on_unique_ids():
    structured = parse_structured_json(json.dumps({
        "subjective": [{"claim": "Reports chest pain", "source_spans": ["S1"]}],
        "objective": [], "assessment": [], "plan": [],
        "assessment_reasoning_stated": False,
    }))
    clean = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=5.0, text="reports chest pain"),
    ])
    verify_grounding(structured, clean)   # no raise


# ---------------------------------------------------------------------------
# 6. Settle-gate — holds an unsettled file
# ---------------------------------------------------------------------------

def test_settle_gate_meta_marker_is_settled(tmp_path):
    enc = tmp_path / "enc"
    chunk = _write_chunk(enc, 1, ["x"], meta=True)
    meta = chunk.with_suffix(".meta.json")
    assert is_chunk_settled(chunk, meta_path=meta, now=1e12) is True


def test_settle_gate_holds_file_without_marker(tmp_path):
    # No .meta.json commit marker + fresh mtime + no prior observation → HELD.
    enc = tmp_path / "enc"
    chunk = _write_chunk(enc, 1, ["x"], meta=False)
    meta = chunk.with_suffix(".meta.json")
    assert not meta.is_file()
    import os
    st = os.stat(chunk)
    # too-fresh (now == mtime) → unsettled regardless of prior obs.
    assert is_chunk_settled(chunk, meta_path=meta, now=st.st_mtime) is False
    # aged but no prior observation → still unsettled (need 2-sweep stability).
    assert is_chunk_settled(chunk, meta_path=meta, now=st.st_mtime + 999) is False
    # aged + stable size/mtime across sweeps → settled (the no-marker fallback).
    assert is_chunk_settled(
        chunk, meta_path=meta, now=st.st_mtime + 999,
        prev_stat=(st.st_size, st.st_mtime),
    ) is True


def test_accumulate_holds_unsettled_chunk(tmp_path):
    # An expected chunk whose commit marker hasn't landed → HELD, not folded.
    enc = tmp_path / "inbox" / "enc-A"
    _write_chunk(enc, 1, ["one"], meta=True)
    _write_chunk(enc, 2, ["two"], meta=False)   # no marker → unsettled
    r = accumulate_encounter(enc, config=_config())
    assert r.folded == 1 and r.held == 1 and r.segments == 1  # folded 1, held 2
