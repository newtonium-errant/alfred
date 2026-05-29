"""Routine daemon configuration.

Mirrors ``brief/config.py`` / ``bit/config.py`` — typed dataclasses,
``load_from_unified`` builder, hand-rolled construction (we avoid the
generic ``_build`` helper to sidestep the ``_DATACLASS_MAP`` collision
+ empty-dict traps per project CLAUDE.md).

Routine record schema (2026-05-29 Phase 2A Ship A):

  * Item dataclass surfaces the item-level fields routine records
    carry under ``items:`` — text, priority, time, warn_after_gap_days,
    plus the deadline-bearing extension fields ``due_pattern``,
    ``surface_at_days``, ``escalate_at_days``.

  * DuePattern dataclass describes the recurrence shape for items
    that have a recurring deadline (e.g. monthly clinic rent, weekly
    garbage day). Six pattern types (``weekly``, ``biweekly``,
    ``monthly``, ``every_n_days``, ``monthly_nth_weekday``,
    ``weekly_soft``) mirror the cadence dispatcher's vocabulary but
    operate at the per-ITEM layer (each routine item can have its
    own deadline; the routine itself fires per its top-level cadence).

These dataclasses do NOT replace the aggregator's existing dict-based
``raw_items`` parse — the aggregator continues to read items as dicts
for backward compatibility. The dataclasses are the canonical typed
surface for tier compute (``compute_auto_routine_candidates`` +
``compute_auto_routine_t2_candidates``) and Ship B's brief render
layer.

T1 / T2 window semantics (operator-stated, Plan-ratified):

  * ``escalate_at_days`` absent → item never auto-surfaces in tier
    (the Walk-Fergus daily-routine shape — no deadline, just
    surface-by-cadence in the brief's routines section).
  * ``escalate_at_days`` PRESENT + ``surface_at_days`` absent or
    ``<= escalate_at_days`` → T1-only window (the Garbage-Day shape:
    ``escalate_at_days: 1`` means T1 on the day before due).
  * ``surface_at_days > escalate_at_days`` → T2 ramp + T1 escalation
    (the Pay-Clinic-Rental shape: ``surface_at_days: 5`` +
    ``escalate_at_days: 0`` means T2 appears 5 days out, then T1
    on the due day itself).

  T1 window: ``[0, escalate_at_days]`` (days_to_due in this inclusive range)
  T2 window: ``(escalate_at_days, surface_at_days]`` (strictly above
             escalate, inclusive of surface)

  ``escalate_at_days: 0`` is a load-bearing edge case (T1 fires only
  on the due date itself, e.g. clinic rent on the 1st). T2 in that
  case covers days 1..surface_at_days inclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred.common.schedule import ScheduleConfig


DEFAULT_TIMEZONE = "America/Halifax"
# Fires one minute before the brief (06:00 default) so the brief at
# 06:00 reads the freshly-written daily aggregator note. Mirrors the
# BIT default-lead pattern (BIT at 05:55, routine at 05:59).
DEFAULT_TIME = "05:59"
# Per ``feedback_hardcoding_and_alfred_naming.md``: the salem-only
# guarantee is an instance check, not a literal — the daemon-start
# guard reads ``config.telegram.instance.name`` and refuses to start
# on any other instance. ``REQUIRED_INSTANCE`` is the canonical
# normalised form (``_normalize_instance_name`` output) that passes
# the guard.
REQUIRED_INSTANCE = "salem"


# Canonical due_pattern.type values — Ship D SKILL will quote these
# verbatim so the talker recognises operator phrasing. A rename here
# = update SKILL in lockstep. Each value's semantics + required
# auxiliary fields are documented on DuePattern.from_dict.
DUE_PATTERN_TYPES: frozenset[str] = frozenset({
    "weekly",
    "biweekly",
    "monthly",
    "every_n_days",
    "monthly_nth_weekday",
    "weekly_soft",
})


@dataclass
class DuePattern:
    """Recurring-deadline pattern for a routine item.

    Schema discriminator: ``type`` — one of :data:`DUE_PATTERN_TYPES`.
    Auxiliary fields per type:

      * ``weekly``           — ``day`` (weekday name, e.g. ``"thu"``)
      * ``biweekly``         — ``day`` + ``anchor`` (ISO date of a
                               reference week's matching weekday;
                               the cycle alternates every 14 days)
      * ``monthly``          — ``day`` (1-31 or ``"last"``)
      * ``every_n_days``     — ``n`` (positive int) + ``anchor``
                               (ISO date the cycle counts from)
      * ``monthly_nth_weekday`` — ``n`` (1, 2, 3, 4 or -1 for "last")
                               + ``weekday`` (weekday name)
      * ``weekly_soft``      — no auxiliary fields needed; the
                               "due" date is the end of the current
                               ISO week (Sunday)

    All auxiliary fields default to ``None``; per-type validation
    happens in :mod:`alfred.routine.due` where the pattern is
    resolved to a concrete next-due date.

    ``soft`` is a duplicate signal to ``type == "weekly_soft"`` —
    pre-Phase-2A operator YAML may carry ``soft: true`` as an
    annotation on ``type: weekly``. Treated as equivalent at
    resolution time; new YAML should prefer ``type: weekly_soft``.
    """

    type: str
    day: str | int | None = None
    anchor: str | None = None
    n: int | None = None
    weekday: str | None = None
    soft: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> DuePattern | None:
        """Parse a YAML-loaded dict into a DuePattern.

        Returns ``None`` when:
          * ``data`` is not a dict (defensive against operator
            hand-edit corruption — e.g. ``due_pattern: weekly``
            instead of ``due_pattern: {type: weekly, day: thu}``).
          * ``type`` is missing or not in :data:`DUE_PATTERN_TYPES`.

        Per the schema-tolerance contract (CLAUDE.md load() rule):
        unknown auxiliary fields are silently dropped. Tested at
        the dataclass-construction boundary so a future schema
        addition (e.g. ``year`` for annual deadlines) doesn't
        break existing operator YAML.
        """
        if not isinstance(data, dict):
            return None
        type_raw = data.get("type")
        if not isinstance(type_raw, str) or type_raw not in DUE_PATTERN_TYPES:
            return None
        # ``day`` may be a string (weekday name, "last") OR int 1-31.
        day = data.get("day")
        anchor = data.get("anchor")
        n = data.get("n")
        weekday = data.get("weekday")
        soft_raw = data.get("soft")
        return cls(
            type=type_raw,
            day=day if isinstance(day, (str, int)) else None,
            anchor=str(anchor) if anchor is not None else None,
            n=int(n) if isinstance(n, int) else None,
            weekday=str(weekday) if isinstance(weekday, str) else None,
            soft=bool(soft_raw) if soft_raw is not None else False,
        )


@dataclass
class Item:
    """One routine item — the per-list-entry shape carried under
    ``items:`` in a routine record's frontmatter.

    Existing fields (Phase 1):
      * ``text`` — operator-facing line (e.g. ``"Walk Fergus"``)
      * ``priority`` — ``"critical"`` / ``"tracked"`` / ``"aspirational"``
      * ``time`` — optional HH:MM string for critical items
      * ``warn_after_gap_days`` — tracked-item gap threshold

    Phase 2A extension (deadline-bearing items):
      * ``due_pattern`` — recurring-deadline shape (see :class:`DuePattern`)
      * ``surface_at_days`` — T2 ramp threshold (days before due when
        the item starts surfacing as a T2 candidate)
      * ``escalate_at_days`` — T1 escalation threshold (days before
        due when the item moves to T1)

    See module docstring for the T1/T2 window math + the three
    operator-stated semantics combinations.
    """

    text: str
    priority: str
    time: str | None = None
    warn_after_gap_days: int | None = None
    due_pattern: DuePattern | None = None
    surface_at_days: int | None = None
    escalate_at_days: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Item | None:
        """Parse a YAML-loaded dict into an Item.

        Returns ``None`` when:
          * ``data`` is not a dict.
          * ``text`` is missing or empty (an item without text
            can't be rendered or completion-tracked).

        Per the schema-tolerance contract: unknown frontmatter fields
        are silently dropped. ``priority`` defaults to ``"tracked"``
        when absent (matches the aggregator's existing fallback at
        ``raw_item.get("priority") or "tracked"``).

        ``due_pattern`` parses defensively — a malformed pattern
        becomes ``None`` rather than raising, so a single bad item
        doesn't taint the whole routine record's parse. Per
        ``feedback_intentionally_left_blank.md`` the consumer
        emits a structured log on the drop.
        """
        if not isinstance(data, dict):
            return None
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        priority = str(data.get("priority") or "tracked").lower()
        time_raw = data.get("time")
        time = (
            str(time_raw).strip() if isinstance(time_raw, str) and time_raw.strip()
            else None
        )
        warn_raw = data.get("warn_after_gap_days")
        try:
            warn = int(warn_raw) if warn_raw is not None else None
        except (TypeError, ValueError):
            warn = None
        due_pattern = DuePattern.from_dict(data.get("due_pattern"))
        surface_raw = data.get("surface_at_days")
        try:
            surface_at_days = (
                int(surface_raw) if surface_raw is not None else None
            )
        except (TypeError, ValueError):
            surface_at_days = None
        escalate_raw = data.get("escalate_at_days")
        try:
            escalate_at_days = (
                int(escalate_raw) if escalate_raw is not None else None
            )
        except (TypeError, ValueError):
            escalate_at_days = None
        return cls(
            text=text.strip(),
            priority=priority,
            time=time,
            warn_after_gap_days=warn,
            due_pattern=due_pattern,
            surface_at_days=surface_at_days,
            escalate_at_days=escalate_at_days,
        )


@dataclass
class OutputConfig:
    """Where the aggregator writes the daily summary note.

    The default ``daily/`` directory is the operator-facing landing
    page for today's routines + (eventually) other day-scoped content.
    Janitor should NOT scan this directory (the file is derivative);
    operator config wires ``vault.dont_scan_dirs`` accordingly.
    """

    directory: str = "daily"
    name_template: str = "{date}"


@dataclass
class StateConfig:
    """State file path — tracks per-day write history for status output."""

    path: str = "./data/routine_state.json"
    max_history: int = 30


@dataclass
class RoutineConfig:
    """Top-level routine daemon config.

    ``vault_path`` and ``log_file`` are populated from the unified
    top-level ``vault.path`` / ``logging.dir`` blocks; everything else
    has dataclass defaults so an empty ``routine: {}`` block in
    config.yaml works.

    ``instance_name`` carries the normalised peer-key form of
    ``telegram.instance.name`` — pre-resolved here so the daemon-start
    guard doesn't have to re-import the telegram-compat helper.
    """

    vault_path: str = ""
    enabled: bool = True
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    log_file: str = "./data/routine.log"
    instance_name: str = ""


def load_from_unified(raw: dict[str, Any]) -> RoutineConfig:
    """Build ``RoutineConfig`` from the unified config dict."""
    section = raw.get("routine", {}) or {}
    vault_path = (raw.get("vault") or {}).get("path", "./vault")
    log_dir = (raw.get("logging") or {}).get("dir", "./data")

    schedule_raw = section.get("schedule", {}) or {}
    schedule = ScheduleConfig(
        time=schedule_raw.get("time", DEFAULT_TIME),
        timezone=schedule_raw.get("timezone", DEFAULT_TIMEZONE),
    )

    output_raw = section.get("output", {}) or {}
    output = OutputConfig(
        directory=output_raw.get("directory", "daily"),
        name_template=output_raw.get("name_template", "{date}"),
    )

    state_raw = section.get("state", {}) or {}
    state = StateConfig(
        path=state_raw.get("path", f"{log_dir}/routine_state.json"),
        max_history=int(state_raw.get("max_history", 30)),
    )

    # Resolve instance name via the canonical normaliser. Empty when
    # the operator omitted ``telegram.instance.name`` from config —
    # the daemon-start guard refuses to start in that case (rather
    # than silently treating an unset instance as Salem). Per
    # ``feedback_hardcoding_and_alfred_naming.md`` we fail-loud on
    # missing instance name.
    from alfred.telegram._compat import _normalize_instance_name
    telegram_raw = raw.get("telegram") or {}
    instance_raw = telegram_raw.get("instance") or {}
    raw_name = ""
    if isinstance(instance_raw, dict):
        raw_name = str(instance_raw.get("name") or "")
    instance_name = _normalize_instance_name(raw_name)

    return RoutineConfig(
        vault_path=vault_path,
        enabled=bool(section.get("enabled", True)),
        schedule=schedule,
        output=output,
        state=state,
        log_file=f"{log_dir}/routine.log",
        instance_name=instance_name,
    )


__all__ = [
    "DEFAULT_TIME",
    "DEFAULT_TIMEZONE",
    "DUE_PATTERN_TYPES",
    "DuePattern",
    "Item",
    "OutputConfig",
    "REQUIRED_INSTANCE",
    "RoutineConfig",
    "StateConfig",
    "load_from_unified",
]
