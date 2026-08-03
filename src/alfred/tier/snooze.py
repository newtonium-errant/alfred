"""Board snooze — "not on the tier list for a few days".

Operator screenshot 2026-08-03: board rows offer only ✓ DONE, so the only way
to clear a row the operator isn't doing today was to claim it was finished.

THE CONSTRAINT THAT SHAPES EVERYTHING HERE: a snooze must never fake a
completion. It writes **nothing to the vault** — no ``status``, no completion
date, no ``completion_log`` append, no cadence advance, no ``done_at``. It is
recorded in this sidecar store alone, and the projection filters against it.

Why a sidecar rather than per-lane vault stamps (ratified 2026-08-03):

  * "Not on my tier list for a few days" is a statement about the BOARD, not a
    property of the task. The task's due date and status are unchanged, and
    someone reading the task note should not find scheduling noise from a board
    gesture. CLAUDE.md's "vault is the source of truth, state files are
    bookkeeping" cuts that way.
  * The T3 free-text lane has no record to stamp at all, so a frontmatter
    design would need a sidecar anyway — leaving two mechanisms, two failure
    modes, and two places to look when a snooze misbehaves.

Accepted trade: a snoozed task shows nothing in Obsidian. Vault visibility, if
ever wanted, is an additive follow-up rather than a redesign.

**Urgency breaks through — on a DELTA, not an absolute** (operator-ruled). The
baseline is captured at snooze time, and the item returns early only when it
gets MORE urgent than it was when the operator waved it off:

    ==============================  ===========================
    what changed during the snooze  outcome
    ==============================  ===========================
    nothing                         holds
    was not due → now due/overdue   breaks through (crossed_due)
    due date moved EARLIER          breaks through (moved_earlier)
    due date moved LATER            holds
    was ALREADY overdue at snooze   holds for the full duration
    ==============================  ===========================

That last row is the load-bearing one. The motivating card was "T1 RRTS
Invoicing overdue-by-1d" — *already* overdue when the operator wanted it gone.
An absolute "is it overdue?" predicate would make snooze a no-op on exactly
that card: tap 3d, and it returns on the next projection. Snoozing something
overdue means "yes, I know it's late, not today," and that is honoured.

This is the same move as the feed's snapshot fingerprint (``feed/model.py``):
record the state at decision time and act on the DELTA, never on the absolute.

T3 free-text has no due-ness, so its snooze is **absolute by construction** —
there is no delta that could fire, not a special case in the code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Default store path. Instance-scoped per the CLAUDE.md per-instance-defaults
# rule (the 2026-07-31 feed-store incident: a cwd-relative shared default let
# one instance's writer pollute another's store). Operators override via
# ``tier.snooze.path``; every caller threads an explicit path in production.
DEFAULT_SNOOZE_PATH = "./data/board_snooze.salem.json"

# The v1 duration ladder. Operator asked for "a few days"; three fixed buttons
# keep the card's action row from becoming a menu. An explicit until-<date>
# picker is v2.
SNOOZE_DURATIONS: dict[str, int] = {
    "snooze_1d": 1,
    "snooze_3d": 3,
    "snooze_7d": 7,
}

UNSNOOZE_ACTION = "unsnooze"

# Breakthrough reasons — the ``reason`` field on board.snooze_breakthrough.
# Naming WHICH delta fired is the difference between "the card came back early"
# being explicable and being mysterious.
REASON_CROSSED_DUE = "crossed_due"
REASON_MOVED_EARLIER = "moved_earlier"


@dataclass
class SnoozeEntry:
    """One snoozed board row, keyed by its slot stable key.

    ``due_iso_at_snooze`` / ``overdue_at_snooze`` are the URGENCY BASELINE —
    the whole point of the record. Without them "more urgent than when you
    dismissed it" is not expressible and the predicate collapses to the
    absolute reading that breaks the motivating card.
    """

    snoozed_until: str          # YYYY-MM-DD — suppressed while today < this
    snoozed_at: str = ""        # ISO timestamp of the operator action
    lane: str = ""              # task | routine | tier (provenance, for logs)
    duration_label: str = ""    # "1d" / "3d" / "7d"
    due_iso_at_snooze: str = ""  # "" when the item had no due date
    overdue_at_snooze: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnoozeEntry":
        """Schema-tolerant construct (the house load() contract)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def resolve_snooze_path(raw: Any) -> str | None:
    """``tier.snooze.path`` out of a unified config dict, or ``None``.

    THE single parse of this key. The writer (the board action dispatcher) and
    the reader (the projection, via ``tier_defaults.snooze_path``) both come
    through here, so they cannot resolve to different files — a drift between
    them would mean snoozes written where nothing reads them, which is the
    same read/write split that BLOCKed this feature at gate.
    """
    if not isinstance(raw, dict):
        return None
    tier_section = raw.get("tier")
    if not isinstance(tier_section, dict):
        return None
    snooze_section = tier_section.get("snooze")
    if not isinstance(snooze_section, dict):
        return None
    return str(snooze_section.get("path") or "") or None


def slot_stable_key(entry: Any) -> str:
    """Durable identity for a tier-lane entry.

    CANONICAL. ``brief.feed_producer._slot_stable_key`` delegates here so the
    board card's feed id and the snooze store's key can never drift apart — a
    drift would silently snooze nothing (or the wrong row), and that class of
    read/write key mismatch is exactly what cost this round its first day.

    task → path (wikilink target); routine item → (record, text); free-text T3
    → item text. Origin-prefixed so the three spaces can't collide.
    """
    origin = getattr(entry, "origin", "")
    if origin == "routine_item" and getattr(entry, "routine_record", None) and getattr(entry, "item_text", None):
        return f"routine:{entry.routine_record}::{entry.item_text}"
    if origin == "task" and getattr(entry, "path", None):
        return f"task:{entry.path}"
    name = getattr(entry, "name", "") or ""
    return f"text:{name}" if name else ""


def load_snoozes(path: str | Path | None) -> dict[str, SnoozeEntry]:
    """Load the store (empty dict when absent/unreadable/malformed).

    Degrades to "nothing is snoozed" rather than raising — a corrupt snooze
    store must never take the board down. The failure is logged so a board that
    stopped honouring snoozes is diagnosable (ILB).
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(
            "board.snooze.store_read_failed",
            path=str(p), error=str(exc), error_type=type(exc).__name__,
            detail="board renders unsnoozed this pass",
        )
        return {}
    if not isinstance(raw, dict):
        log.warning(
            "board.snooze.store_read_failed",
            path=str(p), error="store is not a JSON object",
            error_type="TypeError",
            detail="board renders unsnoozed this pass",
        )
        return {}
    out: dict[str, SnoozeEntry] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            out[str(key)] = SnoozeEntry.from_dict(value)
        except TypeError:
            continue  # malformed row — skip, don't crash the board
    return out


def save_snoozes(path: str | Path, entries: dict[str, SnoozeEntry]) -> None:
    """Atomically rewrite the store (.tmp → rename, the house pattern)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in entries.items()}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(p)


def add_snooze(
    path: str | Path,
    key: str,
    *,
    days: int,
    today: date,
    lane: str = "",
    due_iso: str = "",
    duration_label: str = "",
) -> SnoozeEntry:
    """Record a snooze. Returns the stored entry.

    ``snoozed_until = today + days``, and suppression runs while
    ``today < snoozed_until`` — so 3d on the 3rd hides the 3rd, 4th and 5th and
    the row is back on the 6th. "A few days" means a few days of not seeing it.
    """
    entries = load_snoozes(path)
    entry = SnoozeEntry(
        snoozed_until=(today + timedelta(days=days)).isoformat(),
        snoozed_at=datetime.now(timezone.utc).isoformat(),
        lane=lane,
        duration_label=duration_label or f"{days}d",
        due_iso_at_snooze=due_iso or "",
        overdue_at_snooze=bool(due_iso) and due_iso <= today.isoformat(),
    )
    entries[key] = entry
    save_snoozes(path, entries)
    return entry


def remove_snooze(path: str | Path, key: str) -> bool:
    """Drop a snooze (the ``unsnooze`` verb). True when one was removed."""
    entries = load_snoozes(path)
    if key not in entries:
        return False
    del entries[key]
    save_snoozes(path, entries)
    return True


def breakthrough_reason(
    entry: SnoozeEntry, *, current_due_iso: str, today: date,
) -> str | None:
    """Which urgency DELTA fired, or ``None`` when the snooze holds.

    See the module docstring's table. Order matters only for reporting: a row
    that both crossed into due AND moved earlier reports ``crossed_due``, the
    more meaningful of the two.
    """
    current = (current_due_iso or "").strip()
    if not current:
        return None  # no due-ness (T3) → absolute by construction
    today_iso = today.isoformat()
    now_overdue = current <= today_iso
    if now_overdue and not entry.overdue_at_snooze:
        return REASON_CROSSED_DUE
    baseline = (entry.due_iso_at_snooze or "").strip()
    if baseline and current < baseline:
        return REASON_MOVED_EARLIER
    return None


def is_snoozed(
    entry: SnoozeEntry, *, today: date, current_due_iso: str = "",
) -> tuple[bool, str | None]:
    """``(suppressed, breakthrough_reason)`` for one stored snooze.

    Expiry is checked FIRST: an expired snooze is simply over, and reporting a
    breakthrough on it would log a reason for a return that the clock caused.
    """
    if today.isoformat() >= entry.snoozed_until:
        return False, None
    reason = breakthrough_reason(entry, current_due_iso=current_due_iso, today=today)
    if reason is not None:
        return False, reason
    return True, None


@dataclass
class SnoozeFilterStats:
    """What the projection hid, and what broke through."""

    suppressed: int = 0
    broke_through: list[tuple[str, str]] = field(default_factory=list)


def filter_snoozed_entries(
    entries: list[Any],
    snoozes: dict[str, SnoozeEntry],
    *,
    today: date,
) -> tuple[list[Any], SnoozeFilterStats]:
    """Drop snoozed rows from one tier lane.

    Applied in ``compute_today_view`` — the single projection the board AND the
    brief's tier section both read — so the two can never disagree about what
    is on today's list. Filtering in the feed producer instead would hide a row
    from the board while the brief kept rendering it, which is the divergence
    the feed layer was built to close.
    """
    stats = SnoozeFilterStats()
    if not snoozes:
        return list(entries), stats
    kept: list[Any] = []
    for entry in entries:
        key = slot_stable_key(entry)
        stored = snoozes.get(key) if key else None
        if stored is None:
            kept.append(entry)
            continue
        suppressed, reason = is_snoozed(
            stored, today=today,
            current_due_iso=str(getattr(entry, "due_iso", "") or ""),
        )
        if suppressed:
            stats.suppressed += 1
            continue
        if reason is not None:
            stats.broke_through.append((key, reason))
        kept.append(entry)
    return kept, stats


__all__ = [
    "DEFAULT_SNOOZE_PATH",
    "REASON_CROSSED_DUE",
    "REASON_MOVED_EARLIER",
    "SNOOZE_DURATIONS",
    "UNSNOOZE_ACTION",
    "SnoozeEntry",
    "SnoozeFilterStats",
    "add_snooze",
    "breakthrough_reason",
    "filter_snoozed_entries",
    "is_snoozed",
    "load_snoozes",
    "remove_snooze",
    "resolve_snooze_path",
    "save_snoozes",
    "slot_stable_key",
]
