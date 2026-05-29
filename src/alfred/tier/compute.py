"""Tier computation — pure projection over task frontmatter (V2).

Tier-V2 reframes tier as a **daily curation ritual** stored in
``vault/daily/<date>.md`` rather than persistent per-task attributes.
See :mod:`alfred.tier.daily_curation` for the data layer + Ship 2's
``alfred.brief.tier_section`` for the render that consumes both this
auto-T1 surface and the operator-curated shortlists.

The only compute primitive V2 needs is **auto-T1 candidate discovery**:
"which open tasks should be presented as T1 candidates this morning?"
The brief renderer reads this list, marks them as T1 auto-candidates,
and the operator confirms-or-drops via talker.

Auto-surface criteria (in priority order — short-circuits on first
match):

  * ``due`` is today → reason ``"due today"``
  * ``due`` is tomorrow → reason ``"due tomorrow"``
  * ``escalate_at_days`` is set + ``due`` is within that window (more
    than 1 day out — the 0/1-day cases above subsume the rest) →
    reason ``"escalate window (Nd before due)"``

Defensive filters: parse failures, non-task ``type:``, closed
``status:``, ``alfred_triage: True`` (janitor-generated records that
go to the Daily Sync Triage Queue, not the tier section).

V1 retired (2026-05-29 Ship 3). The per-task ``base_tier`` /
``escalate_to`` / priority-fallback projection through the prior
``compute_effective_tier`` function is gone, along with the
``PRIORITY_TO_BASE_TIER`` constant, ``derive_base_tier_from_priority``
helper, ``TierResult`` namedtuple, and ``DEFAULT_ESCALATION_GAP``
constant. The 24 existing ``base_tier`` records remain on disk
(deferred backfill per Ship 5 — those records stay until either the
operator curates them daily or runs the backfill).

Reason strings (``"due today"`` / ``"due tomorrow"`` / ``"escalate
window (Nd before due)"``) are stable contract surface for Ship 2's
brief render + Ship 4's SKILL (SKILL quotes the strings verbatim so
the talker recognises operator replies). Change the strings here =
update Ship 2 + Ship 4 in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# Task statuses considered "open" — surfaced in the tier section /
# selection pool. Per dispatch ratification: blocked tasks still
# surface (operator needs to see them in the queue). Done / cancelled
# are excluded.
OPEN_STATUSES: frozenset[str] = frozenset({"todo", "active", "blocked"})


def coerce_due_date(value: Any) -> date | None:
    """Coerce a frontmatter ``due`` value to a ``date``.

    PyYAML parses ``due: 2026-05-28`` as a ``date`` object directly;
    the isoformat-string branch handles operator-edited records where
    the field came in as a quoted string (``due: '2026-05-28'``).
    datetime instances are normalised to their date component.

    Public API: the V2 brief render layer in
    :mod:`alfred.brief.tier_section` parses ``due`` for distance
    formatting + sort keying; a future tier-CLI surface or related
    render path has the same need. One canonical helper > N copies of
    the parser threaded through inline calls.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Tier-V2 surface — auto-T1 candidate discovery
# ---------------------------------------------------------------------------


@dataclass
class AutoT1Candidate:
    """One task that auto-surfaces as a T1 candidate this morning.

    ``path`` is the vault-relative path (e.g. ``"task/RRTS Payroll.md"``)
    — Ship 2's brief uses this to construct the wikilink. ``name`` is
    the operator-facing display string (from frontmatter ``name`` or
    file stem). ``due_iso`` is the task's deadline as an ISO date
    string (always present — a candidate without a due date wouldn't
    have triggered the auto-surface). ``surface_reason`` is the
    canonical reason string Ship 2 renders inline:

      * ``"due today"``
      * ``"due tomorrow"``
      * ``"escalate window (Nd before due)"`` — N is the
        ``escalate_at_days`` value (e.g. ``"escalate window (3d
        before due)"`` for a task with ``escalate_at_days: 3``).

    Reason strings are stable contract per the module docstring.
    """

    path: str
    name: str
    due_iso: str
    surface_reason: str


def compute_auto_t1_candidates(
    vault_path: Path, now: datetime,
) -> list[AutoT1Candidate]:
    """Walk ``vault/task/*.md`` and return tasks auto-surfacing as T1.

    Filter logic (in this exact order — short-circuits on first
    rejection):

      1. Frontmatter parse failure → skip silently. Ship 2's brief
         renders parse failures separately; this compute path is
         "what auto-surfaces" and a broken record can't.
      2. ``type != "task"`` → skip. Defensive against stray files.
      3. ``status`` NOT in :data:`OPEN_STATUSES` → skip. Done /
         cancelled tasks aren't tier-rankable.
      4. ``alfred_triage is True`` → skip. Janitor triage records go
         to the Daily Sync Triage Queue (Ship 3 section provider),
         not the tier section. Per the operator-stated semantics
         2026-05-29.
      5. ``due`` missing or unparseable → skip. No deadline → can't
         auto-surface.
      6. ``due`` is today → surface with reason ``"due today"``.
      7. ``due`` is tomorrow → surface with reason ``"due tomorrow"``.
      8. ``due`` is more than 1 day out BUT inside the
         ``escalate_at_days`` window → surface with reason
         ``"escalate window (Nd before due)"``.
      9. Otherwise → skip (deadline too far out).

    ``now`` is the caller-supplied reference instant. The function
    uses only ``now.date()`` for date math; the time component is
    irrelevant here (the brief daemon passes ``datetime.now(tz)``).

    Returns the candidate list sorted by ``due_iso`` ascending then
    by ``name`` — deterministic order so Ship 2's brief render stays
    stable across consecutive aggregator runs on the same morning.

    Per ``feedback_intentionally_left_blank``: this function emits no
    log lines itself (compute path is pure); each call-site that
    uses the result is responsible for the "ran, here's the count"
    log. Tests assert the no-logs invariant via ``capture_logs``.
    """
    import frontmatter  # type: ignore[import-untyped]

    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return []

    today_local = now.date()
    tomorrow_local = today_local + timedelta(days=1)

    candidates: list[AutoT1Candidate] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "task":
            continue
        status = str(fm.get("status") or "todo").lower()
        if status not in OPEN_STATUSES:
            continue
        if fm.get("alfred_triage") is True:
            continue
        due = coerce_due_date(fm.get("due"))
        if due is None:
            continue

        reason: str | None = None
        if due == today_local:
            reason = "due today"
        elif due == tomorrow_local:
            reason = "due tomorrow"
        else:
            # Check the escalate_at_days window.
            escalate_at_days_raw = fm.get("escalate_at_days")
            try:
                escalate_at_days = (
                    int(escalate_at_days_raw)
                    if escalate_at_days_raw is not None
                    else None
                )
            except (TypeError, ValueError):
                escalate_at_days = None
            if escalate_at_days is not None and escalate_at_days > 0:
                days_to_due = (due - today_local).days
                # The 0-day + 1-day cases were caught above. The
                # escalate window is "more than 1 day but within the
                # window" — gate on ``2 <= days_to_due <=
                # escalate_at_days``. (A task ``escalate_at_days: 1``
                # is already covered by the tomorrow-branch; only
                # ``escalate_at_days >= 2`` produces NEW surfacings
                # here.)
                if 2 <= days_to_due <= escalate_at_days:
                    reason = (
                        f"escalate window ({escalate_at_days}d before due)"
                    )

        if reason is None:
            continue

        name = str(fm.get("name") or path.stem)
        rel_path = f"task/{path.name}"
        candidates.append(AutoT1Candidate(
            path=rel_path,
            name=name,
            due_iso=due.isoformat(),
            surface_reason=reason,
        ))

    candidates.sort(key=lambda c: (c.due_iso, c.name.lower()))
    return candidates


__all__ = [
    "AutoT1Candidate",
    "OPEN_STATUSES",
    "coerce_due_date",
    "compute_auto_t1_candidates",
]
