"""Per-tier confidence flag persistence.

The confidence flags live in a tiny JSON state file at
``daily_sync.state.path``. They start at the seed values from
``config.daily_sync.confidence`` (default: all False) and get flipped by
``/calibration_ok <tier>`` Telegram replies.

c3/c4/c5 (the surfacing layers) read these flags to gate per-tier
surfacing on Andrew's explicit approval. Today nothing reads them;
that's the point — we want the flags to exist, persist across daemon
restarts, and have a CLI to flip them, all before any consumer is built.

State file shape::

    {
      "confidence": {"high": true, "medium": false, "low": false, "spam": true},
      "last_batch": {"date": "2026-04-22", "items": [...], "message_ids": [...]},
      "last_fired_date": "2026-04-22",
      "last_error": {"ts": "2026-05-14T09:00:00+00:00", "message": "..."} | None
    }

``last_error`` mirrors the brief.state pattern from 2026-05-14 and
its janitor/distiller siblings (commits 66a6344 and 13529c5). The
daemon's outer ``except Exception:`` at daemon.py:378 calls
:func:`record_error_on_state` to capture the failure cause; the BIT
``last-successful-fire`` probe surfaces it on its WARN/FAIL detail.
Cleared by :func:`clear_last_error_on_state` on each successful
fire (the ``state["last_fired_date"] = today_iso`` save point).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .config import ConfidenceConfig

log = structlog.get_logger(__name__)

# The valid confidence-flag keys. The four priority TIERS plus, since #7 7c-i,
# the ``filing`` axis gate (NOT a priority tier — the topical-filing gate whose
# consumer is the 7c-ii Gmail label write). Additive; the four tiers are
# unperturbed. ``/calibration_ok filing`` flips it via ``set_confidence``.
_VALID_TIERS = ("high", "medium", "low", "spam", "filing")


def load_state(state_path: str | Path) -> dict[str, Any]:
    """Load the Daily Sync state file. Returns ``{}`` when absent.

    Tolerant of malformed JSON: a corrupt file falls back to an empty
    dict so the daemon keeps running (the next save will overwrite the
    bad file with a clean one).
    """
    path = Path(state_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_path: str | Path, state: dict[str, Any]) -> None:
    """Atomically persist the state dict to JSON.

    Writes to a temp file in the same directory then renames into place
    so a crash mid-write doesn't leave a half-baked file.
    """
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def list_confidence(
    state_path: str | Path,
    seed: ConfidenceConfig,
) -> dict[str, bool]:
    """Return the current per-tier flag map.

    Falls back to ``seed`` (config defaults) for any tier missing from
    the state file. Always returns a dict with all four tier keys so
    callers can render a stable status block.
    """
    state = load_state(state_path)
    persisted = state.get("confidence", {}) or {}
    if not isinstance(persisted, dict):
        persisted = {}
    return {
        "high": bool(persisted.get("high", seed.high)),
        "medium": bool(persisted.get("medium", seed.medium)),
        "low": bool(persisted.get("low", seed.low)),
        "spam": bool(persisted.get("spam", seed.spam)),
        # #7 7c-i — the topical-filing-axis gate (built-before-consumer; 7c-ii reads it).
        "filing": bool(persisted.get("filing", seed.filing)),
    }


def set_confidence(
    state_path: str | Path,
    tier: str,
    value: bool,
    *,
    seed: ConfidenceConfig,
) -> dict[str, bool]:
    """Flip one tier's confidence flag and persist. Returns the full new map.

    Raises :class:`ValueError` for unknown tiers — the slash-command
    handler converts this to a user-friendly Telegram reply.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {', '.join(_VALID_TIERS)}"
        )
    state = load_state(state_path)
    flags = list_confidence(state_path, seed)
    flags[tier] = bool(value)
    state["confidence"] = flags
    save_state(state_path, state)
    return flags


def format_confidence_report(flags: dict[str, bool]) -> str:
    """Render the per-tier flag map as a human-readable Telegram reply."""
    rows = []
    for tier in _VALID_TIERS:
        check = "✅" if flags.get(tier, False) else "⏳"
        rows.append(f"  {check} {tier}")
    return "Per-tier surfacing confidence:\n" + "\n".join(rows)


def record_error_on_state(
    state_path: str | Path,
    message: str,
) -> None:
    """Capture a daemon-level failure into ``state['last_error']`` and persist.

    Called from the daily_sync daemon's outer ``except Exception:`` at
    daemon.py:378 so the BIT ``last-successful-fire`` probe can
    surface the failure cause (e.g. ``KeyError: 'foo'``) on the BIT
    line rather than forcing the operator to grep
    ``data/daily_sync.log``.

    daily_sync's state is dict-shaped (not a dataclass like
    brief/janitor/distiller StateManager); this helper inlines the
    load → mutate → save round-trip so the daemon call site stays a
    one-liner. Behaviour mirrors brief.StateManager.record_error:
    persists the {ts, message} dict, defensive on save-failure
    (logs warning, doesn't crash). Added 2026-05-14 per
    ``project_cross_daemon_swallow_audit.md``.
    """
    state = load_state(state_path)
    state["last_error"] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }
    try:
        save_state(state_path, state)
    except OSError as e:
        log.warning("daily_sync.state.record_error_save_failed", error=str(e))


def clear_last_error_on_state(state: dict[str, Any]) -> None:
    """Wipe ``state['last_error']`` in-place.

    Called by the daemon's successful-fire path BEFORE the existing
    ``save_state`` (the ``state['last_fired_date'] = today_iso`` save
    point). Mirrors the brief.State.add_run(success=True)
    clear-on-success semantics — reaching this call site means the
    fire completed without raising, so wipe any stale failure context
    the probe would otherwise trail on the BIT line.

    No-op when ``last_error`` is already absent / None — the happy
    path stays clean.

    Mutates the dict in place rather than returning a new one because
    daily_sync's existing daemon code uses a single ``state`` dict
    through its fire path and saves it as a unit; making this a
    return-value would force a call-site rewrite for no benefit.
    """
    if "last_error" in state:
        state["last_error"] = None
