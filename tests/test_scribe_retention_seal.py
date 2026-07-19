"""Seal-lifecycle-core contract pins (task #13 slice 13a — design §3, §10).

Contract-first. The FAIL-CLOSED SEAL ORDERING is the highest-stakes invariant (crypto + medico-
legal), so its pins run UNCONDITIONALLY via an injected deterministic fake sealer (no crypto dep) —
per the regression-pin-unconditional rule. Only the ACTUAL-crypto round-trip is dep-gated behind
``cryptography`` (design §10 last bullet). Covered here:

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
    _emit_sealed_row_for(ev, enc_dir)        # row whose manifest matches the on-disk chunks
    _write_recovery_blob(tmp_path)           # a durable, well-formed blob at the recovery path
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
    # arrives under the same label → same encounter_id. The blob exists, but the on-disk manifest is
    # the NEW audio, not the row's → refuse the wipe, preserve the new audio, recovery_mismatch.
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path)
    # a sealed row for DIFFERENT (original) audio — a wrong manifest vs the current chunks
    ev.retention_sealed(subject_id=_ENC, chunk_count=2, total_bytes=10, manifest_sha256="d" * 64,
                        sealed_to_key_fp="fp", cipher=SEAL_CIPHER)
    _write_recovery_blob(tmp_path)           # the ORIGINAL archive blob exists
    with structlog.testing.capture_logs() as cap:
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


# ============================ actual-crypto round-trip (DEP-GATED) ============================


def test_cryptography_seal_roundtrip_byte_identical(tmp_path):
    pytest.importorskip("cryptography")   # ONLY the real-crypto round-trip is dep-gated (§10)
    from alfred.scribe.retention import (
        CryptographySealer, build_seal_tar, extract_seal_tar, generate_keypair,
    )
    pub, priv = generate_keypair()
    sealer = CryptographySealer()
    assert sealer.cipher == SEAL_CIPHER
    gathered = [(1, "chunk_1.webm", b"audio-one"), (2, "chunk_2.webm", b"audio-two")]
    manifest = [{"seq": s, "sha256": sha256_hex(d), "bytes": len(d)} for (s, _n, d) in gathered]
    tar = build_seal_tar(gathered, manifest)
    blob = sealer.seal(tar, pub)
    assert sealer.verify_wellformed(blob)
    recovered = extract_seal_tar(sealer.unseal(blob, priv))
    assert recovered["chunk_1.webm"] == b"audio-one"
    assert recovered["chunk_2.webm"] == b"audio-two"
    assert SEAL_MANIFEST_NAME in recovered


def test_cryptography_wrong_key_and_tamper_fail(tmp_path):
    pytest.importorskip("cryptography")
    from alfred.scribe.retention import CryptographySealer, SealError, generate_keypair
    pub, priv = generate_keypair()
    _pub2, priv2 = generate_keypair()
    sealer = CryptographySealer()
    blob = sealer.seal(b"secret-audio", pub)
    with pytest.raises(SealError):
        sealer.unseal(blob, priv2)                 # wrong private key → AEAD auth fails
    tampered = bytearray(blob)
    tampered[-1] ^= 0xFF                            # flip a ciphertext byte
    with pytest.raises(SealError):
        sealer.unseal(bytes(tampered), priv)       # even the CORRECT key fails auth on tamper


def test_cryptography_end_to_end_seal_unseals_to_chunks(tmp_path):
    pytest.importorskip("cryptography")
    from alfred.scribe.retention import CryptographySealer, extract_seal_tar, generate_keypair
    pub, priv = generate_keypair()
    ev = _events(tmp_path)
    enc_dir = _make_encounter(tmp_path, n_chunks=2)
    original = {f"chunk_{s}.webm": (enc_dir / f"chunk_{s}.webm").read_bytes() for s in (1, 2)}
    out = seal_encounter(enc_dir, _ENC, events=ev, sealer=CryptographySealer(),
                         recipient_public_key=pub, retained_dir=tmp_path / "retained")
    assert out.status == SEAL_STATUS_SEALED
    blob = (tmp_path / "retained" / f"{_ENC}{SEAL_BLOB_SUFFIX}").read_bytes()
    members = extract_seal_tar(CryptographySealer().unseal(blob, priv))
    for name, data in original.items():
        assert members[name] == data          # byte-identical round-trip


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
