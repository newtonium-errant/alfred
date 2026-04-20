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
from typing import Any

import frontmatter

from .config import TransportConfig
from .state import TransportState
from .utils import get_logger

log = get_logger(__name__)


# Types that the scheduler inspects for remind_at. Only ``task``
# records today — other types might grow reminder support later, at
# which point the filter broadens.
_REMINDER_TYPES: set[str] = {"task"}

# Task statuses that are eligible for reminders. Done / cancelled
# tasks keep any residual ``remind_at`` in the frontmatter as a
# historical record, but the scheduler does not fire on them.
_ELIGIBLE_STATUSES: set[str] = {"todo", "active"}


# The body comment signature the scheduler appends after a successful
# reminder dispatch. One line per reminder so the audit trail is
# grep-able and easy to render as a list in a task record's body.
_REMINDER_COMMENT_RE = re.compile(r"<!-- ALFRED:REMINDER [^>]+-->")


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
) -> tuple[list[DueReminder], list[DueReminder]]:
    """Walk ``vault_path/task/**/*.md`` and classify each record.

    Returns ``(due, stale)`` — ``due`` fires; ``stale`` dead-letters.
    Both lists are computed in one pass so the scheduler can pick up
    either outcome without a second vault walk.

    Eligibility (for both lists):
    - ``type == "task"``
    - ``status in {"todo", "active"}``
    - ``remind_at`` present and parseable
    - Either ``reminded_at`` absent OR ``reminded_at < remind_at``
      (so updating a task's ``remind_at`` to a new time re-arms it)
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return [], []

    due: list[DueReminder] = []
    stale: list[DueReminder] = []
    stale_cutoff = now - timedelta(minutes=stale_max_minutes)

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
        if status not in _ELIGIBLE_STATUSES:
            continue

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
        )

        if remind_at < stale_cutoff:
            stale.append(entry)
        else:
            due.append(entry)

    return due, stale


def format_reminder(entry: DueReminder) -> str:
    """Render the message body for a due reminder.

    Precedence (per ratified recommendation 3):

    1. ``reminder_text`` field if present and non-empty — verbatim.
    2. ``"Reminder: {title} (due {due})"`` when ``due`` is present.
    3. ``"Reminder: {title}"`` otherwise.
    """
    if entry.reminder_text:
        return entry.reminder_text
    if entry.due:
        return f"Reminder: {entry.title} (due {entry.due})"
    return f"Reminder: {entry.title}"


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
) -> None:
    """Scheduler loop — fire due reminders and drain scheduled pending sends.

    Returns when ``shutdown_event`` is set. Exceptions inside a tick
    are caught and logged so one bad record cannot wedge the loop.
    """
    interval = max(1, int(config.scheduler.poll_interval_seconds))
    stale_max = int(config.scheduler.stale_reminder_max_minutes)

    log.info(
        "transport.scheduler.starting",
        poll_interval_seconds=interval,
        stale_reminder_max_minutes=stale_max,
        user_id=user_id,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            log.info("transport.scheduler.stopped")
            return
        try:
            await _tick(config, state, send_fn, vault_path, user_id)
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
) -> None:
    """Run one scheduler pass — due reminders, stale reminders, pending queue."""
    now = datetime.now(timezone.utc)

    # 1) Task-record reminders.
    due, stale = find_due_reminders(
        vault_path, now, config.scheduler.stale_reminder_max_minutes,
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
        text = format_reminder(entry)
        dedupe_key = (
            f"reminder-{entry.rel_path}-{entry.remind_at.isoformat()}"
        )
        try:
            await send_fn(
                user_id=user_id, text=text, dedupe_key=dedupe_key,
            )
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

    # 2) Pending-queue drain.
    due_scheduled = state.pop_due(now)
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

    # Save state once at end of tick — fewer disk writes, atomic.
    if due or stale or due_scheduled:
        try:
            state.save()
        except OSError:
            log.exception("transport.scheduler.state_save_failed")
