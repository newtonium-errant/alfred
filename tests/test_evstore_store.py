"""EventStore behavior pins (§4/§15.5 — "the best QA spec in any proposal").

Covers: genesis, append+verify roundtrip, 1-byte mid-file edit breaks verify, seq-gap
detection, torn-tail pass vs mid-file fail, two-process flock concurrency, PHI payload-allowlist
+ scalar-type (unweakened) enforcement, preflight (no append), anchor + days_since_last_anchor,
tolerant query/latest/tail, register idempotency/conflict, permissions.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest

from alfred.evstore import EventStore, EventStoreError
from alfred.evstore.chain import GENESIS_PREV

_CLOCK = "2026-07-16T12:00:00+00:00"


def _store(tmp_path, **kw) -> EventStore:
    s = EventStore(tmp_path / "events", clock=lambda: _CLOCK, **kw)
    s.register_kind(
        "attest.recorded", family="attestation",
        fields=frozenset({"body_sha", "forced", "grounding_reasons"}),
        stream="clinical", durable=True,
    )
    s.register_kind(
        "access.read", family="access",
        fields=frozenset({"record_type", "path_digest", "via"}),
        stream="access", durable=False,
    )
    return s


# --- genesis + roundtrip ----------------------------------------------------

def test_first_append_writes_genesis_then_entry(tmp_path):
    s = _store(tmp_path)
    s.set_genesis_predecessor("clinical", predecessor_file="legacy.jsonl", predecessor_sha256="ab12")
    r = s.append("clinical", "attest.recorded", subject_id="enc-1", actor="jd",
                 actor_kind="clinician", payload={"body_sha": "ff", "forced": False})
    assert r.seq == 2  # genesis is seq 1
    entries = s.tail("clinical", 10)
    assert entries[0]["kind"] == "stream.genesis"
    assert entries[0]["payload"]["predecessor_file"] == "legacy.jsonl"
    assert entries[0]["payload"]["predecessor_sha256"] == "ab12"
    assert entries[0]["payload"]["canonicalization"] == "c14n-v1"
    assert entries[1]["kind"] == "attest.recorded" and entries[1]["seq"] == 2


def test_verify_ok_on_clean_chain(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    rep = s.verify("clinical")
    assert rep.ok and rep.entries == 6 and rep.head_seq == 6 and rep.first_bad_seq is None


def test_verify_empty_stream_ok(tmp_path):
    s = _store(tmp_path)
    rep = s.verify("clinical")
    assert rep.ok and rep.entries == 0 and rep.head_seq == 0 and rep.head_sha == GENESIS_PREV


# --- tamper: 1-byte mid-file edit + seq-gap ---------------------------------

def test_one_byte_mid_file_edit_breaks_verify(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    lines = p.read_text().splitlines()
    # flip one character inside entry #3's body_sha value (a committed, non-final line).
    lines[2] = lines[2].replace('"h1"', '"h9"', 1)
    p.write_text("\n".join(lines) + "\n")
    rep = s.verify("clinical")
    assert not rep.ok and rep.first_bad_seq == 3  # the edited entry's recomputed sha mismatches


def test_deleting_a_middle_entry_breaks_verify(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    lines = p.read_text().splitlines()
    del lines[2]  # remove a committed middle entry → seq gap + prev break
    p.write_text("\n".join(lines) + "\n")
    rep = s.verify("clinical")
    assert not rep.ok and rep.first_bad_seq is not None


def test_reorder_breaks_verify(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    lines = p.read_text().splitlines()
    lines[2], lines[3] = lines[3], lines[2]  # swap two committed entries
    p.write_text("\n".join(lines) + "\n")
    assert not s.verify("clinical").ok


# --- torn tail: pass (final fragment) vs fail (mid-file break) ---------------

def test_torn_final_fragment_passes_with_warning(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    with open(p, "a") as f:
        f.write('{"partial": "crash mid-appen')  # NO trailing newline — crash artifact
    rep = s.verify("clinical")
    assert rep.ok and rep.torn_tail and rep.entries == 4  # the 4 committed entries still verify


def test_seal_then_append_keeps_verify_green(tmp_path):
    # torn-tail RECOVERY (§4, never truncate): a fragment gets sealed with a bare \n on the next
    # append, which chains to the last VALID tip. verify must STILL pass — the fragment is a
    # non-chain artifact skipped without breaking continuity.
    s = _store(tmp_path)
    for i in range(3):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    with open(p, "a") as f:
        f.write('{"partial": "torn')  # crash fragment, no newline
    s.append("clinical", "attest.recorded", payload={"body_sha": "recovered"})  # seals + chains
    rep = s.verify("clinical")
    assert rep.ok and not rep.torn_tail  # sealed fragment is mid-file now; chain links across it
    # the recovered entry chains to the pre-crash tip (seq continues, no gap).
    tail = s.tail("clinical", 2)
    assert tail[-1]["payload"]["body_sha"] == "recovered"


# --- flock concurrency ------------------------------------------------------

def _appender_worker(events_dir: str, count: int) -> None:
    s = EventStore(events_dir)
    s.register_kind("attest.recorded", family="attestation",
                    fields=frozenset({"body_sha"}), stream="clinical", durable=True)
    for i in range(count):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"{os.getpid()}-{i}"})


def test_two_process_flock_no_seq_gaps(tmp_path):
    events_dir = str(tmp_path / "events")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_appender_worker, args=(events_dir, 100)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0
    s = EventStore(events_dir)
    s.register_kind("attest.recorded", family="attestation",
                    fields=frozenset({"body_sha"}), stream="clinical", durable=True)
    rep = s.verify("clinical")
    assert rep.ok and rep.entries == 201  # genesis + 200 appends, no gaps
    seqs = [e["seq"] for e in s.tail("clinical", 1000)]
    assert seqs == list(range(1, 202))


# --- PHI enforcement (allowlist + scalar-type, UNWEAKENED) ------------------

def test_unregistered_kind_refused(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.append("clinical", "note.invented", payload={})


def test_kind_on_wrong_stream_refused(tmp_path):
    # access.read is registered only on the access stream — appending it to clinical is refused
    # (kind→stream binding is structural).
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.append("clinical", "access.read", payload={"via": "cli"})


def test_payload_field_outside_frozenset_refused(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.append("clinical", "attest.recorded", payload={"body_sha": "x", "patient_name": "Jane"})


def test_nested_dict_payload_refused(tmp_path):
    # the §2.1 scalar rule is UNWEAKENED — a one-level dict is NOT admitted.
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.append("clinical", "attest.recorded", payload={"body_sha": {"nested": 1}})


def test_nested_list_payload_refused(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.append("clinical", "attest.recorded", payload={"grounding_reasons": [["a"], ["b"]]})


def test_flat_scalar_list_payload_allowed(tmp_path):
    s = _store(tmp_path)
    r = s.append("clinical", "attest.recorded",
                 payload={"grounding_reasons": ["unsupported", "hedged"], "forced": True})
    assert r.seq == 2
    assert s.tail("clinical", 1)[0]["payload"]["grounding_reasons"] == ["unsupported", "hedged"]


# --- preflight (no append) --------------------------------------------------

def test_preflight_does_not_append(tmp_path):
    s = _store(tmp_path)
    s.preflight()  # opens + flocks + tips, no write
    assert not (tmp_path / "events" / "clinical.jsonl").exists()
    assert not (tmp_path / "events" / "access.jsonl").exists()


def test_preflight_fails_loud_on_uncreatable_dir(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    s = EventStore(blocker / "events")  # can't mkdir under a file
    s.register_kind("attest.recorded", family="attestation",
                    fields=frozenset({"body_sha"}), stream="clinical", durable=True)
    with pytest.raises(EventStoreError):
        s.preflight()


# --- anchor + days_since_last_anchor ----------------------------------------

def test_anchor_exports_tip_and_zeroes_staleness(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    assert s.verify("clinical").days_since_last_anchor is None  # never anchored
    rec = s.anchor("clinical")
    assert rec["head_seq"] == 4 and rec["stream"] == "clinical" and rec["store_protocol"] == 1
    files = list((tmp_path / "events" / "anchors").glob("anchor-clinical-*.json"))
    assert len(files) == 1
    assert s.verify("clinical").days_since_last_anchor == 0


# --- tolerant query / latest / tail -----------------------------------------

def test_query_filters_and_latest(tmp_path):
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", subject_id="enc-A", payload={"body_sha": "a1"})
    s.append("clinical", "attest.recorded", subject_id="enc-B", payload={"body_sha": "b1"})
    s.append("clinical", "attest.recorded", subject_id="enc-A", payload={"body_sha": "a2"})
    a_rows = s.query("clinical", subject_id="enc-A")
    assert [r["payload"]["body_sha"] for r in a_rows] == ["a1", "a2"]
    assert s.latest("clinical", subject_id="enc-A")["payload"]["body_sha"] == "a2"
    assert s.query("clinical", kind="attest.recorded", limit=2)[-1]["payload"]["body_sha"] == "a2"
    assert s.query("clinical", subject_id="enc-NONE") == []


def test_query_skips_torn_lines(tmp_path):
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "x"})
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write("garbage not json\n")
    assert len(s.query("clinical", family="attestation")) == 1  # tolerant reader skips garbage


# --- registration idempotency / conflict ------------------------------------

def test_register_idempotent_same_spec(tmp_path):
    s = _store(tmp_path)
    s.register_kind("access.read", family="access",
                    fields=frozenset({"record_type", "path_digest", "via"}),
                    stream="access", durable=False)  # identical → no error


def test_register_conflict_refused(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(EventStoreError):
        s.register_kind("attest.recorded", family="attestation",
                        fields=frozenset({"different"}), stream="clinical", durable=True)


# --- permissions ------------------------------------------------------------

def test_dir_and_file_permissions(tmp_path):
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "x"})
    ev = tmp_path / "events"
    assert (ev.stat().st_mode & 0o777) == 0o700
    assert ((ev / "clinical.jsonl").stat().st_mode & 0o777) == 0o600
