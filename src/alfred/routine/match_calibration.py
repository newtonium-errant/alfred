"""Self-correcting ``routine_done`` matcher — Phase 1 capture sink.

The fuzzy completion matcher (``_matches_item`` / ``_match_confidence`` in
``routine.cli``) makes a JUDGMENT: does this free-text completion match this
routine item? Per the platform self-correcting-by-design standard
(``feedback_self_correcting_design_standard``), a judgment path must learn from
its mistakes: **capture the correction signal → feed it back → human-approve**.

This module is the **capture** half (Phase 1). When the vault-wide fuzzy match
succeeds with a LOW confidence (below the configured threshold), ``cmd_done``
appends one :class:`PendingMatch` row here — a pending-review queue the Daily
Sync ``routine_match`` section reads each morning and presents for operator
confirm/reject (Phase 2 closes the loop into the learned glossary).

Guardrail (load-bearing): the match path writes ONLY to this PENDING sink. The
learned glossary (the corpus the matcher consults) is mutated ONLY by an
operator reply through the Daily Sync ``reply_dispatch`` — never by a match.
Capturing a pending row is NOT a behavior change: nothing reads this file except
the read-only Daily Sync surface.

Append-only JSONL, per-instance (routine is Salem-only), schema-tolerant load
(the ``from_dict`` known-field filter — the load() contract) so a row written
by a newer/older tool version never crashes the reader.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Default capture sink + threshold. Per-instance ``.salem.jsonl`` mirrors the
# existing calibration corpora (``email_calibration.salem.jsonl`` etc.);
# routine + the Daily Sync channel are both Salem-scoped. Operators override
# via the ``routine.match_calibration`` config block. T=0.5 is the Phase 1
# starting floor (GREENLIT Q1) — observability refines it from real traffic.
DEFAULT_PENDING_PATH = "./data/routine_match_pending.salem.jsonl"
DEFAULT_CONFIDENCE_THRESHOLD = 0.5


@dataclass
class PendingMatch:
    """One low-confidence ``routine_done`` fuzzy match awaiting operator review.

    Captured at match time (``cmd_done`` success branch) when
    ``confidence < threshold``. Read-only until the operator confirms/rejects it
    in the Daily Sync surface (Phase 2).
    """

    query: str  # the operator's free-text completion phrase
    matched_to: str  # the routine item text the matcher chose
    record: str  # the routine record name the item lives on
    confidence: float  # the _match_confidence score at capture time
    completion_date: str = ""  # the date the completion was logged for
    captured_at: str = ""  # ISO timestamp of capture

    @classmethod
    def from_dict(cls, data: dict) -> "PendingMatch":
        """Schema-tolerant construct — filter to known fields (load contract).

        A row written by a different tool version with extra/missing fields
        loads without crashing; unknown keys are dropped, absent keys take
        dataclass defaults. ``query``/``matched_to``/``record`` are required
        (no default) — a row missing them is malformed and skipped by
        :func:`load_pending`.
        """
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def append_pending(path: str | Path, entry: PendingMatch) -> None:
    """Append one pending-match row to the capture JSONL (mkdir parent).

    One write per low-confidence match. The routine CLI is invoked
    per-completion (talker subprocess or operator CLI), so there are no
    concurrent writers to this file within a single completion.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_pending(path: str | Path) -> list[PendingMatch]:
    """Load all pending-match rows (schema-tolerant; empty list if absent).

    Malformed rows (bad JSON, or missing a required field) are skipped with a
    warning rather than crashing the reader — the Daily Sync surface must
    degrade gracefully on a partially-corrupt capture file.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[PendingMatch] = []
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            out.append(PendingMatch.from_dict(data))
        except (ValueError, TypeError) as exc:
            log.warning(
                "routine.match_calibration.skip_bad_pending_row",
                path=str(p), error=str(exc),
            )
    return out


__all__ = [
    "DEFAULT_PENDING_PATH",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "PendingMatch",
    "append_pending",
    "load_pending",
]
