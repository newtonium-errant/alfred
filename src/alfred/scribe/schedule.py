"""PHIA s.50 retention SCHEDULE artifact (task #13 §4, slice 13c).

A versioned, operator-published JSON document declaring per-record-class retention windows. The
``retention.schedule_published`` [D] emitter pins its sha into the clinical chain, so "which schedule
governed this box on date X" is chain-answerable (declared, not tribal). Enforcement posture (§4):
the retention sweep SURFACES over-window PHI classes (a ``review_due`` count + a latched signal) —
it NEVER auto-destroys; destruction stays the explicit, audited operator playbook (§5).

Kept in its own module (not ``retention.py``) so the schedule slice is isolated from the seal-path
core. Reuses the R7-hardened :func:`retention._atomic_write_bytes` for the publish write.

Schedule JSON shape (v1)::

    {
      "schedule_version": "v1",
      "effective_date": "2026-07-19",
      "classes": {
        "<class>": {"window_days": <int >= 0 | null>, "basis": "<why>"},   # null ⇒ never auto-pruned
        ...  # EXACTLY the SCHEDULE_CLASSES set — no extra, no missing (a frozen contract)
      },
      "minor_rule": {"majority_age": 19, "post_majority_years": 10}
    }

The nine classes + the operator-confirmed v1 windows are design §4/§11. ``window_days: null`` is the
never-auto-pruned sentinel (consent / audit / retention events / voice presets). ``audit_access_log``
additionally carries a ``floor_days`` (NS Reg s.11(3) ≥ 1yr). ``minor_rule`` is a DECLARED policy for
the operator's manual review — the sweep cannot compute a patient's age (PHI-free chain, no DOB), so
the age-of-majority rule is never applied automatically.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from alfred.evstore import sha256_hex
from alfred.scribe.retention import _atomic_write_bytes

# The nine s.50 record classes (design §4 table). A frozen contract: a published schedule MUST carry
# EXACTLY these — an extra class is a typo / drift, a missing class is an incomplete schedule; either
# fails validation (fail-closed, mirrors the #11 KINDS registry discipline).
SCHEDULE_CLASSES: frozenset[str] = frozenset({
    "encounter_audio_sealed", "transcript_ledger", "clinical_note", "consent_events",
    "audit_access_log", "retention_events", "diarize_stats", "voice_presets", "bug_reports",
})

# The PHI record class the sweep surfaces over-window (the sealed ``.age`` blobs it can enumerate on
# disk). The transcript/note windows are declared for the record; their surfacing is not the sweep's
# file-enumeration job (the note lives in the vault). See ``retention_sweep._surface_over_window``.
SURFACED_PHI_CLASS = "encounter_audio_sealed"

# The telemetry class whose window the retention prune consults (falling back to its own constant when
# no schedule is published). PHI-FREE — a rolling prune, not an audited PHI destruction (§4).
TELEMETRY_CLASS = "diarize_stats"

_DAYS_PER_YEAR = 365  # coarse — the windows drive a SOFT 'review due' surface signal, never a destroy


class ScheduleError(ValueError):
    """A retention schedule failed structural validation (missing/extra class, a bad window, a
    malformed date). Fail-closed: publish REFUSES it; the sweep's load treats it as no-usable-schedule
    (skip surfacing, never crash)."""


def default_schedule_v1() -> dict:
    """The operator-confirmed v1 schedule (design §4 windows + §11 ruling). The bundled example JSON
    must match this (a drift pin asserts it); the operator publishes the real one on-box at install."""
    yr10 = 10 * _DAYS_PER_YEAR
    return {
        "schedule_version": "v1",
        "effective_date": "2026-07-19",
        "classes": {
            "encounter_audio_sealed": {
                "window_days": yr10, "basis": "10 yr from last activity — clinical-record norm / CMPA / dispute protection"},
            "transcript_ledger": {
                "window_days": yr10, "basis": "10 yr (= audio) — derived clinical record"},
            "clinical_note": {
                "window_days": yr10, "basis": "10 yr — the clinical record (PHIA s.50 core)"},
            "consent_events": {
                "window_days": None, "basis": "never auto-pruned — consent evidence must outlive the record it gates"},
            "audit_access_log": {
                "window_days": None, "floor_days": _DAYS_PER_YEAR,
                "basis": "never auto-pruned (floor 1 yr) — PHIA s.63 + NS Reg s.11(3)"},
            "retention_events": {
                "window_days": None, "basis": "never auto-pruned — proof-of-destruction must be permanent (s.50)"},
            "diarize_stats": {
                "window_days": 180, "basis": "180 d rolling — PHI-FREE ML telemetry, not a clinical record"},
            "voice_presets": {
                "window_days": None, "basis": "until superseded / re-enroll — biometric embedding, not per-encounter PHI"},
            "bug_reports": {
                "window_days": _DAYS_PER_YEAR, "basis": "1 yr or until resolved"},
        },
        # DECLARED policy for the operator's manual review (the sweep never applies it — no DOB in the
        # PHI-free chain). NS majority = 19; retain to age-19 + 10 yr, whichever is LONGER than adult.
        "minor_rule": {"majority_age": 19, "post_majority_years": 10},
    }


def validate_schedule(data: Any) -> dict:
    """Structurally validate a schedule dict, returning it on success or raising :class:`ScheduleError`.
    Fail-closed on: a non-dict, an empty/absent ``schedule_version``, a malformed ``effective_date``
    (must parse as an ISO date), a ``classes`` set that is not EXACTLY :data:`SCHEDULE_CLASSES`, a
    non-scalar / negative / non-int-or-null ``window_days``, or a malformed ``minor_rule``."""
    if not isinstance(data, dict):
        raise ScheduleError("schedule must be a JSON object")
    version = data.get("schedule_version")
    if not isinstance(version, str) or not version.strip():
        raise ScheduleError("schedule_version must be a non-empty string")
    eff = data.get("effective_date")
    if not isinstance(eff, str) or not eff.strip():
        raise ScheduleError("effective_date must be a non-empty ISO date string")
    try:
        date.fromisoformat(eff)
    except ValueError as exc:
        raise ScheduleError(f"effective_date is not a valid ISO date: {exc}") from exc
    classes = data.get("classes")
    if not isinstance(classes, dict):
        raise ScheduleError("classes must be a JSON object")
    present = set(classes)
    if present != set(SCHEDULE_CLASSES):
        missing = sorted(SCHEDULE_CLASSES - present)
        extra = sorted(present - SCHEDULE_CLASSES)
        raise ScheduleError(
            f"classes must be EXACTLY the {len(SCHEDULE_CLASSES)} s.50 record classes — "
            f"missing={missing}, unexpected={extra}")
    for name, spec in classes.items():
        if not isinstance(spec, dict):
            raise ScheduleError(f"class {name!r} must be a JSON object")
        window = spec.get("window_days")
        if window is not None and (not isinstance(window, int) or isinstance(window, bool) or window < 0):
            raise ScheduleError(
                f"class {name!r} window_days must be a non-negative integer or null (got {window!r})")
        floor = spec.get("floor_days")
        if floor is not None and (not isinstance(floor, int) or isinstance(floor, bool) or floor < 0):
            raise ScheduleError(f"class {name!r} floor_days must be a non-negative integer or null")
    minor = data.get("minor_rule")
    if not isinstance(minor, dict):
        raise ScheduleError("minor_rule must be a JSON object")
    for key in ("majority_age", "post_majority_years"):
        val = minor.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise ScheduleError(f"minor_rule.{key} must be a non-negative integer")
    return data


def canonical_schedule_bytes(data: dict) -> bytes:
    """Deterministic on-disk bytes for a schedule — sorted keys, 2-space indent, trailing newline. The
    published sha is taken over EXACTLY these bytes, so ``schedule show`` recomputes it identically."""
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")


def schedule_sha256(data: dict) -> str:
    """sha256 over the canonical bytes of ``data`` — the digest pinned by ``retention.schedule_published``."""
    return sha256_hex(canonical_schedule_bytes(data))


def publish_schedule(dest_path: str | Path, data: Any) -> dict:
    """Validate ``data`` then atomically write its canonical bytes to ``dest_path`` (the R7-hardened
    atomic write) and return ``{schedule_version, schedule_sha256, effective_date}`` — the exact
    payload the caller pins via ``retention.schedule_published`` [D]. Raises :class:`ScheduleError` on
    a malformed schedule (REFUSE — never publish an invalid s.50 artifact)."""
    validate_schedule(data)
    payload_bytes = canonical_schedule_bytes(data)
    _atomic_write_bytes(Path(dest_path), payload_bytes)
    return {
        "schedule_version": data["schedule_version"],
        "schedule_sha256": sha256_hex(payload_bytes),
        "effective_date": data["effective_date"],
    }


def load_schedule(path: str | Path) -> dict | None:
    """Load + validate the schedule at ``path``, or ``None`` when absent / unreadable / malformed —
    FAIL-CLOSED for the sweep (a malformed schedule is treated as no usable schedule → skip surfacing,
    never crash the sweep). Use :func:`validate_schedule` directly when a raise is wanted (the CLI)."""
    p = Path(path)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # absent / unreadable / unsearchable-parent (is_file re-raises EACCES) / malformed → fail-closed
        # None (the sweep skips surfacing; D5 — a stat-EACCES must not escape and kill the sweep tick).
        return None
    try:
        return validate_schedule(data)
    except ScheduleError:
        return None


def class_window_days(schedule: dict, class_name: str) -> int | None:
    """The retention window (in days) for ``class_name`` in ``schedule``, or ``None`` for a
    never-auto-pruned class (or an unknown class). Never raises — a malformed class spec reads as
    never-pruned (the fail-open, non-destructive direction)."""
    spec = (schedule.get("classes") or {}).get(class_name)
    if not isinstance(spec, dict):
        return None
    window = spec.get("window_days")
    return window if isinstance(window, int) and not isinstance(window, bool) and window >= 0 else None
