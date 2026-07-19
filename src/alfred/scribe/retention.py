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
from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
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
# Wipe residue after a COMMITTED seal (durable row + blob durable) — some plaintext could NOT be
# removed (a real unlink failure, an un-relocated ledger, or an unexpected nested entry). NEVER
# reported as clean SEALED: the sweep surfaces it for operator escalation; the next sweep retries.
SEAL_STATUS_WIPE_INCOMPLETE = "wipe_incomplete"
# The already-sealed recovery FAILED CLOSED — the chain row exists but the blob is missing/malformed
# in THIS retained_dir, OR the on-disk plaintext does not match the row's manifest (a re-opened
# same-label encounter). Plaintext is left INTACT; the operator must reconcile (§3.3 findings 2/3/7).
SEAL_STATUS_RECOVERY_MISMATCH = "recovery_mismatch"
# A CLOSED zero-chunk encounter (clinician opened, no audio, /close) was DISPOSED — nothing to seal,
# so no retention.* event, but the label-named dir (whose NAME is PHI) is removed (§E ruling).
SEAL_STATUS_EMPTY_DISPOSED = "empty_disposed"


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


def _fsync_dir(path: Path) -> None:
    """fsync a DIRECTORY fd so a rename/create within it is power-loss durable — POSIX rename
    durability requires fsyncing the CONTAINING dir, not just the file (the file fsync alone leaves
    the dir entry in the page cache). Mirrors ``evstore._fsync_dir`` (store.py:594), the discipline
    the repo already applies to durable event appends. Best-effort: a dir that cannot be opened for
    fsync never crashes the seal (the fail direction is safe — a lost rename leaves plaintext, and
    the already-sealed recovery is now blob-existence-gated)."""
    try:
        dfd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _write_all(fd: int, data: bytes) -> None:
    """Write EVERY byte of ``data`` — a single ``os.write`` / write(2) may transfer a SHORT count
    without error (Linux caps one write at ~2 GiB; ENOSPC returns short; a signal can interrupt), so
    discarding the return silently truncates a >2 GiB blob — and ``max_encounter_bytes`` defaults to
    exactly 2 GiB. Loop until the buffer is fully drained; a zero/negative return is a hard IO error."""
    mv = memoryview(data)
    total = 0
    while total < len(mv):
        n = os.write(fd, mv[total:])
        if n <= 0:  # pragma: no cover — defensive; a healthy write(2) never returns 0 on a nonempty buf
            raise OSError(f"os.write returned {n} with {len(mv) - total} bytes unwritten")
        total += n


def _unlink_quiet(path: Path) -> bool:
    """Unlink, tolerating ONLY a missing file (the idempotent-recovery case). Returns ``True`` on
    success or already-gone, ``False`` on a REAL unlink failure (EACCES/EPERM/EROFS/EBUSY) so the
    caller can COUNT it. A real failure is NEVER swallowed as success — that is exactly what let a
    seal log 'plaintext wiped' with PHI still on disk (findings 5/8)."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomic, fsync-durable byte write: temp → FULL write → fsync(file) → ``os.replace`` →
    fsync(dir), 0600. The DIRECTORY fsync makes the RENAME durable, so the sealed blob is truly on
    stable storage BEFORE the durable ``retention.sealed`` row references it (§3.3 step 2, findings
    1/10/11); the full-write loop defeats a short ``os.write`` (findings 9/13/17); the tmp is
    unlinked on ANY write/fsync failure so a partial (possibly-plaintext) file is never left behind
    (finding 21). Raises on a write/fsync error (the caller's fail-closed path handles it)."""
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created:
        _fsync_dir(parent)  # make the retained/ (transcripts/) dir creation itself durable
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink_quiet(tmp)  # never leave a partial (possibly-plaintext transcript) .tmp behind
        raise
    else:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(parent)      # make the RENAME (the dir entry) power-loss durable (POSIX)


@dataclass(frozen=True)
class _WipeResult:
    """Outcome of :func:`_relocate_and_wipe` — lets ``seal_encounter`` distinguish a CLEAN wipe from
    one that left PHI residue (a real unlink failure, an un-relocated ledger, or an unexpected nested
    entry that blocks the rmdir). ``residue`` True ⇒ the label-named dir persists and the outcome
    must NOT be reported as clean SEALED (findings 5/8/16)."""

    ledger_relocated: bool = False
    unlink_failures: int = 0
    dir_removed: bool = False
    residue: bool = False


def _relocate_and_wipe(enc_dir: Path, encounter_id: str, retained_dir: Path) -> _WipeResult:
    """§3.3 step 5 + §3.4 disposition — RELOCATE the transcript ledger out of the (possibly-PHI-
    label) encounter dir into ``<retained_dir>/transcripts/`` (kept plaintext under LUKS, Jamie's
    active artifact), then WIPE every remaining plaintext file (chunks, meta, ``_CLOSED``) and
    remove the label-named dir. Idempotent: a no-op if the dir is already gone (the crash-between-4-
    and-5 recovery re-runs this). Never raises — IO failures fold into the returned :class:`_WipeResult`
    so the caller surfaces residue rather than a false 'wiped' claim."""
    enc_dir = Path(enc_dir)
    if not enc_dir.exists():
        return _WipeResult(dir_removed=True, residue=False)  # already gone — a clean idempotent no-op

    ledger_relocated = False
    ledger_residue = False
    src_ledger = ledger.ledger_path(enc_dir, encounter_id)
    if src_ledger.is_file():
        dest = ledger.ledger_path(Path(retained_dir) / "transcripts", encounter_id)
        try:
            src_bytes = src_ledger.read_bytes()
            _atomic_write_bytes(dest, src_bytes)
            # DIGEST-VERIFY the relocated copy BEFORE destroying the source — never unlink the only
            # other copy of a keep-forever clinical transcript (§3.4) against a truncated/unverified
            # destination (finding 4). A short write already raised inside _atomic_write_bytes; this
            # catches a torn replace / silent divergence too.
            relocated_ok = sha256_hex(dest.read_bytes()) == sha256_hex(src_bytes)
        except OSError:
            relocated_ok = False
        if relocated_ok and _unlink_quiet(src_ledger):
            ledger_relocated = True
        else:
            ledger_residue = True
            log.error(
                "scribe.retention.ledger_relocate_verify_failed", encounter_id=encounter_id,
                detail="the relocated transcript did not verify against the source (or the source "
                       "could not be unlinked) — source KEPT (never destroy the only good copy of a "
                       "keep-forever clinical transcript). Wipe flagged incomplete; retry next sweep.")

    unlink_failures = 0
    for p in sorted(enc_dir.iterdir()):
        if p.is_file():
            if not _unlink_quiet(p):
                unlink_failures += 1
    dir_removed = False
    try:
        enc_dir.rmdir()
        dir_removed = True
    except OSError:
        # The dir still holds an entry: an un-unlinked PHI file (unlink_failures>0), the kept ledger
        # source (ledger_residue), OR an unexpected nested subdir/entry (the wipe is deliberately
        # NON-recursive — never recurse blindly into unexpected content). The label-named dir NAME is
        # itself PHI, so this is real residue — attributed HONESTLY, not as a bare 'residual subdir'.
        log.error(
            "scribe.retention.enc_dir_not_empty", encounter_id=encounter_id,
            unlink_failures=unlink_failures, ledger_residue=ledger_residue,
            detail="encounter dir NOT empty after wipe — plaintext residue remains (un-unlinkable PHI "
                   "file, an un-relocated transcript, or an unexpected nested entry). The label-named "
                   "dir (a PHI name) persists; the seal outcome is flagged wipe_incomplete.")
    residue = bool(unlink_failures) or ledger_residue or not dir_removed
    return _WipeResult(
        ledger_relocated=ledger_relocated, unlink_failures=unlink_failures,
        dir_removed=dir_removed, residue=residue)


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

    # IDEMPOTENCY + crash recovery (§3.3): the CHAIN says "a durable retention.sealed landed" — but
    # NOT that the blob survived, nor that today's on-disk plaintext is the SAME audio the row
    # attests. The recovery is FAIL-CLOSED (findings 2/3/7): it verifies the blob + the manifest
    # BEFORE wiping, and never wipes uncovered / row-without-blob PHI.
    sealed_row = events.retention_sealed_row(encounter_id)
    if sealed_row is not None:
        return _recover_already_sealed(
            enc_dir, encounter_id, retained_dir, sealed_row, sealer)

    # Step 1 — gather chunks (seq order), build the per-chunk manifest + digests.
    chunks = _discover_seal_chunks(enc_dir)
    if not chunks:
        # A CLOSED zero-chunk encounter (clinician opened, no audio, /close) must be DISPOSED — the
        # label-named dir NAME is itself PHI, and nothing else ever cleans it (no seal row is ever
        # created), so leaving it would leak a patient-named dir forever while logging 'nothing to
        # do' (§E ruling, finding 12). An OPEN (un-closed) zero-chunk dir is genuinely mid-flight →
        # leave it (a later chunk may still arrive).
        if (enc_dir / CLOSE_SENTINEL_NAME).exists():
            wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
            log.info(
                "scribe.retention.empty_encounter_disposed", encounter_id=encounter_id,
                residue=wipe.residue,
                detail="a CLOSED zero-chunk encounter was disposed — nothing was sealed (NO "
                       "retention.* event), but the label-named dir (a PHI name) is removed. Never "
                       "leave a patient-named dir with 'nothing to do' logged forever.")
            return SealOutcome(
                status=(SEAL_STATUS_WIPE_INCOMPLETE if wipe.residue else SEAL_STATUS_EMPTY_DISPOSED),
                encounter_id=encounter_id)
        log.info(
            "scribe.retention.no_chunks", encounter_id=encounter_id,
            detail="no audio chunks present + not closed — nothing to seal yet (ran, nothing to do).")
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

    # Step 5 — ONLY NOW wipe plaintext (relocate the transcript ledger first, §3.4). A wipe that
    # leaves residue (real unlink failure / un-relocated ledger / unexpected nested entry) must NOT
    # be reported as clean SEALED — the seal + durable row DID commit, but plaintext PHI is still on
    # disk, so the outcome is wipe_incomplete for operator escalation (findings 5/8/16).
    wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
    if wipe.residue:
        log.error(
            "scribe.retention.wipe_incomplete", encounter_id=encounter_id, chunk_count=chunk_count,
            unlink_failures=wipe.unlink_failures,
            detail="the durable retention.sealed row committed + the blob is sealed, but plaintext "
                   "PHI could NOT be fully wiped — the label-named dir persists. Status=wipe_incomplete "
                   "for operator escalation; the next sweep retries the wipe (idempotent via the chain).")
        return SealOutcome(
            status=SEAL_STATUS_WIPE_INCOMPLETE, encounter_id=encounter_id, chunk_count=chunk_count,
            total_bytes=total_bytes, manifest_sha256=manifest_sha256, sealed_to_key_fp=fp,
            cipher=sealer.cipher, blob_path=str(blob_path))
    log.info(
        "scribe.retention.sealed", encounter_id=encounter_id, chunk_count=chunk_count,
        total_bytes=total_bytes, cipher=sealer.cipher, sealed_to_key_fp=fp,
        detail="encounter audio sealed + plaintext wiped (retained-encrypted archive).")
    return SealOutcome(
        status=SEAL_STATUS_SEALED, encounter_id=encounter_id, chunk_count=chunk_count,
        total_bytes=total_bytes, manifest_sha256=manifest_sha256, sealed_to_key_fp=fp,
        cipher=sealer.cipher, blob_path=str(blob_path))


def _recover_already_sealed(
    enc_dir: Path, encounter_id: str, retained_dir: Path, sealed_row: dict, sealer: Sealer,
) -> SealOutcome:
    """FAIL-CLOSED crash-between-durable-event-and-wipe recovery (§3.3, findings 2/3/7). The chain row
    proves the durable append landed — NOT that the blob survived (finding 1's lost rename, a
    retained_dir migration, a blob-store mishap), nor that today's on-disk plaintext is the SAME
    audio the row attests (a re-opened same-label encounter maps to the same ``encounter_id`` and
    accumulates NEW consented audio). Before wiping ANY plaintext this requires, in order:
      (i)   the sealed blob EXISTS in the CALLER's ``retained_dir`` + is structurally well-formed,
      (ii)  if plaintext chunks are present, their manifest EXACTLY matches the row's own
            ``chunk_count`` + ``manifest_sha256`` (the manifest is plaintext-deterministic).
    Any mismatch → loud ERROR, NO wipe, ``recovery_mismatch`` for operator escalation. NEVER wipe on
    a row-without-blob, and NEVER destroy audio the sealed row does not cover."""
    enc_dir = Path(enc_dir)
    blob_path = Path(retained_dir) / f"{encounter_id}{SEAL_BLOB_SUFFIX}"

    # (i) the blob must EXIST in this retained_dir and be structurally well-formed (no private key).
    if not blob_path.is_file():
        log.error(
            "scribe.retention.recovery_blob_missing", encounter_id=encounter_id,
            detail="chain says retention.sealed but the sealed blob is ABSENT in this retained_dir — "
                   "REFUSING to wipe plaintext (a row-without-blob would destroy the only copy of PHI "
                   "the chain falsely attests is retrievable). Operator must reconcile.")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)
    try:
        blob = blob_path.read_bytes()
    except OSError:
        blob = b""
    if not blob or not sealer.verify_wellformed(blob):
        log.error(
            "scribe.retention.recovery_blob_malformed", encounter_id=encounter_id,
            detail="the sealed blob is unreadable / not well-formed — REFUSING to wipe plaintext "
                   "(the archive may be corrupt; never destroy the plaintext safety copy against it).")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # (ii) if plaintext chunks remain, they MUST match the sealed row's manifest — otherwise this is
    # NOT the audio that was sealed (re-opened same-label encounter, finding 2) → never wipe it.
    chunks = _discover_seal_chunks(enc_dir) if enc_dir.exists() else []
    if chunks:
        gathered = [(seq, p.read_bytes()) for (p, seq) in chunks]
        manifest_list = [
            {"seq": seq, "sha256": sha256_hex(data), "bytes": len(data)} for (seq, data) in gathered]
        payload = sealed_row.get("payload") or {}
        if (len(manifest_list) != payload.get("chunk_count")
                or _manifest_digest(manifest_list) != payload.get("manifest_sha256")):
            log.error(
                "scribe.retention.recovery_manifest_mismatch", encounter_id=encounter_id,
                on_disk_chunks=len(manifest_list), row_chunks=payload.get("chunk_count"),
                detail="on-disk plaintext does NOT match the sealed row's manifest — this audio was "
                       "NOT covered by the seal (a re-opened same-label encounter accumulated NEW "
                       "consented audio). REFUSING to wipe; operator must reconcile (seal the new "
                       "audio under a distinct id, or destroy explicitly). Never silently destroy it.")
            return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # Verified: the blob covers this plaintext → complete the wipe (idempotent). NEVER re-emit.
    wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
    if wipe.residue:
        log.error(
            "scribe.retention.wipe_incomplete", encounter_id=encounter_id,
            unlink_failures=wipe.unlink_failures,
            detail="already-sealed recovery could not fully wipe plaintext — residue remains "
                   "(wipe_incomplete); the next sweep retries.")
        return SealOutcome(status=SEAL_STATUS_WIPE_INCOMPLETE, encounter_id=encounter_id)
    log.info(
        "scribe.retention.already_sealed", encounter_id=encounter_id,
        detail="retention.sealed already on the chain, blob + manifest verified — completed the "
               "plaintext wipe (idempotent crash-between-event-and-wipe recovery); no re-emit.")
    return SealOutcome(status=SEAL_STATUS_ALREADY_SEALED, encounter_id=encounter_id)


def _transient_wipe(enc_dir: Path, encounter_id: str, retained_dir: Path) -> SealOutcome:
    """§3.5 transient posture — wipe the audio WITHOUT sealing. Emits NO ``retention.*`` event
    (there is no sealed artifact to attest), but a loud, counted structlog signal so "wiped, not
    retained" is distinguishable from "nothing to do" (intentionally-left-blank). The transcript is
    still relocated + kept (only the dense audio is dropped)."""
    chunk_count = len(_discover_seal_chunks(enc_dir))
    wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir)
    if wipe.residue:
        # Even transient must not claim 'wiped' with PHI still on disk (findings 5/8 apply here too).
        log.error(
            "scribe.retention.wipe_incomplete", encounter_id=encounter_id, chunk_count=chunk_count,
            unlink_failures=wipe.unlink_failures,
            detail="retention.mode=transient wipe could NOT fully remove plaintext — residue remains "
                   "(wipe_incomplete); the label-named dir persists. The next sweep retries.")
        return SealOutcome(status=SEAL_STATUS_WIPE_INCOMPLETE, encounter_id=encounter_id,
                           chunk_count=chunk_count)
    log.warning(
        "scribe.retention.transient_wiped", encounter_id=encounter_id, chunk_count=chunk_count,
        detail="retention.mode=transient — audio WIPED without sealing (no retained archive, no "
               "retention.* event); transcript relocated + kept. A deliberate, config-visible "
               "posture (§3.5), never the silent default.")
    return SealOutcome(
        status=SEAL_STATUS_TRANSIENT_WIPED, encounter_id=encounter_id, chunk_count=chunk_count)
