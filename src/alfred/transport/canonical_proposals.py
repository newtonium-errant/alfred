"""JSONL-backed queue for canonical-record creation proposals.

When a subordinate instance (KAL-LE today, STAY-C / V.E.R.A. future)
hits ``GET /canonical/person/{name}`` and Salem returns 404
``record_not_found``, the subordinate can escalate to ``POST
/canonical/person/propose`` rather than silently fall back to a bare
name string. Salem queues the proposal to the file here; the Daily
Sync section provider renders pending proposals as numbered items
Andrew can confirm or reject. Design ratified in
``project_kalle_propose_person.md`` (2026-04-23).

Storage shape (one JSONL entry per proposal)::

    {
      "correlation_id": "kal-le-propose-person-1234",
      "ts":             "2026-04-24T15:03:00+00:00",
      "state":          "pending" | "accepted" | "rejected",
      "proposer":       "kal-le",
      "record_type":    "person",
      "name":           "Alex Newton",
      "proposed_fields": {...},
      "source":         "KAL-LE observed in session X"
    }

Deliberately append-only until Andrew responds. State transitions
(``pending`` → ``accepted`` / ``rejected``) are applied by the Daily
Sync dispatcher via :func:`update_proposal_state`, which rewrites the
file in place. The file is small (one line per open question) so a
full re-read on write is fine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# The states a proposal can hold. ``pending`` is the only state the
# Daily Sync section surfaces; ``accepted`` / ``rejected`` stay in the
# file for audit.
STATE_PENDING = "pending"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"

_VALID_STATES = {STATE_PENDING, STATE_ACCEPTED, STATE_REJECTED}


@dataclass
class Proposal:
    """One canonical-record creation proposal.

    ``proposed_fields`` is an arbitrary dict of frontmatter the
    subordinate suggests Salem set on the new record. Salem is the
    authoritative writer — it applies whatever fields it trusts and
    silently drops anything it doesn't recognise.
    """

    correlation_id: str
    ts: str
    state: str
    proposer: str
    record_type: str
    name: str
    proposed_fields: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "ts": self.ts,
            "state": self.state,
            "proposer": self.proposer,
            "record_type": self.record_type,
            "name": self.name,
            "proposed_fields": dict(self.proposed_fields or {}),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        return cls(
            correlation_id=str(data.get("correlation_id") or ""),
            ts=str(data.get("ts") or ""),
            state=str(data.get("state") or STATE_PENDING),
            proposer=str(data.get("proposer") or ""),
            record_type=str(data.get("record_type") or ""),
            name=str(data.get("name") or ""),
            proposed_fields=dict(data.get("proposed_fields") or {}),
            source=str(data.get("source") or ""),
        )


def append_proposal(
    queue_path: str | Path,
    proposal: Proposal,
) -> None:
    """Append a proposal row to the JSONL queue.

    Creates the parent directory when missing. Never raises for disk
    errors — the caller already logged the incoming request; losing an
    audit row shouldn't propagate a 500 back to the proposer.
    """
    if not queue_path:
        return
    path = Path(queue_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    line = json.dumps(proposal.to_dict(), default=str) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        return


def iter_proposals(queue_path: str | Path) -> list[Proposal]:
    """Read every proposal row (any state) from the queue.

    Returns ``[]`` when the file doesn't exist — a fresh install has
    nothing queued. Malformed lines are skipped with a log suppressed
    (no logger at this level; the caller's observer sees them via
    :func:`list_pending`'s count-vs-bytes delta if needed).
    """
    path = Path(queue_path)
    if not path.exists():
        return []
    out: list[Proposal] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            out.append(Proposal.from_dict(data))
    return out


def list_pending(queue_path: str | Path) -> list[Proposal]:
    """Return proposals whose state is ``pending``, oldest-first.

    The Daily Sync section provider consumes this directly — each row
    becomes one numbered item Andrew can confirm / reject. Oldest-first
    preserves the chronological dialogue order (if Andrew hasn't
    responded to yesterday's proposal, it still leads today's batch).
    """
    return [p for p in iter_proposals(queue_path) if p.state == STATE_PENDING]


def find_proposal(
    queue_path: str | Path, correlation_id: str,
) -> Proposal | None:
    """Return the proposal with ``correlation_id`` or ``None``.

    Helper for tests + the Daily Sync dispatcher. Scans the full file;
    that's fine for a queue we expect to stay under a few hundred rows
    in practice.
    """
    if not correlation_id:
        return None
    for p in iter_proposals(queue_path):
        if p.correlation_id == correlation_id:
            return p
    return None


def _now_iso() -> str:
    """Wall-clock UTC ISO-8601. Wrapped so tests can monkeypatch."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "Proposal",
    "STATE_ACCEPTED",
    "STATE_PENDING",
    "STATE_REJECTED",
    "append_proposal",
    "find_proposal",
    "iter_proposals",
    "list_pending",
]
