"""Tier computation — pure projection over task frontmatter.

**Tier-V2 transition note (2026-05-29).** This module is mid-migration
from V1 (persistent per-task ``base_tier``/``escalate_to`` attributes)
to V2 (daily-curation ritual in ``vault/daily/<date>.md`` — see
``alfred.tier.daily_curation``).

V1 symbols are kept in place during Ship 1 because ``brief/tier_section.py``
+ ``telegram/today_command.py`` still import them; dropping now would
break the brief daemon + ``/today`` at module-import time. Ship 2's
``tier_section.py`` rewrite will drop V1 symbols in the same commit
that switches the render to consume ``DailyCuration``. **DO NOT add
new V1 callers** — every new tier consumer should read from
``daily_curation.load_daily_curation`` + the new V2 surface
:func:`compute_auto_t1_candidates`.

V2 reframes tier as a daily-curation ritual:
- **T1** — imminent deadline (today/tomorrow). Auto-surfaced
  via :func:`compute_auto_t1_candidates`; operator-confirmed.
- **T2** — work-getting-ahead OR maintenance task being put off.
  Operator-selected from the open-task pool.
- **T3** — self-care intentions for today. Operator-picked from the
  routine's Aspirational items OR free-text additions.

The old V1 framing below describes the deprecated per-task tier
attribute model. Kept for reference until Ship 2 drops it:

- **Tier 1** — the *now* queue. Time-critical, action-today.
- **Tier 2** — the *soon* queue. On the radar, not urgent today.
- **Tier 3** — the *someday* queue. Aspirational, no deadline pressure.

Five operator-facing frontmatter fields on ``task`` records:

- ``base_tier``       (int 1/2/3)  — intrinsic tier the operator set.
- ``escalate_to``     (int)        — tier the task escalates to as the
                                     deadline approaches. Default:
                                     ``max(1, base_tier - 1)`` (one tier
                                     up, capped at T1).
- ``escalate_at_days`` (int)       — days BEFORE ``due`` when the
                                     escalation fires. **Opt-in**:
                                     omitting this field means the task
                                     never escalates, even with a ``due``.
- ``due``             (date / str) — deadline. Tier escalation is
                                     deadline-relative; absent ``due`` =
                                     no escalation possible.
- ``priority``        (str)        — intrinsic-importance (urgent /
                                     high / medium / low). Used ONLY
                                     as the fallback to derive a
                                     ``base_tier`` for pre-migration
                                     tasks; orthogonal to escalation.

The output is a ``TierResult`` namedtuple ``(base_tier, effective_tier,
reason)``. ``reason`` is a short human-readable string suitable for
debug logging or — sliced into a render annotation — the brief.

**``effective_tier`` is never written to the record.** This module is
a read-side projection only. The render layer composes the annotation;
the record stays canonical.

# Computation contract

Given ``task_fm`` (frontmatter dict) and ``now`` (caller-supplied
``datetime`` for testability — no internal ``datetime.now()`` calls):

1. Resolve ``base_tier``:
   - If ``base_tier`` is an int 1/2/3 → use it.
   - Else if ``priority`` is one of urgent/high/medium/low → derive per
     ``PRIORITY_TO_BASE_TIER`` (urgent→1, high/medium→2, low→3).
   - Else → default to 3 (most aspirational; safest assumption is
     "no deadline pressure declared").

2. Resolve escalation parameters:
   - ``escalate_to`` defaults to ``max(1, base_tier - 1)`` when absent.
   - ``escalate_at_days`` has NO default — absent means no escalation.

3. Compute ``effective_tier``:
   - If no ``due`` field → ``effective_tier = base_tier``, reason
     ``"base (no due date)"``.
   - If ``due`` is in the past → ``effective_tier = escalate_to``,
     reason ``"overdue — escalated to T{n}"``. **Past-due is always
     maximum escalation regardless of ``escalate_at_days``** — a
     missed deadline is by definition past the escalation window.
   - If ``escalate_at_days`` absent → ``effective_tier = base_tier``,
     reason ``"base (escalation not configured)"``.
   - If ``(due - now.date()).days <= escalate_at_days`` →
     ``effective_tier = escalate_to``, reason ``"escalated — Nd to due"``.
   - Else → ``effective_tier = base_tier``, reason
     ``"base — Nd to escalation window"``.

The reason strings are stable contract surface for the brief render
layer. If you change a string here, update ``tier_section.py``'s
annotation derivation in lockstep.
"""

from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# Default escalation gap (in tiers) when ``escalate_to`` is omitted.
# ``base_tier - 1`` capped at 1 — one tier up, never above T1.
# Exposed as a constant for tests + documentation discoverability.
DEFAULT_ESCALATION_GAP = 1

# Task statuses considered "open" — surfaced in the tier section.
# Per dispatch ratification: blocked tasks still surface (operator needs
# to see them in the queue). Done/cancelled are excluded.
OPEN_STATUSES: frozenset[str] = frozenset({"todo", "active", "blocked"})

# Fallback mapping: when ``base_tier`` is unset on a task, derive it
# from ``priority``. Pre-migration tasks have ``priority`` but not
# ``base_tier``; this lets them render sensibly without bulk edits.
PRIORITY_TO_BASE_TIER: dict[str, int] = {
    "urgent": 1,
    "high": 2,
    "medium": 2,
    "low": 3,
}


TierResult = namedtuple(
    "TierResult",
    ["base_tier", "effective_tier", "reason"],
)


def derive_base_tier_from_priority(priority: Any) -> int | None:
    """Map a ``priority`` value to a base tier per ``PRIORITY_TO_BASE_TIER``.

    Returns the int tier or ``None`` if ``priority`` is missing /
    unrecognised. Case-insensitive — operator hand-edits sometimes
    capitalize (``"Urgent"``).
    """
    if not isinstance(priority, str):
        return None
    key = priority.strip().lower()
    return PRIORITY_TO_BASE_TIER.get(key)


def _coerce_tier_int(value: Any) -> int | None:
    """Coerce a frontmatter value to a tier int (1, 2, or 3).

    Returns ``None`` if the value is missing or out-of-range. Operators
    hand-write YAML; ``base_tier: "2"`` (str) should parse the same as
    ``base_tier: 2`` (int). Out-of-range values (e.g. ``base_tier: 5``)
    fall back to ``None`` so the caller can apply the priority-derivation
    or default-to-3 fallback.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n not in (1, 2, 3):
        return None
    return n


def coerce_due_date(value: Any) -> date | None:
    """Coerce a frontmatter ``due`` value to a ``date``.

    PyYAML parses ``due: 2026-05-28`` as a ``date`` object directly; the
    isoformat-string branch handles operator-edited records where the
    field came in as a quoted string (``due: '2026-05-28'``). datetime
    instances are normalised to their date component.

    Public API (promoted from ``_coerce_due_date`` 2026-05-28, Phase 2
    cleanup): the render layer in ``alfred.brief.tier_section`` parses
    ``due`` for two distinct purposes (distance formatting + sort
    keying), and a future tier-CLI surface or a related render path
    has the same need. One canonical helper > N copies of the parser
    threaded through inline calls or duplicated in closures.
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


def compute_effective_tier(
    task_fm: dict[str, Any],
    now: datetime,
) -> TierResult:
    """Compute the effective tier for a task at instant ``now``.

    ``task_fm`` is the task record's frontmatter dict. ``now`` is the
    caller-supplied reference instant — pass ``datetime.now(tz)`` from
    the brief render path; tests pass a deterministic fixture.

    Returns a ``TierResult(base_tier, effective_tier, reason)``. See the
    module docstring for the full computation contract.
    """
    # --- 1. Resolve base_tier --------------------------------------
    base = _coerce_tier_int(task_fm.get("base_tier"))
    if base is None:
        derived = derive_base_tier_from_priority(task_fm.get("priority"))
        if derived is not None:
            base = derived
            base_source = "from priority"
        else:
            base = 3
            base_source = "default"
    else:
        base_source = "set"

    # --- 2. Resolve escalation parameters --------------------------
    escalate_to_raw = _coerce_tier_int(task_fm.get("escalate_to"))
    if escalate_to_raw is None:
        escalate_to = max(1, base - DEFAULT_ESCALATION_GAP)
    else:
        escalate_to = escalate_to_raw

    escalate_at_days_raw = task_fm.get("escalate_at_days")
    escalate_at_days: int | None
    try:
        escalate_at_days = (
            int(escalate_at_days_raw) if escalate_at_days_raw is not None else None
        )
    except (TypeError, ValueError):
        escalate_at_days = None

    due = coerce_due_date(task_fm.get("due"))

    # --- 3. Compute effective_tier ---------------------------------
    # No due date — base tier holds. Annotate the source so the render
    # layer can decide whether to add "(from priority)" / etc.
    if due is None:
        reason = _compose_base_reason(base_source, "no due date")
        return TierResult(base, base, reason)

    days_to_due = (due - now.date()).days

    # Past-due: maximum escalation regardless of escalate_at_days.
    # A missed deadline is by definition past the escalation window.
    if days_to_due < 0:
        overdue_days = abs(days_to_due)
        reason = (
            f"overdue {overdue_days}d — escalated to T{escalate_to}"
            if base_source == "set"
            else (
                f"overdue {overdue_days}d — escalated to T{escalate_to} "
                f"(base {base_source})"
            )
        )
        return TierResult(base, escalate_to, reason)

    # Opt-in: no escalate_at_days → no escalation fires.
    if escalate_at_days is None:
        reason = _compose_base_reason(
            base_source, "escalation not configured"
        )
        return TierResult(base, base, reason)

    # Inside the escalation window.
    if days_to_due <= escalate_at_days:
        reason = (
            f"escalated — {days_to_due}d to due"
            if base_source == "set"
            else f"escalated — {days_to_due}d to due (base {base_source})"
        )
        return TierResult(base, escalate_to, reason)

    # Outside the escalation window. Annotate the days remaining to
    # the window so a debug reader can see why the escalation didn't
    # fire yet.
    days_to_window = days_to_due - escalate_at_days
    reason = (
        f"base — {days_to_window}d to escalation window"
        if base_source == "set"
        else (
            f"base — {days_to_window}d to escalation window "
            f"(base {base_source})"
        )
    )
    return TierResult(base, base, reason)


def _compose_base_reason(base_source: str, note: str) -> str:
    """Compose a base-tier reason string with an explanation suffix.

    Centralised so the prefix shape stays consistent across all base-
    tier branches. ``base_source`` is one of:

    - ``"set"``           → operator set ``base_tier`` explicitly
                            (no suffix; the cleanest reason string)
    - ``"from priority"`` → ``base_tier`` derived from ``priority``;
                            suffix reads "...; from priority" — the
                            render layer searches for this substring
                            to add the ``(from priority)`` annotation
    - ``"default"``       → neither set nor derivable; suffix reads
                            "...; default" so debug readers can tell
                            the task has no operator-set tier signal

    ``note`` is the branch-specific tail (e.g. ``"no due date"``).
    """
    if base_source == "set":
        return f"base ({note})"
    # "from priority" already contains the "from " preposition; the
    # other non-set source ("default") gets its own short suffix. This
    # branching keeps the reason strings natural-language readable.
    if base_source == "from priority":
        return f"base ({note}; from priority)"
    return f"base ({note}; {base_source})"


# ---------------------------------------------------------------------------
# Tier-V2 surface — auto-T1 candidate discovery (2026-05-29 Ship 1)
# ---------------------------------------------------------------------------
#
# The V2 model lifts tier selection out of per-task attributes and into
# the daily curation ritual stored in ``vault/daily/<date>.md``. The
# only auto-surface compute layer needs to provide is "which tasks
# should be presented as T1 candidates this morning?" — Ship 2's brief
# renderer reads this list, marks them as T1 auto-candidates, and the
# operator confirms-or-drops via talker.
#
# Compute scope:
#   * Scan ``vault/task/*.md`` (mirrors the brief's existing
#     ``_iter_task_records`` shape — defensive YAML pre-validation +
#     type filter + open-status filter).
#   * For each open task, decide if it should auto-surface as T1 today:
#     - ``due`` is today or tomorrow → surface (reason: ``"due today"``
#       / ``"due tomorrow"``).
#     - ``escalate_at_days`` is set + ``due`` is within that window →
#       surface (reason: ``"escalate window (Nd before due)"``).
#   * Defensive ``alfred_triage`` filter — janitor-generated triage
#     records are NOT tier-rankable work, must never auto-surface.
#     Per ``feedback_tier_semantics_andrew_model`` 2026-05-29.
#
# The returned candidates feed Ship 2's brief render. Ship 4's SKILL
# update will teach the talker the operator-confirm phrase grammar.
# Both downstream consumers reference the canonical reason strings
# verbatim — change the strings here = update Ship 2 + Ship 4 in
# lockstep.


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
        ``escalate_at_days`` value (e.g. ``"escalate window (3d before due)"``
        for a task with ``escalate_at_days: 3``).

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
      3. ``status`` NOT in :data:`OPEN_STATUSES` → skip. Done/cancelled
         tasks aren't tier-rankable.
      4. ``alfred_triage is True`` → skip. Janitor triage records go
         to the Daily Sync friction list (separate ship), not the
         tier section. Per the operator-stated semantics 2026-05-29.
      5. ``due`` missing or unparseable → skip. No deadline → can't
         auto-surface.
      6. ``due`` is today → surface with reason ``"due today"``.
      7. ``due`` is tomorrow → surface with reason ``"due tomorrow"``.
      8. ``due`` is more than 1 day out BUT inside the
         ``escalate_at_days`` window → surface with reason
         ``"escalate window (Nd before due)"``.
      9. Otherwise → skip (deadline too far out).

    ``now`` is the caller-supplied reference instant. The function uses
    only ``now.date()`` for date math; the time component is irrelevant
    here (the brief daemon passes ``datetime.now(tz)`` for parity with
    the V1 ``compute_effective_tier`` signature).

    Returns the candidate list sorted by ``due_iso`` ascending then
    by ``name`` — deterministic order so Ship 2's brief render stays
    stable across consecutive aggregator runs on the same morning.

    Per ``feedback_intentionally_left_blank``: this function emits no
    log lines itself (compute path is pure); each call-site that uses
    the result is responsible for the "ran, here's the count" log.
    Tests assert the no-logs invariant via ``capture_logs``.
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
                # window" — gate on ``2 <= days_to_due <= escalate_at_days``.
                # (A task ``escalate_at_days: 1`` is already covered by
                # the tomorrow-branch; only ``escalate_at_days >= 2``
                # produces NEW surfacings here.)
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
    "DEFAULT_ESCALATION_GAP",
    "OPEN_STATUSES",
    "PRIORITY_TO_BASE_TIER",
    "TierResult",
    "coerce_due_date",
    "compute_auto_t1_candidates",
    "compute_effective_tier",
    "derive_base_tier_from_priority",
]
