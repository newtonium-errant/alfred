"""The completion-WHEN corrections store — the backdate act record.

Self-correcting-by-design (platform standard): the when-selector is a
judgment surface — the system PROPOSES "done today" (the quick gesture's
default) and the operator sometimes answers with a different date. That
correction signal is captured here, per act, so the accumulated record can
later drive a proposal ("Garbage Day keeps getting logged a day late —
surface it a day earlier?") through the learn → propose → operator-approves
loop. This module is part (1), capture; the readout/proposal half is a
ledgered follow-up and the row shape below is what it will read.

THE SHAPE EXTENDS THE PLATFORM'S correction-record row
(``alfred.tier.sort_proposal.record_ruling``: ``{ts, shape, proposed,
chosen, confirmed, proposed_rule, id}``) rather than inventing a new
grammar: ``proposed``/``chosen`` are ISO DATES here (the default when vs
the operator's when), ``confirmed`` keeps its platform definition
(``chosen == proposed``), and two fields are added because a when-ruling is
about a specific routine item — ``item`` (the verbatim item text) and
``record`` (the routine record name). ``shape`` carries the recurrence type
(``weekly`` / ``monthly`` / …), the learnable discriminator, mirroring the
sort store's entry-shape role.

OWN SIDECAR, deliberately — never appended into the sort corrections file:
``load_tally`` there folds ``chosen`` against the slot vocabulary and its
docstring's contract is entry-shapes; date-valued rows would survive only by
being silently skipped, which is drift wearing schema-tolerance's coat. Same
derivation discipline instead: one path function beside the feed store, one
writer, ``file_rmw_lock`` + append, and the caller BELTS the write (a
capture failure must never cost the operator the completion itself).
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.common.file_lock import file_rmw_lock

log = structlog.get_logger(__name__)

#: Sidecar filename beside the per-instance feed store (instance-scoped for
#: free, exactly like the sort corrections store).
WHEN_CORRECTIONS_FILENAME = "completion_when.jsonl"


def when_corrections_path_for(feed_store_path: str | Path) -> Path:
    """The sidecar beside the feed store — ONE derivation for every caller."""
    return Path(feed_store_path).parent / WHEN_CORRECTIONS_FILENAME


def record_when_ruling(
    path: str | Path,
    *,
    shape: str,
    item: str,
    record: str,
    proposed: str,
    chosen: str,
    feed_item_id: str = "",
) -> None:
    """Append one when-ruling — the correction signal, captured at the act.

    ``proposed`` is the ISO date the plain verb would have written (the act's
    today); ``chosen`` is the ISO date the operator picked. ``confirmed`` is
    derived, not passed (the platform rule: a second spelling would drift).
    Raises on I/O failure — the CALLER decides what a capture failure may
    cost, and the act dispatcher belts it: a completion must land even when
    the learning store cannot.
    """
    from alfred.feed.model import _now_iso

    row = {
        "ts": _now_iso(),
        "shape": shape,
        "item": item,
        "record": record,
        "proposed": proposed,
        "chosen": chosen,
        "confirmed": chosen == proposed,
        "id": feed_item_id,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_rmw_lock(p):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info(
        "routine.completion.when_ruling_recorded",
        shape=shape,
        item=item,
        record=record,
        proposed=proposed,
        chosen=chosen,
    )


__all__ = [
    "WHEN_CORRECTIONS_FILENAME",
    "record_when_ruling",
    "when_corrections_path_for",
]
