"""Seal-lifecycle-core contract pins (task #13 slice 13a — design §3, §10).

Contract-first. The FAIL-CLOSED SEAL ORDERING is the highest-stakes invariant (crypto + medico-
legal), so its pins run UNCONDITIONALLY via an injected deterministic fake sealer (no crypto dep) —
per the regression-pin-unconditional rule. Only the ACTUAL-crypto round-trip is dep-gated behind
``pyrage`` (the operator-ruled age backend; design §10 last bullet). Covered here:

  * config: retention block load, fail-SAFE mode normalization, schema-tolerance, defaults.
  * emitters: the 5 typed retention emitters — exact (stream, kind, actor) + PHI-free scalar payloads
    the store accepts (the widening pin in test_scribe_events.py stays green — no new kinds).
  * seal ordering (§3.3): seal → verify → durable event → ONLY THEN wipe; a durable-append failure
    leaves plaintext INTACT + unacknowledged; a self-verify failure never wipes; idempotent across a
    simulated crash at each step.
  * completeness (§3.3): manifest_sha256 / chunk_count / total_bytes / sealed_to_key_fp / cipher.
  * what-stays (§3.4): transcript relocated (not destroyed), label dir removed, out-of-tree note
    untouched.
  * transient mode (§3.5): wipe-without-seal + observable signal + count; retained is the default.
  * actual-crypto round-trip (dep-gated): the blob unseals to byte-identical chunks.
"""
from __future__ import annotations

import json
import os

import pytest
import structlog

from alfred.evstore import EventStoreError, sha256_hex
from alfred.scribe.config import (
    RETENTION_MODE_RETAINED, RETENTION_MODE_TRANSIENT, load_from_unified,
)
from alfred.scribe.events import CLINICAL, KINDS, ScribeEvents
from alfred.scribe import retention as ret_mod
from alfred.scribe.retention import (
    SEAL_BLOB_SUFFIX, SEAL_CIPHER, SEAL_MANIFEST_NAME, SEAL_STATUS_ALREADY_SEALED,
    SEAL_STATUS_EMPTY_DISPOSED, SEAL_STATUS_NO_CHUNKS, SEAL_STATUS_RECOVERY_MISMATCH,
    SEAL_STATUS_SEALED, SEAL_STATUS_TRANSIENT_WIPED, SEAL_STATUS_VERIFY_FAILED,
    SEAL_STATUS_WIPE_INCOMPLETE, key_fingerprint, seal_encounter,
)


def _emit_sealed_row_for(ev, enc_dir, encounter_id=None):
    """Emit a durable retention.sealed row whose manifest EXACTLY matches ``enc_dir``'s on-disk
    chunks (so the fail-closed recovery's manifest gate passes — a faithful crash-between-event-and-
    wipe). Returns the real (chunk_count, total_bytes, manifest_sha256)."""
    encounter_id = encounter_id or _ENC
    chunks = ret_mod._discover_seal_chunks(enc_dir)
    gathered = [(seq, p.read_bytes()) for (p, seq) in chunks]
    manifest = [{"seq": s, "sha256": sha256_hex(d), "bytes": len(d)} for (s, d) in gathered]
    msha = ret_mod._manifest_digest(manifest)
    total = sum(m["bytes"] for m in manifest)
    ev.retention_sealed(subject_id=encounter_id, chunk_count=len(manifest), total_bytes=total,
                        manifest_sha256=msha, sealed_to_key_fp="fp", cipher=SEAL_CIPHER)
    return len(manifest), total, msha


def _write_recovery_blob(tmp_path, encounter_id=None, data=b"FAKESEAL1-recovered-blob"):
    """Place a well-formed (fake-sealer-shaped) sealed blob at the recovery path."""
    encounter_id = encounter_id or _ENC
    p = tmp_path / "retained" / f"{encounter_id}{SEAL_BLOB_SUFFIX}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _write_recovery_artifacts(tmp_path, enc_dir, ev, *, encounter_id=None,
                              blob=b"FAKESEAL1-recovered-blob", manifest=None):
    """Emit the sealed row + write the blob + the matching PHI-free manifest sidecar so the fail-closed
    recovery's (blob-exists + structurally-well-formed + sidecar-authenticates + blob-sha-matches +
    per-chunk-subset) gates pass for a faithful crash-between-event-and-wipe. ``manifest`` defaults to
    the sidecar/row attesting ``enc_dir``'s current on-disk chunks (the completable case)."""
    encounter_id = encounter_id or _ENC
    if manifest is None:
        chunks = ret_mod._discover_seal_chunks(enc_dir)
        manifest = [{"seq": s, "sha256": sha256_hex(p.read_bytes()), "bytes": len(p.read_bytes())}
                    for (p, s) in chunks]
    msha = ret_mod._manifest_digest(manifest)
    total = sum(m["bytes"] for m in manifest)
    ev.retention_sealed(subject_id=encounter_id, chunk_count=len(manifest), total_bytes=total,
                        manifest_sha256=msha, sealed_to_key_fp="fp", cipher=SEAL_CIPHER)
    retained = tmp_path / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    (retained / f"{encounter_id}{SEAL_BLOB_SUFFIX}").write_bytes(blob)
    ret_mod._write_manifest_sidecar(retained, encounter_id, manifest, sha256_hex(blob))

_CLOCK = "2026-07-19T09:00:00+00:00"
_ENC = "enc-0123456789abcdef"                      # an opaque encounter id (PHI-free, §10)
_TEST_PUBKEY = bytes(range(32))                    # a fixed 32-byte "pubkey" for the fake sealer


# --- deterministic fake sealer (NO real crypto — drives the ordering pins unconditionally) -----


class _FakeSealer:
    """A reversible, well-formed-blob fake — enough for the ordering/idempotency/completeness pins
    WITHOUT a crypto dep. ``cipher`` is an obviously-fake label (never mistaken for a real one)."""

    cipher = "fake-xor-test"

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        return b"FAKESEAL1" + bytes([len(recipient_public_key)]) + recipient_public_key + plaintext

    def verify_wellformed(self, blob: bytes) -> bool:
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        assert self.verify_wellformed(blob)
        n = blob[9]
        return blob[10 + n:]


class _BadVerifySealer(_FakeSealer):
    """Produces a blob whose self-verify FAILS (§3.3 step 3) — must abort before the wipe."""

    def verify_wellformed(self, blob: bytes) -> bool:
        return False


class _StubEvents:
    """A minimal events double for failure injection: controls the idempotency row + whether the
    durable ``retention_sealed`` append RAISES (the store-down fail-closed case)."""

    def __init__(self, *, sealed_row=None, raise_on_seal=False):
        self._row = sealed_row
        self._raise = raise_on_seal
        self.seal_calls: list[dict] = []

    def retention_sealed_row(self, subject_id):
        return self._row

    def retention_sealed(self, **kw):
        self.seal_calls.append(kw)
        if self._raise:
            raise EventStoreError("event store inactive — cannot emit durable 'retention.sealed'")
        return None


# --- fixtures ----------------------------------------------------------------------------------


def _events(tmp_path, mode="clinical"):
    raw = {"scribe": {"mode": mode, "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


def _make_encounter(tmp_path, *, encounter_id=_ENC, n_chunks=2, ext="webm",
                    with_ledger=True, with_closed=True):
    """Build a plaintext encounter dir under a PHI-shaped label name (so the label-dir-removed pin
    is meaningful). Returns the enc dir."""
    enc_dir = tmp_path / "inbox" / "jane-doe-2026-07-19"
    enc_dir.mkdir(parents=True)
    for seq in range(1, n_chunks + 1):
        (enc_dir / f"chunk_{seq}.{ext}").write_bytes(f"audio-bytes-for-seq-{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text(
            json.dumps({"seq": seq, "synthetic": True}), encoding="utf-8")
    if with_closed:
        (enc_dir / "_CLOSED").write_text(
            json.dumps({"protocol": 2, "final_seq": n_chunks}), encoding="utf-8")
    if with_ledger:
        (enc_dir / f"{encounter_id}.transcript.json").write_text(
            json.dumps({"encounter_id": encounter_id, "segments": []}), encoding="utf-8")
    return enc_dir


def _seal(tmp_path, enc_dir, ev, *, sealer=None, mode=RETENTION_MODE_RETAINED, encounter_id=_ENC):
    return seal_encounter(
        enc_dir, encounter_id, events=ev, sealer=sealer or _FakeSealer(),
        recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained", mode=mode)


def _sealed_rows(ev, encounter_id=_ENC):
    return ev.query(CLINICAL, family="retention", kind="retention.sealed", subject_id=encounter_id)


# ============================ config pins ============================


def test_retention_defaults_when_block_absent(tmp_path):
    cfg = load_from_unified({"scribe": {"mode": "clinical", "encounter_salt": "s"}})
    r = cfg.retention
    assert r.mode == RETENTION_MODE_RETAINED          # retained is the fail-safe default
    assert r.retained_dir == "" and r.seal_public_key_path == "" and r.schedule_path == ""
    assert r.abandon_grace_days == 7


def test_retention_mode_normalizes_transient_exact(tmp_path):
    cfg = load_from_unified({"scribe": {"retention": {"mode": " Transient "}}})
    assert cfg.retention.mode == RETENTION_MODE_TRANSIENT     # case/space-insensitive exact match


@pytest.mark.parametrize("bad", ["", "retain", "wipe", "transientish", None, 5])
def test_retention_mode_fails_safe_to_retained(bad):
    # NEVER silently transient — anything but exactly "transient" resolves to retained (§3.5).
    cfg = load_from_unified({"scribe": {"retention": {"mode": bad}}})
    assert cfg.retention.mode == RETENTION_MODE_RETAINED


def test_retention_paths_and_grace_coerce(tmp_path):
    cfg = load_from_unified({"scribe": {"retention": {
        "retained_dir": "/data/retained", "seal_public_key_path": None,
        "schedule_path": "/seal/sched.json", "abandon_grace_days": "14",
        "unknown_future_key": "ignored"}}})   # schema-tolerance: unknown dropped, no crash
    r = cfg.retention
    assert r.retained_dir == "/data/retained"
    assert r.seal_public_key_path == ""       # D4 YAML-null coerced to "" (not "None")
    assert r.schedule_path == "/seal/sched.json"
    assert r.abandon_grace_days == 14
    assert not hasattr(r, "unknown_future_key")


def test_retention_bad_grace_keeps_default():
    cfg = load_from_unified({"scribe": {"retention": {"abandon_grace_days": "not-a-number"}}})
    assert cfg.retention.abandon_grace_days == 7


# ============================ emitter pins ============================


def test_retention_sealed_emitter_shape(tmp_path):
    ev = _events(tmp_path)
    ev.retention_sealed(subject_id=_ENC, chunk_count=3, total_bytes=999,
                        manifest_sha256="m" * 64, sealed_to_key_fp="fp0011223344556677",
                        cipher=SEAL_CIPHER)
    row = ev.retention_sealed_row(_ENC)
    assert row["stream"] == CLINICAL and row["kind"] == "retention.sealed"
    assert row["family"] == "retention"
    assert row["actor"] == "stayc_scribe" and row["actor_kind"] == "system"
    assert row["payload"] == {"chunk_count": 3, "total_bytes": 999, "manifest_sha256": "m" * 64,
                              "sealed_to_key_fp": "fp0011223344556677", "cipher": SEAL_CIPHER}


def test_all_five_retention_emitters_are_durable_and_accepted(tmp_path):
    # PHI-free-by-construction: each payload passes the store's field/scalar enforcement (no raise),
    # lands durably, and carries the operator/system actor the design specifies. NO generic emit
    # verb exists — these typed methods are the only constructors.
    ev = _events(tmp_path)
    ev.retention_schedule_published(schedule_version="v1", schedule_sha256="a" * 64,
                                    effective_date="2026-07-19")
    ev.retention_sealed(subject_id=_ENC, chunk_count=1, total_bytes=1, manifest_sha256="b" * 64,
                        sealed_to_key_fp="fp", cipher=SEAL_CIPHER)
    ev.retention_unsealed(subject_id=_ENC, reason_code="audit", ticket_ref="TKT-1")
    ev.retention_destroy_intent(subject_id=_ENC, schedule_version="v1", manifest_sha256="c" * 64)
    ev.retention_destroyed(subject_id=_ENC, schedule_version="v1", manifest_sha256="c" * 64)
    fam = ev.query(CLINICAL, family="retention")
    kinds = [e["kind"] for e in fam]
    assert kinds == ["retention.schedule_published", "retention.sealed", "retention.unsealed",
                     "retention.destroy_intent", "retention.destroyed"]
    sched = ev.latest(CLINICAL, family="retention", kind="retention.schedule_published")
    assert sched["actor_kind"] == "operator" and sched["subject_id"] == ""


def test_retention_payloads_are_phi_free_by_schema(tmp_path):
    # No patient-identifier field exists anywhere in the retention schema (the registered fields).
    blocked = {"name", "label", "patient", "patient_name", "dob", "mrn", "raw_label",
               "transcript", "body", "text", "note"}
    for k in KINDS:
        if k.family != "retention":
            continue
        assert not (set(k.fields) & blocked), f"{k.kind} carries a PHI-shaped field"


# ============================ seal ordering (§3.3) ============================


def test_seal_happy_path_full_order(tmp_path):
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    out = _seal(tmp_path, enc_dir, ev)
    # blob present + at the opaque-id path (.sealed, no label leak)
    blob_path = tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}"
    assert out.status == SEAL_STATUS_SEALED
    assert blob_path.is_file()
    assert out.blob_path == str(blob_path)
    # durable retention.sealed row landed (exactly one)
    rows = _sealed_rows(ev)
    assert len(rows) == 1
    assert rows[0]["payload"]["chunk_count"] == 2
    # ONLY THEN wiped: chunks + meta + _CLOSED gone, and the label dir itself removed (no PHI residue)
    assert not enc_dir.exists()
    # transcript RELOCATED (not destroyed), out of the label dir
    reloc = tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json"
    assert reloc.is_file()
    assert json.loads(reloc.read_text())["encounter_id"] == _ENC


def test_seal_durable_append_failure_leaves_plaintext_intact(tmp_path):
    # Fail-closed: a store-down durable append must NOT wipe plaintext + must NOT acknowledge a seal.
    enc_dir = _make_encounter(tmp_path)
    stub = _StubEvents(sealed_row=None, raise_on_seal=True)
    with pytest.raises(EventStoreError):
        seal_encounter(enc_dir, _ENC, events=stub, sealer=_FakeSealer(),
                       recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained")
    # plaintext chunks STILL present (never wiped before the durable commit)
    assert (enc_dir / "chunk_1.webm").is_file()
    assert (enc_dir / "chunk_2.webm").is_file()
    assert enc_dir.is_dir()
    # the durable emit WAS attempted (ordering: seal + verify happened first), and it raised
    assert len(stub.seal_calls) == 1


def test_seal_self_verify_failure_never_wipes(tmp_path):
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    out = _seal(tmp_path, enc_dir, ev, sealer=_BadVerifySealer())
    assert out.status == SEAL_STATUS_VERIFY_FAILED
    # plaintext intact, NO event, and the unverified blob removed (next sweep re-seals cleanly)
    assert (enc_dir / "chunk_1.webm").is_file()
    assert _sealed_rows(ev) == []
    assert not (tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}").exists()


def test_seal_torn_write_digest_unstable_never_wipes(tmp_path, monkeypatch):
    # The self-verify's DIGEST-STABLE half: if the on-disk blob differs from what was sealed (a torn
    # write), abort — plaintext intact, no event.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    real_write = ret_mod._atomic_write_bytes

    def _corrupt_write(path, data):
        if str(path).endswith(SEAL_BLOB_SUFFIX):
            real_write(path, data + b"CORRUPT")   # on-disk != sealed bytes
        else:
            real_write(path, data)

    monkeypatch.setattr(ret_mod, "_atomic_write_bytes", _corrupt_write)
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_VERIFY_FAILED
    assert (enc_dir / "chunk_1.webm").is_file()
    assert _sealed_rows(ev) == []


def test_seal_no_chunks_is_ilb_noop(tmp_path):
    ev = _events(tmp_path)
    enc_dir = tmp_path / "inbox" / "empty-enc"
    enc_dir.mkdir(parents=True)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_NO_CHUNKS
    assert _sealed_rows(ev) == []
    assert [c for c in cap if c["event"] == "scribe.retention.no_chunks"]


# ============================ idempotency / crash recovery (§3.3) ============================


def test_seal_idempotent_second_call_is_noop(tmp_path):
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    first = _seal(tmp_path, enc_dir, ev)
    assert first.status == SEAL_STATUS_SEALED
    # second call: enc dir gone, chain has the row → already_sealed, NO second row
    second = _seal(tmp_path, enc_dir, ev)
    assert second.status == SEAL_STATUS_ALREADY_SEALED
    assert len(_sealed_rows(ev)) == 1


def test_crash_between_blob_and_event_reseal(tmp_path):
    # Crash between step 2/3 (blob on disk) and step 4 (event): NO chain row yet + plaintext still
    # present → the next call RE-SEALS (overwrites the orphan blob) and completes. Exactly one row.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    orphan = tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"FAKESEAL1-orphan-from-a-prior-crash")
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_SEALED
    assert len(_sealed_rows(ev)) == 1
    assert not enc_dir.exists()          # plaintext now wiped after the durable commit
    assert orphan.is_file()              # blob re-written


def test_crash_between_event_and_wipe_completes_wipe(tmp_path):
    # Crash between step 4 (durable event landed) and step 5 (wipe): the row exists, the blob is
    # durable, and the on-disk plaintext MATCHES the row's manifest → the next call COMPLETES the
    # wipe (idempotent) WITHOUT double-emitting. The fail-closed recovery gate (blob-exists +
    # manifest-match) passes because this IS the audio that was sealed.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _write_recovery_artifacts(tmp_path, enc_dir, ev)  # row + blob + sidecar matching on-disk chunks
    assert enc_dir.is_dir()
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_ALREADY_SEALED
    assert not enc_dir.exists()              # wipe completed
    assert len(_sealed_rows(ev)) == 1        # NOT double-emitted
    reloc = tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json"
    assert reloc.is_file()                   # ledger still relocated on the recovery path


def test_recovery_refuses_wipe_when_blob_missing(tmp_path):
    # Finding 3 (probe-proven PHI destruction): chain row + plaintext + NO blob → the OLD code wiped
    # the never-retrievable audio. Fail-closed: refuse, plaintext INTACT, recovery_mismatch.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _emit_sealed_row_for(ev, enc_dir)        # row present, but no blob written anywhere
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_RECOVERY_MISMATCH
    assert (enc_dir / "chunk_1.webm").is_file()          # plaintext NEVER wiped on a row-without-blob
    assert [c for c in cap if c["event"] == "scribe.retention.recovery_blob_missing"]


def test_recovery_refuses_wipe_when_plaintext_manifest_mismatches(tmp_path):
    # Finding 2 (re-opened same-label encounter): the original sealed+wiped, then NEW consented audio
    # arrives under the same label → same encounter_id. The blob + sidecar attest the ORIGINAL audio
    # (same seqs, DIFFERENT shas); the on-disk chunks are the NEW audio → the subset check refuses the
    # wipe, preserves the new audio, recovery_mismatch.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)                  # on-disk NEW audio
    original = [{"seq": s, "sha256": sha256_hex(f"ORIGINAL-audio-{s}".encode()), "bytes": 20}
                for s in (1, 2)]                          # a DIFFERENT-sha manifest (the sealed audio)
    with structlog.testing.capture_logs() as cap:
        _write_recovery_artifacts(tmp_path, enc_dir, ev, manifest=original)
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_RECOVERY_MISMATCH
    assert (enc_dir / "chunk_1.webm").is_file()          # the NEW consented audio is PRESERVED
    assert [c for c in cap if c["event"] == "scribe.retention.recovery_manifest_mismatch"]


# ============================ completeness (§3.3) ============================


def test_seal_completeness_digests_and_counts(tmp_path):
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=3)
    # expected manifest = sorted per-chunk {seq, sha256, bytes}
    expected_manifest = []
    total = 0
    for seq in range(1, 4):
        b = f"audio-bytes-for-seq-{seq}".encode()
        expected_manifest.append({"seq": seq, "sha256": sha256_hex(b), "bytes": len(b)})
        total += len(b)
    expected_msha = ret_mod._manifest_digest(expected_manifest)
    out = _seal(tmp_path, enc_dir, ev)
    row = _sealed_rows(ev)[0]["payload"]
    assert out.chunk_count == row["chunk_count"] == 3
    assert out.total_bytes == row["total_bytes"] == total
    assert out.manifest_sha256 == row["manifest_sha256"] == expected_msha
    assert row["sealed_to_key_fp"] == key_fingerprint(_TEST_PUBKEY) == sha256_hex(_TEST_PUBKEY)[:16]
    assert row["cipher"] == _FakeSealer.cipher


# ============================ what-stays (§3.4) ============================


def test_out_of_tree_note_untouched_by_seal(tmp_path):
    # seal_encounter's wipe is scoped to enc_dir — a vault note living OUTSIDE the encounter tree is
    # structurally untouched (§3.4: the clinical note is the working record, never wiped on seal).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    note = tmp_path / "vault" / "clinical_note" / f"{_ENC}.md"
    note.parent.mkdir(parents=True)
    note.write_text("# clinical note body", encoding="utf-8")
    _seal(tmp_path, enc_dir, ev)
    assert note.is_file() and note.read_text() == "# clinical note body"


def test_seal_without_ledger_still_wipes(tmp_path):
    # A seal with no transcript ledger present (edge) still wipes audio + removes the dir; the
    # transcripts/ relocation is simply skipped (no ledger to move).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, with_ledger=False)
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_SEALED
    assert not enc_dir.exists()
    assert not (tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json").exists()


# ============================ transient mode (§3.5) ============================


def test_transient_wipes_without_seal_or_event(tmp_path):
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev, mode=RETENTION_MODE_TRANSIENT)
    assert out.status == SEAL_STATUS_TRANSIENT_WIPED
    assert out.chunk_count == 2
    # audio wiped, dir removed, NO blob, NO retention.* event
    assert not enc_dir.exists()
    assert not (tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}").exists()
    assert _sealed_rows(ev) == []
    assert ev.query(CLINICAL, family="retention") == []
    # observable signal with the count (ILB: "wiped, not retained" ≠ "nothing to do")
    sig = [c for c in cap if c["event"] == "scribe.retention.transient_wiped"]
    assert len(sig) == 1 and sig[0]["chunk_count"] == 2
    # transcript still relocated + KEPT (only the dense audio is dropped)
    assert (tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json").is_file()


def test_transient_is_not_the_default(tmp_path):
    # An absent retention block ⇒ retained ⇒ a seal actually happens (never a silent transient wipe).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    cfg = load_from_unified({"scribe": {"mode": "clinical", "encounter_salt": "s"}})
    out = _seal(tmp_path, enc_dir, ev, mode=cfg.retention.mode)
    assert out.status == SEAL_STATUS_SEALED
    assert len(_sealed_rows(ev)) == 1


# ============================ actual-crypto round-trip (DEP-GATED on pyrage/age) ============================


def test_age_seal_roundtrip_byte_identical(tmp_path):
    pytest.importorskip("pyrage")   # ONLY the real-crypto round-trip is dep-gated (§10)
    from alfred.scribe.retention import (
        AgeSealer, build_seal_tar, extract_seal_tar, generate_keypair,
    )
    pub, priv = generate_keypair()
    sealer = AgeSealer()
    assert sealer.cipher == SEAL_CIPHER == "age-x25519"
    gathered = [(1, "chunk_1.webm", b"audio-one"), (2, "chunk_2.webm", b"audio-two")]
    manifest = [{"seq": s, "sha256": sha256_hex(d), "bytes": len(d)} for (s, _n, d) in gathered]
    tar = build_seal_tar(gathered, manifest)
    blob = sealer.seal(tar, pub)
    assert sealer.verify_wellformed(blob)                  # age-encryption.org header
    assert blob.startswith(b"age-encryption.org/")
    recovered = extract_seal_tar(sealer.unseal(blob, priv))
    assert recovered["chunk_1.webm"] == b"audio-one"
    assert recovered["chunk_2.webm"] == b"audio-two"
    assert SEAL_MANIFEST_NAME in recovered


def test_age_wrong_key_and_tamper_fail_typed(tmp_path):
    pytest.importorskip("pyrage")
    from alfred.scribe.retention import AgeSealer, SealError, generate_keypair
    pub, priv = generate_keypair()
    _pub2, priv2 = generate_keypair()
    sealer = AgeSealer()
    blob = sealer.seal(b"secret-audio", pub)
    with pytest.raises(SealError):
        sealer.unseal(blob, priv2)                 # wrong identity → age auth fails (typed SealError)
    tampered = bytearray(blob)
    tampered[-1] ^= 0xFF                            # flip a ciphertext byte
    with pytest.raises(SealError):
        sealer.unseal(bytes(tampered), priv)       # even the CORRECT key fails on tamper


def test_age_malformed_recipient_is_typed_sealerror(tmp_path):
    # findings 18/19: a corrupt/degenerate recipient must raise the module's TYPED SealError (age's
    # bech32 parse is canonical), NOT an untyped ValueError the 13b sweep would misclassify as a crash.
    pytest.importorskip("pyrage")
    from alfred.scribe.retention import AgeSealer, SealError
    sealer = AgeSealer()
    with pytest.raises(SealError):
        sealer.seal(b"audio", b"not-a-valid-age-recipient")
    with pytest.raises(SealError):
        sealer.seal(b"audio", b"age1thisisnotcanonicalbech32xxxxxxxxxxxxxxxxxxxxxx")


def test_age_end_to_end_seal_unseals_to_chunks(tmp_path):
    pytest.importorskip("pyrage")
    from alfred.scribe.retention import AgeSealer, extract_seal_tar, generate_keypair
    pub, priv = generate_keypair()
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=2)
    original = {f"chunk_{s}.webm": (enc_dir / f"chunk_{s}.webm").read_bytes() for s in (1, 2)}
    out = seal_encounter(enc_dir, _ENC, events=ev, sealer=AgeSealer(),
                         recipient_public_key=pub, retained_dir=tmp_path / "retained")
    assert out.status == SEAL_STATUS_SEALED
    blob = (tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}").read_bytes()
    members = extract_seal_tar(AgeSealer().unseal(blob, priv))
    for name, data in original.items():
        assert members[name] == data          # byte-identical round-trip


def test_sealer_unavailable_when_pyrage_absent(monkeypatch):
    # finding 20: the stale '# pragma: no cover — exercised via SealerUnavailable pin' now has a REAL
    # pin. Simulate pyrage being absent → AgeSealer()/generate_keypair() raise SealerUnavailable, and
    # the module still imports (the lazy-import guarantee the ordering pins rely on).
    import sys
    from alfred.scribe.retention import AgeSealer, SealerUnavailable, generate_keypair
    monkeypatch.setitem(sys.modules, "pyrage", None)          # import pyrage → ImportError
    monkeypatch.setitem(sys.modules, "pyrage.x25519", None)
    with pytest.raises(SealerUnavailable):
        AgeSealer()
    with pytest.raises(SealerUnavailable):
        generate_keypair()


# ============================ wipe residue observability (§3.3 findings 5/8/16) ============================


def test_wipe_obstruction_surfaces_incomplete_and_warns(tmp_path):
    # A residual nested subdir blocks the (deliberately non-recursive) rmdir → the seal + row DID
    # commit, but the label-named dir persists → status wipe_incomplete + a loud, observable log.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    (enc_dir / "unexpected-cache").mkdir()
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert len(_sealed_rows(ev)) == 1                    # the seal committed (blob + durable row)
    assert enc_dir.exists()                              # dir persists (un-recursed residue)
    assert [c for c in cap if c["event"] == "scribe.retention.wipe_incomplete"]
    assert [c for c in cap if c["event"] == "scribe.retention.enc_dir_not_empty"]
    assert not [c for c in cap if c["event"] == "scribe.retention.sealed"]   # never the clean 'wiped' log


def test_unlink_failure_flags_wipe_incomplete_not_clean_sealed(tmp_path, monkeypatch):
    # A REAL unlink failure (EACCES/EPERM class) must NOT be swallowed as success: PHI stays on disk,
    # so the outcome is wipe_incomplete, never SEALED-with-'plaintext wiped'.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    real = ret_mod._unlink_quiet

    def _fail_on_chunks(path):
        if path.name.startswith("chunk_") and path.name.endswith(".webm"):
            return False                                 # simulate a real unlink failure
        return real(path)

    monkeypatch.setattr(ret_mod, "_unlink_quiet", _fail_on_chunks)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert (enc_dir / "chunk_1.webm").is_file()          # PHI audio STILL on disk (not falsely wiped)
    assert [c for c in cap if c["event"] == "scribe.retention.wipe_incomplete"]
    assert not [c for c in cap if c["event"] == "scribe.retention.sealed"]


# ============================ zero-chunk closed disposition (§E ruling, finding 12) ============================


def test_closed_zero_chunk_encounter_is_disposed(tmp_path):
    # A clinician opens a patient-labeled encounter, records nothing, sends /close. No audio ⇒ nothing
    # to seal ⇒ NO retention.* event, but the label-named dir (a PHI name) MUST be removed — never
    # left with 'nothing to do' logged forever.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=True, with_closed=True)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_EMPTY_DISPOSED
    assert not enc_dir.exists()                          # PHI-named dir removed
    assert ev.query(CLINICAL, family="retention") == []  # nothing sealed ⇒ NO retention event
    assert [c for c in cap if c["event"] == "scribe.retention.empty_encounter_disposed"]
    # the ledger (if any) is still relocated, not destroyed
    assert (tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json").is_file()


def test_open_zero_chunk_encounter_is_left_alone(tmp_path):
    # An OPEN (un-closed) zero-chunk dir is mid-flight — a chunk may still arrive → leave it (no_chunks).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=False, with_closed=False)
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_NO_CHUNKS
    assert enc_dir.exists()                              # not disposed (might still receive audio)


# ============================ emitter [D] posture + facade guards (findings 6/14/15) ============================


def test_all_five_retention_emitters_raise_when_inactive(tmp_path):
    # The whole §3.3 wipe gate rides on retention_sealed RAISING on a down store — pin the durable
    # posture of ALL FIVE against the REAL facade (kills the durable→capture mutant that survived the
    # full suite). Mirrors test_durable_emitter_raises_when_inactive.
    ev = _events(tmp_path, mode="synthetic")
    ev._active = False  # a degraded (non-clinical, preflight-failed) store
    with pytest.raises(EventStoreError):
        ev.retention_sealed(subject_id=_ENC, chunk_count=1, total_bytes=1, manifest_sha256="a" * 64,
                            sealed_to_key_fp="fp", cipher=SEAL_CIPHER)
    with pytest.raises(EventStoreError):
        ev.retention_schedule_published(schedule_version="v1", schedule_sha256="a" * 64,
                                        effective_date="2026-07-19")
    with pytest.raises(EventStoreError):
        ev.retention_unsealed(subject_id=_ENC, reason_code="audit", ticket_ref="TKT-1")
    with pytest.raises(EventStoreError):
        ev.retention_destroy_intent(subject_id=_ENC, schedule_version="v1", manifest_sha256="c" * 64)
    with pytest.raises(EventStoreError):
        ev.retention_destroyed(subject_id=_ENC, schedule_version="v1", manifest_sha256="c" * 64)


def test_seal_keeps_plaintext_when_real_store_append_raises(tmp_path, monkeypatch):
    # Integration: drive seal_encounter against a REAL ScribeEvents whose store.append raises at
    # step 4 — the raise must propagate and plaintext must survive (never wiped-but-unsealed).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)

    def _boom(*a, **k):
        raise EventStoreError("store down at append")

    monkeypatch.setattr(ev.store, "append", _boom)
    with pytest.raises(EventStoreError):
        _seal(tmp_path, enc_dir, ev)
    assert (enc_dir / "chunk_1.webm").is_file()          # plaintext survives a real store-append failure
    assert (enc_dir / "chunk_2.webm").is_file()
    assert _sealed_rows(ev) == []                          # nothing acknowledged


def test_retention_unsealed_rejects_non_enum_reason_code(tmp_path):
    # finding 14: reason_code is a CLOSED enum — a free-text reason is refused at the facade.
    ev = _events(tmp_path)
    with pytest.raises(EventStoreError):
        ev.retention_unsealed(subject_id=_ENC, reason_code="because I felt like it", ticket_ref="T-1")
    assert ev.query(CLINICAL, family="retention") == []


def test_retention_unsealed_caps_ticket_ref_length(tmp_path):
    # finding 14 (probe: a 3100-char patient name into the permanent chain): ticket_ref is
    # facade length-capped — an oversized ref is refused, never landing PHI in the redaction-
    # independent chain.
    ev = _events(tmp_path)
    with pytest.raises(EventStoreError):
        ev.retention_unsealed(subject_id=_ENC, reason_code="dispute", ticket_ref="Jane Doe " * 400)
    assert ev.query(CLINICAL, family="retention") == []
    # a valid short ref under a valid reason still lands
    ev.retention_unsealed(subject_id=_ENC, reason_code="rediarize", ticket_ref="TKT-42")
    row = ev.latest(CLINICAL, family="retention", kind="retention.unsealed")
    assert row["payload"] == {"reason_code": "rediarize", "ticket_ref": "TKT-42"}


# ====== manifest-scoped wipe (R1/R2/R6/R12 — findings 1/2/3/4/5/6/11/15/26/31/40/41) ======


def test_relocate_failure_keeps_ledger_source_and_enc_dir(tmp_path, monkeypatch):
    # HIGH findings 1/2/5: a FAILED transcript relocation (retained volume fills) must NEVER destroy
    # the only source copy of a keep-forever clinical transcript, and MUST leave the enc dir so the
    # 'retry next sweep' promise is true. The manifest-scoped wipe removes ONLY the sealed chunk set —
    # never the ledger via a blanket iterdir loop (the pre-fix wipe destroyed the just-'KEPT' source).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    real_write = ret_mod._atomic_write_bytes

    def _fail_transcript_write(path, data):
        if str(path).endswith(".transcript.json"):
            raise OSError("ENOSPC on the retained transcript volume")
        real_write(path, data)

    monkeypatch.setattr(ret_mod, "_atomic_write_bytes", _fail_transcript_write)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE          # NEVER a clean SEALED
    assert (enc_dir / f"{_ENC}.transcript.json").is_file()    # SOURCE ledger SURVIVES (the only copy)
    assert enc_dir.exists()                                   # dir persists — 'retry next sweep' is true
    assert len(_sealed_rows(ev)) == 1                         # the seal DID commit (blob + durable row)
    assert [c for c in cap if c["event"] == "scribe.retention.ledger_relocate_verify_failed"]
    assert not [c for c in cap if c["event"] == "scribe.retention.sealed"]   # never the clean 'wiped'


def test_relocate_refuses_to_overwrite_divergent_archived_transcript(tmp_path):
    # HIGH finding 3: encounter_id is deterministic from (label, salt), so a same-label re-open
    # resolves the SAME archive dest. A DIVERGENT transcript already there (a prior session's archived
    # copy) must NEVER be overwritten — refuse, keep both, wipe_incomplete.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    dest = tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    prior = json.dumps({"encounter_id": _ENC, "segments": ["PRIOR SESSION FULL TRANSCRIPT"]})
    dest.write_text(prior, encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert dest.read_text() == prior                          # prior session's transcript PRESERVED
    assert (enc_dir / f"{_ENC}.transcript.json").is_file()    # source KEPT (both copies survive)
    assert [c for c in cap if c["event"] == "scribe.retention.ledger_relocate_dest_divergent"]


def test_relocate_idempotent_when_dest_already_identical(tmp_path):
    # A crash-recovery re-run where the transcript was ALREADY archived (byte-identical dest): the
    # relocation is idempotent — the source is safely dropped, NO divergent-dest escalation.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    src_bytes = (enc_dir / f"{_ENC}.transcript.json").read_bytes()
    dest = tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src_bytes)                               # identical archived copy already present
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_SEALED                   # clean — dest matched, source dropped
    assert not enc_dir.exists()
    assert dest.read_bytes() == src_bytes


def test_torn_transcript_relocation_keeps_source_never_wipes(tmp_path, monkeypatch):
    # HIGH finding 6 (the previously-unpinned digest-verify): a torn/diverged relocated transcript
    # (dest bytes != source) must NOT unlink the source. Pins the `relocated_ok = sha256(dest) ==
    # sha256(src)` gate — the mutant `relocated_ok = True` re-lands the source-destroying bug and
    # must FAIL this test (mutation-verified in the build report).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    real_write = ret_mod._atomic_write_bytes

    def _corrupt_transcript(path, data):
        if str(path).endswith(".transcript.json"):
            real_write(path, data + b"TORN")                 # on-disk dest != the source bytes
        else:
            real_write(path, data)

    monkeypatch.setattr(ret_mod, "_atomic_write_bytes", _corrupt_transcript)
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert (enc_dir / f"{_ENC}.transcript.json").is_file()   # source KEPT against the torn dest


def test_relocate_and_wipe_is_manifest_scoped_late_chunk_survives(tmp_path):
    # R1 core (finding 4's abandoned-gate race): _relocate_and_wipe removes EXACTLY the manifest chunk
    # set — a chunk on disk but NOT in chunk_paths (it arrived AFTER the gather) SURVIVES + surfaces as
    # residue, never wiped unsealed. Also pins the derived meta-sidecar removal.
    enc_dir = tmp_path / "inbox" / "jane-doe-2026-07-19"
    enc_dir.mkdir(parents=True)
    for seq in (1, 2, 3):
        (enc_dir / f"chunk_{seq}.webm").write_bytes(f"audio-{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text("{}", encoding="utf-8")
    (enc_dir / "_CLOSED").write_text("{}", encoding="utf-8")
    manifest = [enc_dir / "chunk_1.webm", enc_dir / "chunk_2.webm"]      # chunk_3 arrived LATE
    res = ret_mod._relocate_and_wipe(enc_dir, _ENC, tmp_path / "retained", chunk_paths=manifest)
    assert res.residue is True and res.dir_removed is False
    assert enc_dir.exists()
    assert (enc_dir / "chunk_3.webm").is_file()              # the late chunk SURVIVES (not in manifest)
    assert (enc_dir / "_CLOSED").is_file()                   # sentinel kept — dir stays eligible (R6)
    assert not (enc_dir / "chunk_1.webm").exists()           # manifest chunks wiped
    assert not (enc_dir / "chunk_1.meta.json").exists()      # + their derived meta sidecars
    assert not (enc_dir / "chunk_2.webm").exists()


def test_relocate_and_wipe_removes_closed_last_then_rmdir_on_clean(tmp_path):
    # R6: on a CLEAN wipe (all manifest files gone, ledger relocated) _CLOSED is removed LAST and the
    # dir is rmdir'd — no residue, no leaked PHI-named dir.
    enc_dir = tmp_path / "inbox" / "clean-visit"
    enc_dir.mkdir(parents=True)
    (enc_dir / "chunk_1.webm").write_bytes(b"a")
    (enc_dir / "chunk_1.meta.json").write_text("{}", encoding="utf-8")
    (enc_dir / "_CLOSED").write_text("{}", encoding="utf-8")
    res = ret_mod._relocate_and_wipe(enc_dir, _ENC, tmp_path / "retained",
                                     chunk_paths=[enc_dir / "chunk_1.webm"])
    assert res.residue is False and res.dir_removed is True
    assert not enc_dir.exists()


def test_disposal_residue_keeps_closed_sentinel_for_retry(tmp_path):
    # findings 15/40: a disposal that leaves residue (an unexpected nested subdir blocks the
    # non-recursive rmdir) must NOT destroy _CLOSED — the eligibility sentinel survives so the
    # encounter re-qualifies for disposal next sweep (never one-shot-escalate then forever-leak).
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=False, with_closed=True)
    (enc_dir / "editor-cache").mkdir()
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert (enc_dir / "_CLOSED").is_file()                   # sentinel KEPT — still disposal-eligible
    assert enc_dir.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the 0o000 unsearchable-dir perms")
def test_relocate_and_wipe_never_raises_on_unsearchable_dir(tmp_path):
    # findings 11/41: the 'Never raises' contract must be TRUE — an unsearchable (0o000) enc dir folds
    # into residue, never a PermissionError that reroutes PHI-residue from wipe_incomplete to a
    # generic encounter_error.
    enc_dir = tmp_path / "inbox" / "locked-visit"
    enc_dir.mkdir(parents=True)
    (enc_dir / "chunk_1.webm").write_bytes(b"a")
    os.chmod(enc_dir, 0o000)
    try:
        res = ret_mod._relocate_and_wipe(enc_dir, _ENC, tmp_path / "retained",
                                         chunk_paths=[enc_dir / "chunk_1.webm"])
    finally:
        os.chmod(enc_dir, 0o700)                             # restore so tmp cleanup can descend
    assert res.residue is True                               # folded into residue — no raise


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses 0o500 unlink-denial perms")
def test_unlink_quiet_returns_false_on_real_eacces_true_on_missing(tmp_path):
    # findings 26/31: _unlink_quiet swallows ONLY a missing file (→True); a REAL EACCES/EPERM must
    # return False so the caller COUNTS it. Pins the helper DIRECTLY (not via a monkeypatched stub) —
    # the swallow-all revert (`except OSError: return True`) must FAIL this (mutation-verified).
    d = tmp_path / "locked"
    d.mkdir()
    victim = d / "chunk_1.webm"
    victim.write_bytes(b"phi")
    os.chmod(d, 0o500)                                       # r-x: can stat but not unlink (EACCES)
    try:
        assert ret_mod._unlink_quiet(victim) is False       # real failure NEVER swallowed as success
    finally:
        os.chmod(d, 0o700)
    assert ret_mod._unlink_quiet(d / "does-not-exist") is True  # missing → True (idempotent)


def test_dispose_empty_disposes_unclosed_zero_chunk_dir(tmp_path):
    # E-extension (seal-half): a zero-chunk NOT-closed dir with dispose_empty=True (the sweep's
    # stale-abandoned gate) is DISPOSED — PHI-named dir removed, ledger relocated, NO retention event,
    # no crypto needed. Inherits the manifest-scoped (chunk_paths=[]) + _CLOSED-last semantics.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=True, with_closed=False)
    with structlog.testing.capture_logs() as cap:
        out = seal_encounter(enc_dir, _ENC, events=ev, sealer=_FakeSealer(),
                             recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained",
                             dispose_empty=True)
    assert out.status == SEAL_STATUS_EMPTY_DISPOSED
    assert not enc_dir.exists()                              # PHI-named dir removed
    assert ev.query(CLINICAL, family="retention") == []      # nothing sealed ⇒ NO retention event
    assert (tmp_path / "retained" / "transcripts" / f"{_ENC}.transcript.json").is_file()  # ledger kept
    assert [c for c in cap if c["event"] == "scribe.retention.empty_encounter_disposed"]


def test_unclosed_zero_chunk_without_dispose_flag_left_alone(tmp_path):
    # Without dispose_empty (default) an OPEN zero-chunk dir is still left alone (no_chunks) — the
    # E-extension only disposes when the sweep positively determines stale-abandonment.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=False, with_closed=False)
    out = seal_encounter(enc_dir, _ENC, events=ev, sealer=_FakeSealer(),
                         recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained")
    assert out.status == SEAL_STATUS_NO_CHUNKS
    assert enc_dir.exists()


# ====== manifest-sidecar recovery: subset / truncated-blob / sealer-None (R3/R4/R5) ======


def test_recovery_completes_crash_mid_wipe_subset(tmp_path):
    # R3 (findings 20/29): a crash DURING the step-5 wipe leaves a strict SUBSET of the sealed chunks.
    # Recovery recognizes each present chunk matches its sidecar entry (seq→sha) and COMPLETES the
    # wipe — never a permanent recovery_mismatch escalation, never the misdiagnosing 'NEW audio' text.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=3)
    _write_recovery_artifacts(tmp_path, enc_dir, ev)     # sidecar/row attest all 3 chunks
    (enc_dir / "chunk_1.webm").unlink()                  # crashed after chunk_1 was already wiped
    (enc_dir / "chunk_1.meta.json").unlink()
    out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_ALREADY_SEALED      # the subset {2,3} completes, no escalation
    assert not enc_dir.exists()
    assert len(_sealed_rows(ev)) == 1


def test_recovery_refuses_truncated_blob_via_sidecar_digest(tmp_path):
    # R4 / findings 8/21/23: a blob that passes the structural check but is TRUNCATED/CORRUPT (its sha
    # no longer matches the sidecar's blob_sha256) must NOT be wiped against — recovery_mismatch,
    # plaintext INTACT. The pre-fix prefix-only gate wiped the never-retrievable audio here.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _write_recovery_artifacts(tmp_path, enc_dir, ev)     # sidecar records the FULL blob's sha
    blob_path = tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}"
    blob_path.write_bytes(b"FAKESEAL1-TRUNCATED")        # well-formed prefix, DIFFERENT sha
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_RECOVERY_MISMATCH
    assert (enc_dir / "chunk_1.webm").is_file()          # plaintext NEVER wiped against a corrupt blob
    assert [c for c in cap if c["event"] == "scribe.retention.recovery_blob_corrupt"]


def test_recovery_refuses_missing_sidecar(tmp_path):
    # The sidecar is the recovery reference — without it (deleted/corrupt) recovery cannot subset-verify
    # and MUST fail closed, plaintext intact.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _emit_sealed_row_for(ev, enc_dir)                    # row present
    _write_recovery_blob(tmp_path)                       # blob present, but NO sidecar
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_RECOVERY_MISMATCH
    assert (enc_dir / "chunk_1.webm").is_file()
    assert [c for c in cap if c["event"] == "scribe.retention.recovery_sidecar_mismatch"]


def test_recovery_refuses_wipe_when_sealer_none_and_plaintext_present(tmp_path):
    # R5 (findings 9/14/24/38, blob-present variant): a chain row + on-disk plaintext + blob but NO
    # sealer (pyrage lost from the venv) must NOT AttributeError on None.verify_wellformed — it
    # fail-closes to recovery_mismatch with plaintext intact.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _write_recovery_artifacts(tmp_path, enc_dir, ev)
    with structlog.testing.capture_logs() as cap:
        out = seal_encounter(enc_dir, _ENC, events=ev, sealer=None,
                             recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained")
    assert out.status == SEAL_STATUS_RECOVERY_MISMATCH
    assert (enc_dir / "chunk_1.webm").is_file()          # plaintext intact — no crash, no wipe
    assert [c for c in cap if c["event"] == "scribe.retention.recovery_sealer_unavailable"]


def test_recovery_disposes_empty_dir_when_sealer_none(tmp_path):
    # R5 (findings 9/14/24/38, the core): a CLOSED zero-chunk dir with a PRIOR sealed row + blob but
    # NO sealer must DISPOSE (needs no crypto) — never an AttributeError-loop that leaks the PHI-named
    # dir forever. The dir is removed; no re-emit.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=0, with_ledger=False, with_closed=True)
    ev.retention_sealed(subject_id=_ENC, chunk_count=2, total_bytes=10, manifest_sha256="d" * 64,
                        sealed_to_key_fp="fp", cipher=SEAL_CIPHER)   # prior seal, dir re-created empty
    _write_recovery_blob(tmp_path)
    out = seal_encounter(enc_dir, _ENC, events=ev, sealer=None,
                         recipient_public_key=_TEST_PUBKEY, retained_dir=tmp_path / "retained")
    assert out.status == SEAL_STATUS_ALREADY_SEALED      # disposed via the zero-chunk no-crypto path
    assert not enc_dir.exists()                          # PHI-named dir removed, not AttributeError-stuck
    assert len(_sealed_rows(ev)) == 1                    # no re-emit


def test_age_verify_wellformed_rejects_truncated_blob(tmp_path):
    # R4 (findings 8/21/23): the structural age-v1 check rejects a blob truncated before its MAC line
    # or with an empty payload — cases the old 19-byte prefix check accepted.
    pytest.importorskip("pyrage")
    from alfred.scribe.retention import AgeSealer, generate_keypair
    pub, _priv = generate_keypair()
    sealer = AgeSealer()
    blob = sealer.seal(b"some-audio-bytes-for-the-tar", pub)
    assert sealer.verify_wellformed(blob) is True              # a full, well-formed age blob passes
    assert sealer.verify_wellformed(blob[:20]) is False        # truncated to the intro (no MAC line)
    assert sealer.verify_wellformed(b"age-encryption.org/") is False   # the OLD prefix no longer passes
    mac = blob.find(b"\n--- ")
    payload_start = blob.find(b"\n", mac + 5) + 1
    assert sealer.verify_wellformed(blob[:payload_start]) is False      # header intact, EMPTY payload


def test_recovery_wipe_obstruction_surfaces_incomplete(tmp_path, monkeypatch):
    # finding 10 (recovery leg unpinned): an already-sealed recovery whose wipe leaves PHI residue (a
    # real unlink failure) must report wipe_incomplete + NOT the clean 'already_sealed' line — the
    # mutant that drops the recovery-leg `if wipe.residue:` branch must FAIL this.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    _write_recovery_artifacts(tmp_path, enc_dir, ev)
    real = ret_mod._unlink_quiet

    def _fail_on_chunks(path):
        if path.name.startswith("chunk_") and path.name.endswith(".webm"):
            return False
        return real(path)

    monkeypatch.setattr(ret_mod, "_unlink_quiet", _fail_on_chunks)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert (enc_dir / "chunk_1.webm").is_file()          # PHI still on disk (not falsely 'wiped')
    assert [c for c in cap if c["event"] == "scribe.retention.wipe_incomplete"]
    assert not [c for c in cap if c["event"] == "scribe.retention.already_sealed"]


def test_transient_wipe_obstruction_surfaces_incomplete(tmp_path, monkeypatch):
    # finding 10 (transient leg unpinned): a transient wipe that leaves PHI residue must report
    # wipe_incomplete + NOT the clean 'transient_wiped' line — the mutant dropping the transient-leg
    # `if wipe.residue:` branch must FAIL this.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    real = ret_mod._unlink_quiet

    def _fail_on_chunks(path):
        if path.name.startswith("chunk_") and path.name.endswith(".webm"):
            return False
        return real(path)

    monkeypatch.setattr(ret_mod, "_unlink_quiet", _fail_on_chunks)
    with structlog.testing.capture_logs() as cap:
        out = _seal(tmp_path, enc_dir, ev, mode=RETENTION_MODE_TRANSIENT)
    assert out.status == SEAL_STATUS_WIPE_INCOMPLETE
    assert (enc_dir / "chunk_1.webm").is_file()
    assert [c for c in cap if c["event"] == "scribe.retention.wipe_incomplete"]
    assert not [c for c in cap if c["event"] == "scribe.retention.transient_wiped"]


# ====== atomic-write durability (R7 / R11 — findings 7/22/25/27/28/39) ======


def test_write_all_drains_short_writes(tmp_path, monkeypatch):
    # finding 27 (R11): _write_all writes EVERY byte even when os.write returns a SHORT count (a single
    # os.write can transfer < len — the >2 GiB cap / ENOSPC / a signal). Cap os.write at 7 bytes/call
    # and assert the full payload lands; the single-`os.write` mutant would truncate at 7 bytes.
    real_write = os.write
    calls = []

    def _short_write(fd, data):
        n = real_write(fd, bytes(data[:7]))                  # transfer at most 7 bytes per call
        calls.append(n)
        return n

    payload = b"x" * 100
    target = tmp_path / "blob.bin"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        monkeypatch.setattr(os, "write", _short_write)
        ret_mod._write_all(fd, payload)
    finally:
        os.close(fd)
    assert target.read_bytes() == payload                    # every byte written despite short writes
    assert len(calls) >= 2 and max(calls) <= 7               # the loop actually iterated (not one write)


def test_atomic_write_unlinks_tmp_on_replace_failure(tmp_path, monkeypatch):
    # finding 28: an os.replace failure (EROFS/EACCES between fsync and rename) must NOT leave the
    # full-payload .tmp behind — after fsync the tmp holds the COMPLETE (possibly plaintext transcript)
    # payload, which the 13d destroy path would miss by canonical name.
    target = tmp_path / "retained" / "x.transcript.json"

    def _boom_replace(src, dst):
        raise OSError("EROFS: read-only fs after an IO error")

    monkeypatch.setattr(os, "replace", _boom_replace)
    with pytest.raises(OSError):
        ret_mod._atomic_write_bytes(target, b"full-plaintext-transcript-payload")
    assert not target.with_name(target.name + ".tmp").exists()   # tmp cleaned up on the replace failure
    assert not target.exists()


def test_blob_rename_dir_fsync_fires_after_replace(tmp_path, monkeypatch):
    # finding 25 (R11): _atomic_write_bytes must fsync the parent dir AFTER os.replace so the RENAME
    # (the dirent) is power-loss durable BEFORE the caller's durable retention.sealed row. Spy-pin the
    # call ORDER — the mutant dropping the post-replace _fsync_dir ('remove redundant fsyncs') must
    # FAIL this. Durability has no functional end-state assert; a call/order spy is the in-style pin.
    calls = []
    real_replace = os.replace
    real_fsync_dir = ret_mod._fsync_dir

    def _spy_replace(src, dst):
        calls.append(("replace", str(dst)))
        return real_replace(src, dst)

    def _spy_fsync_dir(path):
        calls.append(("fsync_dir", str(path)))
        return real_fsync_dir(path)

    monkeypatch.setattr(os, "replace", _spy_replace)
    monkeypatch.setattr(ret_mod, "_fsync_dir", _spy_fsync_dir)
    target = tmp_path / "retained" / "blob.age"
    ret_mod._atomic_write_bytes(target, b"age-blob-bytes")
    assert ("replace", str(target)) in calls
    ri = max(i for i, c in enumerate(calls) if c == ("replace", str(target)))
    assert any(c == ("fsync_dir", str(target.parent)) for c in calls[ri + 1:]), \
        "the parent-dir fsync must fire AFTER os.replace (rename durability)"


def test_mkdir_durable_fsyncs_parent_of_each_created_level(tmp_path, monkeypatch):
    # findings 7/39: creating retained/ + retained/transcripts/ must fsync the PARENT of each new
    # level (POSIX: a new dir's dirent lives in its PARENT). The pre-fix code fsynced the newly created
    # dir ITSELF, leaving the dirents that NAME the subtree non-durable → assert the PARENTS are synced.
    fsynced = []
    real_fsync_dir = ret_mod._fsync_dir

    def _spy(path):
        fsynced.append(str(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(ret_mod, "_fsync_dir", _spy)
    base = tmp_path / "data"
    base.mkdir()
    target = base / "retained" / "transcripts" / "x.json"
    ret_mod._atomic_write_bytes(target, b"payload")
    assert str(base) in fsynced                              # records the 'retained' dirent
    assert str(base / "retained") in fsynced                # records the 'transcripts' dirent


def test_seal_aborts_before_durable_row_on_dir_fsync_failure(tmp_path, monkeypatch):
    # finding 22: a dir-fsync EIO (the kernel's ONLY signal a rename dirent may be lost) must NOT be
    # swallowed — the seal ABORTS before the durable retention.sealed row + the plaintext wipe.
    # Fail-closed: plaintext intact, no row.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)

    def _boom_fsync_dir(path):
        raise OSError("EIO on the retained dir fsync")

    monkeypatch.setattr(ret_mod, "_fsync_dir", _boom_fsync_dir)
    with pytest.raises(OSError):
        _seal(tmp_path, enc_dir, ev)
    assert (enc_dir / "chunk_1.webm").is_file()              # plaintext intact — never wiped
    assert _sealed_rows(ev) == []                            # no durable row (aborted before step 4)
