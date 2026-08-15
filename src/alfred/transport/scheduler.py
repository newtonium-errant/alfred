"""In-process scheduler hosted inside the talker daemon.

Fires ``remind_at`` reminders on task records and drains due entries
from the transport state's ``pending_queue``. Runs as a sibling
asyncio task alongside the aiohttp server and PTB's long-poller.

Responsibilities, one tick every ``scheduler.poll_interval_seconds``:

1. Walk the vault's ``task/`` tree for records whose
   ``remind_at`` is in the past and that haven't been reminded for
   this remind_at value yet. Dispatch via the send callable. Stamp
   ``reminded_at``, clear ``remind_at``, append an
   ``<!-- ALFRED:REMINDER ... -->`` body comment for audit.
2. Drain the pending-queue (``state.pop_due(now)``). These are sends
   the server parked with a future ``scheduled_at`` — now their time
   has come.

Reminders whose ``remind_at`` is older than
``scheduler.stale_reminder_max_minutes`` are dead-lettered instead of
fired. Rationale: a daemon that was down for two days should NOT spit
out 48 hours of accumulated reminders on restart — the user won't
remember the context and the spam buries newer signals.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

from .config import TransportConfig
from .exceptions import TELEGRAM_UNAVAILABLE_REASON, TelegramUnavailable
from .returns_feed import emit_return_card, sweep_return_cards
from .state import TransportState
from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from alfred.feed import FeedEmitHandle

log = get_logger(__name__)


# Types that the scheduler inspects for remind_at. Only ``task``
# records today — other types might grow reminder support later, at
# which point the filter broadens.
_REMINDER_TYPES: set[str] = {"task"}

# Task statuses that are eligible for reminders. Done / cancelled
# tasks keep any residual ``remind_at`` in the frontmatter as a
# historical record, but the scheduler does not fire on them.
_ELIGIBLE_STATUSES: set[str] = {"todo", "active"}

# Grace window for ``remind_at`` values that land in the past.
#
# A reminder whose ``remind_at`` is at most this many seconds in the
# past at scheduler-tick time fires normally. The grace covers two
# legitimate cases:
#   * Clock skew between the writer and the scheduler tick.
#   * "Remind me right now" semantics — a task created with
#     ``remind_at = now()`` will land a few hundred ms in the past by
#     the time the next tick sees it.
#
# Anything older than the grace is REFUSED — never fires, never
# stamps ``reminded_at``, never dead-letters. Per
# ``feedback_intentionally_left_blank.md``: silence here is the bug
# the QA pass caught (Salem set ``remind_at`` 6 days in the past,
# scheduler bucketed as stale, ``clear_remind_at_and_stamp`` consumed
# it without notifying Andrew). The refusal path emits a warning log
# every tick the task is seen — recurring noise is the right signal
# until the operator repairs the date.
_REMIND_AT_PAST_GRACE_SECONDS: int = 60


# The body comment signature the scheduler appends after a successful
# reminder dispatch. One line per reminder so the audit trail is
# grep-able and easy to render as a list in a task record's body.
_REMINDER_COMMENT_RE = re.compile(r"<!-- ALFRED:REMINDER [^>]+-->")


# ---------------------------------------------------------------------------
# Return kinds (Phase 2c+h)
# ---------------------------------------------------------------------------
#
# A due reminder returns in one of three shapes. The discriminator is
# WHICH FIELD the record carries, never the presence of ``remind_at``.
#
# That distinction is load-bearing and has already been mismeasured
# once: ``remind_at`` is the *trigger* every reminder shares, so it
# cannot separate the kinds. As of the 2026-08-14 routing pass the vault
# holds 7 task records carrying ``remind_at`` — 4 snoozes (``return_slot``),
# 2 waiting-chases (``waiting_on``), and 1 carrying NEITHER
# (``Order Fergus Tick Meds from Vet``, a residual reminder on a closed
# task). A two-way "snooze vs waiting" split would misfile that third
# class; hence three kinds, with ``plain`` a first-class answer rather
# than a fallback bucket.
RETURN_KIND_SNOOZE = "snooze_return"
RETURN_KIND_WAITING = "waiting_chase"
RETURN_KIND_PLAIN = "plain_reminder"

ALL_RETURN_KINDS: frozenset[str] = frozenset(
    {RETURN_KIND_SNOOZE, RETURN_KIND_WAITING, RETURN_KIND_PLAIN}
)


def classify_return(entry: DueReminder) -> str:
    """Classify HOW a due reminder returns. Never raises, never guesses.

    Precedence:
      1. ``return_slot`` present  → :data:`RETURN_KIND_SNOOZE`
      2. ``waiting_on`` present   → :data:`RETURN_KIND_WAITING`
      3. neither                  → :data:`RETURN_KIND_PLAIN`

    **``remind_at`` is deliberately not consulted.** Every due reminder
    has one by construction, so reading it here would classify every
    record into the same bucket while looking like a real test. The
    fields above are what the operator's ruling actually wrote.

    Rule 1 outranks rule 2 for the both-fields case: a snooze that also
    records who it is waiting on is still a snooze return — the operator
    set a slot for it, and an explicitly-set slot is the more deliberate
    statement. The case does not occur in the vault today; the
    precedence is fixed here anyway, because a precedence that depends
    on data being well-formed is not a precedence.
    """
    if _nonempty(entry.return_slot):
        return RETURN_KIND_SNOOZE
    if _nonempty(entry.waiting_on):
        return RETURN_KIND_WAITING
    return RETURN_KIND_PLAIN


def _nonempty(value: Any) -> bool:
    """True when a frontmatter value is a meaningful string.

    Frontmatter round-trips absent fields as ``None`` and blanked ones
    as ``""``; both mean "operator did not set this", so both must read
    as absent or an empty string would classify a plain reminder as a
    chase against nobody.
    """
    return isinstance(value, str) and bool(value.strip())


def _str_or_none(value: Any) -> str | None:
    """Coerce a frontmatter scalar to a trimmed string, or ``None``.

    PyYAML hands back ``date`` objects for unquoted ISO dates and
    ``None`` for absent keys, so the return-kind fields arrive in mixed
    shapes. Everything meaningful becomes a string here; absent and
    blank both become ``None`` so downstream ``_nonempty`` checks see
    one representation of "operator did not set this".
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# Callable shape — same as the server's SendCallable.
SendCallable = Callable[..., Awaitable[list[int]]]


@dataclass
class DueReminder:
    """A task record whose ``remind_at`` is due and eligible to fire."""

    abs_path: Path
    rel_path: str
    title: str
    remind_at: datetime
    due: str | None
    reminder_text: str | None
    status: str

    # --- Phase 2c+h return-kind fields -----------------------------------
    # Written by the 2026-08-14 routing pass; read here to decide HOW a
    # reminder returns, never WHETHER it returns. Defaulted so existing
    # positional construction (tests, older callers) keeps working.
    waiting_on: str | None = None
    return_slot: str | None = None
    escalate_on: str | None = None
    escalate_to: str | None = None
    # The PREVIOUS ``reminded_at`` stamp, when this record has fired
    # before and was re-armed by a new ``remind_at``. ``None`` on a
    # first fire. This is the re-snooze correction signal (scope item 5).
    reminded_at: datetime | None = None


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp or return ``None`` on any error.

    Tolerates the ``Z`` shorthand and missing timezone info (treated
    as UTC). This is the one-way-door parser for ``remind_at`` —
    malformed values are surfaced to the operator via logging, not
    raised.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # ``date`` (no time) is a common shorthand — interpret as
        # midnight UTC. Frontmatter may produce a date object.
        if len(s) == 10 and s.count("-") == 2:
            s = s + "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def find_due_reminders(
    vault_path: Path,
    now: datetime,
    stale_max_minutes: int,
) -> tuple[
    list[DueReminder], list[DueReminder], list[DueReminder], list[DueReminder]
]:
    """Walk ``vault_path/task/**/*.md`` and classify each record.

    Returns ``(due, stale, refused_past_time, skipped_closed)``:
      * ``due`` — fires normally; scheduler dispatches + stamps
        ``reminded_at``.
      * ``stale`` — older than ``stale_max_minutes`` but inside the
        legitimate-past window (operator opted into reminders that
        accumulated during a daemon outage); dead-letters + stamps
        so they don't re-fire.
      * ``refused_past_time`` — ``remind_at`` is more than
        ``_REMIND_AT_PAST_GRACE_SECONDS`` in the past (suspicious;
        the writer almost certainly miscalculated). The scheduler
        does NOT dispatch, dead-letter, or stamp these. Operator
        sees a recurring warning log until the task is repaired
        (delete or correct ``remind_at``). Closes the QA-finding bug
        where Salem wrote ``remind_at`` 6 days in the past and the
        scheduler silently consumed it.
      * ``skipped_closed`` — the reminder came due but the task is
        already ``done`` / ``cancelled``. NOT fired, NOT stamped: a
        closed task keeps its residual ``remind_at`` as history, which
        is deliberate. This list exists so the skip is *reportable*
        rather than silent — before Phase 2c+h the status filter was a
        bare ``continue``, so a return that arrived after the operator
        had already finished the task left no trace anywhere. Per
        ``feedback_intentionally_left_blank.md``: "nothing to do" and
        "broken" must not look the same. Live instance at the time of
        writing: ``task/Order Fergus Tick Meds from Vet.md``.

    All four lists are computed in one vault walk so the scheduler
    can pick any outcome without a second pass.

    Eligibility (for the first three lists):
    - ``type == "task"``
    - ``status in {"todo", "active"}``
    - ``remind_at`` present and parseable
    - Either ``reminded_at`` absent OR ``reminded_at < remind_at``
      (so updating a task's ``remind_at`` to a new time re-arms it)

    ``skipped_closed`` is the same minus the status rule, plus
    ``remind_at <= now`` — a *future* reminder on a closed task is not
    a skip yet, it is simply not due.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return [], [], [], []

    due: list[DueReminder] = []
    stale: list[DueReminder] = []
    refused_past_time: list[DueReminder] = []
    skipped_closed: list[DueReminder] = []
    stale_cutoff = now - timedelta(minutes=stale_max_minutes)
    past_grace_cutoff = now - timedelta(seconds=_REMIND_AT_PAST_GRACE_SECONDS)

    for md_path in task_dir.rglob("*.md"):
        try:
            post = frontmatter.load(str(md_path))
        except Exception as exc:  # noqa: BLE001 — tolerate parse errors
            log.warning(
                "transport.scheduler.frontmatter_parse_failed",
                path=str(md_path),
                error=str(exc),
            )
            continue

        meta = post.metadata or {}
        if meta.get("type") not in _REMINDER_TYPES:
            continue
        status = str(meta.get("status") or "").lower()

        remind_at = _parse_iso(meta.get("remind_at"))
        if remind_at is None:
            continue

        reminded_at = _parse_iso(meta.get("reminded_at"))
        if reminded_at is not None and reminded_at >= remind_at:
            # Already fired for this value of remind_at — skip.
            continue

        if remind_at > now:
            continue  # Not yet due.

        try:
            rel_path = str(md_path.relative_to(vault_path))
        except ValueError:
            rel_path = md_path.name

        title = (
            str(meta.get("name") or meta.get("subject") or md_path.stem)
            .strip()
        )
        entry = DueReminder(
            abs_path=md_path,
            rel_path=rel_path,
            title=title,
            remind_at=remind_at,
            due=str(meta["due"]) if meta.get("due") else None,
            reminder_text=(
                str(meta["reminder_text"]).strip()
                if meta.get("reminder_text")
                else None
            ),
            status=status,
            waiting_on=_str_or_none(meta.get("waiting_on")),
            return_slot=_str_or_none(meta.get("return_slot")),
            escalate_on=_str_or_none(meta.get("escalate_on")),
            escalate_to=_str_or_none(meta.get("escalate_to")),
            reminded_at=reminded_at,
        )

        # Status gate — deliberately BELOW entry construction as of
        # Phase 2c+h, so a reminder skipped because its task is already
        # closed can be reported with the same shape as every other
        # bucket instead of vanishing at a bare ``continue``.
        #
        # The checks above are unchanged and still run first, so the
        # three original buckets keep identical eligibility: a closed
        # task with an unparseable, future, or already-fired
        # ``remind_at`` still falls out exactly where it did before and
        # lands in no bucket. The only behaviour that changed is the
        # closed-AND-due-AND-unfired case, which previously produced
        # nothing at all.
        if status not in _ELIGIBLE_STATUSES:
            skipped_closed.append(entry)
            continue

        # Past-time refusal takes precedence over the stale-window
        # bucket. A reminder more than the grace in the past is
        # almost certainly a writer error (Salem's QA finding:
        # ``remind_at`` 6 days in the past). The stale path would
        # consume + dead-letter it; refusal preserves the task's
        # un-fired state so the operator can repair the date.
        #
        # Note: with the new past-grace cutoff (60s) being narrower
        # than the stale cutoff (default 3h), the stale branch below
        # is reachable in practice only if an operator tightens
        # stale_max_minutes below 1 — which would itself be a
        # config bug. The branch is preserved (rather than removed)
        # so the bucketing scaffolding stays intact + a future widen
        # of the past-grace window doesn't silently re-enable stale
        # consumption. ``feedback_intentionally_left_blank.md``
        # principle: prefer explicit-and-redundant over deleted-
        # because-unreachable.
        if remind_at < past_grace_cutoff:
            refused_past_time.append(entry)
        elif remind_at < stale_cutoff:
            stale.append(entry)
        else:
            due.append(entry)

    return due, stale, refused_past_time, skipped_closed


def render_return_line(entry: DueReminder) -> str:
    """Render a due reminder as one line of operator-facing text.

    SURFACE-AGNOSTIC. This is the one place a returning reminder is put
    into words, so the wording is not owned by any single channel. The
    Telegram path reaches it through :func:`format_reminder`; the deck /
    PWA reader calls it directly. Two surfaces rendering the same return
    differently is a bug that only shows up in front of the operator.

    The record stays the source of truth: this function DERIVES the line
    from ``waiting_on`` and the title rather than the fire path stamping
    a rendered string onto the record. A stamped string would go stale
    the moment ``waiting_on`` changed, and there is no way to tell a
    stale stamp from a current one by looking at it.

    Precedence (per ratified recommendation 3, extended by Phase 2c+h):

    1. ``reminder_text`` field if present and non-empty — verbatim.
    2. waiting-chase framing when the record carries ``waiting_on``.
    3. ``"Reminder: {title} (due {due})"`` when ``due`` is present.
    4. ``"Reminder: {title}"`` otherwise.

    Rule 1 still outranks the chase framing: an operator who wrote
    ``reminder_text`` by hand said exactly what they wanted the message
    to be, and a generated frame must not overwrite it.

    A waiting item is not a snooze that came back — the task was never
    parked, it is blocked on somebody else. Wording it as a chase
    ("Chase Carfax: ...") tells the operator what the next physical
    action is; "Reminder: Fix Carfax Mileage Discrepancy" does not, and
    the two were indistinguishable before this change.
    """
    if entry.reminder_text:
        return entry.reminder_text
    # Vocabulary from the shared helper; capitalisation and the
    # full-line shape are this surface's presentation.
    from alfred.tier.slots import chase_phrase

    phrase = chase_phrase(entry.waiting_on)
    if phrase is not None:
        opener = phrase[:1].upper() + phrase[1:]
        if entry.due:
            return f"{opener}: {entry.title} (due {entry.due})"
        return f"{opener}: {entry.title}"
    if entry.due:
        return f"Reminder: {entry.title} (due {entry.due})"
    return f"Reminder: {entry.title}"


def format_reminder(entry: DueReminder) -> str:
    """Telegram message body for a due reminder.

    Delegates to :func:`render_return_line`. Kept as its own name
    because the send path and its tests have called it since the
    scheduler shipped, and because the Telegram transport is being
    retired: when that happens this wrapper goes and the renderer
    stays, since the wording was never Telegram's to own.
    """
    return render_return_line(entry)


def resolve_return_slot(
    entry: DueReminder, now: datetime,
) -> tuple[str | None, str]:
    """Resolve which slot a returning task should land in.

    Returns ``(canonical_slot_or_None, rule)``. The rule is the "why",
    for logging: ``return_slot`` / ``escalated`` / ``none`` /
    ``unrecognized``.

    Escalation is GENERIC, not septic-special: any record carrying
    ``escalate_on`` in the past AND ``escalate_to`` returns the
    escalated slot instead of its ordinary one. Septic is simply the
    first carrier.

    A malformed ``escalate_on`` does NOT escalate — the ordinary slot
    still applies. Escalation is the sharper outcome (it moves work
    into Duty), so an unparseable date must not be able to trigger it;
    failing back to the calmer answer is the safe direction.
    """
    from alfred.tier.slots import resolve_effective_slot

    # Delegates to the ONE spelling of the escalation rule, shared with
    # the returned-task reader in ``tier.compute``. See that function's
    # docstring for why fire-time and read-time both need it.
    return resolve_effective_slot(
        return_slot=entry.return_slot,
        escalate_on=entry.escalate_on,
        escalate_to=entry.escalate_to,
        today=now.date(),
    )


def clear_remind_at_and_stamp(entry: DueReminder, now: datetime) -> None:
    """Mark a task as reminded: clear ``remind_at``, stamp ``reminded_at``,
    append an ``<!-- ALFRED:REMINDER -->`` body comment.

    We write through ``frontmatter.load`` + ``frontmatter.dumps`` so
    other frontmatter fields stay untouched and YAML quoting is
    preserved. The audit comment goes at the tail of the body so the
    user-authored content stays at the top of the rendered task.
    """
    post = frontmatter.load(str(entry.abs_path))
    post.metadata.pop("remind_at", None)
    post.metadata["reminded_at"] = now.isoformat()

    # Phase 2c+h — the return lands in its ruled slot.
    #
    # This is the DELIVERY step, not a bookkeeping one. Writing ``slot:``
    # here feeds rule 1 of the day-plan classifier (``explicit_slot`` —
    # the operator's own word, highest precedence), so the deck's own
    # projection deals the returning task into the right slot on its
    # next pass. No parallel card emitter, no second dealing path.
    #
    # It is written on the SAME frontmatter round-trip that clears
    # ``remind_at``, so a task can never end up reminded-but-unslotted
    # (or slotted-but-still-armed) through a partial write.
    slot, slot_rule = resolve_return_slot(entry, now)
    if slot is not None:
        post.metadata["slot"] = slot
        log.info(
            "transport.scheduler.return_slot_written",
            path=entry.rel_path,
            slot=slot,
            rule=slot_rule,
            return_slot=entry.return_slot,
            escalate_to=entry.escalate_to,
            escalate_on=entry.escalate_on,
        )
    elif slot_rule == "unrecognized":
        # normalize_slot already warned with the offending value; this
        # line records the CONSEQUENCE at the point of loss — the
        # operator's ruling did not land on this record.
        log.warning(
            "transport.scheduler.return_slot_not_applied",
            path=entry.rel_path,
            return_slot=entry.return_slot,
            escalate_to=entry.escalate_to,
            hint=(
                "return_slot did not resolve to a canonical slot — the "
                "task returns unslotted rather than where it was ruled."
            ),
        )

    audit_line = (
        f"<!-- ALFRED:REMINDER fired_at={now.isoformat()} "
        f"remind_at={entry.remind_at.isoformat()} -->"
    )
    body = post.content or ""
    if audit_line not in body:
        body = body.rstrip() + "\n\n" + audit_line + "\n"
    post.content = body

    rendered = frontmatter.dumps(post)
    entry.abs_path.write_text(rendered, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run(
    config: TransportConfig,
    state: TransportState,
    send_fn: SendCallable,
    vault_path: Path,
    user_id: int,
    shutdown_event: asyncio.Event | None = None,
    feed_handle: "FeedEmitHandle | None" = None,
) -> None:
    """Scheduler loop — fire due reminders and drain scheduled pending sends.

    Returns when ``shutdown_event`` is set. Exceptions inside a tick
    are caught and logged so one bad record cannot wedge the loop.

    ``feed_handle`` is the returned-reminder ring (T2-1): present → every fire
    also deals a ``reminder_returned`` card, which is what reaches the deck and
    the phone. ``None`` → the record write and the Telegram send are the only
    legs, i.e. the pre-T2-1 behaviour.
    """
    interval = max(1, int(config.scheduler.poll_interval_seconds))
    stale_max = int(config.scheduler.stale_reminder_max_minutes)

    log.info(
        "transport.scheduler.starting",
        poll_interval_seconds=interval,
        stale_reminder_max_minutes=stale_max,
        user_id=user_id,
        # ILB, and at INFO because a doorbell that was never armed has no
        # signature of its own — the failure looks exactly like "no reminders
        # came due". This is the one line that distinguishes them, so it says
        # which state it is on BOTH outcomes rather than only on the good one.
        feed_ring=(
            "armed" if feed_handle is not None and feed_handle.enabled else "off"
        ),
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            log.info("transport.scheduler.stopped")
            return
        try:
            await _tick(
                config, state, send_fn, vault_path, user_id,
                feed_handle=feed_handle,
            )
        except Exception:  # noqa: BLE001 — loop must survive
            log.exception("transport.scheduler.tick_error")

        # Sleep in a way that responds to shutdown within poll_interval.
        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                log.info("transport.scheduler.stopped")
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval)


async def _tick(
    config: TransportConfig,
    state: TransportState,
    send_fn: SendCallable,
    vault_path: Path,
    user_id: int,
    *,
    feed_handle: "FeedEmitHandle | None" = None,
) -> None:
    """Run one scheduler pass — due reminders, stale reminders, pending queue."""
    now = datetime.now(timezone.utc)

    # 1) Task-record reminders.
    due, stale, refused_past_time, skipped_closed = find_due_reminders(
        vault_path, now, config.scheduler.stale_reminder_max_minutes,
    )

    # Reminders that came due on an already-closed task. Not an error
    # and not repairable — a done task legitimately keeps its residual
    # ``remind_at`` — so this is INFO, not a warning, and it is emitted
    # as one summary line per tick rather than one line per record per
    # tick. (The ``refused_past_time`` path below deliberately re-warns
    # every tick because that IS a repairable writer error; this one
    # would just be recurring noise about a correct state.)
    #
    # Emitted only when the bucket is non-empty: the scheduler already
    # logs ``starting``, so an operator can tell "running and nothing
    # closed came due" from "not running" without a per-tick zero line.
    if skipped_closed:
        log.info(
            "transport.scheduler.reminders_skipped_task_closed",
            count=len(skipped_closed),
            paths=[e.rel_path for e in skipped_closed[:10]],
            statuses=sorted({e.status for e in skipped_closed}),
            kinds=sorted({classify_return(e) for e in skipped_closed}),
            hint=(
                "reminder came due but the task is already closed — not "
                "fired and not stamped, by design. Remove remind_at to "
                "silence."
            ),
        )

    # Past-time refusal: log the gap, do NOT fire / dead-letter /
    # stamp. The task stays in "todo, no reminder fired" state until
    # the operator repairs the ``remind_at`` value (or removes it).
    # The warning re-fires on every tick the task is seen — recurring
    # noise is the right signal per ``feedback_intentionally_left_blank
    # .md``. Operator greps ``scheduler.reminder_refused_past_time`` to
    # catch any tasks Salem (or another writer) miscalculated.
    for entry in refused_past_time:
        delta_seconds = (entry.remind_at - now).total_seconds()
        log.warning(
            "transport.scheduler.reminder_refused_past_time",
            path=entry.rel_path,
            title=entry.title,
            remind_at=entry.remind_at.isoformat(),
            now=now.isoformat(),
            delta_seconds=delta_seconds,
            grace_seconds=_REMIND_AT_PAST_GRACE_SECONDS,
            hint=(
                "remind_at is more than the grace window in the past — "
                "writer error. Task NOT consumed; repair or remove the "
                "remind_at value to silence this warning."
            ),
        )

    for entry in stale:
        log.warning(
            "transport.scheduler.stale_reminder",
            path=entry.rel_path,
            remind_at=entry.remind_at.isoformat(),
            title=entry.title,
        )
        state.append_dead_letter(
            {
                "id": f"reminder-{entry.rel_path}-{entry.remind_at.isoformat()}",
                "user_id": user_id,
                "text": format_reminder(entry),
                "rel_path": entry.rel_path,
                "remind_at": entry.remind_at.isoformat(),
            },
            reason="stale_reminder_window_exceeded",
        )
        # Clear the remind_at so we don't re-enqueue on the next tick.
        try:
            clear_remind_at_and_stamp(entry, now)
        except OSError:
            log.exception(
                "transport.scheduler.stamp_failed",
                path=entry.rel_path,
            )

    for entry in due:
        kind = classify_return(entry)

        # Self-correcting-by-design signal: this reminder had already
        # fired once and was re-armed with a new ``remind_at``, which
        # means the operator pushed it rather than acting on it. One
        # push is ordinary life; a streak is the system asking the wrong
        # thing at the wrong time, and that is a correction signal.
        #
        # EMIT ONLY. The consumer (deck rotation's re-snooze streak →
        # shelve-proposal) is a separate lane by dispatch; building a
        # counter-store here would prejudge its shape. What this
        # guarantees is that the signal EXISTS in the log from the day
        # the behaviour ships, so the consumer has real history to read
        # instead of starting from zero.
        if entry.reminded_at is not None:
            log.info(
                "transport.scheduler.reminder_resnoozed",
                path=entry.rel_path,
                title=entry.title,
                kind=kind,
                previous_reminded_at=entry.reminded_at.isoformat(),
                remind_at=entry.remind_at.isoformat(),
                pushed_by_seconds=(
                    entry.remind_at - entry.reminded_at
                ).total_seconds(),
                return_slot=entry.return_slot,
                waiting_on=entry.waiting_on,
            )

        # THE RING (T2-1) — dealt HERE, above the send, and deliberately not
        # beside the stamp below.
        #
        # The entry is due and eligible at this point; nothing after this can
        # make it un-returned. Putting the emit below ``send_fn`` would put it
        # behind that call's failure ``continue``, which is precisely the
        # consumed-as-sent hazard this leg exists to close: on an instance with
        # no Telegram bot the send used to return ``[]`` — read as success — so
        # the return was stamped and the operator was told nothing at all.
        #
        # The two legs share NO failure exit. ``emit_return_card`` never raises
        # (it logs loud and answers False), so a feed fault cannot cost the
        # Telegram message; and the send's ``continue`` below does not un-emit,
        # because the card's key is the (record, remind_at) pair — the retry
        # tick re-derives the same id and folds to a no-op.
        #
        # PY-A (2026-08-15) made the return value LOAD-BEARING rather than
        # advisory: the send now raises ``TelegramUnavailable`` on a botless
        # instance, and the handler for that raise consumes the return only if
        # ``card_dealt`` is True. That is this comment's own rule — "consumed
        # once the operator has been told" — applied one case earlier than T4.
        #
        # WHEN T4 REMOVES THE SEND LEG: the stamp moves up to follow the EMIT,
        # not the send. The ordering intent is that a return is consumed once
        # the operator has been told, and after T4 the card IS the telling.
        card_dealt = emit_return_card(feed_handle, entry, now=now)

        text = format_reminder(entry)
        dedupe_key = (
            f"reminder-{entry.rel_path}-{entry.remind_at.isoformat()}"
        )
        try:
            await send_fn(
                user_id=user_id, text=text, dedupe_key=dedupe_key,
            )
        except TelegramUnavailable:
            # The Telegram leg is DARK — not failing, absent. That is a
            # different question from the ``except`` below, and it gets a
            # different answer, because the rule this file already states is
            # "a return is consumed once the operator has been TOLD" and on a
            # web-only instance the RING above is the telling.
            #
            # So: consume iff the card actually landed. Treating the dark case
            # as a failure instead would leave ``remind_at`` armed on every
            # instance we run, re-firing the same reminder every tick forever
            # — a regression against the pre-lane behaviour, which at least
            # consumed the return (dishonestly). Treating it as a SUCCESS is
            # what shipped, and it stamped ``sent_at`` rows for sends that
            # never happened.
            #
            # No ``state.record_send`` on either branch: nothing was sent, so
            # nothing may claim a sent_at row or hold a 24h dedupe slot.
            log.warning(
                "transport.scheduler.reminder_returned_no_telegram",
                path=entry.rel_path,
                title=entry.title,
                kind=kind,
                remind_at=entry.remind_at.isoformat(),
                ring_dealt=card_dealt,
                consumed=card_dealt,
                reason=TELEGRAM_UNAVAILABLE_REASON,
                detail=(
                    "no Telegram bot on this instance; the returned-reminder "
                    "feed card is the delivery. Consumed only if that card "
                    "landed — otherwise the reminder stays armed so a fixed "
                    "feed config re-fires it."
                ),
            )
            if not card_dealt:
                continue
            try:
                clear_remind_at_and_stamp(entry, now)
            except OSError:
                log.exception(
                    "transport.scheduler.stamp_failed",
                    path=entry.rel_path,
                )
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "transport.scheduler.send_failed",
                path=entry.rel_path,
                error=str(exc),
                response_summary=(
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )
            # Retry on the next tick — don't stamp.
            continue

        log.info(
            "transport.scheduler.reminder_fired",
            path=entry.rel_path,
            title=entry.title,
            kind=kind,
            return_slot=entry.return_slot,
            waiting_on=entry.waiting_on,
            remind_at=entry.remind_at.isoformat(),
        )

        try:
            clear_remind_at_and_stamp(entry, now)
        except OSError:
            log.exception(
                "transport.scheduler.stamp_failed",
                path=entry.rel_path,
            )
        state.record_send({
            "id": dedupe_key,
            "user_id": user_id,
            "text": text,
            "dedupe_key": dedupe_key,
            "sent_at": now.isoformat(),
            "rel_path": entry.rel_path,
        })

    # 1b) Retire the returned-reminder cards whose task has moved on.
    #
    # AFTER the due-loop on purpose: a task completed since the last tick
    # retires within one interval, and a card dealt THIS tick survives the same
    # tick's sweep because the record either carries the fire's stamp (send
    # succeeded) or still carries the SAME ``remind_at`` (send failed) — and the
    # sweep reads an identical ``remind_at`` as "not yet stamped", never as a
    # re-arm. Belt-guarded inside; a feed fault cannot reach the queue drain.
    sweep_return_cards(feed_handle, vault_path, now)

    # 2) Pending-queue drain.
    due_scheduled = state.pop_due(now)
    dark_dead_lettered = 0
    for pending_entry in due_scheduled:
        text = pending_entry.get("text", "")
        target_user = int(pending_entry.get("user_id") or user_id)
        dedupe_key = pending_entry.get("dedupe_key") or ""
        try:
            await send_fn(
                user_id=target_user,
                text=text,
                dedupe_key=dedupe_key or None,
            )
        except TelegramUnavailable:
            # Dead-letter rather than re-park. ``TelegramUnavailable`` is
            # raised on ``app is None``, which is fixed for the lifetime of
            # this process — so the re-park below would put the entry back on
            # a queue that can never drain, growing without bound and
            # re-logging every tick forever. Dead-lettering preserves the
            # payload, names the cause, and is already an inspectable state
            # (``alfred transport dead-letter``, GET /outbound/status/{id}).
            # Nothing was sent, so no ``record_send``.
            state.append_dead_letter(pending_entry, TELEGRAM_UNAVAILABLE_REASON)
            dark_dead_lettered += 1
            log.warning(
                "transport.scheduler.pending_dead_lettered_no_telegram",
                id=pending_entry.get("id"),
                user_id=target_user,
                dedupe_key=dedupe_key,
                scheduled_at=pending_entry.get("scheduled_at"),
                reason=TELEGRAM_UNAVAILABLE_REASON,
                detail=(
                    "scheduled send could not be delivered: this instance has "
                    "no Telegram bot. NOT retried — the condition is fixed "
                    "for the process lifetime."
                ),
            )
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "transport.scheduler.pending_send_failed",
                id=pending_entry.get("id"),
                error=str(exc),
                response_summary=f"{exc.__class__.__name__}: {exc}",
            )
            # Re-park on failure so the next tick retries.
            state.pending_queue.append(pending_entry)
            continue

        state.record_send({
            "id": pending_entry.get("id"),
            "user_id": target_user,
            "text": text,
            "dedupe_key": dedupe_key,
            "sent_at": now.isoformat(),
        })

    # One summary line when a botless instance ate scheduled sends. The
    # per-entry warnings above are the record; this is the number an operator
    # can act on, and it exists because "the queue quietly stopped draining"
    # has no signature of its own.
    if dark_dead_lettered:
        log.warning(
            "transport.scheduler.pending_queue_no_telegram",
            count=dark_dead_lettered,
            dead_letter_depth=len(state.dead_letter),
            reason=TELEGRAM_UNAVAILABLE_REASON,
            detail=(
                "scheduled sends were dead-lettered because this instance has "
                "no Telegram bot. They were NOT delivered and will not retry; "
                "inspect with `alfred transport dead-letter`."
            ),
        )

    # Save state once at end of tick — fewer disk writes, atomic.
    if due or stale or due_scheduled:
        try:
            state.save()
        except OSError:
            log.exception("transport.scheduler.state_save_failed")
