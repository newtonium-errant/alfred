"""Due-date resolution for routine items with recurring deadlines (compat shim).

Routine consolidation Step 3 (2026-06-28): the recurrence + behind/due logic
moved into the unified :mod:`alfred.routine.recurrence` grammar. This module
is now a THIN COMPAT SHIM so existing callers (``aggregator.py``,
``tier/compute.py``, ``test_due``) keep importing the same four functions with
the same signatures, on :class:`alfred.routine.config.DuePattern` instances,
and the historical error policy is preserved exactly:

  * :func:`resolve_due_date` — next upcoming due date (>= today), or ``None``
    for a malformed pattern (catching :class:`CadenceError` and emitting the
    ``routine.due.malformed`` log, as before — the operator-facing entry point
    owns the malformed signal).
  * :func:`is_done_in_current_cycle` / :func:`completion_satisfies_current_cycle`
    / :func:`overdue_effective_due` — the behind/due substrate predicates,
    delegating to the unified helpers (which resolve via the catching path, so
    a malformed pattern yields the not-resolvable answer, NOT a raise — these
    stay silent to keep the tier-compute no-logs invariant).

The ``due_pattern:`` item-level frontmatter key + ``DuePattern`` dataclass are
unchanged; ``DuePattern`` → ``Recurrence`` conversion reuses
``Recurrence.from_dict`` (one normalizer: weekly singular ``day`` → ``days``,
``soft`` → ``weekly_soft``, biweekly anchor-weekday validation stays at
resolve so a future mismatch still drops to ``None`` + warns).
"""

from __future__ import annotations

from datetime import date

import structlog

from .config import DuePattern
from .recurrence import (
    CadenceError,
    Recurrence,
)
from .recurrence import (
    completion_satisfies_current_cycle as _rec_completion_satisfies,
)
from .recurrence import (
    is_done_in_current_cycle as _rec_is_done_in_current_cycle,
)
from .recurrence import next_due_on_or_after as _rec_next_due
from .recurrence import overdue_effective_due as _rec_overdue_effective_due

log = structlog.get_logger(__name__)


def _to_recurrence(due_pattern: DuePattern | None) -> Recurrence | None:
    """Convert a :class:`DuePattern` to the unified :class:`Recurrence`.

    Reuses ``Recurrence.from_dict`` so the singular-weekly-``day`` →
    ``days`` fold, the ``soft`` → ``weekly_soft`` fold, and the type gate are
    all applied in ONE place. Returns ``None`` for ``None`` input (every
    ``DUE_PATTERN_TYPES`` value is a known recurrence type, so a real
    DuePattern always converts).
    """
    if due_pattern is None:
        return None
    return Recurrence.from_dict({
        "type": due_pattern.type,
        "day": due_pattern.day,
        "anchor": due_pattern.anchor,
        "n": due_pattern.n,
        "weekday": due_pattern.weekday,
        "soft": due_pattern.soft,
    })


def resolve_due_date(due_pattern: DuePattern, today: date) -> date | None:
    """Return the next upcoming due date for the pattern (>= today).

    Returns ``None`` for malformed patterns, emitting a
    ``routine.due.malformed`` log on the None path so operators see which
    item dropped + why — the historical error policy, preserved.
    """
    if due_pattern is None:
        return None
    rec = _to_recurrence(due_pattern)
    if rec is None:
        return None
    try:
        return _rec_next_due(rec, today)
    except CadenceError as exc:
        log.warning(
            "routine.due.malformed",
            type=due_pattern.type,
            error=str(exc),
            detail=(
                "malformed due_pattern — item will not auto-surface "
                "in tier; check operator YAML for missing or invalid "
                "auxiliary fields."
            ),
        )
        return None
    except (TypeError, ValueError) as exc:
        log.warning(
            "routine.due.malformed",
            type=due_pattern.type,
            error=str(exc),
        )
        return None


def is_done_in_current_cycle(
    due_pattern: DuePattern,
    completion_dates: list[date],
    today: date,
) -> bool:
    """True iff any completion lands inside the current cycle window.

    Compat shim over the unified helper. Window semantics per pattern type
    (ISO week / 14-day / calendar month / N-day) are unchanged.
    """
    return _rec_is_done_in_current_cycle(
        _to_recurrence(due_pattern), completion_dates, today,
    )


def completion_satisfies_current_cycle(
    item_text: str,
    completion_log: dict | None,
    due_pattern: DuePattern | None,
    today: date,
) -> bool:
    """True if a completion covers the current/upcoming cycle (nearest-cycle
    ±half-cycle heuristic). Compat shim over the unified helper."""
    return _rec_completion_satisfies(
        item_text, completion_log, _to_recurrence(due_pattern), today,
    )


def overdue_effective_due(
    due_pattern: DuePattern | None,
    completion_log: dict | None,
    item_text: str,
    today: date,
) -> date | None:
    """Effective due that admits overdue retention. Compat shim over the
    unified helper (prev_due on a recently-lapsed unsatisfied cycle, else
    current_due)."""
    return _rec_overdue_effective_due(
        _to_recurrence(due_pattern), completion_log, item_text, today,
    )


__all__ = [
    "completion_satisfies_current_cycle",
    "is_done_in_current_cycle",
    "overdue_effective_due",
    "resolve_due_date",
]
