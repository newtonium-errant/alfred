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
      "last_fired_date": "2026-04-22"
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import ConfidenceConfig

_VALID_TIERS = ("high", "medium", "low", "spam")


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
