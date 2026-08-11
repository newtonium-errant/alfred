"""Reading the VAPID push-delivery trial ledger (#96 stage 1).

The trial's SENDER lives in the web app (`web/lib/algernon/pushTrial.ts`) —
web-push/VAPID is a Node library and the poller that carries the heartbeat is a
Next singleton. This module is the operator's READ surface for what that sender
recorded: `alfred push-trial status`.

## This module derives nothing about the schedule, on purpose

The sender writes a ``scheduled`` row for every slot when the trial starts, so
answering "was this slot ever sent?" is a JOIN on ``push_id`` — not a
recomputation of when a send was due. That is deliberate: a Python
reimplementation of the TypeScript schedule would be two definitions of one
thing in two languages, and the first divergence would be invisible (both sides
would keep reporting confidently, about different schedules). The only thing
shared across the boundary is the ROW SHAPE, which is a data contract with a
fixture pin rather than duplicated logic.

## The four states, and why `sent` is not a failure

The trial exists because "I didn't get a notification" is ambiguous, so this
must never collapse the two failure shapes into one number:

``scheduled``
    Its time passed and NO send was attempted — the instrument was down (the
    sender rides a Next singleton that resumes on the next push-route hit after
    a restart). Says NOTHING about delivery. Counting it as a miss would invent
    the exact false signal the trial was built to remove.
``send_failed``
    The send itself errored. An instrument-side fact, also not a delivery datum.
``sent``
    Left the server, no receipt. GENUINELY UNKNOWN — it may never have arrived,
    or it may have arrived and gone untapped. Only the operator can collapse
    this, which is what the reconcile prompt is for.
``received``
    He tapped it. The latency is real evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Mirrors ``pushTrial.ts``'s ``trialPath()`` default. The env var is the same
#: one the sender honours, so operator and instrument cannot end up on two files.
TRIAL_PATH_ENV = "ALFRED_WEB_PUSH_TRIAL"
DEFAULT_TRIAL_PATH = "./data/push_trial.jsonl"

ROW_SCHEDULED = "scheduled"
ROW_SENT = "sent"
ROW_SEND_FAILED = "send_failed"
ROW_RECEIPT = "receipt"

#: Per-slot outcome. NOT a two-way delivered/undelivered split — see the module
#: docstring for why the distinction is the whole point of the instrument.
STATE_PENDING = "pending"          # due in the future; nothing owed yet
STATE_NOT_SENT = "not_sent"        # due passed, no attempt — INSTRUMENT DOWN
STATE_SEND_FAILED = "send_failed"  # attempted, errored — instrument-side
STATE_UNKNOWN = "unknown"          # sent, no receipt — the operator must rule
STATE_RECEIVED = "received"        # tapped


@dataclass
class SlotStatus:
    """One scheduled slot and everything the ledger knows about it."""

    push_id: str
    due_ts: str = ""
    sent_ts: str = ""
    received_ts: str = ""
    error: str = ""
    state: str = STATE_PENDING
    #: Seconds between send and tap. ``None`` unless both are known.
    latency_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "push_id": self.push_id,
            "due_ts": self.due_ts,
            "sent_ts": self.sent_ts,
            "received_ts": self.received_ts,
            "error": self.error,
            "state": self.state,
            "latency_s": self.latency_s,
        }


@dataclass
class TrialStatus:
    slots: list[SlotStatus] = field(default_factory=list)
    #: Rows whose ``push_id`` has no ``scheduled`` row — a send or receipt for a
    #: slot this ledger never planned. Surfaced rather than dropped: it means
    #: the ledger was written by a different schedule (a restarted trial), and
    #: silently ignoring those rows would under-report real deliveries.
    orphan_ids: list[str] = field(default_factory=list)
    unreadable: bool = False
    skipped_rows: int = 0

    @property
    def counts(self) -> dict[str, int]:
        out = {
            STATE_PENDING: 0, STATE_NOT_SENT: 0, STATE_SEND_FAILED: 0,
            STATE_UNKNOWN: 0, STATE_RECEIVED: 0,
        }
        for s in self.slots:
            out[s.state] = out.get(s.state, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "orphan_ids": list(self.orphan_ids),
            "unreadable": self.unreadable,
            "skipped_rows": self.skipped_rows,
            "slots": [s.to_dict() for s in self.slots],
        }


def trial_path() -> str:
    """Where the ledger lives.

    Env-only, and deliberately NOT config-backed: the sender
    (``pushTrial.ts``) resolves this from ``ALFRED_WEB_PUSH_TRIAL`` with the
    same default and reads no YAML. A config key here would be a second source
    of truth that the writer cannot see — the operator would point this surface
    at a file nothing writes and read a permanently empty trial as "nothing
    arrived".
    """
    import os
    configured = (os.environ.get(TRIAL_PATH_ENV) or "").strip()
    return configured or DEFAULT_TRIAL_PATH


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_rows(path: str | Path) -> tuple[list[dict[str, Any]], int, bool]:
    """``(rows, skipped, unreadable)``. A half-written ledger still reports."""
    target = Path(path)
    if not target.exists():
        return [], 0, False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "push_trial.ledger_unreadable", path=str(target), error=str(exc),
            detail="the trial ledger exists but could not be read — status "
                   "cannot say anything about delivery",
        )
        return [], 0, True
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(data, dict) and isinstance(data.get("push_id"), str):
            rows.append(data)
        else:
            skipped += 1
    return rows, skipped, False


def build_status(
    path: str | Path, *, now: datetime | None = None,
) -> TrialStatus:
    """Join the ledger into one row per scheduled slot."""
    when = now or datetime.now(timezone.utc)
    rows, skipped, unreadable = read_rows(path)
    status = TrialStatus(unreadable=unreadable, skipped_rows=skipped)
    if unreadable:
        return status

    slots: dict[str, SlotStatus] = {}
    for row in rows:
        if row.get("type") == ROW_SCHEDULED:
            pid = row["push_id"]
            slots.setdefault(pid, SlotStatus(push_id=pid))
            slots[pid].due_ts = str(row.get("due_ts") or "")

    for row in rows:
        pid = row["push_id"]
        rtype = row.get("type")
        if rtype == ROW_SCHEDULED:
            continue
        slot = slots.get(pid)
        if slot is None:
            if pid not in status.orphan_ids:
                status.orphan_ids.append(pid)
            continue
        if rtype == ROW_SENT:
            slot.sent_ts = str(row.get("sent_ts") or "")
        elif rtype == ROW_SEND_FAILED:
            slot.sent_ts = str(row.get("sent_ts") or "")
            slot.error = str(row.get("error") or "")
        elif rtype == ROW_RECEIPT:
            slot.received_ts = str(row.get("received_ts") or "")

    for slot in slots.values():
        due = _parse_ts(slot.due_ts)
        sent = _parse_ts(slot.sent_ts)
        got = _parse_ts(slot.received_ts)
        if got is not None:
            slot.state = STATE_RECEIVED
            if sent is not None:
                slot.latency_s = round((got - sent).total_seconds(), 1)
        elif slot.error:
            slot.state = STATE_SEND_FAILED
        elif sent is not None:
            slot.state = STATE_UNKNOWN
        elif due is not None and due <= when:
            slot.state = STATE_NOT_SENT
        else:
            slot.state = STATE_PENDING

    status.slots = sorted(slots.values(), key=lambda s: (s.due_ts, s.push_id))
    if status.orphan_ids:
        log.warning(
            "push_trial.orphan_rows", count=len(status.orphan_ids),
            push_ids=status.orphan_ids[:10],
            detail="sent/receipt rows for slots this ledger never scheduled — "
                   "the trial was probably restarted with a different start date",
        )
    return status


def render_status(status: TrialStatus, path: str) -> str:
    """The operator-facing table.

    Says what each number MEANS, because the states are the finding. A bare
    count of "3 not received" invites exactly the reading the trial exists to
    prevent — that three pushes failed to arrive, when some may never have been
    sent at all.
    """
    lines = [f"Push delivery trial ({path}):"]
    if status.unreadable:
        lines.append("  ! the ledger exists but could not be read — no delivery")
        lines.append("    conclusion can be drawn until that is fixed.")
        return "\n".join(lines)
    if not status.slots:
        # Intentionally-left-blank: an empty ledger is "not started", which is
        # not the same as "started and nothing arrived".
        lines.append("  (no slots scheduled yet — the trial has not started, or")
        lines.append("   the sender has not run since PUSH_TRIAL_START was set)")
        if status.skipped_rows:
            lines.append(f"  {status.skipped_rows} unparseable row(s) skipped.")
        return "\n".join(lines)

    c = status.counts
    lines.append("")
    lines.append(f"  {'slot':<16} {'due':<20} {'state':<12} latency")
    for s in status.slots:
        lat = f"{s.latency_s:.0f}s" if s.latency_s is not None else "-"
        lines.append(f"  {s.push_id:<16} {s.due_ts[:19]:<20} {s.state:<12} {lat}")
    lines.append("")
    lines.append(f"  received     {c[STATE_RECEIVED]:>3}  — arrived AND he tapped it (real evidence)")
    lines.append(f"  unknown      {c[STATE_UNKNOWN]:>3}  — sent, never tapped. Could be a missed")
    lines.append( "                    delivery OR a delivery he ignored. NOT a failure")
    lines.append( "                    until he says which — that is the ruling below.")
    lines.append(f"  not sent     {c[STATE_NOT_SENT]:>3}  — due passed, no send attempted. The")
    lines.append( "                    INSTRUMENT was down; says nothing about delivery.")
    lines.append(f"  send failed  {c[STATE_SEND_FAILED]:>3}  — the send itself errored (instrument-side).")
    lines.append(f"  pending      {c[STATE_PENDING]:>3}  — still in the future.")
    if status.orphan_ids:
        lines.append("")
        lines.append(f"  ! {len(status.orphan_ids)} row(s) reference slots this ledger never")
        lines.append("    scheduled — the trial was likely restarted mid-flight.")
    if status.skipped_rows:
        lines.append(f"  ! {status.skipped_rows} unparseable row(s) skipped.")
    if c[STATE_UNKNOWN]:
        lines.append("")
        lines.append("  To settle the unknowns, answer for each: did it arrive?")
        lines.append("  Anything you never saw on the phone is a real miss; anything")
        lines.append("  you saw and ignored is a delivery. Without that ruling the")
        lines.append("  trial cannot tell the two apart — which is the whole point.")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_TRIAL_PATH",
    "STATE_NOT_SENT",
    "STATE_PENDING",
    "STATE_RECEIVED",
    "STATE_SEND_FAILED",
    "STATE_UNKNOWN",
    "SlotStatus",
    "TRIAL_PATH_ENV",
    "TrialStatus",
    "build_status",
    "read_rows",
    "render_status",
    "trial_path",
]
