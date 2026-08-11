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

**One defer verb (#14).** The board used to offer two ways to push something
away: Park (indefinite, session-only, invented on the deck) and Snooze (dated,
stored here). Two verbs for one intention is a choice the operator has to make
before the one they actually care about, so Park was folded in as the ladder's
fourth rung — ``snooze_until_i_say``, an entry with no end date. Nothing about
the breakthrough rules changes for it: the delta reads the same, because what
earns a row its way back is urgency, not the calendar.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Default store path — a DIRECT-CONSTRUCTION placeholder with no instance
# segment (#84).
#
# The previous value was ``board_snooze.salem.json``, and the comment above it
# claimed the path was "instance-scoped per the CLAUDE.md per-instance-defaults
# rule". It was instance-NAMED, which is the opposite: a shared code path
# carrying one instance's name means every other instance writes into a file
# labelled Salem. A comment asserting the property it violates is worse than no
# comment, because it stops the next reader looking.
#
# Production threads an explicit path (``tier.snooze.path`` via
# :func:`resolve_snooze_path`, the single parse both the writer and the reader
# come through). :func:`default_snooze_path` is the instance-derived value for
# a caller that needs one — byte-identical to the old literal for Salem.
DEFAULT_SNOOZE_PATH = "./data/board_snooze.json"

# The duration ladder. Three dated rungs plus the INDEFINITE one — ``None`` days,
# meaning "until I say". That fourth rung is the old board **Park** verb: #14
# unified the two defer gestures into this one ladder, and Park's indefinite
# semantics survive here as a duration choice rather than as a second verb.
#
# ``None`` is the ONLY non-int value and it means exactly one thing: no end date
# is recorded. Read :data:`SNOOZE_INDEFINITE_ACTION` below for why that is an
# absence rather than a far-future date.
SNOOZE_DURATIONS: dict[str, int | None] = {
    "snooze_1d": 1,
    "snooze_3d": 3,
    "snooze_7d": 7,
    "snooze_until_i_say": None,
}

# "Until I say" (#14). Stored as an entry with NO ``snoozed_until`` AT ALL —
# not a null sentinel, and emphatically not a 9999-day date.
#
# A far-future date would make the code *look* uniform (one comparison path) at
# the cost of making it lie: the store would assert an end date the operator
# never chose, and every reader — a log line, a future "snoozed until…" render,
# an operator with a text editor — would repeat that lie back. The absence is
# honest and it is also SAFER, because a reader that forgets to handle it gets
# an empty string rather than a plausible wrong date it will happily render.
SNOOZE_INDEFINITE_ACTION = "snooze_until_i_say"

# The stored ``duration_label`` for the indefinite rung. Operator's own words —
# this is what "what did I snooze and for how long?" answers with.
INDEFINITE_LABEL = "until I say"

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

    ``snoozed_until`` DEFAULTS TO EMPTY, and that default is load-bearing rather
    than tidiness: an "until I say" entry is written with the key absent, so a
    required field here would make :meth:`from_dict` raise ``TypeError`` — which
    :func:`load_snoozes` catches and turns into a SKIPPED ROW. Every indefinite
    snooze would silently un-snooze itself on the next board render, and the
    only symptom would be rows quietly coming back. The default is what keeps
    the absence readable instead of fatal.
    """

    snoozed_until: str = ""     # YYYY-MM-DD, or "" = indefinite ("until I say")
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


def default_snooze_path(raw: Any) -> str:
    """The instance-derived snooze store path for a unified config (#84).

    ``<data dir>/board_snooze.<slug>.json``. For Salem this is
    ``./data/board_snooze.salem.json`` — byte-identical to the literal this
    replaced, so an instance that adopts it inherits its existing store
    rather than starting empty.

    NOT a fallback inside :func:`resolve_snooze_path`: that function returning
    ``None`` for an unconfigured instance is load-bearing (callers treat the
    empty as "snoozing not configured"), and quietly manufacturing a path
    would turn an off feature on. This is for a caller that wants the
    conventional location by name.
    """
    from alfred.common.instance_paths import (
        instance_data_path,
        instance_state_filename_or_unscoped,
    )

    instance = ""
    if isinstance(raw, dict):
        telegram = raw.get("telegram")
        if isinstance(telegram, dict):
            block = telegram.get("instance")
            if isinstance(block, dict):
                instance = str(block.get("name") or "")
    return instance_data_path(
        raw if isinstance(raw, dict) else {},
        instance_state_filename_or_unscoped(
            "board_snooze", instance, suffix="json"),
    )


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


def _serialize(entry: SnoozeEntry) -> dict[str, Any]:
    """One stored row. An INDEFINITE snooze omits ``snoozed_until`` entirely.

    Deliberately narrow — this drops exactly one key, and only when it is empty.
    A general "drop every falsy field" would also swallow ``overdue_at_snooze:
    False``, which is a real, load-bearing answer (it is the baseline that makes
    the already-overdue row of the breakthrough table hold), not an absence.
    """
    payload = asdict(entry)
    if not payload.get("snoozed_until"):
        payload.pop("snoozed_until", None)
    return payload


def save_snoozes(path: str | Path, entries: dict[str, SnoozeEntry]) -> None:
    """Atomically rewrite the store (.tmp → rename, the house pattern)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: _serialize(v) for k, v in entries.items()}
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
    days: int | None,
    today: date,
    lane: str = "",
    due_iso: str = "",
    duration_label: str = "",
) -> SnoozeEntry:
    """Record a snooze. Returns the stored entry.

    ``snoozed_until = today + days``, and suppression runs while
    ``today < snoozed_until`` — so 3d on the 3rd hides the 3rd, 4th and 5th and
    the row is back on the 6th. "A few days" means a few days of not seeing it.

    ``days=None`` is the indefinite rung (#14, the old Park): no end date is
    recorded, and the row stays off the board until an explicit ``unsnooze`` —
    or until the urgency delta breaks it through, which fires on an undated
    entry exactly as it does on a dated one (urgency is what earns a return,
    never the calendar).

    The label is derived HERE rather than at the call site so the store's
    vocabulary has one author; a caller may still override it.
    """
    entries = load_snoozes(path)
    entry = SnoozeEntry(
        snoozed_until=(
            "" if days is None else (today + timedelta(days=days)).isoformat()
        ),
        snoozed_at=datetime.now(timezone.utc).isoformat(),
        lane=lane,
        duration_label=(
            duration_label or (INDEFINITE_LABEL if days is None else f"{days}d")
        ),
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

    An INDEFINITE entry (no ``snoozed_until``) has no expiry to check, so it
    falls straight through to the delta — which is the whole point of #14's
    ruling that breakthrough applies identically to dated and undated snoozes.
    "Until I say" suspends the CLOCK, not the operator's own urgency: an item
    that becomes genuinely more pressing than it was still comes back.
    """
    if entry.snoozed_until and today.isoformat() >= entry.snoozed_until:
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
    "default_snooze_path",
    "INDEFINITE_LABEL",
    "REASON_CROSSED_DUE",
    "REASON_MOVED_EARLIER",
    "SNOOZE_DURATIONS",
    "SNOOZE_INDEFINITE_ACTION",
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
