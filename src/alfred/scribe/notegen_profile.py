"""#14 item-12 slice 14b — the versioned ``note_profile`` artifact + fail-closed loader.

A per-instance, operator-owned, versioned JSON artifact describing the note-gen structure (SOAP
sections + a succinctness target + operator-approved terminology prefs + a prompt-tuner-reviewed
style-guidance ref). Mirrors the retention-SCHEDULE artifact pattern (design #13 §4): atomic
temp→replace write, a canonical-bytes sha256 fingerprint, fail-closed read.

**14b scope = the artifact + loader + create-time ATTRIBUTION + the init/show CLI. NO CONSUMERS.**
The profile drives ONLY frontmatter attribution in 14b (``note_profile_id`` / ``note_profile_version``
at draft-create — the fields 14a's edit-diff capture already reads). Profile-driven RENDERING
(section select/reorder/heading, the succinctness rule, the style block) is a LATER slice (§3.2 #7)
and the quality pass is 14c — so ``render_soap`` / ``build_prompt`` / the frozen extraction JSON are
BYTE-UNCHANGED this slice.

**NOTE-GEN ALWAYS WORKS (the fail-safe invariant, #26-loader posture):** note-gen NEVER hard-depends
on a profile file. Absent → the built-in :data:`DEFAULT_PROFILE` (id ``soap``, version ``0`` — today's
implicit behavior, and exactly 14a's ``soap``/``0`` reader default). A CORRUPT profile is REFUSED +
LOGGED and skipped in favour of the next-lower valid version, else the DEFAULT — never a crash, never
silently-used garbage.

**id frozen / version as the ledger:** ``note_type`` stays STABLE (``soap``) — a discontinuity there
would corrupt 14a's signal-I attribution stream. ``profile_version`` INCREMENTS (0 default → 1 seed →
2… as the operator-approval ledger); that increment IS the intended attribution signal ("a v_n tuning
change's effect shows up in v_{n+1} rows"), not a discontinuity. ``note_type`` names the STRUCTURE
(soap), not the clinic context — non-SOAP types arrive LATER as parallel structural names.

The ``show`` sha256 is DISPLAY-ONLY (a fingerprint + an on-disk-canonical self-check); the DURABLE
chain-pinned sha (a ``note.template_published`` evstore event) is deferred to LATER (§3.1), so 14b
touches NO evstore kind and NO widening pin.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from alfred.evstore import sha256_hex
from alfred.scribe.notegen import SOAP_SECTIONS, _SECTION_HEADINGS
from alfred.scribe.retention import _atomic_write_bytes

if TYPE_CHECKING:
    from alfred.scribe.config import ScribeConfig

log = structlog.get_logger(__name__)

_PROFILES_SUBDIR = "notegen/profiles"
_PROFILE_NAME_RE = re.compile(r"^note_profile_v(\d+)\.json$")

# The single ratified SOAP profile (Q4). ``note_type`` names the STRUCTURE, stable across versions.
DEFAULT_NOTE_TYPE = "soap"
DEFAULT_SUCCINCTNESS_TARGET = 25   # words/claim — a FIELD only in 14b (14c/14d read it; not a gate)


class ProfileError(Exception):
    """A note_profile JSON is malformed — REFUSED (the CLI raises; the loader fail-closes to DEFAULT)."""


@dataclass(frozen=True)
class ProfileSection:
    key: str
    heading: str
    required: bool
    order: int

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "heading": self.heading, "required": self.required, "order": self.order}


@dataclass(frozen=True)
class NoteProfile:
    """A validated note_profile. 14b consumes ONLY ``note_type`` + ``profile_version`` (attribution);
    the rest is stored for the later render/quality/eval consumers (fields-from-day-one)."""

    profile_version: int
    note_type: str
    sections: tuple[ProfileSection, ...]
    succinctness_target_words_per_claim: int
    terminology_preferences: tuple[dict, ...] = ()
    style_guidance_ref: str | None = None
    plain_language_source: str = ""
    created_by: str = "operator"
    created_at: str = ""
    prev_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "note_type": self.note_type,
            "sections": [s.to_dict() for s in self.sections],
            "succinctness_target_words_per_claim": self.succinctness_target_words_per_claim,
            "terminology_preferences": [dict(t) for t in self.terminology_preferences],
            "style_guidance_ref": self.style_guidance_ref,
            "plain_language_source": self.plain_language_source,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "prev_version": self.prev_version,
        }


def _default_sections() -> tuple[ProfileSection, ...]:
    """The current implicit SOAP structure — headings from ``notegen._SECTION_HEADINGS`` (single source
    of truth, never re-typed), S/A/P required + O optional (a sensible primary-care default; consumed
    only by 14c's quality pass, inert in 14b)."""
    required = {"subjective": True, "objective": False, "assessment": True, "plan": True}
    return tuple(
        ProfileSection(key=sec, heading=_SECTION_HEADINGS[sec], required=required[sec], order=i + 1)
        for i, sec in enumerate(SOAP_SECTIONS)
    )


# The built-in default — today's implicit behavior at version 0. Used when no profile file exists /
# all are corrupt. id/version match 14a's soap/0 reader default (attribution continuity).
DEFAULT_PROFILE = NoteProfile(
    profile_version=0,
    note_type=DEFAULT_NOTE_TYPE,
    sections=_default_sections(),
    succinctness_target_words_per_claim=DEFAULT_SUCCINCTNESS_TARGET,
)


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{field_name} must be an int (got {type(value).__name__})")
    return value


def profile_from_dict(data: Any) -> NoteProfile:
    """Validate + build a :class:`NoteProfile` from a raw dict. Raises :class:`ProfileError` on any
    malformed REQUIRED structural field (``profile_version`` / ``note_type`` / ``sections`` /
    ``succinctness_target_words_per_claim``); optional fields default (schema-tolerant, forward-compat —
    an unknown extra key is ignored, not fatal)."""
    if not isinstance(data, dict):
        raise ProfileError("note_profile must be a JSON object")
    version = _coerce_int(data.get("profile_version"), "profile_version")
    if version < 0:
        raise ProfileError("profile_version must be >= 0")
    note_type = data.get("note_type")
    if not isinstance(note_type, str) or not note_type.strip():
        raise ProfileError("note_type must be a non-empty string")
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ProfileError("sections must be a non-empty list")
    sections: list[ProfileSection] = []
    for s in raw_sections:
        if not isinstance(s, dict):
            raise ProfileError("each section must be a JSON object")
        key, heading = s.get("key"), s.get("heading")
        if not isinstance(key, str) or not key:
            raise ProfileError("section.key must be a non-empty string")
        if not isinstance(heading, str) or not heading:
            raise ProfileError("section.heading must be a non-empty string")
        if not isinstance(s.get("required"), bool):
            raise ProfileError(f"section[{key!r}].required must be a bool")
        sections.append(ProfileSection(
            key=key, heading=heading, required=s["required"], order=_coerce_int(s.get("order"), "section.order")))
    target = _coerce_int(data.get("succinctness_target_words_per_claim"), "succinctness_target_words_per_claim")
    if target <= 0:
        raise ProfileError("succinctness_target_words_per_claim must be > 0")
    prefs = data.get("terminology_preferences") or []
    if not isinstance(prefs, list) or not all(isinstance(t, dict) for t in prefs):
        raise ProfileError("terminology_preferences must be a list of objects")
    style_ref = data.get("style_guidance_ref")
    if style_ref is not None and not isinstance(style_ref, str):
        raise ProfileError("style_guidance_ref must be a string or null")
    prev = data.get("prev_version")
    if prev is not None and (isinstance(prev, bool) or not isinstance(prev, int)):
        raise ProfileError("prev_version must be an int or null")
    return NoteProfile(
        profile_version=version, note_type=note_type, sections=tuple(sections),
        succinctness_target_words_per_claim=target,
        terminology_preferences=tuple(dict(t) for t in prefs), style_guidance_ref=style_ref,
        plain_language_source=str(data.get("plain_language_source", "")),
        created_by=str(data.get("created_by", "operator")),
        created_at=str(data.get("created_at", "")), prev_version=prev)


def canonical_profile_bytes(data: dict) -> bytes:
    """Deterministic on-disk bytes — sorted keys, 2-space indent, trailing newline (mirrors
    ``schedule.canonical_schedule_bytes``), so ``show`` recomputes the sha identically."""
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")


def profile_sha256(data: dict) -> str:
    """sha256 over the canonical bytes — the display fingerprint (no durable chain pin in 14b)."""
    return sha256_hex(canonical_profile_bytes(data))


def resolve_profiles_dir(config: "ScribeConfig") -> Path:
    """``<input_dir parent>/notegen/profiles`` — derived purely from ``config.input_dir`` (mirrors
    #26's ``resolve_candidates_dir`` / the bug + retention dirs), per-instance-correct."""
    return Path(config.input_dir).expanduser().parent / _PROFILES_SUBDIR


def profile_path(profiles_dir: str | Path, version: int) -> Path:
    return Path(profiles_dir) / f"note_profile_v{version}.json"


def write_profile(dest_path: str | Path, data: Any) -> dict:
    """Validate ``data`` then atomically write its canonical bytes (0600 via ``_atomic_write_bytes``)
    and return ``{profile_version, note_type, profile_sha256}``. Raises :class:`ProfileError` on a
    malformed profile — REFUSE, never write an invalid artifact."""
    profile = profile_from_dict(data)
    payload = canonical_profile_bytes(profile.to_dict())
    _atomic_write_bytes(Path(dest_path), payload)
    return {"profile_version": profile.profile_version, "note_type": profile.note_type,
            "profile_sha256": sha256_hex(payload)}


def load_profile(path: str | Path) -> NoteProfile | None:
    """Load + validate the profile at ``path``, or ``None`` when absent / unreadable / malformed —
    FAIL-CLOSED (a corrupt profile is treated as no usable profile). Use :func:`profile_from_dict`
    directly when a raise is wanted (the CLI)."""
    p = Path(path)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return profile_from_dict(data)
    except ProfileError:
        return None


def resolve_active_profile(config: "ScribeConfig") -> NoteProfile:
    """The ACTIVE profile — the HIGHEST-version VALID ``note_profile_v<N>.json`` under the profiles dir,
    else the built-in :data:`DEFAULT_PROFILE`. A corrupt higher-version file is SKIPPED + LOGGED (never
    silently used, never crashes note-gen) and the next-lower valid version wins. This is the fail-safe
    'note-gen always works' invariant — an absent or wholly-corrupt store degrades to today's behavior.

    TOTAL by contract: this NEVER raises. The per-file loop handles the anticipated corruption modes
    (absent / unreadable / malformed → DEFAULT via ``load_profile``'s fail-closed), and the OUTERMOST
    catch-all below degrades ANY unanticipated escape (a shape the coercion doesn't guard, a future
    latent bug) to the DEFAULT + a DISTINCT ``resolve_failed`` log — so an unexpected failure can never
    crash note-gen on a clinical draft, and is diagnosable rather than a silent fallback."""
    try:
        profiles_dir = resolve_profiles_dir(config)
        candidates: list[tuple[int, Path]] = []
        try:
            for entry in profiles_dir.iterdir():
                m = _PROFILE_NAME_RE.match(entry.name)
                if m and entry.is_file():
                    candidates.append((int(m.group(1)), entry))
        except OSError:
            return DEFAULT_PROFILE          # no dir yet / unreadable → the built-in default (today's behavior)
        for version, path in sorted(candidates, key=lambda c: c[0], reverse=True):
            profile = load_profile(path)
            if profile is not None:
                return profile
            log.warning(
                "scribe.notegen_profile.corrupt", path=str(path), version=version,
                detail="a note_profile file is malformed — SKIPPED (falling back to the next-lower valid "
                       "version, else the built-in default). Note-gen is UNAFFECTED. Fix or remove the file.")
        return DEFAULT_PROFILE
    except Exception:  # noqa: BLE001 — TOTAL contract: resolve NEVER raises, so note-gen always works
        log.warning(
            "scribe.notegen_profile.resolve_failed",
            detail="note_profile resolution hit an UNANTICIPATED error — degrading to the built-in "
                   "default (soap/0) so note-gen is UNAFFECTED. Distinct from the per-file .corrupt skip: "
                   "this is an unexpected escape (a shape the coercion missed / a latent bug) — investigate.")
        return DEFAULT_PROFILE


def seed_profile_dict(version: int = 1) -> dict:
    """The seed profile the ``init`` CLI writes — the built-in default STRUCTURE at ``version`` (default
    v1), the current implicit SOAP behavior made an explicit, versionable artifact."""
    seed = DEFAULT_PROFILE.to_dict()
    seed["profile_version"] = version
    seed["prev_version"] = None
    return seed
