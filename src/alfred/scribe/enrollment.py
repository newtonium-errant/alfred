"""Voice-preset store / registry / binding / resolve — scribe P4-5a (torch-free).

The data-model HEART of P4-5 enrollment: named ``(user, mic, room)`` voice PRESETS
on disk (embeddings-only — NO raw audio ever), a directory-scan registry with a
load contract that has TEETH, per-encounter binding via an atomic sidecar, and
digest-pinned resolution that fails OPEN to all-``unknown`` on ANY problem. NO
torch — the whole surface is CI-testable against the ``embed_voice`` fake seam.

FROZEN CONTRACT (see project_p45_enrollment_design DATA MODEL): one preset = one
named centroid; ids ``pst-<13ms>-<16hex>`` (path == id); atomic ``O_EXCL`` 0600
writes; files ARE the index (dir scan); cap 32/user; classification set
{usable, incompatible_model, incompatible_engine, unsupported_schema, revoked,
corrupt}; tombstone on revoke (drops centroids, blocks id reuse); binding sidecar
``_ENROLLMENT.json`` (ids only); ``resolve_for_encounter`` enforces
``binding.centroid_digest == preset.centroid_digest`` on EVERY resolution — every
refusal ⇒ no anchor ⇒ all-``unknown`` + one reason-coded ``scribe.enrollment.unusable``
log, NEVER a block (fail-open-for-availability).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import struct
import time
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

# --- id + identity grammar (ONE grammar, fullmatch, path == id) --------------
PRESET_ID_RE = re.compile(r"^pst-[0-9]{13}-[0-9a-f]{16}$")
SESSION_ID_RE = re.compile(r"^enr-[0-9]{13}-[0-9a-f]{16}$")
# User identity = a scribe.clinicians entry VERBATIM (case-sensitive, attest
# semantics). ONE regex, no case normalization anywhere.
USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

NAME_MAX = 64
# The ACTIVE-preset cap (memo). Tombstones do NOT count against it (operator ruling) —
# see count_active_presets.
MAX_PRESETS_PER_USER = 32
# The stored-ID ceiling (active + tombstones). Tombstones persist forever to block id
# reuse, so the id space needs its OWN generous bound — hit it and the write is refused
# LOUDLY (``tombstone_cap``) rather than growing without limit.
MAX_PRESET_FILES_PER_USER = 256
UNIT_NORM_TOL = 1e-3            # unit-norm load-contract tolerance
BINDING_NAME = "_ENROLLMENT.json"
LEARNING_DIRNAME = "learning"
AUDIT_NAME = "audit.log"

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
CENTROID_SOURCE_RECORDED = "recorded"

# Classification set (list state + resolution). ``usable`` is the only good state.
CLASS_USABLE = "usable"
CLASS_INCOMPATIBLE_MODEL = "incompatible_model"
CLASS_INCOMPATIBLE_ENGINE = "incompatible_engine"
CLASS_UNSUPPORTED_SCHEMA = "unsupported_schema"
CLASS_REVOKED = "revoked"
CLASS_CORRUPT = "corrupt"

# resolve_for_encounter typed refusals (every one → all-unknown fail-open + log).
REFUSAL_NO_BINDING = "no_binding"
REFUSAL_UNKNOWN_PRESET = "unknown_preset"
REFUSAL_REVOKED = "revoked"
REFUSAL_INCOMPATIBLE_MODEL = "incompatible_model"
REFUSAL_INCOMPATIBLE_ENGINE = "incompatible_engine"
REFUSAL_UNSUPPORTED_SCHEMA = "unsupported_schema"
REFUSAL_CORRUPT = "corrupt"
REFUSAL_DIGEST_MISMATCH = "digest_mismatch"


class EnrollmentError(Exception):
    """A caller-facing enrollment operation failed (validation / cap / write)."""


# --- id minting --------------------------------------------------------------

def _ms() -> int:
    return int(time.time() * 1000)


def mint_preset_id() -> str:
    """A fresh ``pst-<13-digit-ms>-<16hex>`` id (path == id)."""
    return f"pst-{_ms():013d}-{secrets.token_hex(8)}"


def mint_session_id() -> str:
    """A fresh ``enr-<13-digit-ms>-<16hex>`` enroll-session id (RAM-only)."""
    return f"enr-{_ms():013d}-{secrets.token_hex(8)}"


def valid_user(user: Any) -> bool:
    return isinstance(user, str) and USER_RE.fullmatch(user) is not None


def validate_user_for_enroll(user: str, clinicians: list[str]) -> None:
    """Fail-CLOSED user gate for ``/enroll/start`` — BEFORE any recording, never a
    wasted 45 s. Must match the id grammar AND be a ``scribe.clinicians`` entry
    VERBATIM (case-sensitive, matching attest). Raises :class:`EnrollmentError`."""
    if not valid_user(user):
        raise EnrollmentError(f"user {user!r} is not a valid identity (^[a-z0-9][a-z0-9._-]{{0,63}}$)")
    if user not in set(clinicians):
        raise EnrollmentError(
            f"user {user!r} is not in scribe.clinicians — enrollment is refused for "
            f"a non-clinician identity (fail-closed, matches attest)."
        )


# --- vector math (unit vectors; deterministic canonical digest) --------------

def l2_norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


def unit_normalize(vec: list[float]) -> list[float]:
    n = l2_norm(vec)
    if not math.isfinite(n) or n <= 0.0:
        canon = [0.0] * len(vec)
        if canon:
            canon[0] = 1.0
        return canon
    return [x / n for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Inputs SHOULD be unit vectors (centroids/embeddings are);
    computed defensively over raw dot / norms so a non-unit input can't produce a
    silently-wrong >1 value. Non-finite → 0.0 (fail-safe low)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = l2_norm(a), l2_norm(b)
    if not (math.isfinite(dot) and na > 0.0 and nb > 0.0):
        return 0.0
    c = dot / (na * nb)
    return max(-1.0, min(1.0, c))


def spherical_mean_centroid(vectors: list[list[float]], *, trim: float = 0.1) -> list[float]:
    """Trimmed spherical-mean centroid of unit ``vectors``.

    Mean → normalize (prelim centroid); drop the ``trim`` fraction of vectors with
    the LOWEST cosine to the prelim (outlier windows — coughs, silence, cross-talk);
    re-mean the kept + normalize. Deterministic (stable sort). A single vector
    passes through unit-normalized; an empty list raises (a degenerate hard gate the
    caller enforces earlier)."""
    if not vectors:
        raise EnrollmentError("cannot build a centroid from zero windows")
    dim = len(vectors[0])
    prelim = unit_normalize([sum(v[i] for v in vectors) / len(vectors) for i in range(dim)])
    if len(vectors) >= 3 and trim > 0.0:
        scored = sorted(vectors, key=lambda v: cosine(v, prelim), reverse=True)
        keep = scored[: max(1, math.ceil(len(scored) * (1.0 - trim)))]
    else:
        keep = vectors
    return unit_normalize([sum(v[i] for v in keep) / len(keep) for i in range(dim)])


def centroid_digest(centroids: list[list[float]]) -> str:
    """sha256 over the CANONICAL bytes of the centroid list — reproducible + exact.

    Canonical bytes = each float as IEEE-754 big-endian double (``>d``), in list
    order, prefixed by the count + per-centroid dim, so two centroid lists digest
    equal iff they are element-wise byte-identical. This is the value the binding
    PINS at every resolution (a hostile mid-encounter swap → different digest →
    ``digest_mismatch`` refusal → fail-open)."""
    h = hashlib.sha256()
    h.update(struct.pack(">I", len(centroids)))
    for c in centroids:
        h.update(struct.pack(">I", len(c)))
        for x in c:
            h.update(struct.pack(">d", float(x)))
    return h.hexdigest()


# --- the preset record (schema v1) -------------------------------------------

@dataclass
class Preset:
    """One named voice preset (schema_version 1). Write-once per centroid_version —
    human-initiated writes ONLY; NO pipeline-written field lives in this file."""

    preset_id: str
    user: str
    name: str
    status: str
    centroids: list[list[float]]
    embedding_dim: int
    centroid_digest: str
    centroid_version: int
    centroid_source: str
    enrolled_at: str
    created_at: str
    updated_at: str
    engine: dict[str, Any]
    sample_stats: dict[str, Any]
    quality: dict[str, Any]
    thresholds: Any = None                      # reserved (5b)
    device_hint: dict[str, Any] = field(default_factory=dict)
    revoked: Any = None                         # None | {at, reason}
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preset_id": self.preset_id, "user": self.user, "name": self.name,
            "status": self.status, "centroids": [list(c) for c in self.centroids],
            "embedding_dim": self.embedding_dim, "centroid_digest": self.centroid_digest,
            "centroid_version": self.centroid_version, "centroid_source": self.centroid_source,
            "enrolled_at": self.enrolled_at, "created_at": self.created_at,
            "updated_at": self.updated_at, "engine": dict(self.engine),
            "sample_stats": dict(self.sample_stats), "quality": dict(self.quality),
            "thresholds": self.thresholds, "device_hint": dict(self.device_hint),
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Preset":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# The REQUIRED (no-default) Preset fields. ``Preset.from_dict`` filters unknown keys but
# CANNOT invent a MISSING one — a preset JSON lacking any of these raises TypeError inside
# the constructor, which (before this guard) propagated out of ``load_preset`` →
# ``resolve_for_encounter`` → ``accumulate_encounter`` and BLOCKED the encounter forever,
# violating the frozen fail-open contract. DERIVED from the dataclass, so it can never
# drift from the schema (adding a required field auto-extends the check).
_REQUIRED_PRESET_FIELDS: tuple[str, ...] = tuple(
    name for name, f in Preset.__dataclass_fields__.items()
    if f.default is MISSING and f.default_factory is MISSING
)


# --- atomic 0600 store primitives --------------------------------------------

def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)          # belt (mkdir mode is umask-masked)
    except OSError:
        pass


def _atomic_write_json(path: Path, obj: Any, *, exclusive: bool = False) -> None:
    """House atomic write: unique ``O_EXCL`` 0600 temp → fsync → ``os.replace`` →
    chmod belt. ``exclusive=True`` refuses to overwrite an existing FINAL path
    (write-once artifacts: the binding sidecar)."""
    _ensure_dir(path.parent)
    if exclusive and path.exists():
        raise EnrollmentError(f"refusing to overwrite existing {path.name} (write-once)")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                os.unlink(tmp)
            except OSError:
                pass


def user_dir(enrollment_dir: str | Path, user: str) -> Path:
    return Path(enrollment_dir) / user


def preset_path(enrollment_dir: str | Path, user: str, preset_id: str) -> Path:
    return user_dir(enrollment_dir, user) / f"{preset_id}.json"


# --- load contract WITH TEETH + classification -------------------------------

def _structural_ok(data: dict[str, Any], path: Path) -> str | None:
    """Return a failure classification (``unsupported_schema`` / ``corrupt``) if the
    preset dict fails the load-contract teeth, else ``None`` (structurally valid).

    Teeth: schema gate, REQUIRED-FIELD PRESENCE, id/path agreement, finite floats, dim
    consistency, unit-norm within tolerance. A failure is CLASSIFIED (never crashes,
    never blocks)."""
    if data.get("schema_version") != SCHEMA_VERSION:
        return CLASS_UNSUPPORTED_SCHEMA
    # REQUIRED-FIELD PRESENCE — a v1 file missing any no-default field would otherwise
    # raise TypeError inside Preset.from_dict and propagate out of the "NEVER raises"
    # load path, permanently blocking the bound encounter. Classify it corrupt instead.
    if any(f not in data for f in _REQUIRED_PRESET_FIELDS):
        return CLASS_CORRUPT
    pid = data.get("preset_id")
    if not isinstance(pid, str) or not PRESET_ID_RE.fullmatch(pid) or f"{pid}.json" != path.name:
        return CLASS_CORRUPT
    if not valid_user(data.get("user")) or data.get("user") != path.parent.name:
        return CLASS_CORRUPT
    dim = data.get("embedding_dim")
    if not isinstance(dim, int) or dim <= 0:
        return CLASS_CORRUPT
    centroids = data.get("centroids")
    # An ACTIVE preset must carry usable centroids; a revoked tombstone legitimately
    # drops them (handled by the revoked classification upstream).
    if data.get("status") == STATUS_ACTIVE:
        if not isinstance(centroids, list) or not centroids:
            return CLASS_CORRUPT
        for c in centroids:
            if not isinstance(c, list) or len(c) != dim:
                return CLASS_CORRUPT
            if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in c):
                return CLASS_CORRUPT
            if abs(l2_norm([float(x) for x in c]) - 1.0) > UNIT_NORM_TOL:
                return CLASS_CORRUPT
        if data.get("centroid_digest") != centroid_digest([[float(x) for x in c] for c in centroids]):
            return CLASS_CORRUPT
    return None


def load_preset(path: Path) -> tuple[Preset | None, str | None]:
    """Load one preset file → ``(preset, failure_class)``.

    ``(Preset, None)`` on a structurally-valid record (active OR a revoked
    tombstone). ``(None, "corrupt"|"unsupported_schema")`` on a load-contract
    failure. NEVER raises — a bad file classifies, surfaces via ``list``, never a
    crash, never a block. That "never raises" is LOAD-BEARING: every caller
    (``resolve_for_encounter`` → ``accumulate_encounter``, ``list_user_presets`` →
    the presets CLI + ``GET /scribe/presets``, ``_find_usable_preset`` → the binding
    route) relies on it to fail OPEN; a raise here blocks the encounter forever."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, CLASS_CORRUPT
    if not isinstance(data, dict):
        return None, CLASS_CORRUPT
    try:
        fail = _structural_ok(data, path)
        if fail is not None:
            return None, fail
        return Preset.from_dict(data), None
    except Exception:  # noqa: BLE001 — BELT: the load path NEVER raises. _structural_ok's
        # teeth should catch every malformed shape, but a construction/validation failure
        # must CLASSIFY (corrupt), never propagate and block an encounter (fail-open).
        return None, CLASS_CORRUPT


def engine_class(preset: Preset, engine_fingerprint: dict[str, Any]) -> str:
    """Classify a STRUCTURALLY-VALID ACTIVE preset against the current engine — the
    DERIVED-at-read invalidation (no write on a read path). ``incompatible_model``
    if the embedding model differs; ``incompatible_engine`` if the model matches but
    the revision / engine_version differs; else ``usable``."""
    pe = preset.engine or {}
    if pe.get("embedding_model") != engine_fingerprint.get("embedding_model"):
        return CLASS_INCOMPATIBLE_MODEL
    if (pe.get("embedding_revision") != engine_fingerprint.get("embedding_revision")
            or pe.get("engine_version") != engine_fingerprint.get("engine_version")):
        return CLASS_INCOMPATIBLE_ENGINE
    return CLASS_USABLE


def classify(preset: Preset | None, failure_class: str | None,
             engine_fingerprint: dict[str, Any]) -> str:
    """The full classification of a loaded preset (list + resolution share it):
    structural failure → its class; revoked → ``revoked``; else the engine class."""
    if failure_class is not None:
        return failure_class
    assert preset is not None
    if preset.status == STATUS_REVOKED or preset.revoked is not None:
        return CLASS_REVOKED
    return engine_class(preset, engine_fingerprint)


@dataclass
class RegistryEntry:
    path: Path
    preset: Preset | None
    classification: str


def list_user_presets(enrollment_dir: str | Path, user: str,
                      engine_fingerprint: dict[str, Any]) -> list[RegistryEntry]:
    """Directory-scan the user's presets → classified entries. Files ARE the index.
    Never raises on a bad file (it classifies ``corrupt``/``unsupported_schema``)."""
    d = user_dir(enrollment_dir, user)
    entries: list[RegistryEntry] = []
    if not d.is_dir():
        return entries
    for p in sorted(d.iterdir()):
        if not (p.is_file() and p.suffix == ".json" and PRESET_ID_RE.fullmatch(p.stem)):
            continue
        preset, fail = load_preset(p)
        entries.append(RegistryEntry(p, preset, classify(preset, fail, engine_fingerprint)))
    return entries


def _preset_files(enrollment_dir: str | Path, user: str) -> list[Path]:
    d = user_dir(enrollment_dir, user)
    if not d.is_dir():
        return []
    return [p for p in d.iterdir()
            if p.is_file() and p.suffix == ".json" and PRESET_ID_RE.fullmatch(p.stem)]


def count_active_presets(enrollment_dir: str | Path, user: str) -> int:
    """Count the ACTIVE (non-tombstone) presets — the 32/user CAP gate.

    OPERATOR RULING (panel fix-round): the cap counts ACTIVE presets ONLY. Tombstones
    never consume a slot, which kills the dead-id exhaustion path (a delete-heavy user
    could otherwise permanently burn the cap — `delete` could never free headroom, making
    32 a LIFETIME create budget). Tombstones still PERSIST, so id reuse stays blocked.
    Growth is bounded separately by :func:`count_preset_files` /
    :data:`MAX_PRESET_FILES_PER_USER`.

    (The name previously LIED — it counted tombstones too, on the very function gating
    the cap. Now it does what it says.)"""
    n = 0
    for p in _preset_files(enrollment_dir, user):
        preset, fail = load_preset(p)
        if preset is not None and preset.status == STATUS_ACTIVE and preset.revoked is None:
            n += 1
    return n


def count_preset_files(enrollment_dir: str | Path, user: str) -> int:
    """Count ALL stored preset ids (active + tombstones) — the id-space / growth bound.
    Tombstones persist forever (blocking id reuse), so they need their OWN generous
    ceiling rather than consuming the active cap."""
    return len(_preset_files(enrollment_dir, user))


# --- write / revoke ----------------------------------------------------------

def write_preset(enrollment_dir: str | Path, preset: Preset, *, is_new: bool) -> Path:
    """Persist a preset atomically (0600). ``is_new`` enforces the cap 32/user +
    a fresh id (no overwrite); a re-record (``is_new=False``) replaces the SAME id
    in place (centroid_version already bumped by the caller). Validates the record's
    own digest so a mis-built centroid never persists."""
    if not valid_user(preset.user):
        raise EnrollmentError(f"invalid user {preset.user!r}")
    if not PRESET_ID_RE.fullmatch(preset.preset_id):
        raise EnrollmentError(f"invalid preset_id {preset.preset_id!r}")
    if preset.status == STATUS_ACTIVE:
        expect = centroid_digest(preset.centroids)
        if preset.centroid_digest != expect:
            raise EnrollmentError("centroid_digest does not match centroids (mis-built record)")
    path = preset_path(enrollment_dir, preset.user, preset.preset_id)
    if is_new:
        # ACTIVE-ONLY cap (operator ruling): a tombstone never consumes a slot, so a
        # delete-heavy user can always re-create. Tombstones persist (id reuse blocked).
        if count_active_presets(enrollment_dir, preset.user) >= MAX_PRESETS_PER_USER:
            raise EnrollmentError("preset_cap")   # explicit cap refusal (memo)
        # ...but the stored-id space still needs a ceiling, else tombstones grow forever.
        if count_preset_files(enrollment_dir, preset.user) >= MAX_PRESET_FILES_PER_USER:
            raise EnrollmentError("tombstone_cap")  # loud, explicit — operator must prune
        if path.exists():
            raise EnrollmentError(f"preset_id {preset.preset_id} already exists")
    _atomic_write_json(path, preset.to_dict())
    return path


def revoke_preset(enrollment_dir: str | Path, user: str, preset_id: str, *, reason: str) -> Path:
    """Tombstone a preset: DROP centroids, keep id/name/timestamps + a revoked
    reason, block id reuse. Human-revoke is one of the only two centroid-destroying
    paths (the other is a passed re-record). Returns the path."""
    path = preset_path(enrollment_dir, user, preset_id)
    preset, fail = load_preset(path)
    if preset is None:
        raise EnrollmentError(f"cannot revoke {preset_id}: {fail or 'not found'}")
    now = _iso_now()
    tomb = preset.to_dict()
    tomb.update({
        "status": STATUS_REVOKED, "centroids": [], "centroid_digest": "",
        "updated_at": now, "revoked": {"at": now, "reason": reason},
    })
    _atomic_write_json(path, tomb)
    return path


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --- binding sidecar + resolve -----------------------------------------------

def binding_path(enc_dir: str | Path) -> Path:
    return Path(enc_dir) / BINDING_NAME


def write_binding(enc_dir: str | Path, preset: Preset) -> Path:
    """Atomic, WRITE-ONCE ``_ENROLLMENT.json`` in the encounter inbox — ids only
    (never a centroid/audio). Cannot collide with ``chunk_*`` / ``_CLOSED``; the
    sweep skips it by name. Write-once ⇒ the selection route owns the lock."""
    path = binding_path(enc_dir)
    _atomic_write_json(path, {
        "schema_version": SCHEMA_VERSION, "user": preset.user,
        "preset_id": preset.preset_id, "centroid_version": preset.centroid_version,
        "centroid_digest": preset.centroid_digest, "bound_at": _iso_now(),
    }, exclusive=True)
    return path


def read_binding(enc_dir: str | Path) -> dict[str, Any] | None:
    p = binding_path(enc_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class ResolvedEnrollment:
    user: str
    preset_id: str
    centroid_version: int
    centroids: list[list[float]]
    embedding_dim: int


def resolve_for_encounter(
    enc_dir: str | Path, enrollment_dir: str | Path, engine_fingerprint: dict[str, Any],
) -> ResolvedEnrollment | str:
    """Resolve the encounter's bound preset → :class:`ResolvedEnrollment`, or a typed
    REFUSAL string. EVERY refusal ⇒ no anchor ⇒ the caller drives all-``unknown``
    (fail-open) + this emits ONE reason-coded ``scribe.enrollment.unusable`` log.
    NEVER raises, NEVER blocks (fail-open-for-availability).

    The digest pin is enforced on EVERY resolution: ``binding.centroid_digest`` must
    equal ``preset.centroid_digest`` — a hostile mid-encounter re-record (new
    centroid, bumped version) lands here as ``digest_mismatch``, loud fail-open,
    never a silent re-anchor (the safety-BLOCK laundering close)."""
    # ABSENT vs CORRUPT — a PRESENT-but-unreadable _ENROLLMENT.json is NOT "no preset
    # selected". The memo's degradation ladder draws the line exactly here: no-binding is
    # a first-class choice (silent); a binding artifact that EXISTS but cannot be parsed
    # (truncation, disk-full, hostile write) is a PRESENT-but-unusable binding and MUST
    # drive the reason-coded log — otherwise an operator greps for the signal and finds
    # nothing, unable to tell "clinician chose no preset" from "the binding was destroyed".
    if not binding_path(enc_dir).is_file():
        return _refuse(REFUSAL_NO_BINDING, enc_dir, artifact="binding")
    binding = read_binding(enc_dir)
    if binding is None:
        return _refuse(REFUSAL_CORRUPT, enc_dir, artifact="binding")
    user = binding.get("user")
    preset_id = binding.get("preset_id")
    if not (valid_user(user) and isinstance(preset_id, str) and PRESET_ID_RE.fullmatch(preset_id)):
        # The BINDING artifact itself is malformed (distinct from a corrupt PRESET).
        return _refuse(REFUSAL_CORRUPT, enc_dir, preset_id=preset_id, artifact="binding")
    path = preset_path(enrollment_dir, user, preset_id)
    if not path.is_file():
        return _refuse(REFUSAL_UNKNOWN_PRESET, enc_dir, preset_id=preset_id, artifact="preset")
    preset, fail = load_preset(path)
    cls = classify(preset, fail, engine_fingerprint)
    if cls != CLASS_USABLE:
        # The classification IS the refusal reason (they share the same vocabulary);
        # it derives from the PRESET file.
        return _refuse(cls, enc_dir, preset_id=preset_id, artifact="preset")
    assert preset is not None
    if binding.get("centroid_digest") != preset.centroid_digest:
        # The BINDING's pinned digest no longer matches the preset (stale binding /
        # mid-encounter re-record) — the artifact at fault is the binding.
        return _refuse(REFUSAL_DIGEST_MISMATCH, enc_dir, preset_id=preset_id, artifact="binding")
    return ResolvedEnrollment(
        user=preset.user, preset_id=preset.preset_id,
        centroid_version=preset.centroid_version, centroids=preset.centroids,
        embedding_dim=preset.embedding_dim,
    )


def preset_fit_for_status(
    enc_dir: str | Path, enrollment_dir: str | Path, engine_fingerprint: dict[str, Any],
) -> str:
    """P4-5a ``preset_fit`` for ``GET /scribe/status``: ``"ok"`` iff a bound preset
    resolves USABLE (present, classifies usable, digest matches), else ``"unarmed"``.
    NON-LOGGING by design — a status poll must not spam ``scribe.enrollment.unusable``
    (the real resolution + its one log happens in the pipeline). 5a emits only
    ``unarmed|ok``; ``warming|weak|none`` activate with the 5b latch."""
    if not str(enrollment_dir or ""):
        return "unarmed"
    binding = read_binding(enc_dir)
    if binding is None:
        return "unarmed"
    user, preset_id = binding.get("user"), binding.get("preset_id")
    if not (valid_user(user) and isinstance(preset_id, str) and PRESET_ID_RE.fullmatch(preset_id)):
        return "unarmed"
    path = preset_path(enrollment_dir, user, preset_id)
    if not path.is_file():
        return "unarmed"
    preset, fail = load_preset(path)
    if classify(preset, fail, engine_fingerprint) != CLASS_USABLE:
        return "unarmed"
    assert preset is not None
    return "ok" if binding.get("centroid_digest") == preset.centroid_digest else "unarmed"


# ONCE-PER-LIFECYCLE latch for the unusable log, keyed by (encounter, reason). The
# pipeline resolves on EVERY ~30 s sweep for EVERY encounter dir still on disk, so a
# persistently-unusable binding (a revoked preset on a closed-but-not-swept encounter)
# would otherwise warn ~2880×/day forever — burying rarer warnings and training the
# operator to ignore the exact signal that also announces a hostile mid-encounter swap.
# Same shape as the surveyor's once-per-lifecycle observability latch.
_UNUSABLE_LOGGED: set[tuple[str, str]] = set()
_UNUSABLE_LATCH_MAX = 4096                      # bound the set (never grows unboundedly)


def _refuse(reason: str, enc_dir: str | Path, *, preset_id: str | None = None,
            artifact: str = "preset") -> str:
    key = (str(enc_dir), reason)
    if key not in _UNUSABLE_LOGGED:
        if len(_UNUSABLE_LOGGED) < _UNUSABLE_LATCH_MAX:
            _UNUSABLE_LOGGED.add(key)
        log.warning(
            "scribe.enrollment.unusable",
            reason=reason,
            artifact=artifact,                  # "binding" | "preset" — disambiguates a
                                                # corrupt binding from a corrupt preset in
                                                # diagnosis (the enum stays 8; the log widens)
            preset_id=preset_id,                # id-only, never a name (PHI-free)
            detail="bound preset could not be resolved — encounter runs all-unknown "
                   "(fail-open); the P4-2 banner fires. No block. (Logged ONCE per "
                   "encounter+reason — the resolution itself repeats every sweep.)",
        )
    return reason
