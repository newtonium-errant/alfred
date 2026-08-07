"""Re-audit a campaign's ``done`` rows with the CURRENT verifier (#60).

## Why this exists

A campaign's ``done`` rows are only as trustworthy as the verifier that wrote
them, and link001's verifier was satisfiable by the very defect it existed to
catch: a YAML-folded link failed the exact-string match in ``work()`` (so
nothing was edited) and failed it in ``verify()`` too — which the removal branch
reads as *success*. Four of the first live increment's twelve items were
recorded ``done`` over an untouched record on 2026-08-06.

Fixing the verifier stops NEW false-dones. It does nothing about the rows
already on disk, and those rows are the durable half of the damage: the
work-list is FROZEN, so a ``done`` row is never revisited. The links they claim
to have repaired would stay broken permanently, and nothing would ever say so.
Only re-asking the question can undo that, which is what this module does.

## The three rules, and what each one is protecting against

1. **Only ``done`` rows are audited.** ``pending`` / ``failed`` / ``in_flight``
   / ``blocked`` are all going to be revisited by the drain anyway. Rewriting
   them would reset attempt counters and un-fail items the operator may be
   triaging by hand.

2. **Demotion is the only mutation, and it only ever moves downward.** The pass
   can send ``done`` → ``pending`` and nothing else. A repair tool that can also
   write ``done`` is a second way to manufacture the exact bug it was built to
   clean up.

3. **A verify that RAISES does not demote.** Failing to prove an item landed is
   not proof that it didn't — the #40 over-reach of 2026-08-04 is the same
   lesson, where an admission test's inverse was read as a disproof and reached
   ~914 genuine records. An unreadable record would otherwise reopen work that
   was genuinely finished, and for the removal branch that work is irreversible.
   So those rows are counted and logged separately, loudly enough that the
   number is never mistaken for a clean bill of health.

Idempotence falls out of rule 1 rather than being enforced: a demoted row is
``pending`` on the next pass, so it is out of scope and the second run demotes
nothing.

## Why this is a module and not just the CLI handler

It was the handler, in the parked WIP, and that made contract point 3 the one
part of the #60 fix with no tests — driving it needed a built ``DripConfig``, a
work-list file and a state file on disk. The audit is a pure function of a
campaign and a state object; keeping it that way is what lets the rules above be
pinned directly. :func:`alfred.drip.cli.cmd_repair_verify` is the operator-facing
wrapper: it selects campaigns, loads and saves state, and prints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from .state import DONE, PENDING, CampaignState, now_iso

log = structlog.get_logger(__name__)

#: Written into a demoted row's ``last_error``. Carries the issue number on
#: purpose: an operator reading state after the deploy must be able to tell a
#: #60 demotion from an ordinary pending item, and the row is the only place
#: that context survives.
DEMOTION_REASON = (
    "demoted by repair-verify (#60): recorded done, but the current verifier "
    "does not observe this item's effect"
)


@dataclass
class RepairResult:
    """What one campaign's audit found. ``audited == confirmed + demoted +
    unverifiable`` always holds, which is the arithmetic the summary line
    depends on."""

    campaign: str = ""
    audited: int = 0
    confirmed: int = 0
    demoted: int = 0
    unverifiable: int = 0
    #: Item ids, so the caller can print them without re-deriving the verdict.
    demoted_items: list[str] = field(default_factory=list)
    #: ``(item_id, error)`` for rows whose verify raised.
    unverifiable_items: list[tuple[str, str]] = field(default_factory=list)


def repair_false_dones(
    campaign: Any,
    state: CampaignState,
    *,
    apply: bool = False,
) -> RepairResult:
    """Re-verify every ``done`` row; demote the ones the verifier disbelieves.

    ``state`` is mutated IN PLACE when ``apply`` is true. Persisting it is the
    caller's job — this function has no opinion about where state lives, which
    is what makes it testable without a config or a filesystem.

    With ``apply`` false the result is fully populated and nothing is written:
    the counts and ``demoted_items`` are exactly what an ``apply`` run would do.
    Dry-run is the default because the deploy moment for this is one deliberate
    invocation, so previewing which items it intends to reopen costs a command
    and makes the write an informed one.

    ILB: the completion event fires on EVERY call — including a campaign with no
    ``done`` rows at all. "Audited nothing because there was nothing to audit"
    and "did not run" are different facts, and this runs at deploy time, where
    that distinction is the entire point of running it.
    """
    name = getattr(campaign, "name", "") or state.campaign
    result = RepairResult(campaign=name)

    done_ids = sorted(
        item_id for item_id, row in state.items.items() if row.state == DONE
    )

    for item_id in done_ids:
        result.audited += 1
        try:
            landed = bool(campaign.verify(item_id))
        except Exception as exc:  # noqa: BLE001 — rule 3, see the module docstring
            result.unverifiable += 1
            result.unverifiable_items.append((item_id, str(exc)))
            log.warning(
                "drip.repair_verify.verify_raised",
                campaign=name,
                item_id=item_id,
                error=str(exc),
                consequence="left DONE — an unprovable item is not a disproven "
                            "one, and reopening it would re-edit a record that "
                            "may already be correct",
            )
            continue

        if landed:
            result.confirmed += 1
            continue

        result.demoted += 1
        result.demoted_items.append(item_id)
        log.warning(
            "drip.repair_verify.demoted",
            campaign=name,
            item_id=item_id,
            previous_state=DONE,
            new_state=PENDING,
            applied=apply,
            reason="the current verifier does not observe this item's effect — "
                   "it was recorded done by a verifier that could not see the "
                   "defect it was checking for",
        )
        if not apply:
            continue

        row = state.items[item_id]
        row.state = PENDING
        # The retry budget is RESET, not preserved. These attempts were scored
        # against a broken verifier, so the count carries no information about
        # the item — and an item returning to the queue with its attempts
        # already spent (max_attempts defaults to 3) would retire as FAILED
        # without ever having been genuinely tried once.
        row.attempts = 0
        row.awaiting_runs = 0
        row.claimed_by = ""
        row.last_error = DEMOTION_REASON
        row.updated_at = now_iso()

    log.info(
        "drip.repair_verify.summary",
        campaign=name,
        mode="apply" if apply else "dry_run",
        audited=result.audited,
        confirmed=result.confirmed,
        demoted=result.demoted,
        unverifiable=result.unverifiable,
        detail=(
            f"ran, {result.demoted} demoted of {result.audited} done row(s)"
            if result.audited
            else "ran, nothing to check — no item is recorded done"
        ),
    )
    return result


__all__ = ["DEMOTION_REASON", "RepairResult", "repair_false_dones"]
