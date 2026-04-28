"""TTS speed preferences — per (instance, user) persistence + history.

The ``/speed`` slash command lets Andrew adjust ElevenLabs TTS speed for
the currently-chatting instance. Every ElevenLabs path reads the
preference before synthesis so Salem's ``/brief`` audio, future STAY-C
clinical narration, and future V.E.R.A. dispatch all pick up the same
calibration automatically.

The preference is stored on the user's person record under
``preferences.voice``:

    preferences:
      voice:
        speeds:
          salem: 1.2
          stayc: 0.95
        history:
          - instance: salem
            value: 1.0
            set_at: "2026-04-21T14:22:00Z"
            by: initial_default
          - instance: salem
            value: 1.2
            set_at: "2026-04-21T14:30:00Z"
            by: slash_command
            note: "wanted 20% faster than Rachel default"

Scope: keyed by (instance, user). Each instance has its own voice with
different speech characteristics, so a speed that works for Salem's
Rachel may not work for STAY-C's clinical narrator — preferences are
per-instance on purpose.

Storage: person-record frontmatter via the existing ``vault_edit`` ops
layer. The talker scope already permits edits to the primary-user
person record (calibration writes use the same path).

Range: ElevenLabs v2.5 accepts 0.7-1.2. Out-of-range values are
rejected with a user-facing error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import frontmatter

from alfred.vault import ops

from .utils import get_logger

log = get_logger(__name__)


# ElevenLabs v2.5 speed range. Kept here (not in tts.py) because the
# validation is preference-set logic, not synthesis logic — callers
# should reject out-of-range values BEFORE they reach the ElevenLabs
# API so Andrew gets a helpful error rather than an opaque 400.
SPEED_MIN: Final[float] = 0.7
SPEED_MAX: Final[float] = 1.2
SPEED_DEFAULT: Final[float] = 1.0

# Max history entries to surface in the `/speed` report-mode reply.
HISTORY_TAIL: Final[int] = 3


# --- Instance-name normalisation ----------------------------------------
#
# Must agree with bot._normalize_instance_name for the (instance, user)
# key to match between the /speed handler and the TTS call path. Both
# import the canonical helper from ``alfred.telegram._compat`` so the
# two paths cannot drift; ``_compat.py`` is the single source of truth
# for the legacy ``alfred`` → ``salem`` mapping.

from ._compat import _normalize_instance_name  # noqa: E402, F401


# --- Public API ---------------------------------------------------------


class SpeedValidationError(ValueError):
    """Raised when a proposed speed is outside the accepted range."""


def validate_speed(value: float) -> float:
    """Return ``value`` rounded to 2dp iff in range; raise otherwise.

    The rounding keeps frontmatter YAML tidy (``1.2`` not ``1.2000001``)
    without losing precision Andrew would actually care about.
    """
    if value < SPEED_MIN or value > SPEED_MAX:
        raise SpeedValidationError(
            f"Speed must be between {SPEED_MIN} and {SPEED_MAX}. "
            f"You sent {value}."
        )
    return round(float(value), 2)


def parse_speed_command(text: str) -> tuple[str, float | None, str]:
    """Parse a ``/speed …`` command body.

    Returns ``(mode, value, note)`` where ``mode`` is one of:
        * ``"report"`` — no argument, value/note both empty.
        * ``"reset"`` — arg is literally ``default`` or ``reset``.
        * ``"set"`` — arg is a float; optional trailing prose becomes ``note``.
        * ``"error"`` — arg was not parseable; ``note`` is an error hint.

    ``value`` is only populated for ``"set"`` mode. ``note`` is the
    trailing prose (stripped). Callers validate the range separately via
    :func:`validate_speed`.

    Accepts both ``/speed 1.2`` and raw ``1.2 too slow`` (the caller
    strips the ``/speed`` prefix before passing it in). Be tolerant of
    extra whitespace — Andrew dictates some of these on mobile.
    """
    body = (text or "").strip()
    # Strip the command token if present so this also works when the raw
    # text is handed in verbatim (inline-command path).
    for prefix in ("/speed", "speed"):
        if body.lower().startswith(prefix):
            body = body[len(prefix):].strip()
            break

    if not body:
        return ("report", None, "")

    first, _, rest = body.partition(" ")
    first_low = first.strip().lower()
    note = rest.strip()

    if first_low in {"default", "reset"}:
        return ("reset", None, note)

    try:
        value = float(first)
    except ValueError:
        return (
            "error",
            None,
            f"Couldn't parse {first!r} as a number. "
            f"Try /speed 1.2 or /speed default.",
        )
    return ("set", value, note)


# --- Person-record I/O --------------------------------------------------


def _resolve_person_rel(user_rel_path: str) -> str:
    """Normalise the person-record path to include ``.md``."""
    rel = (user_rel_path or "").strip()
    if not rel:
        return ""
    if not rel.endswith(".md"):
        rel = f"{rel}.md"
    return rel


def _read_voice_block(
    vault_path: Path, user_rel_path: str,
) -> dict[str, Any]:
    """Return the ``preferences.voice`` dict, or a fresh skeleton."""
    rel = _resolve_person_rel(user_rel_path)
    if not rel:
        return {"speeds": {}, "history": []}
    file_path = vault_path / rel
    if not file_path.exists():
        log.info(
            "talker.speed.person_record_missing",
            user_rel_path=user_rel_path,
        )
        return {"speeds": {}, "history": []}
    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.speed.person_record_read_failed",
            user_rel_path=user_rel_path,
            error=str(exc),
        )
        return {"speeds": {}, "history": []}

    prefs = post.metadata.get("preferences") or {}
    if not isinstance(prefs, dict):
        return {"speeds": {}, "history": []}
    voice = prefs.get("voice") or {}
    if not isinstance(voice, dict):
        return {"speeds": {}, "history": []}
    speeds = voice.get("speeds") or {}
    history = voice.get("history") or []
    if not isinstance(speeds, dict):
        speeds = {}
    if not isinstance(history, list):
        history = []
    return {"speeds": dict(speeds), "history": list(history)}


def resolve_tts_speed(
    vault_path: Path,
    user_rel_path: str,
    instance_name: str,
) -> float:
    """Return the stored speed for (instance, user), or ``SPEED_DEFAULT``.

    Safe to call on every TTS path — returns the default when the
    person record is missing, malformed, or doesn't yet carry a
    ``preferences.voice.speeds.<instance>`` entry. Never raises.
    """
    key = _normalize_instance_name(instance_name)
    voice = _read_voice_block(vault_path, user_rel_path)
    value = voice.get("speeds", {}).get(key)
    if value is None:
        return SPEED_DEFAULT
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning(
            "talker.speed.bad_stored_value",
            user_rel_path=user_rel_path,
            instance=key,
            value=value,
        )
        return SPEED_DEFAULT


def set_tts_speed(
    vault_path: Path,
    user_rel_path: str,
    instance_name: str,
    speed: float,
    *,
    by: str = "slash_command",
    note: str = "",
) -> dict[str, Any]:
    """Persist ``speed`` for (instance, user); append a history entry.

    ``by`` labels the source of the change in the history entry:
        * ``"initial_default"`` — system-seeded first entry (rare).
        * ``"slash_command"`` — Andrew ran ``/speed <n>``.
        * ``"reset"`` — Andrew ran ``/speed default``.

    Returns a summary dict ``{"written": bool, "speed": float, "key": str,
    "history_len": int, "reason": str}``. On write failure ``written`` is
    False and ``reason`` carries the error detail.

    Writes via ``ops.vault_edit`` with ``set_fields={"preferences": …}``
    so the frontmatter merge layer handles serialisation uniformly
    with other talker writes. ``preferences`` is a nested dict — the
    vault edit layer treats it as a single frontmatter field, so the
    full nested structure is rewritten on each call. Existing keys
    outside ``voice`` are preserved because we read the full
    ``preferences`` dict first, mutate in place, then write back.
    """
    key = _normalize_instance_name(instance_name)
    rel = _resolve_person_rel(user_rel_path)
    summary: dict[str, Any] = {
        "written": False,
        "speed": float(speed),
        "key": key,
        "history_len": 0,
        "reason": "",
    }
    if not rel:
        summary["reason"] = "no_user_rel_path"
        return summary

    file_path = vault_path / rel
    if not file_path.exists():
        summary["reason"] = "person_record_missing"
        log.warning(
            "talker.speed.person_record_missing_on_write",
            user_rel_path=user_rel_path,
        )
        return summary

    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:  # noqa: BLE001
        summary["reason"] = f"person_record_read_failed: {exc}"
        log.warning(
            "talker.speed.person_record_read_failed_on_write",
            user_rel_path=user_rel_path,
            error=str(exc),
        )
        return summary

    prefs_raw = post.metadata.get("preferences")
    prefs: dict[str, Any] = dict(prefs_raw) if isinstance(prefs_raw, dict) else {}
    voice_raw = prefs.get("voice")
    voice: dict[str, Any] = dict(voice_raw) if isinstance(voice_raw, dict) else {}

    speeds_raw = voice.get("speeds")
    speeds: dict[str, Any] = dict(speeds_raw) if isinstance(speeds_raw, dict) else {}
    history_raw = voice.get("history")
    history: list[Any] = list(history_raw) if isinstance(history_raw, list) else []

    speeds[key] = float(speed)
    entry: dict[str, Any] = {
        "instance": key,
        "value": float(speed),
        "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by": by,
    }
    if note:
        entry["note"] = note
    history.append(entry)

    voice["speeds"] = speeds
    voice["history"] = history
    prefs["voice"] = voice

    try:
        ops.vault_edit(
            vault_path, rel, set_fields={"preferences": prefs},
        )
    except Exception as exc:  # noqa: BLE001
        summary["reason"] = f"vault_edit_failed: {exc}"
        log.warning(
            "talker.speed.apply_failed",
            user_rel_path=user_rel_path,
            instance=key,
            error=str(exc),
        )
        return summary

    summary["written"] = True
    summary["reason"] = "ok"
    summary["history_len"] = len(history)
    log.info(
        "talker.speed.set",
        user_rel_path=user_rel_path,
        instance=key,
        speed=float(speed),
        by=by,
        has_note=bool(note),
    )
    return summary


def format_report(
    vault_path: Path,
    user_rel_path: str,
    instance_name: str,
) -> str:
    """Return the ``/speed`` report-mode reply string.

    Shows the current speed for this (instance, user) pair plus the
    last :data:`HISTORY_TAIL` history entries scoped to the same
    instance. If no preference has been set, reports the default as
    "not yet customized" so Andrew knows whether he's talking to a
    stored value or the fallback.
    """
    key = _normalize_instance_name(instance_name)
    voice = _read_voice_block(vault_path, user_rel_path)
    speeds = voice.get("speeds", {})
    history = voice.get("history", [])

    stored = speeds.get(key)
    if stored is None:
        header = f"{instance_name} speed: default {SPEED_DEFAULT} (not yet customized)."
    else:
        header = f"{instance_name} speed: {float(stored)}."

    # History is filtered to this instance so the last 3 entries are
    # actually about this voice. A user who's calibrated three instances
    # in succession shouldn't see 3 STAY-C entries when asking about
    # Salem.
    relevant = [
        h for h in history
        if isinstance(h, dict) and _normalize_instance_name(str(h.get("instance") or "")) == key
    ]
    if not relevant:
        return header

    tail = relevant[-HISTORY_TAIL:]
    lines = [header, "Recent history:"]
    for entry in tail:
        value = entry.get("value", "?")
        set_at = entry.get("set_at", "")
        by = entry.get("by", "")
        note = entry.get("note", "")
        suffix = f" — {note}" if note else ""
        lines.append(f"  • {value} ({by}{', ' + set_at if set_at else ''}){suffix}")
    return "\n".join(lines)


__all__ = [
    "SPEED_MIN",
    "SPEED_MAX",
    "SPEED_DEFAULT",
    "HISTORY_TAIL",
    "SpeedValidationError",
    "validate_speed",
    "parse_speed_command",
    "resolve_tts_speed",
    "set_tts_speed",
    "format_report",
]
