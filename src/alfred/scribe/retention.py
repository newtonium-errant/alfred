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

CRYPTO PRIMITIVE — age X25519, operator-ruled (2026-07-19, design §3.1):
    The production sealer is standard **age** X25519 recipient encryption (:class:`AgeSealer`, via the
    ``pyrage`` library). age is the decisive choice for a ~10-year medico-legal archive: an age blob is
    a self-describing standard format, decryptable with the stock ``age`` binary WITHOUT this codebase
    (decade-scale standalone retrievability — the 13d offline-key retrieval guarantee). The daemon
    holds ONLY the recipient PUBLIC key (an ``age1…`` string); the offline PRIVATE key
    (``AGE-SECRET-KEY-…``) opens the archive, so a live-compromised daemon cannot decrypt. Recorded
    honestly as ``cipher = "age-x25519"`` with a ``.age`` blob suffix (both true to the format).

    The ``pyrage`` import is LAZY (raises :class:`SealerUnavailable`), so this module + the
    fail-closed-ordering pins load and run WITHOUT the crypto dep (tests inject a deterministic fake
    sealer); only the actual-crypto round-trip is dep-gated (design §10). The seal backend lives
    behind a pluggable :class:`Sealer` protocol — a future backend swap flips :data:`SEAL_CIPHER` +
    :data:`SEAL_BLOB_SUFFIX` without touching the lifecycle, verify, or wipe logic. The keygen ceremony
    + the offline-binary decrypt verification are slice 13d.

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

# The honest cipher label recorded in every ``retention.sealed`` row. The seal is the standard **age**
# X25519 recipient format (operator ruling 2026-07-19) — a self-describing blob openable a decade from
# now with the stock ``age`` binary WITHOUT this codebase (§3.1's decisive rationale). A mixed archive
# stays self-identifying via this label.
SEAL_CIPHER = "age-x25519"

# The sealed-blob filename suffix (opaque-id filename, no label leak). ``.age`` — the blob IS age
# format, decryptable by the stock ``age`` binary (the 13d offline-retrieval path).
SEAL_BLOB_SUFFIX = ".age"

# The age v1 intro line + header MAC delimiter. Every age ciphertext is
# ``age-encryption.org/v1\n`` + one-or-more ``-> `` recipient stanzas + a ``--- <b64 mac>\n`` header
# terminator + the binary STREAM payload. ``verify_wellformed`` parses this STRUCTURE (not just a
# prefix) so a truncated blob — header cut short, or an empty payload after the MAC line — is caught
# WITHOUT the private key (findings 8/21/23: a prefix check passed a truncated, undecryptable blob and
# recovery wiped plaintext against it). The digest-stable (torn-write) half of the self-verify is in
# seal_encounter; the exact blob-integrity check on recovery is the manifest-sidecar ``blob_sha256``.
_AGE_V1_INTRO = b"age-encryption.org/v1\n"
_AGE_HEADER_MAC = b"\n--- "

# The manifest member name inside the sealed tar (recovered on unseal to re-verify per-chunk shas).
SEAL_MANIFEST_NAME = "manifest.json"

# The PHI-FREE manifest SIDECAR written beside the blob at seal time (``<encounter_id>.manifest.json``
# in ``retained_dir``). Holds the sorted per-chunk ``[{seq, sha256, bytes}]`` + the sealed ``blob_sha256``
# — ids/digests/scalars only, no PHI. It lets the crash-between-event-and-wipe recovery (a) authenticate
# itself against the durable row (its manifest digest == the row's ``manifest_sha256``), (b) detect a
# truncated/corrupt blob EXACTLY (``sha256(blob) == blob_sha256`` — closes findings 8/21/23), and
# (c) subset-verify each PRESENT on-disk chunk (seq→sha), so a crash-mid-wipe residue is a COMPLETABLE
# subset rather than a permanent mismatch (findings 20/29). The private-key-only tar manifest cannot be
# read on-box (the daemon holds only the recipient), so this PHI-free sidecar is the recovery reference.
# 13d's destroy path unlinks it beside the blob (a destroy contract, flagged in the delta report).
SEAL_MANIFEST_SIDECAR_SUFFIX = ".manifest.json"

# Chunk filename shape (parse seq from the STEM). Matches the pipeline's ``_CHUNK_NAME_RE`` so seal
# discovery agrees with the accumulator on "what is a chunk": ``chunk_<seq>.<ext>`` → stem
# ``chunk_<seq>`` matches; ``chunk_<seq>.meta.json`` → stem ``chunk_<seq>.meta`` does NOT; ``_CLOSED``
# and ``<enc>.transcript.json`` do NOT. DELIBERATELY codec-agnostic (no audio-extension allowlist):
# retention must seal EVERY audio chunk, so a new/unknown container is never silently left unsealed.
_CHUNK_NAME_RE = re.compile(r"^chunk_(\d+)$")

# The per-chunk meta sidecar suffix (``chunk_<seq>.meta.json``). The manifest-scoped wipe (§3.3 step 5)
# removes EXACTLY the manifest chunk files + their meta sidecar — derived from the chunk STEM so the
# wipe set is deterministic, never a blanket iterdir loop (findings 1/2/3/4/5 — a blanket loop
# destroyed the verify-failed KEPT ledger + any late-arriving/unexpected file).
_META_SUFFIX = ".meta.json"

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
    """The age backend (``pyrage``) is not installed. The seal lifecycle + its fail-closed-ordering
    pins run WITHOUT a backend (a fake sealer is injected); only real sealing / the round-trip needs
    one, so this is raised lazily at construction, never at import."""


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


def _age_blob_wellformed(blob: bytes) -> bool:
    """True iff ``blob`` is a STRUCTURALLY well-formed age v1 envelope — the ``age-encryption.org/v1``
    intro, at least one ``-> `` recipient stanza, the ``--- <mac>`` header terminator, AND a non-empty
    binary payload after it (findings 8/21/23: a prefix-only check passed a truncated blob). Base64
    body lines cannot contain ``-`` so the first ``\\n--- `` is unambiguously the header terminator.
    Module-level so recovery can structurally gate a blob WITHOUT constructing a sealer (R4/R5)."""
    if not blob.startswith(_AGE_V1_INTRO):
        return False
    mac_idx = blob.find(_AGE_HEADER_MAC)              # the "\n--- " terminating the header
    if mac_idx == -1:
        return False
    if b"\n-> " not in blob[: mac_idx + 1]:           # ≥ 1 recipient stanza inside the header
        return False
    payload_nl = blob.find(b"\n", mac_idx + len(_AGE_HEADER_MAC))  # end of the "--- <mac>" line
    if payload_nl == -1:
        return False
    return len(blob) > payload_nl + 1                 # a NON-EMPTY binary payload follows the header


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


# --- the concrete crypto backend (age / pyrage — the OPERATOR-RULED production sealer) ----------


def _import_pyrage():
    """Lazy-import ``pyrage`` (the age file-encryption lib), raising :class:`SealerUnavailable` when
    absent so the module + its fail-closed-ordering pins load and run WITHOUT the crypto dep (tests
    inject a fake sealer). Only real sealing / the round-trip needs it."""
    try:
        import pyrage
        from pyrage import x25519
    except ImportError as exc:  # pragma: no cover — exercised via the SealerUnavailable pin below
        raise SealerUnavailable(
            "the `pyrage` age library is not installed — cannot seal (the operator ruled age as the "
            "seal backend, design §3.1). The seal lifecycle + its fail-closed-ordering pins run "
            "WITHOUT it via an injected fake sealer; only real sealing / the round-trip needs it."
        ) from exc
    return pyrage, x25519


class AgeSealer:
    """The production sealer — standard **age** X25519 recipient encryption (via ``pyrage``), the
    operator-ruled backend (2026-07-19, design §3.1). The daemon holds ONLY the recipient PUBLIC key
    (an ``age1…`` string); the offline PRIVATE key (``AGE-SECRET-KEY-…``) opens the archive. A
    live-compromised daemon cannot decrypt. The decisive property over a bespoke blob: an age file is
    a self-describing standard, decryptable a decade from now with the stock ``age`` binary WITHOUT
    this codebase (the 13d offline-retrieval guarantee). Lazy import → :class:`SealerUnavailable`.

    The :class:`Sealer` protocol is bytes-in/bytes-out, so the recipient/identity are passed as the
    UTF-8 bytes of their canonical bech32 strings (``recipient_public_key`` = ``str(recipient)``
    encoded; ``private_key`` = ``str(identity)`` encoded). All pyrage parse/crypto errors are wrapped
    as the module's typed :class:`SealError` (findings 18/19 — a corrupt/degenerate key never escapes
    as an untyped ValueError the sweep would misclassify as a crash)."""

    cipher = SEAL_CIPHER

    def __init__(self) -> None:
        self._pyrage, self._x25519 = _import_pyrage()

    def _recipient(self, recipient_public_key: bytes):
        try:
            return self._x25519.Recipient.from_str(recipient_public_key.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — UnicodeDecodeError OR pyrage RecipientError, both typed
            # pyrage raises RecipientError on a non-canonical / malformed / degenerate recipient
            # (bech32 validation is canonical — closes findings 18/19).
            raise SealError(f"recipient public key is not a valid age recipient: {exc}") from exc

    def _identity(self, private_key: bytes):
        try:
            return self._x25519.Identity.from_str(private_key.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — UnicodeDecodeError OR pyrage IdentityError, both typed
            raise SealError(f"private key is not a valid age identity: {exc}") from exc

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        recipient = self._recipient(recipient_public_key)
        try:
            return self._pyrage.encrypt(plaintext, [recipient])
        except Exception as exc:  # noqa: BLE001 — any age encrypt failure is a typed seal failure
            raise SealError(f"age encryption failed: {exc}") from exc

    def verify_wellformed(self, blob: bytes) -> bool:
        """STRUCTURAL check WITHOUT the private key (the daemon holds only the recipient). Not a prefix
        test (findings 8/21/23 — a prefix pass let a truncated, undecryptable blob wipe plaintext):
        the FULL bytes must be a well-formed age v1 envelope — the ``age-encryption.org/v1`` intro, at
        least one ``-> `` recipient stanza, the ``--- <mac>`` header terminator, AND a NON-EMPTY binary
        payload after it. A blob cut short of its MAC line or its payload fails. The digest-stable
        (torn-write) half is in :func:`seal_encounter`; the exact blob-integrity check on recovery is
        the manifest-sidecar ``blob_sha256`` (a header parse cannot detect a payload-body truncation)."""
        return _age_blob_wellformed(blob)

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        identity = self._identity(private_key)
        try:
            return self._pyrage.decrypt(blob, [identity])
        except Exception as exc:  # noqa: BLE001 — wrong identity / tamper → DecryptError → SealError
            raise SealError("age decryption/authentication failed") from exc


def make_default_sealer() -> Sealer:
    """The production sealer (age / ``pyrage``). Raises :class:`SealerUnavailable` if ``pyrage`` is
    absent."""
    return AgeSealer()


def generate_keypair() -> tuple[bytes, bytes]:
    """Mint an age X25519 ``(public, private)`` keypair as the UTF-8 bytes of their canonical bech32
    strings — ``public`` = ``age1…`` recipient, ``private`` = ``AGE-SECRET-KEY-…`` identity. The 13d
    keygen ceremony + the round-trip test's TEST keypair. Raises :class:`SealerUnavailable` if
    ``pyrage`` is absent. ``generate_keypair`` always emits CANONICAL bytes (finding 18's corruption
    surface only applies to a hand-edited on-box key file)."""
    _pyrage, x25519 = _import_pyrage()
    identity = x25519.Identity.generate()
    pub = str(identity.to_public()).encode("utf-8")
    priv = str(identity).encode("utf-8")
    return pub, priv


# --- tar / manifest helpers (deterministic; unseal recovers the manifest) ----------------------


def _manifest_digest(manifest_list: list[dict]) -> str:
    """``manifest_sha256`` — sha256 over the CANONICAL bytes of the seq-sorted per-chunk list
    (§3.3). Canonical = sorted keys + tight separators, so the digest is stable across runs."""
    canonical = json.dumps(manifest_list, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_hex(canonical)


def _write_manifest_sidecar(
    retained_dir: Path, encounter_id: str, manifest_list: list[dict], blob_sha: str,
) -> None:
    """Write the PHI-free manifest sidecar (``<encounter_id>.manifest.json``) beside the blob —
    atomic + fsync-durable. Records the sorted per-chunk manifest + the sealed ``blob_sha256`` so the
    crash-between-event-and-wipe recovery can subset-verify each present chunk (findings 20/29) AND
    detect a truncated/corrupt blob exactly (findings 8/21/23) — WITHOUT the private key. Written
    BEFORE the durable row (the recovery reference must exist whenever a row does); a crash before it
    leaves no row → the next sweep re-seals, regenerating both."""
    path = Path(retained_dir) / f"{encounter_id}{SEAL_MANIFEST_SIDECAR_SUFFIX}"
    data = json.dumps(
        {"manifest": manifest_list, "blob_sha256": blob_sha},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(path, data)


def _load_manifest_sidecar(retained_dir: Path, encounter_id: str) -> dict | None:
    """Load the manifest sidecar, or ``None`` if absent / unreadable / malformed (recovery then fails
    CLOSED — never wipes plaintext without a verified reference)."""
    path = Path(retained_dir) / f"{encounter_id}{SEAL_MANIFEST_SIDECAR_SUFFIX}"
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    manifest = data.get("manifest")
    blob_sha = data.get("blob_sha256")
    if not isinstance(manifest, list) or not isinstance(blob_sha, str):
        return None
    return {"manifest": manifest, "blob_sha256": blob_sha}


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
    try:
        entries = list(Path(enc_dir).iterdir())
    except FileNotFoundError:
        return []  # a vanished dir → no chunks (idempotent no-op, findings 32/35 — never raise here)
    for p in entries:
        if not p.is_file():
            continue
        m = _CHUNK_NAME_RE.match(p.stem)
        if m:
            found.append((p, int(m.group(1))))
    found.sort(key=lambda c: c[1])
    return found


def encounter_has_chunks(enc_dir: str | Path) -> bool:
    """True iff ``enc_dir`` holds at least one ``chunk_<seq>.<ext>`` audio file — the 13b sweep's
    zero-chunk gate (a CLOSED zero-chunk encounter is DISPOSED, not sealed, §E). A missing dir → False
    (never raise on a vanished dir)."""
    p = Path(enc_dir)
    if not p.is_dir():
        return False
    return bool(_discover_seal_chunks(p))


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


def _relocate_ledger(enc_dir: Path, encounter_id: str, retained_dir: Path) -> tuple[bool, bool]:
    """RELOCATE the transcript ledger (§3.4) out of the (possibly-PHI-label) encounter dir into
    ``<retained_dir>/transcripts/`` — the keep-forever clinical transcript. Returns
    ``(relocated, residue)``:

      * ``relocated`` — the source was safely unlinked (a verified copy exists at the dest).
      * ``residue``  — the source was KEPT (relocation could not be verified, or the dest already
                       holds a DIVERGENT transcript for this encounter_id). The source is NEVER
                       destroyed against an unverified/divergent dest (findings 1/2/3/4/5).

    NEVER raises — IO failures fold into ``residue=True`` (R12). The source ledger is only ever
    removed on a verified relocation; the manifest-scoped wipe loop below NEVER touches it."""
    src_ledger = ledger.ledger_path(enc_dir, encounter_id)
    try:
        if not src_ledger.is_file():
            return False, False  # no ledger to move (a ledger-less encounter is clean here)
        src_bytes = src_ledger.read_bytes()
    except OSError:
        # cannot even read the source ledger — KEEP it, flag residue (never a false 'relocated').
        log.error(
            "scribe.retention.ledger_relocate_verify_failed", encounter_id=encounter_id,
            detail="the transcript ledger could not be read for relocation — source KEPT; wipe "
                   "flagged incomplete, retry next sweep.")
        return False, True
    dest = ledger.ledger_path(Path(retained_dir) / "transcripts", encounter_id)
    try:
        # DEST-COLLISION (R2, finding 3): an encounter_id is deterministic from (label, salt), so a
        # same-label re-open resolves the SAME dest. If the dest already holds this encounter's
        # transcript, NEVER blindly overwrite it (that would destroy a prior session's archived copy):
        #   byte-identical  ⇒ already-relocated (idempotent crash recovery) → unlink the source.
        #   DIVERGENT       ⇒ a different session's archived transcript → KEEP both, escalate.
        if dest.exists():
            dest_bytes = dest.read_bytes()
            if dest_bytes == src_bytes:
                relocated_ok = True  # already archived (identical) — safe to drop the source
            else:
                log.error(
                    "scribe.retention.ledger_relocate_dest_divergent", encounter_id=encounter_id,
                    detail="a DIVERGENT transcript already exists at the archive dest for this "
                           "encounter_id (a same-label re-open) — refusing to overwrite a prior "
                           "session's archived transcript. Source KEPT; wipe flagged incomplete.")
                return False, True
        else:
            _atomic_write_bytes(dest, src_bytes)
            # DIGEST-VERIFY the relocated copy BEFORE destroying the source (finding 4/6) — never
            # unlink the only other copy of a keep-forever clinical transcript against a
            # truncated/torn/diverged destination. A short write already raised inside
            # _atomic_write_bytes; this catches a torn replace / silent divergence too.
            relocated_ok = sha256_hex(dest.read_bytes()) == sha256_hex(src_bytes)
    except OSError:
        relocated_ok = False
    if relocated_ok and _unlink_quiet(src_ledger):
        return True, False
    log.error(
        "scribe.retention.ledger_relocate_verify_failed", encounter_id=encounter_id,
        detail="the relocated transcript did not verify against the source (or the source could not "
               "be unlinked) — source KEPT (never destroy the only good copy of a keep-forever "
               "clinical transcript). Wipe flagged incomplete; retry next sweep.")
    return False, True


def _relocate_and_wipe(
    enc_dir: Path, encounter_id: str, retained_dir: Path, *, chunk_paths: list[Path],
) -> _WipeResult:
    """§3.3 step 5 + §3.4 disposition — RELOCATE the transcript ledger, then WIPE EXACTLY the
    manifest-listed chunk files (``chunk_paths``) + their ``.meta.json`` sidecars, then remove
    ``_CLOSED`` LAST and rmdir the label-named dir. MANIFEST-SCOPED (R1): the wipe set is derived
    from ``chunk_paths`` — NEVER a blanket ``iterdir`` unlink — so a late-arriving chunk, the
    verify-failed KEPT ledger, or any unexpected file SURVIVES and is surfaced as residue (findings
    1/2/3/4/5). Ordering (R6): the ledger is relocated first; ``_CLOSED`` is removed only after every
    manifest file is gone AND the ledger is relocated AND no unexpected entry remains — so a failed
    step never loses the eligibility sentinel (findings 15/40). Idempotent: a gone dir is a clean
    no-op (R6). NEVER raises — every IO failure (incl. an unsearchable dir, findings 11/41) folds
    into ``residue`` (R12); the caller surfaces residue rather than a false 'wiped' claim."""
    enc_dir = Path(enc_dir)
    try:
        if not enc_dir.exists():
            return _WipeResult(dir_removed=True, residue=False)  # already gone — clean idempotent no-op
    except OSError:
        # cannot even stat the dir (unsearchable ancestor) — treat as residue, never raise (R12).
        _log_enc_dir_residue(encounter_id, unlink_failures=0, ledger_residue=False,
                             reason="the encounter dir could not be stat'd (unsearchable) — residue")
        return _WipeResult(residue=True)

    ledger_relocated, ledger_residue = _relocate_ledger(enc_dir, encounter_id, retained_dir)

    # WIPE the manifest chunk set ONLY: each chunk file + its derived ``.meta.json`` sidecar.
    unlink_failures = 0
    for chunk_path in chunk_paths:
        if not _unlink_quiet(chunk_path):
            unlink_failures += 1
        sidecar = enc_dir / (chunk_path.stem + _META_SUFFIX)
        if not _unlink_quiet(sidecar):
            unlink_failures += 1

    # Determine residue: anything left in the dir OTHER than ``_CLOSED`` (a late chunk not in the
    # manifest, the kept ledger, an unexpected nested entry) is residue; so is any unlink failure or
    # un-relocated ledger. ``iterdir`` on an unsearchable dir folds into residue (R12, findings 11/41).
    try:
        leftovers = [p for p in enc_dir.iterdir() if p.name != CLOSE_SENTINEL_NAME]
    except OSError:
        _log_enc_dir_residue(encounter_id, unlink_failures=unlink_failures,
                             ledger_residue=ledger_residue,
                             reason="the encounter dir is unsearchable after the wipe — residue")
        return _WipeResult(
            ledger_relocated=ledger_relocated, unlink_failures=unlink_failures, residue=True)

    if unlink_failures or ledger_residue or leftovers:
        _log_enc_dir_residue(encounter_id, unlink_failures=unlink_failures,
                             ledger_residue=ledger_residue,
                             reason="plaintext residue remains after the manifest-scoped wipe (an "
                                    "un-unlinkable PHI file, an un-relocated transcript, a "
                                    "late-arriving unsealed chunk, or an unexpected nested entry)")
        return _WipeResult(
            ledger_relocated=ledger_relocated, unlink_failures=unlink_failures,
            dir_removed=False, residue=True)

    # CLEAN: the dir holds at most ``_CLOSED`` now. Remove it LAST (R6 — the eligibility sentinel
    # must outlive every other step, findings 15/40), then rmdir.
    closed = enc_dir / CLOSE_SENTINEL_NAME
    if not _unlink_quiet(closed):
        unlink_failures += 1
        _log_enc_dir_residue(encounter_id, unlink_failures=unlink_failures,
                             ledger_residue=ledger_residue,
                             reason="the _CLOSED sentinel could not be removed — residue")
        return _WipeResult(
            ledger_relocated=ledger_relocated, unlink_failures=unlink_failures, residue=True)
    dir_removed = False
    try:
        enc_dir.rmdir()
        dir_removed = True
    except OSError:
        _log_enc_dir_residue(encounter_id, unlink_failures=unlink_failures,
                             ledger_residue=ledger_residue,
                             reason="the label-named dir could not be removed after a clean wipe (a "
                                    "concurrent create raced the rmdir)")
    return _WipeResult(
        ledger_relocated=ledger_relocated, unlink_failures=unlink_failures,
        dir_removed=dir_removed, residue=not dir_removed)


def _log_enc_dir_residue(encounter_id: str, *, unlink_failures: int, ledger_residue: bool,
                         reason: str) -> None:
    """The HONEST residue attribution (never a bare 'residual subdir'). The label-named dir NAME is
    itself PHI, so any residue is real; the seal outcome is flagged wipe_incomplete for escalation."""
    log.error(
        "scribe.retention.enc_dir_not_empty", encounter_id=encounter_id,
        unlink_failures=unlink_failures, ledger_residue=ledger_residue,
        detail=f"{reason}. The label-named dir (a PHI name) persists; the seal outcome is flagged "
               f"wipe_incomplete (retry next sweep).")


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
    dispose_empty: bool = False,
) -> SealOutcome:
    """Seal ONE encounter's audio (unit-level; the sweep in 13b drives this over READY / abandoned
    encounters). Fail-closed strict order (§3.3): tar → seal → self-verify → durable
    ``retention.sealed`` [D] → ONLY THEN wipe plaintext. Idempotent via the chain
    (``events.retention_sealed_row``): a crash at any step is recovered by the next call.

    ``recipient_public_key`` is the 32-byte raw X25519 pubkey the caller loaded from the seal-dir
    (tests pass a test pubkey). ``retained_dir`` is the RESOLVED absolute blob-store dir (the caller
    does the empty-⇒-derive resolution; this function takes a concrete path). ``recipient_public_key``
    is the age recipient (an ``age1…`` string, UTF-8 bytes) the caller loaded from the seal-dir (tests
    pass a fake pubkey). ``mode`` is the resolved ``retained|transient`` posture (§3.5)."""
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
        # A zero-chunk encounter must be DISPOSED — the label-named dir NAME is itself PHI, and
        # nothing else ever cleans it (no seal row is ever created), so leaving it would leak a
        # patient-named dir forever while logging 'nothing to do' (§E ruling, finding 12). Two
        # trigger cases: a CLOSED one (clinician opened, no audio, /close), and a stale-ABANDONED one
        # (no _CLOSED, past the abandon grace — the sweep passes ``dispose_empty=True``, the
        # E-extension). An OPEN, in-grace zero-chunk dir is genuinely mid-flight → leave it (a later
        # chunk may still arrive). Disposal needs NO crypto (nothing to seal).
        if (enc_dir / CLOSE_SENTINEL_NAME).exists() or dispose_empty:
            wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir, chunk_paths=[])
            log.info(
                "scribe.retention.empty_encounter_disposed", encounter_id=encounter_id,
                residue=wipe.residue,
                detail="a zero-chunk encounter was disposed — nothing was sealed (NO retention.* "
                       "event), but the label-named dir (a PHI name) is removed. Never leave a "
                       "patient-named dir with 'nothing to do' logged forever.")
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

    # Step 3.5 — write the PHI-free manifest SIDECAR beside the blob (BEFORE the durable row, so the
    # crash-between-event-and-wipe recovery always finds its subset-verify + blob-integrity reference;
    # a crash before it leaves NO row → the next sweep re-seals and regenerates both). See
    # SEAL_MANIFEST_SIDECAR_SUFFIX. Raises on a write/fsync error (fail-closed — no row, no wipe).
    _write_manifest_sidecar(retained_dir, encounter_id, manifest_list, blob_sha)

    # Step 4 — DURABLE retention.sealed [D]; RAISES on a store-down append (fail-closed). The seal
    # is NOT acknowledged and plaintext is NOT wiped until this commits — exactly the #12
    # withdrawal durable-before-ack ordering. A raise PROPAGATES: the blob-without-event state is
    # recovered by the next sweep (re-seal overwrites the blob, re-emits) — never wiped-but-unsealed.
    fp = key_fingerprint(recipient_public_key)
    events.retention_sealed(
        subject_id=encounter_id, chunk_count=chunk_count, total_bytes=total_bytes,
        manifest_sha256=manifest_sha256, sealed_to_key_fp=fp, cipher=sealer.cipher, now=now)

    # Step 5 — ONLY NOW wipe plaintext (relocate the transcript ledger first, §3.4). The wipe is
    # MANIFEST-SCOPED: only the chunk files this seal covered (+ their meta sidecars + _CLOSED) are
    # removed — a chunk that arrived AFTER the gather (finding 4's abandoned-gate race) is NOT in the
    # set, so it SURVIVES and surfaces as residue rather than being wiped unsealed. A wipe that leaves
    # residue must NOT be reported as clean SEALED — the seal + durable row DID commit, but plaintext
    # PHI is still on disk, so the outcome is wipe_incomplete for operator escalation (findings 5/8/16).
    wipe = _relocate_and_wipe(
        enc_dir, encounter_id, retained_dir, chunk_paths=[p for (p, _seq) in chunks])
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
    enc_dir: Path, encounter_id: str, retained_dir: Path, sealed_row: dict,
    sealer: "Sealer | None",
) -> SealOutcome:
    """FAIL-CLOSED crash-between-durable-event-and-wipe recovery (§3.3, findings 2/3/7). The chain row
    proves the durable append landed — NOT that the blob survived, nor that today's on-disk plaintext
    is the SAME audio the row attests. The order:

      * ZERO plaintext on disk → nothing to destroy → complete the dir cleanup (disposal). Needs NO
        crypto and NO sealer (R5, findings 9/14/24/38: an empty re-opened+closed dir with a prior row
        must DISPOSE, never AttributeError on a ``None`` sealer).
      * plaintext present but ``sealer is None`` → cannot verify the blob → LATCHED fail-closed skip
        (recovery_mismatch), never a crash (R5).
      * plaintext present → (i) the blob EXISTS + is structurally well-formed (R4) + its digest matches
        the PHI-free manifest sidecar's ``blob_sha256`` (findings 8/21/23 — a truncated blob no longer
        passes); (ii) the sidecar authenticates against the row (its manifest digest == the row's
        ``manifest_sha256``); (iii) every PRESENT on-disk chunk matches its sidecar entry seq→sha —
        a strict SUBSET is a COMPLETABLE crash-mid-wipe residue (R3, findings 20/29), an EXTRA or
        MISMATCHED chunk is a re-opened/corrupt encounter → escalate. Only then complete the wipe.
    Any mismatch → loud ERROR, NO wipe, ``recovery_mismatch``. NEVER destroy audio the row does not cover."""
    enc_dir = Path(enc_dir)
    retained_dir = Path(retained_dir)

    # On-disk plaintext to protect? A vanished dir → none; an unsearchable dir → cannot enumerate
    # (treat as residue below). NEVER raise here (R6/R12).
    try:
        chunks = _discover_seal_chunks(enc_dir) if enc_dir.exists() else []
    except OSError:
        chunks = None

    # ZERO plaintext → nothing to destroy; complete the dir cleanup. Needs NO sealer (R5).
    if chunks is not None and not chunks:
        wipe = _relocate_and_wipe(enc_dir, encounter_id, retained_dir, chunk_paths=[])
        if wipe.residue:
            log.error(
                "scribe.retention.wipe_incomplete", encounter_id=encounter_id,
                unlink_failures=wipe.unlink_failures,
                detail="already-sealed recovery (zero on-disk chunks) could not fully clean the dir — "
                       "residue remains (wipe_incomplete); the next sweep retries.")
            return SealOutcome(status=SEAL_STATUS_WIPE_INCOMPLETE, encounter_id=encounter_id)
        log.info(
            "scribe.retention.already_sealed", encounter_id=encounter_id,
            detail="retention.sealed on the chain + no plaintext on disk — completed the dir cleanup "
                   "(idempotent recovery / disposal of a re-opened+closed empty dir); no re-emit.")
        return SealOutcome(status=SEAL_STATUS_ALREADY_SEALED, encounter_id=encounter_id)

    # There IS plaintext (or the dir is unsearchable). Wiping it REQUIRES verifying the blob covers it
    # — which needs a sealer. A None sealer here (pyrage lost from the venv) → fail-closed skip, NEVER
    # AttributeError (R5, findings 9/14/24/38).
    if sealer is None:
        log.error(
            "scribe.retention.recovery_sealer_unavailable", encounter_id=encounter_id,
            detail="chain says retention.sealed and plaintext is on disk, but NO sealer is available "
                   "(pyrage absent) to verify the blob — REFUSING to wipe (never destroy plaintext we "
                   "cannot verify is covered). Retention sealing is latched-skipped; operator reconciles.")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)
    if chunks is None:
        _log_enc_dir_residue(encounter_id, unlink_failures=0, ledger_residue=False,
                             reason="the encounter dir is unsearchable — cannot enumerate plaintext "
                                    "to verify against the seal; REFUSING to wipe")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # (i) the blob must EXIST + be structurally well-formed (R4).
    blob_path = retained_dir / f"{encounter_id}{SEAL_BLOB_SUFFIX}"
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
            detail="the sealed blob is unreadable / not a well-formed age envelope — REFUSING to wipe "
                   "plaintext (the archive may be truncated/corrupt; never destroy the plaintext "
                   "safety copy against it).")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # (ii) the PHI-free manifest sidecar authenticates against the durable row (its manifest digest ==
    # the row's manifest_sha256), and the blob's digest EXACTLY matches the sidecar's blob_sha256 (a
    # truncated but header-well-formed blob is caught here — findings 8/21/23). No sidecar → fail-closed.
    payload = sealed_row.get("payload") or {}
    sidecar = _load_manifest_sidecar(retained_dir, encounter_id)
    if sidecar is None or _manifest_digest(sidecar["manifest"]) != payload.get("manifest_sha256"):
        log.error(
            "scribe.retention.recovery_sidecar_mismatch", encounter_id=encounter_id,
            has_sidecar=sidecar is not None,
            detail="the PHI-free manifest sidecar is missing or does NOT authenticate against the "
                   "sealed row's manifest_sha256 — REFUSING to wipe (recovery cannot verify which "
                   "audio the blob covers without a trusted per-chunk reference). Operator reconciles.")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)
    if sha256_hex(blob) != sidecar.get("blob_sha256"):
        log.error(
            "scribe.retention.recovery_blob_corrupt", encounter_id=encounter_id,
            detail="the sealed blob's digest does NOT match the manifest sidecar's blob_sha256 — the "
                   "archive is TRUNCATED/CORRUPT (undecryptable) — REFUSING to wipe the plaintext "
                   "safety copy against it (findings 8/21/23). Operator must reconcile / re-seal.")
        return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # (iii) SUBSET validation (R3, findings 20/29): every PRESENT on-disk chunk must match its sidecar
    # entry seq→sha. A strict SUBSET (crash-mid-wipe left fewer chunks) is COMPLETABLE — the blob
    # covers them. An EXTRA seq (not in the manifest) or a MISMATCHED sha means this is NOT the sealed
    # audio (a re-opened same-label encounter, finding 2) OR on-disk corruption → escalate, never wipe.
    sidecar_by_seq = {int(m["seq"]): m["sha256"] for m in sidecar["manifest"]
                      if isinstance(m, dict) and "seq" in m and "sha256" in m}
    for (p, seq) in chunks:
        if seq not in sidecar_by_seq or sha256_hex(p.read_bytes()) != sidecar_by_seq[seq]:
            log.error(
                "scribe.retention.recovery_manifest_mismatch", encounter_id=encounter_id,
                on_disk_chunks=len(chunks), row_chunks=payload.get("chunk_count"),
                detail="an on-disk chunk does NOT match the sealed manifest (an EXTRA seq or a changed "
                       "sha) — EITHER a re-opened same-label encounter accumulated NEW consented "
                       "audio, OR an on-disk chunk is corrupt. REFUSING to wipe; operator must "
                       "reconcile (seal genuinely-new audio under a distinct id, or destroy "
                       "explicitly). A crash-mid-wipe SUBSET, by contrast, completes automatically.")
            return SealOutcome(status=SEAL_STATUS_RECOVERY_MISMATCH, encounter_id=encounter_id)

    # Verified: every present chunk is a covered subset → complete the wipe (idempotent). NEVER re-emit.
    wipe = _relocate_and_wipe(
        enc_dir, encounter_id, retained_dir, chunk_paths=[p for (p, _seq) in chunks])
    if wipe.residue:
        log.error(
            "scribe.retention.wipe_incomplete", encounter_id=encounter_id,
            unlink_failures=wipe.unlink_failures,
            detail="already-sealed recovery could not fully wipe plaintext — residue remains "
                   "(wipe_incomplete); the next sweep retries.")
        return SealOutcome(status=SEAL_STATUS_WIPE_INCOMPLETE, encounter_id=encounter_id)
    log.info(
        "scribe.retention.already_sealed", encounter_id=encounter_id,
        detail="retention.sealed already on the chain, blob + sidecar + per-chunk subset verified — "
               "completed the plaintext wipe (idempotent crash-between-event-and-wipe recovery); no "
               "re-emit.")
    return SealOutcome(status=SEAL_STATUS_ALREADY_SEALED, encounter_id=encounter_id)


def _transient_wipe(enc_dir: Path, encounter_id: str, retained_dir: Path) -> SealOutcome:
    """§3.5 transient posture — wipe the audio WITHOUT sealing. Emits NO ``retention.*`` event
    (there is no sealed artifact to attest), but a loud, counted structlog signal so "wiped, not
    retained" is distinguishable from "nothing to do" (intentionally-left-blank). The transcript is
    still relocated + kept (only the dense audio is dropped)."""
    chunks = _discover_seal_chunks(enc_dir)
    chunk_count = len(chunks)
    wipe = _relocate_and_wipe(
        enc_dir, encounter_id, retained_dir, chunk_paths=[p for (p, _seq) in chunks])
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
