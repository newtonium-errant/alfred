"""Campaign runner — budgeted increments over a work-list.

#44, spec: ``~/reports/drip-drain-design-2026-08-04.md``. Operator ratified
2026-08-04 (D1 halved to 12/run, 60/week; D2 ETA headline; D3 propose-only
tuning; D4 both campaigns immediately).

Operator's directive, the north star:

    "a limited budget each day or week so as to not use too many credits at a
    time knowing that it's okay for some tasks if this takes a few weeks."

So: running out of budget is a BORING, EXPECTED daily event. It is the success
path, and every log line and stop reason should read that way.

## Two separate concerns, deliberately not merged

* **Budget** (this module) is about credits. The units are ITEMS, which is a
  PROXY — the box's quota is neither queryable (``claude auth status`` stays
  green while quota-limited) nor natively cappable (``--max-budget-usd`` meters
  API dollars; the box runs on a subscription weekly limit — verified
  2026-08-04). Since no sensor exists, the propose-only auto-tune is the ONLY
  thing that can calibrate the proxy. That is why D3 is not polish.
* **Never-lose-work** (``drip/state.py``) is bookkeeping and is not a budget
  concern at all. See that module for the 907.

## Quota handling

Consumes ``health.agent_failure.classify_agent_failure`` — the existing closed
set ``{quota_limited, auth, other}`` built after the 2026-07-29 outage. NOT a
second detector: one definition of "the agent is quota-blocked" in the system.

On ``quota_limited`` / ``auth`` the run STOPS immediately rather than skipping
onward. Continuing would fail every remaining item identically, which is how 907
failures happened in one sitting. The item goes ``blocked``, which is a pause,
not a verdict: no attempt consumed, no spend recorded, re-attempted first next
run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol

import structlog

from .state import (
    BLOCKED,
    DONE,
    FAILED,
    IN_FLIGHT,
    CampaignState,
    ItemState,
    now_iso,
    save_state,
)

log = structlog.get_logger(__name__)

# --- stop reasons — CLOSED SET ---------------------------------------------
# ILB: "ran and did nothing" and "did not run" must be different lines. A
# background campaign is the easiest place in this system for silent death to
# hide, because nobody is waiting on any individual increment.
STOP_BUDGET_EXHAUSTED = "budget_exhausted"     # the SUCCESS path, most days
STOP_WORKLIST_EMPTY = "worklist_empty"         # campaign complete (or empty)
STOP_QUOTA_BLOCKED = "quota_blocked"
STOP_CIRCUIT_BREAKER = "circuit_breaker"
STOP_DISABLED = "disabled"

STOP_REASONS: frozenset[str] = frozenset({
    STOP_BUDGET_EXHAUSTED, STOP_WORKLIST_EMPTY, STOP_QUOTA_BLOCKED,
    STOP_CIRCUIT_BREAKER, STOP_DISABLED,
})


class Campaign(Protocol):
    """What a campaign supplies. Four things, no more.

    ``verify`` carries two contract requirements that pins enforce because
    types cannot (see ``drip/state.py``): it must key on the ITEM ID, and it
    must not be the negation of the work-list's admission test.
    """

    name: str

    def worklist(self) -> list[str]: ...
    def work(self, item_id: str) -> None: ...
    def verify(self, item_id: str) -> bool: ...
    def spends_quota(self) -> bool: ...


@dataclass
class RunResult:
    """One increment's outcome — the ILB payload and the brief's input."""

    campaign: str = ""
    attempted: int = 0
    done: int = 0
    failed: int = 0
    blocked: int = 0
    skipped_verified: int = 0     # re-queued items already done (crash recovery)
    remaining: int = 0
    total: int = 0
    spent_week: int = 0
    week_cap: int = 0
    stop_reason: str = STOP_WORKLIST_EMPTY
    errors: list[str] = field(default_factory=list)

    @property
    def pct_complete(self) -> float:
        if self.total <= 0:
            return 100.0
        return round(100.0 * (self.total - self.remaining) / self.total, 1)

    def eta_days(self, per_run: int) -> int | None:
        """Days to completion at the current budget — D2's headline number.

        ``None`` when it cannot be computed honestly (no budget, or nothing
        left). A guessed ETA is worse than an absent one: the whole point is
        that "a few weeks" becomes an informed acceptance rather than a hope,
        and a number that isn't grounded can't do that job.
        """
        if self.remaining <= 0:
            return 0
        if per_run <= 0:
            return None
        return -(-self.remaining // per_run)  # ceil


def _is_quota_block(exc: BaseException) -> tuple[bool, str]:
    """Classify a worker exception via the EXISTING probe. Never a second one.

    Signature verified against ``health.agent_failure`` at build time:
    ``classify_agent_failure(stdout, stderr) -> str``. Both streams get the
    exception text because a worker's exception is the only carrier we have —
    ``claude -p`` prints quota messages on STDOUT, which is the whole reason
    that module exists, and a caller that passed stderr alone would reproduce
    the empty-summary half of the 2026-07-29 diagnosis gap.

    NO defensive swallow. An earlier draft caught ``TypeError`` and returned
    "not a quota block" on signature drift — which would have converted a
    refactor into SILENT DEATH of quota detection: every campaign would grind
    through its full budget against a quota-limited account, logging ordinary
    failures, and the one signal designed to stop that would never fire. That is
    the defaulted-gate trap in another costume. If the probe's signature ever
    changes, this should break loudly at the first failed item rather than
    quietly stop protecting anything.
    """
    from alfred.health.agent_failure import (
        AUTH,
        QUOTA_LIMITED,
        classify_agent_failure,
    )

    text = str(exc)
    kind = classify_agent_failure(text, text)
    return kind in (QUOTA_LIMITED, AUTH), str(kind)


def run_increment(
    campaign: Campaign,
    state: CampaignState,
    *,
    state_path: Any,
    max_items_per_run: int,
    max_items_per_week: int,
    max_attempts: int,
    max_failures_per_run: int,
    today: date,
    run_id: str,
    apply: bool = True,
) -> RunResult:
    """Drain one budgeted increment. Never raises; always reports.

    ``apply=False`` is a DRY RUN — selection and budget are computed and
    reported, nothing is worked and nothing is written. Three times during the
    #40/#40b arc a wrong predicate was caught by previewing rather than by
    reasoning, so the preview is a first-class mode with its own pins, not a
    debugging convenience. It must describe the SAME operation the apply
    performs — a preview that differs from the run is worse than none, because
    it looks careful.
    """
    worklist = campaign.worklist()
    result = RunResult(
        campaign=campaign.name,
        total=len(worklist),
        week_cap=max_items_per_week,
    )

    spent_week = state.spend_in_week_ending(today)
    result.spent_week = spent_week

    # Weekly budget: the cap that actually bit on 2026-07-26, and a daily-only
    # limit still burns a week's allowance in four days.
    #
    # It binds ONLY campaigns that spend quota. The weekly cap is a CREDIT
    # guard, and link001 does no LLM work — letting it consume gmail's weekly
    # allowance would throttle a free workload while starving the expensive one,
    # which is the exact asymmetry D4's per-campaign model exists to absorb.
    # The per-run cap still applies to everyone: for a non-spending campaign it
    # bounds vault-write fan-out (surveyor relabel cascade), which is a real
    # cost even though it is not a credit one.
    spends = campaign.spends_quota()
    if spends and max_items_per_week > 0:
        week_headroom = max(max_items_per_week - spent_week, 0)
    else:
        week_headroom = 1 << 30
    budget = min(max_items_per_run, week_headroom)

    # Retriable in list order — FIFO, so nothing starves. in_flight rows come
    # first: they are crash leftovers and may already be done (verify-first).
    def _st(iid: str) -> ItemState:
        return state.items.setdefault(iid, ItemState(item_id=iid))

    candidates = [i for i in worklist if _st(i).state == IN_FLIGHT]
    candidates += [
        i for i in worklist
        if _st(i).state != IN_FLIGHT
        and _st(i).state != DONE
        and not (_st(i).state == FAILED and _st(i).attempts >= max_attempts)
    ]

    if not candidates:
        result.remaining = state.remaining(worklist)
        result.stop_reason = STOP_WORKLIST_EMPTY
        _finish(state, state_path, result, apply=apply)
        return result
    if budget <= 0:
        result.remaining = state.remaining(worklist)
        result.stop_reason = STOP_BUDGET_EXHAUSTED
        _finish(state, state_path, result, apply=apply)
        return result

    for item_id in candidates:
        if result.attempted >= budget:
            result.stop_reason = STOP_BUDGET_EXHAUSTED
            break
        if result.failed >= max_failures_per_run > 0:
            # A campaign failing every item should stop after five, not after
            # two hundred.
            result.stop_reason = STOP_CIRCUIT_BREAKER
            break

        item = _st(item_id)

        # VERIFY-FIRST on a re-queued in_flight row. A crash after the effect
        # landed and before the state write left a genuinely-done item claimed;
        # re-running it would duplicate the work — the 907 inverted.
        if item.state == IN_FLIGHT and item.claimed_by != run_id:
            try:
                if campaign.verify(item_id):
                    if apply:
                        item.state = DONE
                        item.updated_at = now_iso()
                    result.skipped_verified += 1
                    result.done += 1
                    continue
            except Exception as exc:  # noqa: BLE001 — a verify fault is not a loss
                log.warning(
                    "drip.verify_failed_on_requeue",
                    campaign=campaign.name, item_id=item_id, error=str(exc),
                )

        if not apply:
            # Dry run: count what WOULD be attempted, touch nothing.
            result.attempted += 1
            continue

        # Claim BEFORE working, and persist it, so a kill mid-item leaves a
        # visible in_flight rather than a silently skipped item.
        item.state = IN_FLIGHT
        item.claimed_by = run_id
        item.updated_at = now_iso()
        save_state(state_path, state)

        result.attempted += 1
        try:
            campaign.work(item_id)
        except Exception as exc:  # noqa: BLE001
            blocked, kind = _is_quota_block(exc)
            if blocked:
                # A pause, not a verdict: no attempt consumed, no spend
                # recorded, re-attempted first next run.
                item.state = BLOCKED
                item.last_error = f"{kind}: {exc}"[:400]
                item.updated_at = now_iso()
                result.blocked += 1
                result.stop_reason = STOP_QUOTA_BLOCKED
                log.warning(
                    "drip.quota_blocked",
                    campaign=campaign.name, item_id=item_id, kind=kind,
                    note="run stopped; continuing would fail every remaining "
                         "item identically",
                )
                break
            item.state = FAILED
            item.attempts += 1
            item.last_error = str(exc)[:400]
            item.updated_at = now_iso()
            result.failed += 1
            result.errors.append(f"{item_id}: {str(exc)[:120]}")
            continue

        # The worker returned. That is NOT evidence it did anything —
        # files_created=[] returned cleanly too.
        try:
            landed = bool(campaign.verify(item_id))
        except Exception as exc:  # noqa: BLE001
            landed = False
            log.warning(
                "drip.verify_raised",
                campaign=campaign.name, item_id=item_id, error=str(exc),
            )
        if landed:
            item.state = DONE
            item.updated_at = now_iso()
            result.done += 1
            if campaign.spends_quota():
                iso = today.isoformat()
                state.spend_by_day[iso] = state.spend_by_day.get(iso, 0) + 1
        else:
            item.state = FAILED
            item.attempts += 1
            item.last_error = "worker returned but the effect was not observable"
            item.updated_at = now_iso()
            result.failed += 1
            result.errors.append(f"{item_id}: unverified effect")
            log.warning(
                "drip.unverified_effect",
                campaign=campaign.name, item_id=item_id,
                note="worker succeeded with no observable effect — this is the "
                     "907 signature (files_created=[]) caught one layer up",
            )
        save_state(state_path, state)
    else:
        # Loop completed without break: every candidate within budget was tried.
        result.stop_reason = (
            STOP_WORKLIST_EMPTY
            if result.attempted < budget
            else STOP_BUDGET_EXHAUSTED
        )

    result.remaining = state.remaining(worklist)
    result.spent_week = state.spend_in_week_ending(today)
    _finish(state, state_path, result, apply=apply)
    return result


def _finish(
    state: CampaignState, state_path: Any, result: RunResult, *, apply: bool,
) -> None:
    """Persist (apply only) and emit the ILB line — ALWAYS, including no-ops."""
    if apply:
        state.last_run_at = now_iso()
        state.last_stop_reason = result.stop_reason
        save_state(state_path, state)
    log.info(
        "drip.campaign.run",
        campaign=result.campaign,
        mode="apply" if apply else "dry_run",
        attempted=result.attempted,
        done=result.done,
        failed=result.failed,
        blocked=result.blocked,
        skipped_verified=result.skipped_verified,
        remaining=result.remaining,
        total=result.total,
        pct=result.pct_complete,
        spent_week=result.spent_week,
        week_cap=result.week_cap,
        stop_reason=result.stop_reason,
    )


__all__ = [
    "STOP_BUDGET_EXHAUSTED",
    "STOP_CIRCUIT_BREAKER",
    "STOP_DISABLED",
    "STOP_QUOTA_BLOCKED",
    "STOP_REASONS",
    "STOP_WORKLIST_EMPTY",
    "Campaign",
    "RunResult",
    "run_increment",
]
