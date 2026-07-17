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


# --- H1: tolerant-reader / verify predicate ALIGNMENT ------------------------

def test_query_visible_forgery_breaks_verify(tmp_path):
    # The H1 invariant (design §4): any row the tolerant readers SERVE is chain-covered — a forgery
    # cannot be query-servable yet verify-invisible. A forged line carrying all three chain fields
    # (entry_sha+prev+seq) IS served by _iter_entries → it MUST therefore break verify.
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "real1"})
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write(json.dumps({
            "entry_sha": "f" * 64, "prev": "0" * 64, "seq": 3, "kind": "attest.recorded",
            "subject_id": "enc-EVIL", "payload": {"body_sha": "EVIL"}}) + "\n")
    # it IS query-visible (has all three fields)...
    assert any(e.get("subject_id") == "enc-EVIL" for e in s.tail("clinical", 5))
    # ...so verify MUST reject it (recomputed sha mismatch). Query-visible ⇒ chain-covered.
    assert not s.verify("clinical").ok


def test_schema_partial_forgery_invisible_to_readers(tmp_path):
    # A forged line MISSING prev/seq is chain-INVISIBLE to verify (skipped as a fragment); with the
    # aligned predicate it is likewise invisible to query/latest/tail/rebuild — it can never be
    # served as evidence or pinned into the attested-digest index.
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", subject_id="enc-real", payload={"body_sha": "real1"})
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write(json.dumps({"entry_sha": "f" * 64, "kind": "attest.recorded",
                            "subject_id": "enc-EVIL", "payload": {"body_sha": "EVIL"}}) + "\n")
    assert all(e.get("subject_id") != "enc-EVIL" for e in s.query("clinical"))  # not served
    assert s.latest("clinical", subject_id="enc-EVIL") is None
    assert s.verify("clinical").ok  # the fragment is skipped, chain intact (but counted, see M1)


def test_forged_fragment_does_not_poison_tip(tmp_path):
    # H1 DoS variant: a forged row that verify SKIPS (no 'prev') but a WEAKER tip resolver would
    # accept (it has entry_sha+seq) must NOT become the tip — else the next LEGITIMATE append chains
    # onto the forgery's sha and verify-fails forever (unrecoverable, never-truncate). _last_valid
    # requires prev AND recomputes the sha, so the genuine tip is used.
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "real1"})  # genuine tip = seq 2
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write(json.dumps({"entry_sha": "f" * 64, "seq": 99,  # NO prev → verify skips it
                            "kind": "attest.recorded", "payload": {"body_sha": "EVIL"}}) + "\n")
    s.append("clinical", "attest.recorded", payload={"body_sha": "real2"})  # must chain onto seq 2
    rep = s.verify("clinical")
    assert rep.ok  # MUTATION-BIND: a weaker _last_valid chains real2 onto the forgery → verify fails
    assert rep.sealed_fragments >= 1  # the forgery is a counted, un-chained fragment
    seqs = [e["seq"] for e in s.tail("clinical", 5)]
    assert 99 not in seqs and seqs[-1] == 3  # real2 got seq 3, chaining onto the genuine seq 2


# --- M1: verify counts sealed (non-final skipped) fragments ------------------

def test_inserted_foreign_line_counted_not_silently_blessed(tmp_path):
    # An inserted mid-file non-entry line (simulated smuggled free text) — the chain still links
    # across it, but verify must COUNT it (sealed_fragments), not report a fully clean bill.
    s = _store(tmp_path)
    for i in range(3):
        s.append("clinical", "attest.recorded", payload={"body_sha": f"h{i}"})
    p = tmp_path / "events" / "clinical.jsonl"
    lines = p.read_text().splitlines()
    lines.insert(2, json.dumps({"note": "patient John Smith DOB 1961-02-03 undeclared free text"}))
    p.write_text("\n".join(lines) + "\n")
    rep = s.verify("clinical")
    assert rep.ok and rep.torn_tail is False  # chain links across it
    assert rep.sealed_fragments == 1  # ...but the un-chained line is surfaced (ILB), not silent


# --- M4: fsync-on-durable + post_append-under-lock are BOUND -----------------

def test_durable_append_fsyncs(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "x"})  # writes genesis + entry
    synced: list = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    s.append("clinical", "attest.recorded", payload={"body_sha": "y"})  # durable [D] entry
    assert len(synced) >= 1  # MUTATION-BIND: dropping os.fsync in _write_line makes this empty


def test_best_effort_append_does_not_fsync(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.append("access", "access.read", payload={"via": "cli"})  # genesis (fsync) + entry
    synced: list = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    s.append("access", "access.read", payload={"via": "daemon"})  # best-effort entry, no genesis
    assert synced == []  # a best-effort append must NOT fsync


def test_post_append_runs_inside_the_stream_lock(tmp_path):
    # §7.4: the post_append callback runs WHILE the stream flock is still held (so a derived index
    # update joins the append's critical section). Probe it: a NON-blocking re-lock of the same
    # lock file from inside the callback must be DENIED (append holds it).
    import fcntl
    s = _store(tmp_path)
    observed: dict = {}

    def _cb(receipt):
        lockpath = tmp_path / "events" / "clinical.lock"
        with open(lockpath, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["held"] = False  # acquired → append is NOT holding it (mutation)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                observed["held"] = True   # denied → append holds the lock (correct)

    s.append("clinical", "attest.recorded", payload={"body_sha": "x"}, post_append=_cb)
    assert observed["held"] is True  # MUTATION-BIND: post_append outside the lock → False


# --- H1 round-2: the predicate is the FULL effective one (int seq / str prev) ----

def test_non_int_seq_forgery_non_servable_and_no_crash(tmp_path):
    # A forged row with a non-integer seq passes a PRESENCE-only check but fails verify's int(seq):
    # the round-1 hole served it as genuine evidence while verify stayed ok=True. The shared
    # predicate excludes it from the readers too — non-servable, counted, verify does not crash.
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", subject_id="enc-real", payload={"body_sha": "real1"})
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write(json.dumps({"entry_sha": "f" * 64, "prev": "0" * 64, "seq": "abc",
                            "kind": "attest.recorded", "subject_id": "enc-EVIL",
                            "payload": {"body_sha": "EVIL"}}) + "\n")
    s.append("clinical", "attest.recorded", payload={"body_sha": "real2"})  # seals the forgery mid-file
    assert all(e.get("subject_id") != "enc-EVIL" for e in s.query("clinical"))  # NOT served
    assert s.latest("clinical", subject_id="enc-EVIL") is None
    rep = s.verify("clinical")  # no crash
    assert rep.ok and rep.sealed_fragments == 1  # counted, not silently blessed


def test_non_string_prev_tail_does_not_crash_tip_or_verify(tmp_path):
    # Finding 3 (the DoS): a forged TAIL row with a NON-STRING prev makes recompute_entry_sha do
    # `prev + "\n"` → TypeError, crashing tip resolution (the next append) AND verify. The shared
    # predicate makes it a non-entry BEFORE recompute → no crash; the legit append chains onto the
    # genuine tip; the forgery is never served.
    s = _store(tmp_path)
    s.append("clinical", "attest.recorded", payload={"body_sha": "real1"})  # genuine tip = seq 2
    with open(tmp_path / "events" / "clinical.jsonl", "a") as f:
        f.write(json.dumps({"entry_sha": "f" * 64, "prev": 0, "seq": 3,  # prev is INT, not str
                            "kind": "attest.recorded", "subject_id": "enc-EVIL",
                            "payload": {"body_sha": "EVIL"}}) + "\n")
    s.append("clinical", "attest.recorded", payload={"body_sha": "real2"})  # MUST NOT crash on tip
    rep = s.verify("clinical")  # MUST NOT crash
    assert rep.ok  # legit append chained onto the genuine tip, not the forgery
    assert all(e.get("subject_id") != "enc-EVIL" for e in s.tail("clinical", 5))  # non-servable
    assert [e["seq"] for e in s.tail("clinical", 5)][-1] == 3  # real2 got seq 3 (genuine chain)
