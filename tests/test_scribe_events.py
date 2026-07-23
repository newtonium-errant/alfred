"""ScribeEvents facade pins (design doc §5 / §15.5 item 2).

KINDS frozen/widening pin; per-emitter (stream, kind) authority pins; durable-vs-capture
postures; attested-digest index under-lock + rebuild-from-log; ContextVar read-hook suppression;
always-on activation (clinical fail-loud / non-clinical degrade); audit-encounter merge;
genesis legacy predecessor pin; NO generic emit verb.
"""
from __future__ import annotations

import pytest

from alfred.evstore import EventStoreError
from alfred.scribe.events import KINDS, ScribeEvents

_CLOCK = "2026-07-16T12:00:00+00:00"

# The FROZEN reviewed schema (Ruling 3). A new kind, a new field, or a durability flip trips this
# widening pin — the whole point of Ruling 5's structural PHI prevention. Adding a clinical family
# or field is a DELIBERATE reviewed schema change: update this literal in the same commit.
EXPECTED_KINDS = {
    ("access", "access.read"): (["path_digest", "record_type", "status", "via"], False),
    ("access", "access.system_reads_summary"): (["count", "window_start"], False),
    ("access", "store.verified"): (["entries", "ok"], False),
    ("clinical", "attest.recorded"): (
        ["body_sha", "completeness", "creator", "forced", "from_status",
         "grounding_flag_count", "grounding_reasons", "to_status"], True),
    ("clinical", "attest.refused"): (
        ["completeness", "forced", "from_status", "reason", "to_status"], False),
    ("clinical", "consent.confirmed"): (["captured_by", "method"], True),
    ("clinical", "consent.declined"): (["captured_by", "method"], True),
    ("clinical", "consent.violation_refused"): (["seq"], False),
    ("clinical", "consent.withdrawn"): (["at_seq"], True),
    ("clinical", "encounter.cap_hit"): (["cap"], False),
    ("clinical", "encounter.closed"): (["final_seq"], False),
    ("clinical", "encounter.opened"): ([], False),
    ("clinical", "encounter.post_close_chunk_refused"): (["seq"], False),
    # #26 learning family — negation-paraphrase suppression governance (PHI-free)
    ("clinical", "negation.approved"): (["dropped_count", "glossary_version"], True),
    ("clinical", "negation.rejected"): ([], True),
    ("clinical", "note.draft_created"): (["body_sha"], False),
    ("clinical", "note.draft_regenerated"): (["body_sha", "grounding_flag_count", "marker"], False),
    ("clinical", "note.human_edit_detected"): (["body_sha_after", "body_sha_before"], False),
    ("clinical", "note.marker_selfheal"): ([], False),
    ("clinical", "note.post_attest_audio"): ([], False),
    ("clinical", "note.post_attest_edit_detected"): (["attested_body_sha", "current_body_sha"], False),
    ("clinical", "note.ready"): (["body_sha", "expected_final_seq", "folded_through"], False),
    ("clinical", "retention.destroy_intent"): (["manifest_sha256", "schedule_version"], True),
    ("clinical", "retention.destroyed"): (["manifest_sha256", "schedule_version"], True),
    ("clinical", "retention.schedule_published"): (
        ["effective_date", "schedule_sha256", "schedule_version"], True),
    ("clinical", "retention.sealed"): (
        ["chunk_count", "cipher", "manifest_sha256", "sealed_to_key_fp", "total_bytes"], True),
    ("clinical", "retention.unsealed"): (["reason_code", "ticket_ref"], True),
    ("clinical", "store.heartbeat"): (
        ["count_attestation", "count_consent", "count_encounter", "count_note", "count_retention"],
        False),
    ("clinical", "store.verified"): (["entries", "ok"], False),
}


def _events(tmp_path, mode="clinical", **scribe):
    raw = {"scribe": {"mode": mode, "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")},
                      **scribe}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


# --- widening pin -----------------------------------------------------------

def test_kinds_registry_frozen():
    got = {(k.stream, k.kind): (sorted(k.fields), k.durable) for k in KINDS}
    got = {sk: (fields, dur) for sk, (fields, dur) in got.items()}
    assert got == EXPECTED_KINDS  # widening pin — a new kind/field/durability change trips this


def test_no_generic_emit_verb():
    # There is DELIBERATELY no `emit(kind, ...)` — a casual-forgery surface would gut the chain's
    # meaning. Only typed emitters construct clinical events (§2.2).
    assert not hasattr(ScribeEvents, "emit")


# --- per-emitter (stream, kind) authority pins ------------------------------

def test_attest_recorded_authority(tmp_path):
    ev = _events(tmp_path)
    ev.attest_recorded(subject_id="enc-1", attester="jd", from_status="drafted",
                       to_status="attested", creator="stayc_scribe", forced=False,
                       completeness="complete", body_sha="ab", grounding_flag_count=0,
                       grounding_reasons=[], rel_path="clinical/enc-1.md")
    rows = ev.query("clinical", kind="attest.recorded")
    assert len(rows) == 1 and rows[0]["actor"] == "jd" and rows[0]["actor_kind"] == "clinician"


def test_each_emitter_writes_exact_stream_and_kind(tmp_path):
    ev = _events(tmp_path)
    ev.note_draft_created(subject_id="e", body_sha="a")
    ev.note_ready(subject_id="e", body_sha="b", expected_final_seq=3, folded_through=3)
    ev.encounter_opened(subject_id="e")
    ev.encounter_closed(subject_id="e", final_seq=5)
    ev.encounter_cap_hit(subject_id="e", cap="chunks")
    ev.access_read(subject_id="e", record_type="clinical_note", status="attested",
                   path_digest="pd", via="cli", actor="op", actor_kind="operator")
    clinical_kinds = {r["kind"] for r in ev.query("clinical")}
    access_kinds = {r["kind"] for r in ev.query("access")}
    assert {"note.draft_created", "note.ready", "encounter.opened", "encounter.closed",
            "encounter.cap_hit"} <= clinical_kinds
    assert "access.read" in access_kinds
    # a clinical kind must never land on the access stream and vice-versa (kind→stream binding).
    assert "access.read" not in clinical_kinds
    assert "note.ready" not in access_kinds


# --- postures ---------------------------------------------------------------

def test_durable_emitter_raises_when_inactive(tmp_path):
    ev = _events(tmp_path, mode="synthetic")
    ev._active = False  # simulate a degraded store
    with pytest.raises(EventStoreError):
        ev.attest_recorded(subject_id="e", attester="j", from_status="d", to_status="a",
                           creator="c", forced=False, completeness="complete", body_sha="x",
                           grounding_flag_count=0, grounding_reasons=[])


def test_capture_emitter_swallows_when_inactive(tmp_path):
    ev = _events(tmp_path, mode="synthetic")
    ev._active = False
    assert ev.note_draft_created(subject_id="e", body_sha="a") is None  # swallowed, no raise


def test_capture_emitter_swallows_store_error(tmp_path):
    ev = _events(tmp_path)
    # a bad payload would normally raise EventStoreError; the capture posture swallows it.
    import structlog
    with structlog.testing.capture_logs() as cap:
        # force a store error by emitting an unregistered kind through the raw store via a bad call
        out = ev._emit_capture("clinical", "attest.recorded", payload={"illegal_field": 1})
    assert out is None
    assert any(c.get("event") == "scribe.events.emit_failed" for c in cap)


# --- attested-digest index --------------------------------------------------

def test_attested_index_updated_on_attest(tmp_path):
    ev = _events(tmp_path)
    ev.attest_recorded(subject_id="enc-9", attester="jd", from_status="d", to_status="a",
                       creator="c", forced=True, completeness="incomplete", body_sha="cafe",
                       grounding_flag_count=1, grounding_reasons=["hedged"], rel_path="p/enc-9.md")
    got = ev.attested_digest("enc-9")
    assert got["body_sha"] == "cafe" and got["rel_path"] == "p/enc-9.md"
    assert got["attested_at"] == _CLOCK  # index attested_at == event ts (shared clock)


def test_rebuild_index_from_log(tmp_path):
    ev = _events(tmp_path)
    ev.attest_recorded(subject_id="enc-A", attester="jd", from_status="d", to_status="a",
                       creator="c", forced=False, completeness="complete", body_sha="a1",
                       grounding_flag_count=0, grounding_reasons=[], rel_path="p/A.md")
    ev._atomic_write_index({})  # simulate index loss
    assert ev.attested_digest("enc-A") is None
    n = ev.rebuild_index()
    assert n == 1 and ev.attested_digest("enc-A")["body_sha"] == "a1"
    assert ev.attested_digest("enc-A")["rel_path"] == ""  # rel_path is index-only, not chained


# --- read hook + suppression ------------------------------------------------

def test_read_hook_suppresses_pipeline_and_counts(tmp_path):
    ev = _events(tmp_path)
    hook = ev.make_read_hook()
    with ev.access_context("stayc_scribe", "pipeline", "daemon"):
        hook(None, "clinical/e.md", {"source_id": "e", "type": "clinical_note", "status": "drafted"})
        hook(None, "clinical/f.md", {"source_id": "f", "type": "clinical_note", "status": "drafted"})
    assert ev.suppressed_reads == 2
    assert ev.query("access", kind="access.read") == []  # suppressed → not written per-event


def test_read_hook_emits_for_human_actor(tmp_path):
    ev = _events(tmp_path)
    hook = ev.make_read_hook()
    with ev.access_context("drA", "clinician", "attest"):
        hook(None, "clinical/e.md", {"source_id": "enc-e", "type": "clinical_note", "status": "attested"})
    rows = ev.query("access", kind="access.read")
    assert len(rows) == 1
    r = rows[0]
    assert r["actor"] == "drA" and r["payload"]["via"] == "attest"
    assert r["subject_id"] == "enc-e" and r["payload"]["path_digest"]  # digest, not the path
    assert "clinical/e.md" not in str(r)  # the raw path never appears in the trail


def test_flush_suppressed_reads_emits_summary_even_at_zero(tmp_path):
    ev = _events(tmp_path)
    ev.flush_suppressed_reads()  # zero count — still emits (intentionally-left-blank)
    rows = ev.query("access", kind="access.system_reads_summary")
    assert len(rows) == 1 and rows[0]["payload"]["count"] == 0


# --- activation -------------------------------------------------------------

def test_clinical_fails_loud_at_open_on_bad_dir(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s",
                      "events": {"dir": str(blocker / "ev")}}}
    with pytest.raises(EventStoreError):
        ScribeEvents.from_config(raw, log_dir=str(tmp_path / "l"))


def test_nonclinical_degrades_to_inactive(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    raw = {"scribe": {"mode": "synthetic", "events": {"dir": str(blocker / "ev")}}}
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "l"))
    assert ev.active is False


def test_default_events_dir_derived_from_log_dir(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s"}}  # no events.dir override
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"))
    assert str(tmp_path / "logs" / "events") in str(ev._dir)


# --- audit merge + genesis pin ----------------------------------------------

def test_audit_encounter_merges_cross_family(tmp_path):
    ev = _events(tmp_path)
    ev.encounter_opened(subject_id="enc-x")
    ev.attest_recorded(subject_id="enc-x", attester="jd", from_status="d", to_status="a",
                       creator="c", forced=False, completeness="complete", body_sha="b",
                       grounding_flag_count=0, grounding_reasons=[])
    ev.access_read(subject_id="enc-x", record_type="clinical_note", status="attested",
                   path_digest="pd", via="cli", actor="op", actor_kind="operator")
    tl = ev.audit_encounter("enc-x")
    kinds = {e["kind"] for e in tl}
    assert {"encounter.opened", "attest.recorded", "access.read"} <= kinds
    assert all(e["subject_id"] == "enc-x" for e in tl)


def test_genesis_pins_legacy_predecessor(tmp_path):
    legacy = tmp_path / "clinical_attest_audit.jsonl"
    legacy.write_text('{"old":"row"}\n')
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s",
                      "events": {"dir": str(tmp_path / "ev")}}}
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "l"),
                                  legacy_audit_path=str(legacy), clock=lambda: _CLOCK)
    ev.note_draft_created(subject_id="e", body_sha="a")  # trigger genesis
    genesis = ev.query("clinical")[0]
    assert genesis["kind"] == "stream.genesis"
    assert genesis["payload"]["predecessor_file"] == "clinical_attest_audit.jsonl"
    assert len(genesis["payload"]["predecessor_sha256"]) == 64  # the legacy file's sha256 pinned


# --- M2: rebuild_index runs under the clinical-stream flock ------------------

def test_rebuild_index_blocks_while_clinical_lock_held(tmp_path):
    # §7.4: rebuild_index does read-log → atomic-write UNDER the clinical flock, so it can't race a
    # concurrent attest's post_append index update. Pin: while the lock is HELD, a rebuild in
    # another thread cannot complete; once released, it does. (MUTATION-BIND: drop the `with
    # stream_lock` and the rebuild completes immediately while the lock is held.)
    import threading
    ev = _events(tmp_path)
    ev.attest_recorded(subject_id="enc-1", attester="jd", from_status="d", to_status="a",
                       creator="c", forced=False, completeness="complete", body_sha="a1",
                       grounding_flag_count=0, grounding_reasons=[], rel_path="p/1.md")
    done = threading.Event()

    def _rebuild():
        ev.rebuild_index()
        done.set()

    with ev._store.stream_lock("clinical"):
        t = threading.Thread(target=_rebuild)
        t.start()
        assert not done.wait(timeout=0.4)  # blocked on the held lock
    t.join(timeout=3)
    assert done.is_set()  # released → rebuild completes


# --- M3: genesis predecessor is conditional, never a half-pin ---------------

def test_genesis_no_legacy_pins_neither_file_nor_sha(tmp_path):
    import structlog
    with structlog.testing.capture_logs() as cap:
        ev = _events(tmp_path)  # no legacy_audit_path
    ev.note_draft_created(subject_id="e", body_sha="a")  # trigger genesis
    g = ev.query("clinical")[0]
    assert g["kind"] == "stream.genesis"
    assert g["payload"]["predecessor_file"] == ""      # §3.3 — no name-present/sha-empty half-pin
    assert g["payload"]["predecessor_sha256"] == ""
    decided = [c for c in cap if c.get("event") == "scribe.events.genesis_predecessor_decided"]
    assert len(decided) == 1 and decided[0]["has_sha"] is False  # pin the decision log (obs)


def test_genesis_clinical_fails_loud_on_unreadable_legacy(tmp_path):
    # an existing-but-unreadable legacy file (a directory → read_bytes raises IsADirectoryError) in
    # clinical mode → fail LOUD at open rather than write a half-pinned immutable genesis (§3.3).
    legacy = tmp_path / "clinical_attest_audit.jsonl"
    legacy.mkdir()
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    with pytest.raises(EventStoreError):
        ScribeEvents.from_config(raw, log_dir=str(tmp_path / "l"), legacy_audit_path=str(legacy))


def test_genesis_nonclinical_unreadable_legacy_degrades(tmp_path):
    legacy = tmp_path / "clinical_attest_audit.jsonl"
    legacy.mkdir()
    raw = {"scribe": {"mode": "synthetic", "events": {"dir": str(tmp_path / "ev")}}}
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "l"), legacy_audit_path=str(legacy))
    ev.note_draft_created(subject_id="e", body_sha="a")
    g = ev.query("clinical")[0]
    assert g["payload"]["predecessor_file"] == "" and g["payload"]["predecessor_sha256"] == ""


# --- LOW: actor_kind allowlist enforced on the access_read path -------------

def test_access_read_coerces_out_of_allowlist_actor_kind(tmp_path):
    ev = _events(tmp_path)
    ev.access_read(subject_id="e", record_type="clinical_note", status="attested",
                   path_digest="pd", via="cli", actor="x", actor_kind="ATTACKER_ROLE")
    assert ev.query("access", kind="access.read")[0]["actor_kind"] == "unknown"  # §3.2 coerced
    ev.access_read(subject_id="e2", record_type="clinical_note", status="a",
                   path_digest="pd", via="cli", actor="drA", actor_kind="clinician")
    assert ev.query("access", kind="access.read")[-1]["actor_kind"] == "clinician"  # valid passes


# --- LOW: flush keeps the count on a failed emit ----------------------------

def test_flush_suppressed_keeps_count_on_failed_emit(tmp_path):
    import unittest.mock as mock
    ev = _events(tmp_path)
    ev._suppressed_reads = 5
    ev._suppressed_window_start = "2026-07-16"
    with mock.patch.object(ev, "_emit_capture", return_value=None):  # simulate a swallowed failure
        assert ev.flush_suppressed_reads() is None
    assert ev.suppressed_reads == 5  # count PRESERVED (not silently dropped) → carries to next flush
    ev.flush_suppressed_reads()  # a real, successful flush
    assert ev.suppressed_reads == 0  # resets only on success
