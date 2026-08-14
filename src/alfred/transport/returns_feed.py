"""The returned-reminder feed card — T2-1's notification half.

WHAT THIS EXISTS FOR. Before it, a snoozed or waiting task whose ``remind_at``
came due did exactly two things: it rewrote its own record (``reminded_at``,
the ruled ``slot:``) and it sent a Telegram message. The record write is
invisible until the operator opens the day plan, and the Telegram leg only
exists on instances whose bot still lives — on a web-only instance
``_send_via_telegram`` returns ``[]``, which the scheduler reads as SUCCESS and
stamps. So the return was CONSUMED and the operator was never told. This module
is the leg that rings: one feed card per fire, dealt into the deck and pushed to
the phone by the existing needs-you doorbell.

THE EMISSION IS NOT THE SEND'S DEPENDENT. :func:`emit_return_card` is called
BEFORE ``send_fn`` and shares no failure exit with it — an emit fault logs loud
and the send still goes; a send fault does not re-emit and does not un-emit.
That independence is the whole point: making the ring conditional on Telegram
would reproduce the sting on exactly the instances that have no Telegram.

THE CARD'S LIFE, and why it is a reconcile rather than an expiry.

A card is emitted when the reminder fires and must retire when the task leaves
returned-state — done, cancelled, or re-snoozed. Nothing else is watching: the
scheduler clears ``remind_at`` at fire time, so the record that produced the
card can no longer be found by the walk that produced it, and a card that
outlives its closed task is the acked-health-card revival bug with a new
costume.

So the sweep reconciles the kind against the STORE'S OWN cards rather than
against the vault. That direction is forced, not chosen: the card's id encodes
the ``remind_at`` VALUE it fired for, and that value is deleted from the record
by the very act of firing — no vault walk can rebuild the id set. Reading the
ids from the store and asking the record "are you still in returned-state?" is
the only question that can be asked in the right order.

Everything else then comes free from :meth:`alfred.feed.store.FeedStore.reconcile`:
absent → ``acted`` (the retirement), a held defer stays held and a lapsed one
returns, ``created_at`` keeps the episode's first-seen, and the belt makes the
whole thing incapable of breaking a scheduler tick.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alfred.feed import (
    KIND_REMINDER_RETURNED,
    FeedEmitHandle,
    FeedItem,
    FeedStore,
    try_feed_reconcile,
)
from alfred.feed.model import STATE_DEFERRED, STATE_OPEN

from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .scheduler import DueReminder

log = get_logger(__name__)

#: Evidence key carrying the fired ``remind_at``. LOAD-BEARING TWICE: it is the
#: second half of the stable key, and it is what lets the sweep tell a genuine
#: re-arm (a new ``remind_at``) from a fire whose send failed before the stamp
#: (the SAME ``remind_at``, still sitting on the record). Without it the sweep
#: would retire, on the next tick, every card whose Telegram send had failed.
EVIDENCE_REMIND_AT = "remind_at"

#: Evidence key carrying the vault-relative record path — the sweep's only route
#: back from a stored card to the task that produced it.
EVIDENCE_RECORD_PATH = "record_path"

_SOURCE_REF = {"producer": "transport_scheduler"}

#: Named for the containment refusal log, per ``vault.paths`` requiring one.
_WRITER = "transport.scheduler.return_card_sweep"

# Why a card was retired. The reconcile's ``acted`` count says HOW MANY; these
# say WHY, which is the difference between "he finished three tasks" and "three
# records went missing". A refusal that cannot say its reason reads identically
# to one firing for an unrelated cause.
RETIRE_TASK_CLOSED = "task_closed"
RETIRE_RE_ARMED = "re_armed"
RETIRE_RECORD_GONE = "record_gone"
RETIRE_NOT_A_TASK = "not_a_task"


def return_card_stable_key(rel_path: str, remind_at_iso: str) -> str:
    """The card's stable key: ``<record path>|<the remind_at it fired for>``.

    NEVER wall-clock, and never the fire time. Both halves are properties of the
    thing rather than of the moment it was noticed, which is what makes a
    retry-tick idempotent: a send that fails leaves ``remind_at`` unstamped, the
    next tick re-derives the identical key, and the upsert folds to a no-op
    instead of dealing a second card for one reminder.

    Including the ``remind_at`` value (rather than the path alone) is what makes
    a genuine re-snooze a NEW card: the operator pushed the task to a new time,
    that is a new return, and it should arrive as its own item.
    """
    return f"{rel_path}|{remind_at_iso}"


def build_return_card(
    entry: "DueReminder", *, instance: str, now: datetime,
) -> FeedItem:
    """Build the ``reminder_returned`` card for one due reminder.

    THE ONE CARD BUILDER — the fire path and the sweep's refresh both come
    through here, so a card cannot be worded one way when it is dealt and
    another way when it is refreshed. That is not hypothetical tidiness: the
    sweep re-derives the slot on every pass precisely so an escalation boundary
    crossed while the card sits open moves the card and the day-plan row
    TOGETHER, and two builders would have made that a place for them to
    disagree.

    Vocabulary is borrowed, never re-spelled:
      * the title is :func:`~alfred.transport.scheduler.render_return_line` —
        the surface-agnostic renderer that already owns ``reminder_text`` /
        chase-framing / due-suffix precedence, and already consumes
        ``tier.slots.chase_phrase``;
      * the slot is :func:`~alfred.transport.scheduler.resolve_return_slot` —
        the escalation-aware resolver shared with the day-plan reader.

    ``starts_at`` is the reminder's own instant (D7 interval extent): a moment,
    so ``ends_at`` stays ``None`` — "no known end", never "ends immediately".
    """
    from .scheduler import classify_return, render_return_line, resolve_return_slot

    slot, slot_rule = resolve_return_slot(entry, now)
    remind_at_iso = entry.remind_at.isoformat()
    evidence: dict[str, Any] = {
        EVIDENCE_RECORD_PATH: entry.rel_path,
        EVIDENCE_REMIND_AT: remind_at_iso,
        "task_title": entry.title,
        # snooze_return / waiting_chase / plain_reminder — the operator-facing
        # difference between "you parked this" and "you are blocked on somebody".
        "return_kind": classify_return(entry),
        "slot": slot or "",
        "slot_rule": slot_rule,
        "waiting_on": entry.waiting_on or "",
        "due": entry.due or "",
        "status": entry.status,
    }
    return FeedItem.create(
        kind=KIND_REMINDER_RETURNED,
        stable_key=return_card_stable_key(entry.rel_path, remind_at_iso),
        instance=instance,
        title=render_return_line(entry),
        evidence=evidence,
        starts_at=remind_at_iso,
        source_ref=dict(_SOURCE_REF),
    )


def emit_return_card(
    handle: FeedEmitHandle | None,
    entry: "DueReminder",
    *,
    now: datetime,
) -> bool:
    """Upsert ONE card for a due reminder. Returns whether it landed.

    NEVER RAISES. The caller runs inside the scheduler's due-loop and must reach
    ``send_fn`` whatever happens here — a feed fault costs the ring, and it must
    not also cost the Telegram message. The failure is LOUD (a warning carrying
    the error type and the record) rather than swallowed, per
    ``feedback_intentionally_left_blank``: a doorbell that stops ringing has no
    signature of its own.

    ``handle is None`` / disabled → ``False`` with no log. The armed-vs-off
    state is announced ONCE at scheduler start, where it is actionable; a
    per-fire skip line would repeat a fact the operator already has.
    """
    if handle is None or not handle.enabled:
        return False
    try:
        card = build_return_card(entry, instance=handle.instance, now=now)
        FeedStore(handle.store_path).upsert(card)
    except Exception as exc:  # noqa: BLE001 — the ring must not cost the send
        log.warning(
            "transport.scheduler.return_card_emit_failed",
            path=entry.rel_path,
            error=str(exc),
            error_type=type(exc).__name__,
            detail=(
                "the returned-reminder card was NOT dealt — the operator will "
                "not be rung for this return. The send below is unaffected."
            ),
        )
        return False
    log.info(
        "transport.scheduler.return_card_emitted",
        path=entry.rel_path,
        feed_id=card.id,
        slot=card.evidence.get("slot", ""),
        return_kind=card.evidence.get("return_kind", ""),
        instance=handle.instance,
    )
    return True


def _rebuild_entry(
    evidence: dict[str, Any], abs_path: Path, meta: dict[str, Any],
) -> "DueReminder | None":
    """Rebuild the ``DueReminder`` a stored card was built from, or ``None``.

    The ``remind_at`` comes from the CARD (the record's was cleared by the fire
    that produced the card); everything else comes from the LIVE record, which
    is what makes the refresh a refresh.

    Takes the caller's ALREADY-GUARDED evidence dict rather than the item, so
    the type belt lives in exactly one place instead of being re-derived (or
    forgotten) per reader.
    """
    from .scheduler import DueReminder, _parse_iso, _str_or_none

    remind_at = _parse_iso(evidence.get(EVIDENCE_REMIND_AT))
    if remind_at is None:
        return None
    rel_path = str(evidence.get(EVIDENCE_RECORD_PATH) or "")
    title = str(meta.get("name") or meta.get("subject") or abs_path.stem).strip()
    return DueReminder(
        abs_path=abs_path,
        rel_path=rel_path,
        title=title,
        remind_at=remind_at,
        due=str(meta["due"]) if meta.get("due") else None,
        reminder_text=(
            str(meta["reminder_text"]).strip() if meta.get("reminder_text") else None
        ),
        status=str(meta.get("status") or "").lower(),
        waiting_on=_str_or_none(meta.get("waiting_on")),
        return_slot=_str_or_none(meta.get("return_slot")),
        escalate_on=_str_or_none(meta.get("escalate_on")),
        escalate_to=_str_or_none(meta.get("escalate_to")),
        reminded_at=_parse_iso(meta.get("reminded_at")),
    )


def _held(stored: FeedItem) -> FeedItem:
    """Pass a card through UNJUDGED — kept open, content not refreshed.

    The doubt path. Used when the record cannot be READ (parse failure, a path
    that will not resolve inside the vault, evidence we cannot key off). A card
    we cannot verify is a card we must not retire: a lingering card is visible
    and can be dismissed, while a card that vanishes because a file briefly
    failed to parse takes the operator's notification with it and leaves no
    trace to ask about.
    """
    return replace(stored, state=STATE_OPEN, deferred_until=None,
                   acted_at=None, acted_action=None)


def _refresh_or_retire(
    stored: FeedItem,
    vault_path: Path,
    now: datetime,
    *,
    instance: str,
    retired: list[tuple[str, str]],
) -> FeedItem | None:
    """One card's verdict: a refreshed OPEN item, or ``None`` to retire it.

    Retires (definite — the state the card describes has demonstrably ended):
      * the record is gone;
      * it is no longer a ``task``;
      * its status left the scheduler's eligible set (done / cancelled);
      * it carries a DIFFERENT ``remind_at`` — a genuine re-snooze, whose own
        return will deal its own card.

    Holds (doubt — see :func:`_held`): unreadable record, unusable evidence, or
    a rebuild whose id does not match the stored one.

    The SAME ``remind_at`` still sitting on the record is deliberately NOT a
    re-arm: it means the fire has not been stamped yet, which is exactly what a
    failed send leaves behind. Reading it as a re-arm would retire, one tick
    later, every card whose send failed — the ring cancelled by the very
    fault it exists to be independent of.
    """
    import frontmatter

    from alfred.vault.paths import VaultContainmentError, resolve_in_vault

    from .scheduler import _ELIGIBLE_STATUSES, _parse_iso

    # ``evidence`` is TYPED as a dict and is not GUARANTEED to be one. The store
    # folds whatever a writer wrote: ``FeedItem.from_dict`` filters unknown
    # FIELDS but never coerces their types, so one malformed line — an older or
    # newer writer, a hand-edited store — hands us a string here, and a bare
    # ``.get`` would raise AttributeError straight through the sweep and out of
    # the scheduler tick, taking that tick's pending-queue drain with it on
    # every tick until the line is removed. ``snapshot_fingerprint`` carries the
    # same read-belt for the same reason.
    evidence = stored.evidence if isinstance(stored.evidence, dict) else {}

    rel_path = str(evidence.get(EVIDENCE_RECORD_PATH) or "").strip()
    if not rel_path:
        log.warning(
            "transport.scheduler.return_card_unkeyed",
            feed_id=stored.id,
            detail="card carries no record_path — held open, cannot be verified",
        )
        return _held(stored)

    try:
        abs_path = resolve_in_vault(vault_path, rel_path, writer=_WRITER)
    except VaultContainmentError:
        # resolve_in_vault already logged the refusal with its reason.
        return _held(stored)

    if not abs_path.is_file():
        retired.append((stored.id, RETIRE_RECORD_GONE))
        return None

    try:
        post = frontmatter.load(str(abs_path))
    except Exception as exc:  # noqa: BLE001 — a parse fault is doubt, not an end
        log.warning(
            "transport.scheduler.return_card_record_unreadable",
            feed_id=stored.id,
            path=rel_path,
            error=str(exc),
            error_type=type(exc).__name__,
            detail="record unreadable — card held open rather than retired",
        )
        return _held(stored)

    meta = dict(post.metadata or {})
    if meta.get("type") != "task":
        retired.append((stored.id, RETIRE_NOT_A_TASK))
        return None
    if str(meta.get("status") or "").lower() not in _ELIGIBLE_STATUSES:
        retired.append((stored.id, RETIRE_TASK_CLOSED))
        return None

    card_remind_at = _parse_iso(evidence.get(EVIDENCE_REMIND_AT))
    record_remind_at = _parse_iso(meta.get("remind_at"))
    if record_remind_at is not None and record_remind_at != card_remind_at:
        retired.append((stored.id, RETIRE_RE_ARMED))
        return None

    entry = _rebuild_entry(evidence, abs_path, meta)
    if entry is None:
        log.warning(
            "transport.scheduler.return_card_unkeyed",
            feed_id=stored.id,
            path=rel_path,
            detail="card carries no readable remind_at — held open, not refreshed",
        )
        return _held(stored)

    refreshed = build_return_card(entry, instance=instance, now=now)
    if refreshed.id != stored.id:
        # POSITIVE identity or nothing. Two cards agreeing about a record is not
        # evidence they are the same card; only the id is. A mismatch means the
        # key derivation moved under a stored item, and folding a refresh onto
        # it would silently rewrite one card's content into another's identity.
        log.warning(
            "transport.scheduler.return_card_id_drift",
            feed_id=stored.id,
            rebuilt_id=refreshed.id,
            path=rel_path,
            detail="rebuilt id does not match the stored one — held, not refreshed",
        )
        return _held(stored)
    return refreshed


def sweep_return_cards(
    handle: FeedEmitHandle | None,
    vault_path: Path,
    now: datetime,
) -> dict[str, int] | None:
    """Reconcile the open ``reminder_returned`` cards against their records.

    Returns the reconcile counts, or ``None`` when nothing ran (feed off, or no
    live cards to reconcile — the ordinary state on most ticks).

    Runs through :func:`~alfred.feed.belt.try_feed_reconcile`, so a store fault
    degrades to "no retirement this tick" and can never wedge the scheduler
    loop. The belt also emits the ``feed.reconcile`` ILB line with the counts,
    which is why there is no zero-line here: a tick with live cards logs the
    reconcile, and a tick with none is the quiet case the scheduler's own
    ``starting`` line already accounts for.

    ACTED AND ACKED CARDS ARE NOT PASSED. Only ``open`` / ``deferred`` cards go
    into the reconcile's open set, which keeps a decided card decided —
    ``reconcile`` upserts every item it is handed at ``state=open``, so handing
    it an acked card would revive it on the next tick, every tick, forever. That
    is the groundhog bug this codebase has already fixed twice, and it is one
    list comprehension away at all times.
    """
    if handle is None or not handle.enabled:
        return None
    store = FeedStore(handle.store_path)
    try:
        stored_items = store.load()
    except Exception as exc:  # noqa: BLE001 — a read fault must not wedge the tick
        log.warning(
            "transport.scheduler.return_card_sweep_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    live = [
        item for item in stored_items.values()
        if item.kind == KIND_REMINDER_RETURNED
        and item.state in (STATE_OPEN, STATE_DEFERRED)
    ]
    if not live:
        return None

    retired: list[tuple[str, str]] = []
    open_items: list[FeedItem] = []
    for stored in sorted(live, key=lambda i: i.id):
        try:
            verdict = _refresh_or_retire(
                stored, vault_path, now, instance=handle.instance, retired=retired,
            )
        except Exception as exc:  # noqa: BLE001 — one bad card, not the tick
            # PER-CARD, and the scope is the point. Anything raising out of here
            # would leave the sweep, leave ``_tick``, and be caught only by the
            # loop-level handler in ``run`` — which means the pending-queue
            # drain at the bottom of that tick never runs, every tick, for as
            # long as the offending card sits in the store. A feed fault taking
            # down a scheduler leg is exactly what ``feed/belt.py`` exists to
            # make impossible.
            #
            # The card is HELD, matching every other doubt path here: a card we
            # could not judge is not a card we may retire.
            log.warning(
                "transport.scheduler.return_card_verdict_failed",
                feed_id=stored.id,
                error=str(exc),
                error_type=type(exc).__name__,
                detail="card held open — could not be verified this sweep",
            )
            verdict = _held(stored)
        if verdict is not None:
            open_items.append(verdict)

    counts = try_feed_reconcile(store, KIND_REMINDER_RETURNED, open_items)
    if retired:
        # The reconcile's ``acted`` count says how many; this says WHY, per
        # record. "He finished two tasks" and "two records went missing" are the
        # same number and completely different news.
        log.info(
            "transport.scheduler.return_cards_retired",
            count=len(retired),
            retired=[{"id": fid, "reason": reason} for fid, reason in retired],
            detail="card's task left returned-state — retired from the deck",
        )
    return counts
