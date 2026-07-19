"""Sealed retained-audio lifecycle — the seal path (task #13 §3, slice 13a).

The SEAL half of the retention lifecycle: an asymmetric per-encounter seal (public key on-box,
private key OFFLINE) executed in the strict fail-closed order (§3.3) —

    tar the audio → seal to the recipient PUBLIC key → self-verify the ciphertext →
    durable ``retention.sealed`` [D] → **ONLY THEN** wipe plaintext.

The ordering is load-bearing and mirrors the #12 withdrawal durable-before-ack contract: a crash
before the durable event leaves plaintext INTACT and unsealed (recoverable — the next sweep
re-seals), NEVER sealed-but-unrecorded or wiped-but-unsealed. "Already sealed" is decided by the
CHAIN (``events.retention_sealed_row``), so the seal is idempotent across a crash at every step.
This slice ships the unit-level :func:`seal_encounter`; the daemon sweep that drives it over READY /
abandoned encounters is slice 13b.

CRYPTO PRIMITIVE — the operator-visible decision, honestly flagged (design §3.1):
    The design RECOMMENDS **age X25519** for the ~10-year archive because an age blob is openable
    with the stock ``age`` binary WITHOUT this codebase (decade-scale standalone retrievability).
    **No age library or ``age``/``rage`` CLI is vendored** in the sovereign venv, and PyNaCl is
    absent; the only vetted asymmetric-crypto library present is **pyca/cryptography** (a transitive
    dep, not yet declared). So the crypto backend is a genuine operator dependency decision, not a
    silent default.

    This module ships the seal lifecycle behind a pluggable :class:`Sealer` and provides
    :class:`CryptographySealer` — a REAL asymmetric X25519 hybrid seal (ephemeral-static ECDH →
    HKDF-SHA256 → AES-256-GCM) built on the vendored ``cryptography`` library. It gives the SAME
    security property age would (the daemon holds only the public key; the offline private key is
    required to open the archive; a live-compromised daemon cannot decrypt), recorded honestly as
    ``cipher = "x25519-hkdf-sha256-aesgcm"`` — NOT falsely ``"age-x25519"``. It is the codebase-
    coupled retrieval FALLBACK the design flagged (same class as the PyNaCl fallback), with an
    honest label. The ``cryptography`` import is LAZY (raises :class:`SealerUnavailable`), so this
    module + the fail-closed-ordering pins load and run WITHOUT any crypto dep (tests inject a
    deterministic fake sealer); only the actual-crypto round-trip is dep-gated (design §10).

    The age-format standalone-openability (§3.1's decisive rationale) is a one-file swap behind the
    :class:`Sealer` interface — add a ``pyrage``/``age`` backend, flip :data:`SEAL_CIPHER` +
    :data:`SEAL_BLOB_SUFFIX`. That swap is the real-key-ceremony (13d) + the operator's crypto-dep
    call; it is NOT a CI blocker and does NOT change the lifecycle, verify, or wipe logic here.

Import direction (frozen): ``scribe.retention → scribe.events → evstore``; retention never imported
back by events/evstore.
"""
from __future__ import annotations

import io
import json
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from alfred.evstore import sha256_hex
from alfred.scribe import ledger
from alfred.scribe.config import RETENTION_MODE_RETAINED, RETENTION_MODE_TRANSIENT

log = structlog.get_logger("scribe.retention")

# --- seal blob format + cipher label (contract constants — later slices IMPORT, never re-derive) --

# The honest cipher label recorded in every ``retention.sealed`` row (NOT "age-x25519" — this is
# the pyca/cryptography X25519 hybrid, not age format). A future age backend flips this constant.
SEAL_CIPHER = "x25519-hkdf-sha256-aesgcm"

# The sealed-blob filename suffix (opaque-id filename, no label leak). DELIBERATELY ".sealed", NOT
# ".age" — the blob is NOT age format, and naming it ".age" would falsely imply stock-``age``
# decryptability. A future age backend flips this to ".age" alongside SEAL_CIPHER.
SEAL_BLOB_SUFFIX = ".sealed"

# STAYC seal blob wire format: MAGIC(10) ‖ ephemeral_pubkey(32) ‖ nonce(12) ‖ AES-256-GCM(ct+tag).
_SEAL_MAGIC = b"STAYCSEAL1"
_EPK_LEN = 32
_NONCE_LEN = 12
_TAG_LEN = 16
_HKDF_INFO = b"stayc-seal-v1"
_X25519_KEY_LEN = 32

# The manifest member name inside the sealed tar (recovered on unseal to re-verify per-chunk shas).
SEAL_MANIFEST_NAME = "manifest.json"

# Chunk filename shape (parse seq from the STEM). Matches the pipeline's ``_CHUNK_NAME_RE`` so seal
# discovery agrees with the accumulator on "what is a chunk": ``chunk_<seq>.<ext>`` → stem
# ``chunk_<seq>`` matches; ``chunk_<seq>.meta.json`` → stem ``chunk_<seq>.meta`` does NOT; ``_CLOSED``
# and ``<enc>.transcript.json`` do NOT. DELIBERATELY codec-agnostic (no audio-extension allowlist):
# retention must seal EVERY audio chunk, so a new/unknown container is never silently left unsealed.
_CHUNK_NAME_RE = re.compile(r"^chunk_(\d+)$")

# --- SealOutcome status vocabulary (slice 13b's sweep summary consumes these) -----------------
SEAL_STATUS_SEALED = "sealed"
SEAL_STATUS_ALREADY_SEALED = "already_sealed"
SEAL_STATUS_NO_CHUNKS = "no_chunks"
SEAL_STATUS_VERIFY_FAILED = "verify_failed"
SEAL_STATUS_TRANSIENT_WIPED = "transient_wiped"


class SealerUnavailable(RuntimeError):
    """The requested crypto backend is not installed (no vendored age lib / ``cryptography``). The
    seal lifecycle + its fail-closed-ordering pins run WITHOUT a backend (a fake sealer is
    injected); only real sealing / the round-trip needs one, so this is raised lazily at
    construction, never at import."""


class SealError(RuntimeError):
    """A seal / unseal operation failed structurally (bad key length, malformed blob, or an AEAD
    authentication failure on decrypt)."""


class Sealer(Protocol):
    """The pluggable seal primitive. An implementation seals plaintext to a recipient PUBLIC key
    (asymmetric — the daemon never holds the private key), structurally verifies a blob WITHOUT the
    private key (the seal-time self-verify, §3.3 step 3), and unseals with the OFFLINE private key
    (the retrieval path, §6 / the round-trip test). ``cipher`` is the honest label recorded in the
    ``retention.sealed`` row so a mixed archive stays self-identifying."""

    cipher: str

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes: ...

    def verify_wellformed(self, blob: bytes) -> bool: ...

    def unseal(self, blob: bytes, private_key: bytes) -> bytes: ...


def key_fingerprint(public_key: bytes) -> str:
    """``sealed_to_key_fp`` = the first 16 hex chars of sha256(recipient pubkey) (§3.1). The
    which-key-for-this-encounter index: retrieval loads the private key whose pubkey matches."""
    return sha256_hex(public_key)[:16]


@dataclass(frozen=True)
class SealOutcome:
    """The result of a :func:`seal_encounter` call — the sweep (13b) consumes ``status`` for its
    summary/ILB signals; the digests/counts mirror the ``retention.sealed`` payload for callers
    that want them without re-reading the chain."""

    status: str
    encounter_id: str
    chunk_count: int = 0
    total_bytes: int = 0
    manifest_sha256: str = ""
    sealed_to_key_fp: str = ""
    cipher: str = ""
    blob_path: str = ""


# --- the concrete crypto backend (vendored pyca/cryptography; age is the future swap) ----------


class CryptographySealer:
    """Asymmetric X25519 hybrid seal on the vendored ``cryptography`` library — ephemeral-static
    ECDH → HKDF-SHA256 → AES-256-GCM, the recipient pubkey bound as AEAD associated data. Real
    asymmetric property (public seals, offline private unseals); codebase-coupled retrieval (the
    honest fallback the design flagged, §3.1). Lazy import → :class:`SealerUnavailable` if
    ``cryptography`` is absent."""

    cipher = SEAL_CIPHER

    def __init__(self) -> None:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric.x25519 import (
                X25519PrivateKey, X25519PublicKey,
            )
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        except ImportError as exc:  # pragma: no cover — exercised via SealerUnavailable pin
            raise SealerUnavailable(
                "no age library or `cryptography` is available to seal — install a crypto backend "
                "(the operator dependency decision, design §3.1). The seal lifecycle + its "
                "fail-closed-ordering pins run without one via an injected fake sealer."
            ) from exc
        self._PrivateKey = X25519PrivateKey
        self._PublicKey = X25519PublicKey
        self._AESGCM = AESGCM
        self._HKDF = HKDF
        self._hashes = hashes
        self._serialization = serialization

    def _raw_public(self, key) -> bytes:
        return key.public_bytes(
            self._serialization.Encoding.Raw, self._serialization.PublicFormat.Raw)

    def _derive_key(self, shared: bytes, epk_raw: bytes, recipient_raw: bytes) -> bytes:
        return self._HKDF(
            algorithm=self._hashes.SHA256(), length=32, salt=None,
            info=_HKDF_INFO + epk_raw + recipient_raw,
        ).derive(shared)

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        if len(recipient_public_key) != _X25519_KEY_LEN:
            raise SealError(
                f"recipient public key must be {_X25519_KEY_LEN} raw X25519 bytes "
                f"(got {len(recipient_public_key)})")
        ephemeral = self._PrivateKey.generate()
        epk_raw = self._raw_public(ephemeral.public_key())
        recipient = self._PublicKey.from_public_bytes(recipient_public_key)
        shared = ephemeral.exchange(recipient)
        key = self._derive_key(shared, epk_raw, recipient_public_key)
        nonce = os.urandom(_NONCE_LEN)
        ct = self._AESGCM(key).encrypt(nonce, plaintext, recipient_public_key)
        return _SEAL_MAGIC + epk_raw + nonce + ct

    def verify_wellformed(self, blob: bytes) -> bool:
        """Structural check WITHOUT the private key (the daemon holds only the public key): the
        magic + a minimum length that admits a header + a bare AEAD tag. This is the seal-time
        self-verify's structural half; the digest-stable (torn-write) half is in
        :func:`seal_encounter`."""
        return (
            len(blob) >= len(_SEAL_MAGIC) + _EPK_LEN + _NONCE_LEN + _TAG_LEN
            and blob[: len(_SEAL_MAGIC)] == _SEAL_MAGIC
        )

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        if not self.verify_wellformed(blob):
            raise SealError("blob is not a well-formed STAYC seal (bad magic / too short)")
        if len(private_key) != _X25519_KEY_LEN:
            raise SealError(
                f"private key must be {_X25519_KEY_LEN} raw X25519 bytes (got {len(private_key)})")
        off = len(_SEAL_MAGIC)
        epk_raw = blob[off:off + _EPK_LEN]
        off += _EPK_LEN
        nonce = blob[off:off + _NONCE_LEN]
        off += _NONCE_LEN
        ct = blob[off:]
        priv = self._PrivateKey.from_private_bytes(private_key)
        recipient_raw = self._raw_public(priv.public_key())
        shared = priv.exchange(self._PublicKey.from_public_bytes(epk_raw))
        key = self._derive_key(shared, epk_raw, recipient_raw)
        try:
            return self._AESGCM(key).decrypt(nonce, ct, recipient_raw)
        except Exception as exc:  # noqa: BLE001 — any AEAD failure is an unseal failure
            raise SealError("seal decryption/authentication failed") from exc


def make_default_sealer() -> Sealer:
    """The production sealer (vendored ``cryptography`` X25519 hybrid). Raises
    :class:`SealerUnavailable` if no crypto backend is installed."""
    return CryptographySealer()


def generate_keypair() -> tuple[bytes, bytes]:
    """Mint a raw X25519 ``(public, private)`` keypair (32 bytes each). The 13d keygen ceremony +
    the round-trip test's TEST keypair. Raises :class:`SealerUnavailable` if ``cryptography`` is
    absent."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise SealerUnavailable("cryptography is not installed — cannot generate a keypair") from exc
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pub_raw, priv_raw


# --- tar / manifest helpers (deterministic; unseal recovers the manifest) ----------------------


def _manifest_digest(manifest_list: list[dict]) -> str:
    """``manifest_sha256`` — sha256 over the CANONICAL bytes of the seq-sorted per-chunk list
    (§3.3). Canonical = sorted keys + tight separators, so the digest is stable across runs."""
    canonical = json.dumps(manifest_list, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_hex(canonical)


def _add_tar_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add one member with NORMALIZED metadata (mtime/uid/gid/uname/gname zeroed, mode 0600) so the
    sealed PLAINTEXT tar is byte-deterministic (no wall-clock / ownership leakage into the blob)."""
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def build_seal_tar(gathered: list[tuple[int, str, bytes]], manifest_list: list[dict]) -> bytes:
    """Build the in-memory tar sealed for an encounter: ``manifest.json`` first, then each
    ``chunk_<seq>.<ext>`` in seq order. ``gathered`` = ``(seq, filename, bytes)`` seq-sorted."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        manifest_bytes = json.dumps(
            manifest_list, sort_keys=True, separators=(",", ":")).encode("utf-8")
        _add_tar_member(tar, SEAL_MANIFEST_NAME, manifest_bytes)
        for _seq, name, data in gathered:
            _add_tar_member(tar, name, data)
    return buf.getvalue()


def extract_seal_tar(tar_bytes: bytes) -> dict[str, bytes]:
    """Recover ``{member_name: bytes}`` from a sealed tar (the round-trip verify + 13d retrieval).
    Guards against path traversal — a member with a ``/`` or ``..`` in its name is refused."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if "/" in name or name.startswith("..") or os.path.isabs(name):
                raise SealError(f"refusing unsafe tar member name {name!r}")
            extracted = tar.extractfile(member)
            out[name] = extracted.read() if extracted is not None else b""
    return out


def _discover_seal_chunks(enc_dir: Path) -> list[tuple[Path, int]]:
    """``(chunk_path, seq)`` for every ``chunk_<seq>.<ext>`` under ``enc_dir``, INTEGER-seq sorted
    (so ``chunk_10`` follows ``chunk_2``). Excludes ``.meta.json`` sidecars, ``_CLOSED``, and the
    transcript ledger by stem-matching (see ``_CHUNK_NAME_RE``)."""
    found: list[tuple[Path, int]] = []
    for p in Path(enc_dir).iterdir():
        if not p.is_file():
            continue
        m = _CHUNK_NAME_RE.match(p.stem)
        if m:
            found.append((p, int(m.group(1))))
    found.sort(key=lambda c: c[1])
    return found


# --- atomic write + plaintext wipe -------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomic, fsync-durable byte write (temp → ``os.replace``, 0600) — the sealed blob must be
    durable on disk BEFORE the durable ``retention.sealed`` row references it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _relocate_and_wipe(enc_dir: Path, encounter_id: str, retained_dir: Path) -> None:
    """§3.3 step 5 + §3.4 disposition — RELOCATE the transcript ledger out of the (possibly-PHI-
    label) encounter dir into ``<retained_dir>/transcripts/`` (kept plaintext under LUKS, Jamie's
    active artifact), then WIPE every remaining plaintext file (chunks, meta, ``_CLOSED``) and
    remove the label-named dir. Idempotent: a no-op if the dir is already gone (the crash-between-4-
    and-5 recovery re-runs this)."""
    enc_dir = Path(enc_dir)
    if not enc_dir.exists():
        return
    src_ledger = ledger.ledger_path(enc_dir, encounter_id)
    if src_ledger.is_file():
        dest = ledger.ledger_path(Path(retained_dir) / "transcripts", encounter_id)
        _atomic_write_bytes(dest, src_ledger.read_bytes())
        _unlink_quiet(src_ledger)
    for p in sorted(enc_dir.iterdir()):
        if p.is_file():
            _unlink_quiet(p)
    try:
        enc_dir.rmdir()
    except OSError:
        log.warning(
            "scribe.retention.enc_dir_not_empty", encounter_id=encounter_id,
            detail="encounter dir not empty after wipe — a residual subdir/entry remains; left in "
                   "place (a directory wipe must never recurse blindly into unexpected content).")


# --- the seal path (§3.3 strict order) ---------------------------------------------------------


def seal_encounter(
    enc_dir: str | Path,
    encounter_id: str,
    *,
    events,
    sealer: Sealer,
    recipient_public_key: bytes,
    retained_dir: str | Path,
    mode: str = RETENTION_MODE_RETAINED,
    now: str | None = None,
) -> SealOutcome:
    """Seal ONE encounter's audio (unit-level; the sweep in 13b drives this over READY / abandoned
    encounters). Fail-closed strict order (§3.3): tar → seal → self-verify → durable
    ``retention.sealed`` [D] → ONLY THEN wipe plaintext. Idempotent via the chain
    (``events.retention_sealed_row``): a crash at any step is recovered by the next call.

    ``recipient_public_key`` is the 32-byte raw X25519 pubkey the caller loaded from the seal-dir
    (tests pass a test pubkey). ``retained_dir`` is the RESOLVED absolute blob-store dir (the caller
    does the empty-⇒-derive resolution; this function takes a concrete path). ``mode`` is the
    resolved ``retained|transient`` posture (§3.5)."""
    enc_dir = Path(enc_dir)
    retained_dir = Path(retained_dir)

    # TRANSIENT posture (§3.5): wipe audio WITHOUT sealing — no retained blob, NO retention.* event,
    # but an explicit observable signal (never a silent default).
    if mode == RETENTION_MODE_TRANSIENT:
        return _transient_wipe(enc_dir, encounter_id, retained_dir)

    # IDEMPOTENCY + crash recovery (§3.3): the CHAIN is the source of truth for "already sealed".
    if events.retention_sealed_row(encounter_id) is not None:
        # Crash between step 4 (durable event) and step 5 (wipe): the seal is COMMITTED. Complete
        # the wipe (idempotent — a no-op if already wiped). NEVER double-emit.
        _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
        log.info(
            "scribe.retention.already_sealed", encounter_id=encounter_id,
            detail="retention.sealed already on the chain — completed the plaintext wipe "
                   "(idempotent crash-between-event-and-wipe recovery); no re-emit.")
        return SealOutcome(status=SEAL_STATUS_ALREADY_SEALED, encounter_id=encounter_id)

    # Step 1 — gather chunks (seq order), build the per-chunk manifest + digests.
    chunks = _discover_seal_chunks(enc_dir)
    if not chunks:
        # ILB: ran, nothing to seal (no audio on disk — a declined/empty encounter, or already
        # wiped-without-event which is impossible after step 4's ordering).
        log.info(
            "scribe.retention.no_chunks", encounter_id=encounter_id,
            detail="no audio chunks present — nothing to seal (ran, nothing to do).")
        return SealOutcome(status=SEAL_STATUS_NO_CHUNKS, encounter_id=encounter_id)
    gathered = [(seq, p.name, p.read_bytes()) for (p, seq) in chunks]
    manifest_list = [
        {"seq": seq, "sha256": sha256_hex(data), "bytes": len(data)}
        for (seq, _name, data) in gathered
    ]
    total_bytes = sum(int(m["bytes"]) for m in manifest_list)
    chunk_count = len(manifest_list)
    manifest_sha256 = _manifest_digest(manifest_list)
    tar_bytes = build_seal_tar(gathered, manifest_list)

    # Step 2 — seal to the recipient PUBLIC key; atomic-write the blob (fsync-durable).
    blob = sealer.seal(tar_bytes, recipient_public_key)
    blob_sha = sha256_hex(blob)
    blob_path = retained_dir / f"{encounter_id}{SEAL_BLOB_SUFFIX}"
    _atomic_write_bytes(blob_path, blob)

    # Step 3 — SELF-VERIFY (no private key): re-read + digest-stable (catches a torn write) +
    # structural well-formed. NEVER wipe plaintext on an unverified seal.
    reread = blob_path.read_bytes()
    if sha256_hex(reread) != blob_sha or not sealer.verify_wellformed(reread):
        _unlink_quiet(blob_path)  # drop the unverified blob; the next sweep re-seals cleanly
        log.error(
            "scribe.retention.seal_verify_failed", encounter_id=encounter_id,
            chunk_count=chunk_count,
            detail="sealed blob failed self-verify (digest-unstable or malformed) — ABORTED, "
                   "plaintext left intact, retry next sweep. Plaintext is NEVER wiped on an "
                   "unverified seal.")
        return SealOutcome(
            status=SEAL_STATUS_VERIFY_FAILED, encounter_id=encounter_id,
            chunk_count=chunk_count, total_bytes=total_bytes, manifest_sha256=manifest_sha256)

    # Step 4 — DURABLE retention.sealed [D]; RAISES on a store-down append (fail-closed). The seal
    # is NOT acknowledged and plaintext is NOT wiped until this commits — exactly the #12
    # withdrawal durable-before-ack ordering. A raise PROPAGATES: the blob-without-event state is
    # recovered by the next sweep (re-seal overwrites the blob, re-emits) — never wiped-but-unsealed.
    fp = key_fingerprint(recipient_public_key)
    events.retention_sealed(
        subject_id=encounter_id, chunk_count=chunk_count, total_bytes=total_bytes,
        manifest_sha256=manifest_sha256, sealed_to_key_fp=fp, cipher=sealer.cipher, now=now)

    # Step 5 — ONLY NOW wipe plaintext (relocate the transcript ledger first, §3.4).
    _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
    log.info(
        "scribe.retention.sealed", encounter_id=encounter_id, chunk_count=chunk_count,
        total_bytes=total_bytes, cipher=sealer.cipher, sealed_to_key_fp=fp,
        detail="encounter audio sealed + plaintext wiped (retained-encrypted archive).")
    return SealOutcome(
        status=SEAL_STATUS_SEALED, encounter_id=encounter_id, chunk_count=chunk_count,
        total_bytes=total_bytes, manifest_sha256=manifest_sha256, sealed_to_key_fp=fp,
        cipher=sealer.cipher, blob_path=str(blob_path))


def _transient_wipe(enc_dir: Path, encounter_id: str, retained_dir: Path) -> SealOutcome:
    """§3.5 transient posture — wipe the audio WITHOUT sealing. Emits NO ``retention.*`` event
    (there is no sealed artifact to attest), but a loud, counted structlog signal so "wiped, not
    retained" is distinguishable from "nothing to do" (intentionally-left-blank). The transcript is
    still relocated + kept (only the dense audio is dropped)."""
    chunk_count = len(_discover_seal_chunks(enc_dir))
    _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
    log.warning(
        "scribe.retention.transient_wiped", encounter_id=encounter_id, chunk_count=chunk_count,
        detail="retention.mode=transient — audio WIPED without sealing (no retained archive, no "
               "retention.* event); transcript relocated + kept. A deliberate, config-visible "
               "posture (§3.5), never the silent default.")
    return SealOutcome(
        status=SEAL_STATUS_TRANSIENT_WIPED, encounter_id=encounter_id, chunk_count=chunk_count)
