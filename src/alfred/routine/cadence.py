"""Cadence dispatcher — "is the routine due today?" (compat shim).

Routine consolidation Step 3 (2026-06-28): the recurrence logic moved into
the unified :mod:`alfred.routine.recurrence` grammar (one shape, one kernel,
two queries). This module is now a THIN COMPAT SHIM so existing callers
(``aggregator.py``'s routine-level cadence gate + ``test_cadence``) keep
importing ``is_due`` / ``CadenceError`` unchanged.

``is_due`` is :func:`alfred.routine.recurrence.fires_on` — same six shapes
(daily, weekly, every_n_days, monthly, monthly nth-weekday, every_n_months),
same dict input, same raise-on-malformed policy (the aggregator catches
``CadenceError`` + logs ``routine.aggregator.malformed_cadence`` + skips one
bad routine). The frontmatter ``cadence:`` key is unchanged.

The shared date-math kernel (``_weekday_index`` / ``_parse_anchor`` /
``_last_day_of_month`` / ``_nth_weekday_of_month``) also lives in
``recurrence`` now and is re-exported here for any historical importer.
"""

from __future__ import annotations

from datetime import date

from .recurrence import (
    CadenceError,
    _last_day_of_month,
    _nth_weekday_of_month,
    _parse_anchor,
    _weekday_index,
    fires_on,
)


def is_due(cadence: object, today: date) -> bool:
    """Return True iff the cadence fires on ``today``.

    Compat shim over :func:`alfred.routine.recurrence.fires_on`. Accepts the
    routine-level ``cadence`` dict; raises :class:`CadenceError` on a
    malformed or unknown shape.
    """
    return fires_on(cadence, today)


__all__ = [
    "CadenceError",
    "is_due",
    "_last_day_of_month",
    "_nth_weekday_of_month",
    "_parse_anchor",
    "_weekday_index",
]
