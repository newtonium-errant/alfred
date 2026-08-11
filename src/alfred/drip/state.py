"""Campaign cursor + state — the half that must never lose work.

#44, spec: ``~/reports/drip-drain-design-2026-08-04.md`` §3.

## The incident this exists to not repeat

On 2026-07-26 a Gmail-backlog drain marked **907 items DONE that produced no
vault record**. The mechanism is still in the tree as a legacy escape hatch
(``curator/daemon.py``, the ``on_failure.action == "processed"`` branch)::

    state_mgr.state.mark_processed(
        filename=filename,
        files_created=[],          # ← marked DONE having produced nothing
        backend_used="failed_legacy_processed",
    )

``files_created=[]`` IS the incident. It is also, verbatim, the question
:class:`CampaignState` refuses to skip: *did this item produce an observable
effect?* The verifier here is not analogous to the check that was missing — it
is a generalization of the field that was empty.

Quota exhaustion merely produced 907 failures at once; the LOSS came from
marking work done on a failure. So budget (``drip/budget.py``) and
never-lose-work (this module) are separate concerns and are kept in separate
files on purpose.

## The lifecycle

``pending → in_flight → done | failed | blocked``

Three rules, each closing a specific hole:

1. **``in_flight`` is persisted BEFORE the work runs.** A process killed
   mid-item leaves a visible ``in_flight``, never a silently skipped item.
2. **``done`` requires a VERIFIER, not a return code.** A worker that returns
   success without an observable effect leaves the item ``failed``.
3. **Re-queue runs ``verify()`` FIRST.** A crash *after* the effect landed and
   *before* the state write left a genuinely-done item ``in_flight``; re-running
   it would duplicate the work — the 907 inverted. Verifying first settles
   resumption with the machinery already present for rule 2.

And the requirement that makes rule 3 safe: **``verify()`` must key on the ITEM
ID.** A verifier asking "did any record get created?" is satisfied by an earlier
run's output and would mark an untouched item done — the 907 again, wearing the
verifier's own uniform. That is a contract on campaigns (``drip/campaigns.py``),
enforced by pins, because it cannot be enforced by a type.

**A campaign's ``verify()`` must also NOT be the negation of its work-list's
admission test.** Learned in the field on 2026-08-04: #40's neutralize pass used
the inverse of its selector and over-reached onto ~914 genuine records, because
failing an admission test means *unproven*, not *disproven*. The selector asks
"should this item be worked?"; the verifier asks "did the work land?" — different
questions about different moments.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .config import DripConfigError

log = structlog.get_logger(__name__)

# --- item lifecycle ---------------------------------------------------------
PENDING = "pending"
IN_FLIGHT = "in_flight"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"

#: Closed set. Callers switch on these; a new state is a contract change.
ITEM_STATES: frozenset[str] = frozenset(
    {PENDING, IN_FLIGHT, DONE, FAILED, BLOCKED}
)

#: States that will be attempted again on a later run.
RETRIABLE_STATES: frozenset[str] = frozenset({PENDING, FAILED, BLOCKED, IN_FLIGHT})


@dataclass
class ItemState:
    """One work-list item's progress. Keyed by a campaign-stable ``item_id``."""

    item_id: str
    state: str = PENDING
    #: FAILED attempts only. ``blocked`` never increments this — a quota block
    #: is a pause, not a verdict about the item, so a multi-day outage cannot
    #: silently burn an item's retry budget on the operator's billing cycle.
    attempts: int = 0
    last_error: str = ""
    #: The run that most recently claimed this item. An ``in_flight`` row whose
    #: ``claimed_by`` is not the current run is a crash leftover, not a peer's
    #: live work — the single-writer assumption below makes that safe to say.
    claimed_by: str = ""
    #: ASYNC campaigns only. A dispatched item (handed to a daemon that will do
    #: the work later) stays ``in_flight`` until a LATER run observes its
    #: effect. This counts how many runs it has been awaiting, so a dispatch
    #: that never lands becomes FAILED instead of sitting invisible forever —
    #: which would be the 907 shape with extra steps.
    awaiting_runs: int = 0
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ItemState":
        """Schema-tolerant construct (the load() contract). A row written by a
        newer runner loads on rollback; an older one loads forward. A campaign
        that cannot be resumed across a version change is not resumable in the
        sense this feature needs."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class CampaignState:
    """Persistent cursor for one campaign on one instance.

    SINGLE WRITER per (instance, campaign) — guaranteed by the state path
    (:func:`campaign_state_path`), not by a lock. See that function for why the
    instance scoping is load-bearing rather than tidy.
    """

    campaign: str = ""
    items: dict[str, ItemState] = field(default_factory=dict)
    #: ISO date → count of items that consumed budget that day. Weekly spend is
    #: summed over this rather than stored, so a stale counter can't drift from
    #: the record it is supposed to summarize.
    spend_by_day: dict[str, int] = field(default_factory=dict)
    last_run_at: str = ""
    last_stop_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "CampaignState":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        raw_items = known.pop("items", {}) or {}
        state = cls(**known)
        if isinstance(raw_items, dict):
            for item_id, row in raw_items.items():
                if isinstance(row, dict):
                    state.items[str(item_id)] = ItemState.from_dict(row)
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign": self.campaign,
            "items": {
                k: {
                    "item_id": v.item_id,
                    "state": v.state,
                    "attempts": v.attempts,
                    "last_error": v.last_error,
                    "claimed_by": v.claimed_by,
                    "awaiting_runs": v.awaiting_runs,
                    "updated_at": v.updated_at,
                }
                for k, v in self.items.items()
            },
            "spend_by_day": dict(self.spend_by_day),
            "last_run_at": self.last_run_at,
            "last_stop_reason": self.last_stop_reason,
        }

    # --- queries ----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in sorted(ITEM_STATES)}
        for item in self.items.values():
            out[item.state] = out.get(item.state, 0) + 1
        return out

    def remaining(self, worklist_ids: list[str]) -> int:
        """Everything not yet ``done`` — the union of the work-list and any
        item still tracked in state.

        The union matters for campaigns whose ``work()`` removes the item from
        its own work-list (``gmail_backlog`` moves the file out of staging). If
        this counted the work-list alone, a dispatched-but-unverified item
        would vanish from ``remaining`` and the campaign would report itself
        COMPLETE while items were still awaiting confirmation — a progress
        number that lies in the reassuring direction.

        Items never seen still count, so a campaign cannot report completion
        before it starts.
        """
        ids = set(worklist_ids) | {
            iid for iid, it in self.items.items() if it.state != DONE
        }
        return sum(
            1 for iid in ids
            if self.items.get(iid, ItemState(iid)).state != DONE
        )

    def spend_in_week_ending(self, today: date) -> int:
        """Rolling 7-day spend (inclusive of today).

        Rolling rather than calendar-week: the quota that bit on 2026-07-26 is a
        weekly allowance, and a calendar reset would let a campaign spend a full
        week's budget on Sunday and another on Monday.
        """
        total = 0
        for iso, n in self.spend_by_day.items():
            try:
                d = date.fromisoformat(iso)
            except ValueError:
                continue
            if 0 <= (today - d).days < 7:
                total += int(n or 0)
        return total


def drip_instance_dir(data_dir: Path | str, instance: str) -> Path:
    """``<data_dir>/drip/<instance-slug>`` — one instance's whole drip state.

    Tool-scoped AND instance-scoped, per the CLAUDE.md state contract. Both
    matter, for different reasons:

    * **tool-scoped** (``drip/``) so a cursor is never confused with another
      tool's state file;
    * **instance-scoped** because on the box every instance shares ONE
      WorkingDirectory and differs only by ``--config``. A cwd-relative
      tool-scoped default is still one shared file across Salem / KAL-LE /
      Hypatia — the 2026-07-31 feed-store incident exactly.

    The collision here would be worse than the feed-store one. This state is a
    *cursor over a work-list*: two instances sharing it would each advance the
    other's cursor past items neither processed — a silent-loss generator with
    no failure anywhere to notice. Hence single-writer-by-path, and hence
    ``instance`` is required rather than defaulted.

    Extracted from :func:`campaign_state_path` (#83) so the run lock lands in
    the SAME directory by construction rather than by a second copy of the slug
    rule. That is a correctness coupling, not tidiness: a lock derived from a
    drifted slug would guard a directory no runner writes to, and would fail
    OPEN — silently — which is the one failure mode a lock must not have.
    """
    slug = (instance or "").strip().lower().replace(" ", "-")
    if not slug:
        # Fail loud: a shared/unnamed path is the incident above.
        #
        # #66: DripConfigError, not a bare ValueError. The refusal is correct
        # and stays correct — what changed is that it now carries a type the
        # CLI boundary already handles, so the operator gets "Drip: ..." and
        # exit 2 instead of a stack dump for a missing line of YAML. Typed at
        # the SOURCE rather than caught at each of the three call sites, so a
        # fourth caller cannot reintroduce the traceback by forgetting.
        # Still a ValueError subclass — existing ValueError handlers unaffected.
        raise DripConfigError(
            "drip state needs an instance name — an unscoped path is "
            "shared across instances on the box and silently corrupts cursors"
        )
    return Path(data_dir) / "drip" / slug


def campaign_state_path(
    data_dir: Path | str, instance: str, campaign: str,
) -> Path:
    """``<data_dir>/drip/<instance>/<campaign>_state.json``.

    See :func:`drip_instance_dir` for why both scopings are load-bearing.
    """
    return drip_instance_dir(data_dir, instance) / f"{campaign}_state.json"


def load_state(path: Path | str, campaign: str) -> CampaignState:
    """Load (schema-tolerant). Missing or corrupt → a fresh state, logged.

    A corrupt cursor must not crash the runner, but it MUST be loud: silently
    starting from zero would re-run an entire campaign, which for a worklist
    with irreversible items (link001 removal) is the expensive direction.
    """
    p = Path(path)
    if not p.exists():
        return CampaignState(campaign=campaign)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
    except (ValueError, OSError) as exc:
        log.warning(
            "drip.state.load_failed",
            path=str(p), campaign=campaign, error=str(exc),
            consequence="starting from an empty cursor — items already done "
                        "will be re-verified, not blindly re-run",
        )
        return CampaignState(campaign=campaign)
    state = CampaignState.from_dict(data)
    state.campaign = state.campaign or campaign
    return state


def save_state(path: Path | str, state: CampaignState) -> None:
    """Atomic write (.tmp → rename), per the state contract.

    Atomicity is not decoration here: a torn cursor file is the one artifact
    whose corruption loses the record of what has already been done.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        with contextlib_suppress():
            os.unlink(tmp)
        raise


class contextlib_suppress:
    """Tiny local suppressor — avoids an import just for cleanup-on-error."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "BLOCKED",
    "DONE",
    "FAILED",
    "IN_FLIGHT",
    "ITEM_STATES",
    "PENDING",
    "RETRIABLE_STATES",
    "CampaignState",
    "ItemState",
    "campaign_state_path",
    "drip_instance_dir",
    "load_state",
    "now_iso",
    "save_state",
]
