"""Retention-sweep contract pins (task #13 slice 13b — design §3.2, §3.5, §3.6, §4, §10).

Contract-first. The TRIGGER BOUNDARIES + the fail-open prune + the never-wedge isolation are the
13b invariants, so they run UNCONDITIONALLY via an injected fake sealer (no crypto dep) — per the
regression-pin-unconditional rule. Covered here:

  * trigger boundaries (§3.2/§3.6): a STATE_READY encounter is sealed within one sweep; a still-
    accumulating (fresh, un-closed) encounter is NOT; an abandoned (stale, un-closed, past-grace)
    encounter IS defensively sealed-and-kept; an inside-grace one is untouched.
  * transient mode (§3.5): wipe-without-seal + observable counted signal + NO retention.* event.
  * prune (§0.1/§4): age-based drop on the diarize_stats sink, atomic rewrite, corrupt/undateable
    rows preserved (fail-open), 180-day boundary, PHI-class + #11-chain isolation, no retention.* event.
  * never-wedges: a booming encounter is isolated; the sweep continues + returns a summary.
  * seams/gates: no-schedule ILB latch (once); inactive-store gate; no-pubkey gate; retained_dir derive.
  * observability: the per-tick sweep-summary ILB is emitted with its fields (log-emission pin).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.scribe.config import (
    RETENTION_MODE_RETAINED, RETENTION_MODE_TRANSIENT, load_from_unified,
)
from alfred.scribe.enroll_learning import (
    CAPTURE_NAME, KIND_ATTEST_OUTCOME, KIND_DIARIZE_STATS, LEARNING_DIRNAME,
)
from alfred.evstore import sha256_hex
from alfred.scribe import retention as ret_mod
from alfred.scribe import schedule as sched_mod
from alfred.scribe.events import CLINICAL, ScribeEvents
from alfred.scribe.identity import compute_encounter_id
from alfred.scribe.retention import SEAL_BLOB_SUFFIX
from alfred.scribe.retention_sweep import RetentionSweep
from alfred.scribe.state import STATE_DRAFTED, STATE_READY, ScribeState

_NOW = datetime(2026, 7, 19, 9, 0, 0, tzinfo=timezone.utc)
_SALT = "test-salt"
# A CANONICAL age recipient (valid bech32 checksum, non-degenerate point — payload = bytes 1..32).
# The sweep's _resolve_pubkey now does a full bech32 verify (finding 17), so the old malformed
# placeholder would be (correctly) rejected. The fake sealer still ignores the content — no round-trip.
_TEST_AGE_RECIPIENT = "age1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5z5tpwxqergd3c8g7rusqmwn7f2"


# --- doubles -----------------------------------------------------------------


class _FakeSealer:
    """Reversible, well-formed-blob fake (no crypto dep) — obviously-fake cipher label."""

    cipher = "fake-xor-test"

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        return b"FAKESEAL1" + bytes([len(recipient_public_key)]) + recipient_public_key + plaintext

    def verify_wellformed(self, blob: bytes) -> bool:
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        assert self.verify_wellformed(blob)
        n = blob[9]
        return blob[10 + n:]


class _BoomOnceSealer(_FakeSealer):
    """Raises on the FIRST seal (a mid-encounter failure), then behaves — proves the sweep ISOLATES a
    bad encounter and CONTINUES to the next one."""

    def __init__(self) -> None:
        self._boomed = False

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        if not self._boomed:
            self._boomed = True
            raise RuntimeError("injected seal failure")
        return super().seal(plaintext, recipient_public_key)


# --- fixtures / builders -----------------------------------------------------


def _events(tmp_path, *, mode="clinical"):
    raw = {"scribe": {"mode": mode, "encounter_salt": _SALT, "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _NOW.isoformat())


def _pubkey_file(tmp_path):
    p = tmp_path / "seal" / "seal_pub.age"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_TEST_AGE_RECIPIENT + "\n", encoding="utf-8")   # trailing newline is stripped
    return p


def _config(tmp_path, *, mode=RETENTION_MODE_RETAINED, grace=7, with_pubkey=True,
            enrollment=True, schedule_path=""):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    retention = {"mode": mode, "abandon_grace_days": grace, "schedule_path": schedule_path}
    if with_pubkey:
        retention["seal_public_key_path"] = str(_pubkey_file(tmp_path))
    scribe = {
        "mode": "clinical", "encounter_salt": _SALT, "input_dir": str(inbox),
        "retention": retention,
    }
    if enrollment:
        scribe["diarize"] = {"enrollment_dir": str(tmp_path / "enroll")}
    return load_from_unified({"scribe": scribe})


def _sweep(cfg, ev, *, sealer=None):
    factory = (lambda: sealer) if sealer is not None else (lambda: _FakeSealer())
    return RetentionSweep(cfg, ev, sealer_factory=factory)


def _make_encounter(cfg, label, *, n_chunks=2, closed=True, with_ledger=True):
    """A plaintext encounter subdir under input_dir with a PHI-shaped label name."""
    enc_dir = Path(cfg.input_dir) / label
    enc_dir.mkdir(parents=True)
    for seq in range(1, n_chunks + 1):
        (enc_dir / f"chunk_{seq}.webm").write_bytes(f"audio-{label}-{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text(json.dumps({"seq": seq}), encoding="utf-8")
    if closed:
        (enc_dir / "_CLOSED").write_text(json.dumps({"protocol": 2, "final_seq": n_chunks}))
    enc_id = compute_encounter_id(label, salt=_SALT)
    if with_ledger:
        (enc_dir / f"{enc_id}.transcript.json").write_text(
            json.dumps({"encounter_id": enc_id, "segments": []}), encoding="utf-8")
    return enc_dir, enc_id


def _age(path, *, days):
    """Set the mtime of ``path`` AND every immediate child to ``days`` ago (the abandoned check takes
    the MAX mtime across the dir + its children, so both must be aged for the dir to read stale)."""
    ts = (_NOW - timedelta(days=days)).timestamp()
    for child in path.iterdir():
        os.utime(child, (ts, ts))
    os.utime(path, (ts, ts))


def _retention_rows(ev, enc_id=None):
    rows = ev.query(CLINICAL, family="retention", kind="retention.sealed")
    if enc_id is not None:
        rows = [r for r in rows if r["subject_id"] == enc_id]
    return rows


# ============================ trigger boundaries (§3.2/§3.6) ============================


def test_ready_encounter_sealed_within_one_sweep(tmp_path):
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "jane-doe-2026")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.sealed_ready == 1 and summary.sealed_abandoned == 0
    assert (Path(cfg.input_dir).parent / "retained" / f"{enc_id}{SEAL_BLOB_SUFFIX}").is_file()
    assert not enc_dir.exists()                       # sealed → plaintext wiped (13a)
    assert len(_retention_rows(ev, enc_id)) == 1      # durable record landed


def test_still_accumulating_not_sealed(tmp_path):
    # Fresh, un-closed, no READY state → still recording → NOT sealed (skipped).
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "live-visit", closed=False)
    state = ScribeState(tmp_path / "state.json")   # no state entry at all

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.skipped == 1 and summary.sealed_ready == 0 and summary.sealed_abandoned == 0
    assert (enc_dir / "chunk_1.webm").is_file()       # plaintext intact
    assert _retention_rows(ev, enc_id) == []


def test_closed_but_not_ready_is_skipped(tmp_path):
    # _CLOSED present but the pipeline hasn't reached READY (DRAFTED / incomplete-tail) → the design
    # boundary: neither the READY gate nor the abandoned gate (which requires NO _CLOSED) → skipped.
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "closed-drafting", closed=True)
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_DRAFTED)

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.skipped == 1 and summary.sealed_ready == 0
    assert enc_dir.exists()


def test_abandoned_past_grace_defensively_sealed(tmp_path):
    # No _CLOSED, stale beyond grace → defensively sealed-and-KEPT (§3.6). A partial encounter still
    # captured consented audio; it must be sealed, never silently dropped.
    cfg = _config(tmp_path, grace=7)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "forgotten-visit", closed=False)
    _age(enc_dir, days=8)
    state = ScribeState(tmp_path / "state.json")   # never reached READY

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.sealed_abandoned == 1 and summary.sealed_ready == 0
    assert (Path(cfg.input_dir).parent / "retained" / f"{enc_id}{SEAL_BLOB_SUFFIX}").is_file()
    assert not enc_dir.exists()                       # seal-and-keep: sealed blob retained, plaintext wiped
    assert len(_retention_rows(ev, enc_id)) == 1


def test_inside_grace_untouched(tmp_path):
    # No _CLOSED but recent activity (inside grace) → NOT abandoned → untouched (never fires on fresh).
    cfg = _config(tmp_path, grace=7)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "paused-visit", closed=False)
    _age(enc_dir, days=1)
    state = ScribeState(tmp_path / "state.json")

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.skipped == 1 and summary.sealed_abandoned == 0
    assert (enc_dir / "chunk_1.webm").is_file()


def test_retained_dir_derives_under_input_parent(tmp_path):
    # Empty retained_dir ⇒ <input_dir parent>/retained (STAY-C: <STAYC_DATA>/retained) — a
    # per-instance-correct default, not a single-instance literal.
    cfg = _config(tmp_path)
    assert cfg.retention.retained_dir == ""
    sweep = _sweep(cfg, _events(tmp_path))
    assert sweep._resolved_retained_dir() == Path(cfg.input_dir).parent / "retained"


# ============================ transient mode (§3.5) ============================


def test_transient_wipes_without_seal_or_event(tmp_path):
    cfg = _config(tmp_path, mode=RETENTION_MODE_TRANSIENT)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "transient-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.transient_wiped == 1 and summary.sealed_ready == 0
    assert not enc_dir.exists()                                        # audio wiped
    assert not (Path(cfg.input_dir).parent / "retained" / f"{enc_id}{SEAL_BLOB_SUFFIX}").exists()
    assert ev.query(CLINICAL, family="retention") == []               # NO retention.* event
    sig = [c for c in cap if c["event"] == "scribe.retention.transient_wiped"]
    assert len(sig) == 1 and sig[0]["chunk_count"] == 2               # observable counted signal (ILB)


def test_retained_is_the_default_mode(tmp_path):
    cfg = _config(tmp_path)   # no explicit mode override in the retention block
    assert cfg.retention.mode == RETENTION_MODE_RETAINED


# ============================ prune (§0.1/§4) ============================


def _sink_path(cfg):
    return Path(cfg.diarize.enrollment_dir) / LEARNING_DIRNAME / CAPTURE_NAME


def _write_sink(cfg, lines):
    sink = _sink_path(cfg)
    sink.parent.mkdir(parents=True, exist_ok=True)
    sink.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return sink


def _row(kind, days_old, **extra):
    ts = (_NOW - timedelta(days=days_old)).isoformat()
    return json.dumps({"kind": kind, "ts": ts, **extra})


def test_prune_drops_old_keeps_fresh_preserves_corrupt(tmp_path):
    cfg = _config(tmp_path)
    old_ds = _row(KIND_DIARIZE_STATS, 200, source_id="enc-old")
    old_ao = _row(KIND_ATTEST_OUTCOME, 190, source_id="enc-old2")
    fresh = _row(KIND_DIARIZE_STATS, 10, source_id="enc-new")
    corrupt = '{"kind": "diarize_stats", "ts": "2026-'          # torn / unparseable
    undateable = json.dumps({"kind": KIND_DIARIZE_STATS, "source_id": "enc-x"})  # no ts
    sink = _write_sink(cfg, [old_ds, old_ao, fresh, corrupt, undateable])

    dropped = _sweep(cfg, _events(tmp_path))._prune_diarize_stats(_NOW)

    assert dropped == 2                                    # both aged rows dropped
    remaining = sink.read_text().splitlines()
    assert fresh in remaining                              # fresh kept
    assert corrupt in remaining                            # torn PRESERVED (can't date → never drop)
    assert undateable in remaining                         # undateable PRESERVED
    assert old_ds not in remaining and old_ao not in remaining
    assert not sink.with_name(sink.name + ".prune.tmp").exists()   # atomic: no temp left behind


def test_prune_180_day_boundary(tmp_path):
    cfg = _config(tmp_path)
    just_inside = _row(KIND_DIARIZE_STATS, 179, source_id="keep")
    just_outside = _row(KIND_DIARIZE_STATS, 181, source_id="drop")
    sink = _write_sink(cfg, [just_inside, just_outside])

    dropped = _sweep(cfg, _events(tmp_path))._prune_diarize_stats(_NOW)

    remaining = sink.read_text().splitlines()
    assert dropped == 1
    assert just_inside in remaining and just_outside not in remaining


def test_prune_no_rewrite_when_nothing_old(tmp_path):
    # Nothing aged out ⇒ no rewrite (never churn the sink every tick). mtime unchanged is the proof.
    import os
    cfg = _config(tmp_path)
    sink = _write_sink(cfg, [_row(KIND_DIARIZE_STATS, 5, source_id="fresh")])
    before = os.stat(sink).st_mtime_ns
    os.utime(sink, ns=(before - 5_000_000_000, before - 5_000_000_000))
    stamped = os.stat(sink).st_mtime_ns

    dropped = _sweep(cfg, _events(tmp_path))._prune_diarize_stats(_NOW)

    assert dropped == 0
    assert os.stat(sink).st_mtime_ns == stamped           # file not rewritten


def test_prune_dormant_when_no_enrollment_dir(tmp_path):
    cfg = _config(tmp_path, enrollment=False)
    assert _sweep(cfg, _events(tmp_path))._prune_diarize_stats(_NOW) == 0


def test_prune_does_not_touch_phi_or_chain(tmp_path):
    # The prune's blast radius is the telemetry sink ONLY — a seal artifact + the #11 chain are
    # untouched by a run that both seals an encounter AND prunes the sink.
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "sealed-and-pruned")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)
    _write_sink(cfg, [_row(KIND_DIARIZE_STATS, 300, source_id="ancient")])

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.sealed_ready == 1 and summary.pruned_telemetry_rows == 1
    blob = Path(cfg.input_dir).parent / "retained" / f"{enc_id}{SEAL_BLOB_SUFFIX}"
    assert blob.is_file()                                  # seal artifact untouched by the prune
    assert len(_retention_rows(ev, enc_id)) == 1           # #11 chain row survives
    # the prune emitted NO retention.* event (log rotation, not a PHI destruction)
    assert [r for r in ev.query(CLINICAL, family="retention")] == _retention_rows(ev)


# ============================ never-wedges (exception injection) ============================


def test_booming_encounter_is_isolated_sweep_continues(tmp_path):
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    # two READY encounters; the sealer booms on the FIRST seal, succeeds after.
    enc_a, id_a = _make_encounter(cfg, "aaa-boom")
    enc_b, id_b = _make_encounter(cfg, "bbb-ok")
    state = ScribeState(tmp_path / "state.json")
    state.set(id_a, state=STATE_READY)
    state.set(id_b, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev, sealer=_BoomOnceSealer())._run_sync(state, _NOW)

    assert summary.encounter_errors == 1                  # the boom was ISOLATED
    assert summary.sealed_ready == 1                      # the sweep CONTINUED and sealed the other
    assert enc_a.exists() and not enc_b.exists()          # boomed one intact, healthy one sealed
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.encounter_error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses 0o000 read-denial")
def test_sweep_unreadable_chunk_recovery_escalates_operator_attention(tmp_path):
    # D3 (sweep-level): an unreadable chunk in a row-present recovery surfaces as recovery_mismatch +
    # needs_operator_attention, NOT a silent generic encounter_error — the fail-closed PHI-residue
    # escalation channel must fire.
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "unreadable-recover", closed=True)
    _write_sweep_recovery_artifacts(cfg, tmp_path, enc_dir, enc_id, ev)   # row + blob + matching sidecar
    os.chmod(enc_dir / "chunk_1.webm", 0o000)
    state = ScribeState(tmp_path / "state.json")             # no state entry → chain-keyed recover gate
    try:
        with structlog.testing.capture_logs() as cap:
            summary = _sweep(cfg, ev)._run_sync(state, _NOW)
    finally:
        os.chmod(enc_dir / "chunk_1.webm", 0o600)
    assert summary.recovery_mismatch == 1 and summary.encounter_errors == 0
    assert summary.needs_operator_attention() is True
    assert (enc_dir / "chunk_1.webm").is_file()              # never wiped
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.needs_operator_attention"]


def test_abandoned_gate_late_chunk_survives_row_undercounts(tmp_path):
    # D1 (HIGH-3 end-to-end, sweep abandoned gate): a chunk that arrives DURING seal() survives the
    # manifest-scoped wipe, the durable row attests the GATHERED count, and the sweep escalates
    # wipe_incomplete + needs_operator_attention. Kills mutant M5 through the sweep path.
    cfg = _config(tmp_path, grace=7)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "forgotten-race", n_chunks=2, closed=False)
    _age(enc_dir, days=8)

    class _LateSealer(_FakeSealer):
        def seal(self, plaintext, recipient_public_key):
            (enc_dir / "chunk_3.webm").write_bytes(b"late-consented-audio")   # arrives mid-seal
            return super().seal(plaintext, recipient_public_key)

    state = ScribeState(tmp_path / "state.json")
    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev, sealer=_LateSealer())._run_sync(state, _NOW)

    assert summary.wipe_incomplete == 1 and summary.sealed_abandoned == 0
    assert summary.needs_operator_attention() is True
    rows = _retention_rows(ev, enc_id)
    assert len(rows) == 1 and rows[0]["payload"]["chunk_count"] == 2   # row attests the gathered set
    assert (enc_dir / "chunk_3.webm").is_file()                        # late chunk NOT wiped unsealed
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.needs_operator_attention"]


async def test_run_offloads_and_returns_summary(tmp_path):
    # The async entry point offloads to a thread and returns the summary (no raise on a normal sweep).
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "async-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    summary = await _sweep(cfg, ev).run(state, now=_NOW)

    assert summary.sealed_ready == 1
    assert not enc_dir.exists()


# ============================ seams / gates ============================


def test_no_schedule_ilb_latched_once(tmp_path):
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    state = ScribeState(tmp_path / "state.json")
    sweep = _sweep(cfg, ev)

    with structlog.testing.capture_logs() as cap:
        s1 = sweep._run_sync(state, _NOW)
        s2 = sweep._run_sync(state, _NOW)

    assert s1.schedule_present is False and s2.schedule_present is False
    latched = [c for c in cap if c["event"] == "scribe.retention.sweep.no_schedule_published"]
    assert len(latched) == 1                               # emitted ONCE across two sweeps (latched)
    assert s1.review_due == 0                              # NEVER auto-destroy / surface without a schedule


# ============================ s.50 schedule surfacing (13c — §4) ============================


def _publish_sched(cfg, **window_overrides):
    """Publish a v1-shaped schedule to cfg.retention.schedule_path with per-class window_days
    overrides (e.g. encounter_audio_sealed=1, diarize_stats=None)."""
    data = sched_mod.default_schedule_v1()
    for cls, win in window_overrides.items():
        data["classes"][cls]["window_days"] = win
    sched_mod.publish_schedule(cfg.retention.schedule_path, data)


def test_over_window_sealed_blob_surfaced_and_latched(tmp_path):
    # §4: with a schedule, the sweep counts sealed .age blobs older than the encounter_audio_sealed
    # window into summary.review_due + latches a retention_review_due signal — SURFACE ONLY.
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    cfg = _config(tmp_path, schedule_path=str(sched_path))
    _publish_sched(cfg, encounter_audio_sealed=1)          # a 1-day window
    retained = Path(cfg.input_dir).parent / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    old_blob = retained / f"enc-old{SEAL_BLOB_SUFFIX}"
    old_blob.write_bytes(b"FAKESEAL1-old")
    fresh_blob = retained / f"enc-fresh{SEAL_BLOB_SUFFIX}"
    fresh_blob.write_bytes(b"FAKESEAL1-fresh")
    ts_old = (_NOW - timedelta(days=5)).timestamp()
    os.utime(old_blob, (ts_old, ts_old))                   # aged past the window; fresh keeps ~now mtime

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, _events(tmp_path))._run_sync(ScribeState(tmp_path / "state.json"), _NOW)

    assert summary.schedule_present is True
    assert summary.review_due == 1                         # only the aged blob is over-window
    assert old_blob.is_file() and fresh_blob.is_file()     # SURFACE-ONLY — neither blob destroyed
    latched = [c for c in cap if c["event"] == "scribe.retention.sweep.retention_review_due"]
    assert len(latched) == 1


def test_surfacing_never_destroys_across_sweeps(tmp_path):
    # The review_due count rides EVERY sweep summary (ILB), while the latch fires once; and the blob is
    # NEVER touched no matter how many sweeps run (destruction stays the explicit operator playbook).
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    cfg = _config(tmp_path, schedule_path=str(sched_path))
    _publish_sched(cfg, encounter_audio_sealed=1)
    retained = Path(cfg.input_dir).parent / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    blob = retained / f"enc-x{SEAL_BLOB_SUFFIX}"
    blob.write_bytes(b"FAKESEAL1")
    ts = (_NOW - timedelta(days=400)).timestamp()
    os.utime(blob, (ts, ts))
    sweep = _sweep(cfg, _events(tmp_path))
    state = ScribeState(tmp_path / "state.json")

    with structlog.testing.capture_logs() as cap:
        s1 = sweep._run_sync(state, _NOW)
        s2 = sweep._run_sync(state, _NOW)

    assert s1.review_due == 1 and s2.review_due == 1        # counted EVERY tick (ILB)
    assert len([c for c in cap if c["event"] == "scribe.retention.sweep.retention_review_due"]) == 1
    assert blob.is_file()                                   # NEVER auto-destroyed


def test_never_pruned_class_surfaces_nothing(tmp_path):
    # A schedule that declares encounter_audio_sealed never-pruned (window null) surfaces NOTHING even
    # for an ancient blob.
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    cfg = _config(tmp_path, schedule_path=str(sched_path))
    _publish_sched(cfg, encounter_audio_sealed=None)
    retained = Path(cfg.input_dir).parent / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    ancient = retained / f"enc-ancient{SEAL_BLOB_SUFFIX}"
    ancient.write_bytes(b"FAKESEAL1")
    ts = (_NOW - timedelta(days=9999)).timestamp()
    os.utime(ancient, (ts, ts))

    summary = _sweep(cfg, _events(tmp_path))._run_sync(ScribeState(tmp_path / "state.json"), _NOW)

    assert summary.schedule_present is True and summary.review_due == 0
    assert ancient.is_file()


def test_diarize_window_comes_from_schedule(tmp_path):
    # §4: a published schedule's diarize_stats window governs the prune — a 40-day row drops under a
    # 30-day schedule window (it would be KEPT under the 180d fallback).
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    cfg = _config(tmp_path, schedule_path=str(sched_path))
    _publish_sched(cfg, diarize_stats=30)
    _write_sink(cfg, [_row(KIND_DIARIZE_STATS, 40, source_id="mid")])

    summary = _sweep(cfg, _events(tmp_path))._run_sync(ScribeState(tmp_path / "state.json"), _NOW)

    assert summary.pruned_telemetry_rows == 1              # dropped by the 30d schedule window


def test_diarize_never_pruned_skips_prune_latched(tmp_path):
    # A schedule declaring diarize_stats never-pruned SKIPS the prune (an ancient row is kept) + latches
    # a deliberate-no-prune observation (distinguishable from a broken prune).
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    cfg = _config(tmp_path, schedule_path=str(sched_path))
    _publish_sched(cfg, diarize_stats=None)
    sink = _write_sink(cfg, [_row(KIND_DIARIZE_STATS, 9999, source_id="ancient")])

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, _events(tmp_path))._run_sync(ScribeState(tmp_path / "state.json"), _NOW)

    assert summary.pruned_telemetry_rows == 0
    assert len(sink.read_text().splitlines()) == 1         # the ancient row KEPT (never-pruned)
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.diarize_stats_never_pruned"]


def test_inactive_store_skips_seal_but_prune_runs(tmp_path):
    # A DEGRADED store (non-clinical preflight failure ⇒ inactive) ⇒ no durable record possible ⇒
    # leave audio UNTOUCHED (fail-safe: never wipe without the medico-legal store). The PHI-free
    # prune still runs. (from_config leaves a healthy store active regardless of mode; the gate is
    # exercised by forcing the degraded-inactive posture directly.)
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    ev._active = False
    assert ev.active is False
    enc_dir, enc_id = _make_encounter(cfg, "synthetic-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)
    _write_sink(cfg, [_row(KIND_DIARIZE_STATS, 300, source_id="ancient")])

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.sealing_available is False
    assert summary.sealed_ready == 0 and enc_dir.exists()  # audio untouched
    assert summary.pruned_telemetry_rows == 1              # prune independent of the store
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.store_inactive"]


def test_no_pubkey_skips_seal_latched(tmp_path):
    cfg = _config(tmp_path, with_pubkey=False)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "no-key-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        sweep = _sweep(cfg, ev)
        sweep._run_sync(state, _NOW)
        summary = sweep._run_sync(state, _NOW)

    assert summary.sealing_available is False and summary.sealed_ready == 0
    assert enc_dir.exists()                                # not sealed (no recipient key yet — 13d keygen)
    latched = [c for c in cap if c["event"] == "scribe.retention.sweep.no_seal_public_key"]
    assert len(latched) == 1


def test_malformed_pubkey_skips_seal(tmp_path):
    cfg = _config(tmp_path)
    # overwrite the pubkey file with a wrong-length blob
    Path(cfg.retention.seal_public_key_path).write_bytes(b"too-short")
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "bad-key-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.sealing_available is False and enc_dir.exists()
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.seal_public_key_malformed"]


# ============================ observability (log-emission pin, builder rule #9) ============================


def test_sweep_summary_emitted_every_tick_idle(tmp_path):
    cfg = _config(tmp_path)          # empty inbox → nothing to do
    ev = _events(tmp_path)
    state = ScribeState(tmp_path / "state.json")

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.did_work() == 0
    events = [c for c in cap if c["event"] == "scribe.retention.sweep"]
    assert len(events) == 1
    ev0 = events[0]
    assert ev0["encounters_scanned"] == 0
    assert ev0["sealed_ready"] == 0 and ev0["pruned_telemetry_rows"] == 0
    assert "nothing to do" in ev0["detail"]               # idle is distinguishable from broken (ILB)


def test_sweep_summary_reports_work(tmp_path):
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "worked-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        _sweep(cfg, ev)._run_sync(state, _NOW)

    ev0 = [c for c in cap if c["event"] == "scribe.retention.sweep"][0]
    assert ev0["sealed_ready"] == 1 and ev0["encounters_scanned"] == 1
    assert "completed with work" in ev0["detail"]


# ============================ new fail-closed / disposition status surfacing (13a fix-round) ============================


def test_empty_closed_encounter_disposed_by_sweep(tmp_path):
    # §E — a CLOSED zero-chunk encounter is DISPOSED by the sweep: PHI-named dir removed, counted,
    # NO retention.* event (nothing sealed).
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "empty-closed-visit", n_chunks=0, closed=True)
    state = ScribeState(tmp_path / "state.json")   # never reached READY

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.empty_disposed == 1 and summary.sealed_ready == 0
    assert not enc_dir.exists()                                # PHI-named dir removed
    assert ev.query(CLINICAL, family="retention") == []        # nothing sealed


def test_empty_closed_disposed_even_without_pubkey(tmp_path):
    # Disposal needs NO crypto — a CLOSED zero-chunk dir is disposed even before the 13d keygen
    # (no pubkey), so patient-named dirs never accumulate waiting on a key.
    cfg = _config(tmp_path, with_pubkey=False)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "empty-nokey-visit", n_chunks=0, closed=True)
    state = ScribeState(tmp_path / "state.json")

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.empty_disposed == 1
    assert not enc_dir.exists()


def test_wipe_incomplete_surfaced_and_flagged(tmp_path):
    # A committed seal whose wipe leaves residue (a nested subdir blocks the non-recursive rmdir) →
    # wipe_incomplete count + the loud needs_operator_attention error (never buried).
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "residue-visit")
    (enc_dir / "nested-cache").mkdir()
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.wipe_incomplete == 1 and summary.sealed_ready == 0
    assert summary.needs_operator_attention() is True
    assert len(_retention_rows(ev, enc_id)) == 1               # the seal DID commit
    assert enc_dir.exists()                                    # PHI-named dir persists (residue)
    attn = [c for c in cap if c["event"] == "scribe.retention.sweep.needs_operator_attention"]
    assert len(attn) == 1 and attn[0]["wipe_incomplete"] == 1


def test_recovery_mismatch_surfaced(tmp_path):
    # A READY encounter with a retention.sealed row but NO blob → the fail-closed recovery refuses to
    # wipe → recovery_mismatch surfaced (never silently destroys the plaintext).
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "recover-visit")
    ev.retention_sealed(subject_id=enc_id, chunk_count=2, total_bytes=10, manifest_sha256="d" * 64,
                        sealed_to_key_fp="fp", cipher="age-x25519")   # row present, no blob on disk
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.recovery_mismatch == 1 and summary.sealed_ready == 0
    assert summary.needs_operator_attention() is True
    assert (enc_dir / "chunk_1.webm").is_file()                # plaintext INTACT (never wiped)
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.needs_operator_attention"]


# ============================ sweep robustness (R8 — findings 16/17/18/19/37) ============================


def test_prune_preserves_tz_naive_row_never_crashes(tmp_path):
    # finding 16: a tz-NAIVE ts row must not crash the prune (an aware cutoff vs a naive dt raises
    # TypeError, killing the whole prune + the ILB summary + the needs_operator_attention emission
    # every tick). A naive row is undateable → PRESERVED; genuinely-old aware rows still drop.
    cfg = _config(tmp_path)
    naive = json.dumps({"kind": KIND_DIARIZE_STATS, "ts": "2026-07-19T12:00:00", "source_id": "naive"})
    old = _row(KIND_DIARIZE_STATS, 200, source_id="old-aware")
    fresh = _row(KIND_DIARIZE_STATS, 5, source_id="fresh")
    sink = _write_sink(cfg, [naive, old, fresh])

    dropped = _sweep(cfg, _events(tmp_path))._prune_diarize_stats(_NOW)   # must not raise

    remaining = sink.read_text().splitlines()
    assert dropped == 1                                        # only the old AWARE row dropped
    assert naive in remaining                                  # the tz-naive row PRESERVED (undateable)
    assert fresh in remaining
    assert old not in remaining


def test_malformed_bech32_pubkey_skips_seal_latched(tmp_path):
    # finding 17: an age1-prefixed but bech32-INVALID recipient (a truncated paste) previously cleared
    # the bare prefix check → sealing_available=True while EVERY seal failed with an anonymous
    # per-encounter SealError. Now the full bech32 verify catches it → sealing_available=False + a
    # LATCHED seal_public_key_malformed key signal (not an encounter-attributed loop).
    cfg = _config(tmp_path)
    Path(cfg.retention.seal_public_key_path).write_text(_TEST_AGE_RECIPIENT[:-1], encoding="utf-8")
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "bad-checksum-visit")
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_READY)

    with structlog.testing.capture_logs() as cap:
        sweep = _sweep(cfg, ev)
        sweep._run_sync(state, _NOW)
        summary = sweep._run_sync(state, _NOW)

    assert summary.sealing_available is False and summary.sealed_ready == 0
    assert enc_dir.exists()                                    # not sealed (nothing attempted)
    assert summary.encounter_errors == 0                       # NOT an anonymous per-encounter loop
    latched = [c for c in cap if c["event"] == "scribe.retention.sweep.seal_public_key_malformed"]
    assert len(latched) == 1                                   # latched ONCE across two sweeps


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses 0o000 unreadable-file perms")
def test_unreadable_pubkey_latched(tmp_path):
    # finding 37: an unreadable key file (perms 0000 / EIO) previously returned b"" SILENTLY — the ONLY
    # unlatched _resolve_pubkey branch. Now it latches a key-unreadable observation so the operator
    # greps a key signal, not just a bare sealing_available=False.
    cfg = _config(tmp_path)
    keyfile = Path(cfg.retention.seal_public_key_path)
    os.chmod(keyfile, 0o000)
    ev = _events(tmp_path)
    state = ScribeState(tmp_path / "state.json")
    try:
        with structlog.testing.capture_logs() as cap:
            summary = _sweep(cfg, ev)._run_sync(state, _NOW)
    finally:
        os.chmod(keyfile, 0o600)
    assert summary.sealing_available is False
    assert [c for c in cap if c["event"] == "scribe.retention.sweep.seal_public_key_unreadable"]


def test_empty_abandoned_zero_chunk_disposed(tmp_path):
    # E-EXTENSION: a stale-ABANDONED zero-chunk dir (no _CLOSED, no audio, past grace) is DISPOSED —
    # the PHI-named dir must not leak forever while logging no_chunks. Needs no crypto.
    cfg = _config(tmp_path, grace=7)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "abandoned-empty-visit", n_chunks=0, closed=False)
    _age(enc_dir, days=8)
    state = ScribeState(tmp_path / "state.json")

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.empty_disposed == 1 and summary.sealed_abandoned == 0
    assert not enc_dir.exists()                                # PHI-named dir removed
    assert ev.query(CLINICAL, family="retention") == []        # nothing sealed ⇒ NO retention event


def test_empty_abandoned_inside_grace_left_alone(tmp_path):
    # A zero-chunk un-closed dir INSIDE the grace is NOT disposed (a chunk may still arrive) — the
    # E-extension only fires past the abandon grace.
    cfg = _config(tmp_path, grace=7)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "empty-fresh-visit", n_chunks=0, closed=False)
    _age(enc_dir, days=1)
    state = ScribeState(tmp_path / "state.json")

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.empty_disposed == 0 and summary.skipped == 1
    assert enc_dir.exists()


def _write_sweep_recovery_artifacts(cfg, tmp_path, enc_dir, enc_id, ev):
    """Emit a sealed row + blob + matching sidecar for enc_dir's on-disk chunks under the sweep's
    derived retained_dir (a faithful crash-between-event-and-wipe)."""
    chunks = ret_mod._discover_seal_chunks(enc_dir)
    manifest = [{"seq": s, "sha256": sha256_hex(p.read_bytes()), "bytes": len(p.read_bytes())}
                for (p, s) in chunks]
    ev.retention_sealed(subject_id=enc_id, chunk_count=len(manifest),
                        total_bytes=sum(m["bytes"] for m in manifest),
                        manifest_sha256=ret_mod._manifest_digest(manifest), sealed_to_key_fp="fp",
                        cipher="age-x25519")
    retained = Path(cfg.input_dir).parent / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    blob = b"FAKESEAL1-recovered"
    (retained / f"{enc_id}{SEAL_BLOB_SUFFIX}").write_bytes(blob)
    ret_mod._write_manifest_sidecar(retained, enc_id, manifest, sha256_hex(blob))


def test_recover_gate_routes_lost_state_sealed_encounter(tmp_path):
    # finding 30: a closed-WITH-chunks encounter whose ScribeState entry is LOST (a 'state is just
    # bookkeeping' reset) but which has a durable retention.sealed row was SKIPPED forever with
    # plaintext on disk (it hit neither the ready gate nor the abandoned gate). The recover gate keys on
    # the CHAIN and routes it into fail-closed recovery → completes the wipe.
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "lost-state-visit", closed=True)
    _write_sweep_recovery_artifacts(cfg, tmp_path, enc_dir, enc_id, ev)
    state = ScribeState(tmp_path / "state.json")   # NO entry for enc_id (state lost)

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.already_sealed == 1 and summary.skipped == 0   # routed to recovery, NOT skipped
    assert not enc_dir.exists()                                    # wipe completed (plaintext gone)


def test_closed_with_chunks_no_row_still_skipped(tmp_path):
    # The recover gate is CHAIN-keyed: a closed-with-chunks encounter WITHOUT a sealed row (a genuine
    # DRAFTED/INCOMPLETE encounter, the A4 boundary) is still SKIPPED — the gate does not over-fire.
    cfg = _config(tmp_path)
    ev = _events(tmp_path)
    enc_dir, enc_id = _make_encounter(cfg, "drafting-visit", closed=True)
    state = ScribeState(tmp_path / "state.json")
    state.set(enc_id, state=STATE_DRAFTED)

    summary = _sweep(cfg, ev)._run_sync(state, _NOW)

    assert summary.skipped == 1 and summary.already_sealed == 0
    assert enc_dir.exists()


def test_capture_sink_lock_is_exclusive(tmp_path):
    # finding 19: capture_sink_lock is a REAL exclusive flock on a STABLE lock file — while held, a
    # second (different-fd) non-blocking acquisition of the same lock file FAILS. This is the mutual
    # exclusion that serializes the attest-CLI append against the prune's read-then-replace rewrite so
    # a concurrent append is never clobbered (the sink itself is rotated, so flocking IT is unreliable).
    import fcntl

    from alfred.scribe.enroll_learning import (
        CAPTURE_LOCK_NAME, LEARNING_DIRNAME, capture_sink_lock,
    )
    enroll = tmp_path / "enroll"
    lock_file = enroll / LEARNING_DIRNAME / CAPTURE_LOCK_NAME
    with capture_sink_lock(enroll):
        fd = os.open(lock_file, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # held by the CM → cannot acquire
        finally:
            os.close(fd)
    # after release the lock is acquirable again
    fd2 = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)          # now succeeds
        fcntl.flock(fd2, fcntl.LOCK_UN)
    finally:
        os.close(fd2)
