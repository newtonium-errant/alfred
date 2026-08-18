"""One-shot repair of the feed store's VERBLESS-ACTED history.

Items acted before the verb was recorded fold to ``state=acted`` carrying
``acted_action=None`` — the store knows the operator judged the card but not
HOW. This module re-derives the missing verb from the daemon logs and appends
the verb-bearing state events that let the fold answer the question.

WHY THE VERBS ARE RE-DERIVED AT APPLY TIME AND NEVER TRANSCRIBED
----------------------------------------------------------------
A census produced a candidate list, and its FIRST proposal was withdrawn after
it turned out to propose undoing the operator's real verdicts. A transcribed
list cannot be re-checked at the moment it is applied: it is a claim about a
store that has moved on since. So the plan is rebuilt from the logs on every
run, against the store as it is right now, and the census numbers are advisory
only — they are a cross-check on the plan, never its source.

THE JOIN, AND THE PRECISION ASSERT THAT MAKES IT SAFE
-----------------------------------------------------
Each candidate is matched to its own act lines BY ID, then to the one act line
nearest the item's FINAL ``acted_at``. Matching by id is exact; the window and
the delta assert exist for the case that actually bites — an id with SEVERAL
act lines (an ``accept`` at T1, a ``done`` at T2). There the join is choosing
between verbs, and choosing the wrong one writes a FALSE VERDICT into the
operator's history: precisely the failure class this repair exists to fix.

So the search window is generous (:data:`JOIN_WINDOW_SECONDS`) but the accepted
match is not: every matched delta must be sub-second
(:data:`MAX_JOIN_DELTA_SECONDS`), and a single match outside that refuses the
WHOLE plan rather than writing part of it. A widened window over-matches
silently, which is why the tight bound is asserted rather than assumed. The
census measured max 0.048s / p95 0.040s against this corpus, so a sub-second
ceiling is ~20x the observed worst case and still an order of magnitude below
the gap between two distinct operator acts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import structlog

# The canonical ISO parser and the acted-state constant both live in ``model``.
# Re-spelling either here is the drift this package has already paid for once
# (``defer_window_open``'s docstring says it plainly): one predicate, one parser.
from .model import ACTION_RETIRED, STATE_ACTED, _parse_iso

log = structlog.get_logger(__name__)

# --- verbs -------------------------------------------------------------------
#
# These MIRROR ``alfred.daily_sync.action_router``'s action ids, deliberately by
# value rather than by import: ``daily_sync`` imports ``feed``, so importing
# back would close a cycle. A drift pin in the tests asserts these equal the
# router's constants, so the copy cannot rot silently.
VERB_ACCEPT = "accept"
VERB_DONE = "done"
VERB_ACK = "ack"

#: Events whose NAME alone determines the verb. ``feed.act.acked`` is here (and
#: not in the action-field family) because it logs ``id``/``kind`` and NO
#: ``action=`` field — the verb is in the event name or nowhere.
EVENT_VERB_BY_NAME: dict[str, str] = {
    "feed.act.slot.accepted": VERB_ACCEPT,
    "feed.act.slot.done": VERB_DONE,
    "feed.act.acked": VERB_ACK,
}

#: The one act event that carries its verb in an ``action=`` field instead.
ACTED_EVENT = "feed.act.acted"

#: The reconcile event that corroborates a retirement.
RECONCILE_EVENT = "feed.reconcile"

#: How far from ``acted_at`` to LOOK for an act line.
JOIN_WINDOW_SECONDS = 10.0

#: How far a match may actually be and still be believed. See the module
#: docstring — this is the discriminator, not a formality.
MAX_JOIN_DELTA_SECONDS = 1.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# structlog ConsoleRenderer shape, after ANSI strip:
#   2026-05-15T14:22:11.123456Z [info     ] feed.act.acted   id=abc action=done
#
# The LEVEL BRACKET IS OPTIONAL, and that is a measurement rather than a
# courtesy: ConsoleRenderer only emits ``[info     ]`` when a ``level`` key is
# in the event dict, which every in-tree ``setup_logging`` arranges via
# ``structlog.stdlib.add_log_level``. Rendering both shapes through the real
# renderer showed a bracket-less line failing a bracket-required pattern
# outright, so the optional group costs nothing and removes a way for this
# parser to go silently blind if a caller ever logs without that processor.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
    r"\s+(?:\[\s*\w+\s*\]\s+)?"
    r"(?P<event>\S+)"
    r"(?P<rest>.*)$"
)
_FIELD_RE = re.compile(r"(\w+)=('[^']*'|\"[^\"]*\"|\S+)")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


@dataclass(frozen=True)
class LogLine:
    """One parsed ConsoleRenderer line."""

    ts: datetime
    event: str
    fields: dict[str, str]


def parse_log_line(raw: str) -> LogLine | None:
    """Parse one rendered log line, or ``None`` when it isn't one.

    Non-matching lines (tracebacks, bare stdout captured into the umbrella log,
    blank lines) return ``None`` rather than raising — a repair that dies on the
    first stray line in a production log is a repair that never runs.
    """
    match = _LINE_RE.match(_ANSI_RE.sub("", raw).rstrip("\n"))
    if match is None:
        return None
    ts = _parse_iso(match.group("ts"))
    if ts is None:
        return None
    fields = {
        key: _strip_quotes(value)
        for key, value in _FIELD_RE.findall(match.group("rest"))
    }
    return LogLine(ts=ts, event=match.group("event"), fields=fields)


@dataclass(frozen=True)
class ActEvent:
    """An act SUCCESS line: this id was judged, with this verb, at this time."""

    ts: datetime
    item_id: str
    verb: str


@dataclass(frozen=True)
class ReconcileEvent:
    ts: datetime
    kind: str
    acted: int


def _iter_lines(paths: Sequence[Path]) -> Iterable[LogLine]:
    for path in paths:
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                parsed = parse_log_line(raw)
                if parsed is not None:
                    yield parsed


def collect_act_events(paths: Sequence[Path]) -> dict[str, list[ActEvent]]:
    """Every act-success line in ``paths``, grouped by item id."""
    by_id: dict[str, list[ActEvent]] = {}
    for line in _iter_lines(paths):
        item_id = line.fields.get("id", "")
        if not item_id:
            continue
        verb = EVENT_VERB_BY_NAME.get(line.event, "")
        if not verb and line.event == ACTED_EVENT:
            verb = line.fields.get("action", "")
        if not verb:
            continue
        by_id.setdefault(item_id, []).append(
            ActEvent(ts=line.ts, item_id=item_id, verb=verb)
        )
    return by_id


def collect_reconcile_events(paths: Sequence[Path]) -> list[ReconcileEvent]:
    """Every ``feed.reconcile`` line that retired at least one item."""
    events: list[ReconcileEvent] = []
    for line in _iter_lines(paths):
        if line.event != RECONCILE_EVENT:
            continue
        raw_acted = line.fields.get("acted", "")
        try:
            acted = int(raw_acted)
        except (TypeError, ValueError):
            continue
        if acted < 1:
            continue
        events.append(
            ReconcileEvent(ts=line.ts, kind=line.fields.get("kind", ""), acted=acted)
        )
    return events


@dataclass(frozen=True)
class VerbBackfill:
    item_id: str
    verb: str
    delta_seconds: float


@dataclass(frozen=True)
class Retirement:
    item_id: str
    kind: str


@dataclass(frozen=True)
class Skipped:
    item_id: str
    reason: str


class JoinPrecisionError(RuntimeError):
    """A matched act line sat further from ``acted_at`` than the ceiling allows.

    Raised INSTEAD of returning a partial plan. An over-matching join is silent
    by nature, so the only safe response is to refuse the whole run and let an
    operator look at the corpus.
    """


@dataclass
class RepairPlan:
    backfills: list[VerbBackfill] = field(default_factory=list)
    retirements: list[Retirement] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    @property
    def write_count(self) -> int:
        return len(self.backfills) + len(self.retirements)

    @property
    def is_empty(self) -> bool:
        return self.write_count == 0

    @property
    def max_delta_seconds(self) -> float:
        return max((b.delta_seconds for b in self.backfills), default=0.0)


def is_repair_candidate(item: Any) -> bool:
    """A LEGACY verbless-acted item: acted, but with no verb recorded.

    Post-PY-C the reconcile stamps ``state=retired`` for NEW retirements, so
    those never land here. This predicate is deliberately about the legacy
    shape — ``acted`` with an empty verb — and nothing else.
    """
    return getattr(item, "state", "") == STATE_ACTED and not getattr(
        item, "acted_action", None
    )


def _same_second(left: datetime, right: datetime) -> bool:
    return left.replace(microsecond=0) == right.replace(microsecond=0)


def build_plan(
    items: dict[str, Any],
    act_events: dict[str, list[ActEvent]],
    reconcile_events: Sequence[ReconcileEvent],
) -> RepairPlan:
    """Derive the repair plan. Pure — reads, decides, writes nothing.

    :raises JoinPrecisionError: if any accepted match exceeds the sub-second
        ceiling. The whole plan is refused; nothing partial is returned.
    """
    plan = RepairPlan()
    window = timedelta(seconds=JOIN_WINDOW_SECONDS)

    for item_id, item in sorted(items.items()):
        if not is_repair_candidate(item):
            continue

        acted_at = _parse_iso(getattr(item, "acted_at", None))
        if acted_at is None:
            # No anchor to join against — refuse rather than guess a verb.
            plan.skipped.append(Skipped(item_id, "no_acted_at"))
            continue

        candidates = act_events.get(item_id, [])
        if not candidates:
            # ZERO act lines anywhere: a true retirement IF a reconcile of this
            # item's kind retired something in the same second. Without that
            # corroboration it stays untouched — an unexplained item is not a
            # retirement, it is an item we have no evidence about.
            kind = str(getattr(item, "kind", ""))
            corroborated = any(
                ev.kind == kind and _same_second(ev.ts, acted_at)
                for ev in reconcile_events
            )
            if corroborated:
                plan.retirements.append(Retirement(item_id, kind))
            else:
                plan.skipped.append(Skipped(item_id, "no_act_lines_no_reconcile"))
            continue

        in_window = [
            ev for ev in candidates if abs(ev.ts - acted_at) <= window
        ]
        if not in_window:
            plan.skipped.append(Skipped(item_id, "no_act_line_in_window"))
            continue

        nearest = min(in_window, key=lambda ev: abs(ev.ts - acted_at))
        delta = abs(nearest.ts - acted_at).total_seconds()
        if delta > MAX_JOIN_DELTA_SECONDS:
            raise JoinPrecisionError(
                f"act line for {item_id!r} sat {delta:.3f}s from acted_at, over the "
                f"{MAX_JOIN_DELTA_SECONDS:.1f}s ceiling — the join is over-matching "
                f"and would write a verb the operator may never have chosen. "
                f"Refusing the whole plan ({len(plan.backfills)} backfills and "
                f"{len(plan.retirements)} retirements discarded)."
            )
        plan.backfills.append(VerbBackfill(item_id, nearest.verb, delta))

    return plan


def apply_plan(store: Any, plan: RepairPlan) -> int:
    """Append the plan's verb-bearing state events. Returns the write count.

    Every write is ``state=acted`` on an item that is ALREADY acted, so the
    fold changes the verb and nothing else: ``acted_action`` is latest-wins,
    ``acted_at`` is preserved by the store's ``if not existing.acted_at`` guard,
    and the state is what it already was. NOTHING IS REVIVED — there is no path
    in here that writes a non-terminal state.
    """
    for backfill in plan.backfills:
        store.set_state(backfill.item_id, STATE_ACTED, action=backfill.verb)
    for retirement in plan.retirements:
        # Legacy shape on purpose: ``acted`` + ``action=retired``, NOT
        # ``state=retired``. Post-PY-C the fold reads both, and rewriting these
        # to the new state would be a migration nobody asked for on top of a
        # repair — this run records the REASON, it does not restate the history.
        store.set_state(retirement.item_id, STATE_ACTED, action=ACTION_RETIRED)
    return plan.write_count
