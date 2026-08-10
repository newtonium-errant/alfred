"""Attribution-audit corpus — append-only JSONL of confirm/reject actions.

Separate from the email calibration corpus (``daily_sync.corpus``) so
the two audit streams stay independently auditable: the email corpus
is what the email classifier rotates into its few-shot prompt; the
attribution corpus is the audit trail for which agent-inferred
sections Andrew explicitly confirmed or rejected.

Schema for one row::

    {
      "type": "attribution_confirm" | "attribution_reject"
              | "attribution_auto_confirm" | "attribution_contest",
      "marker_id": "inf-20260423-salem-fc766c",
      "record_path": "note/Marker Smoke Test.md",
      "agent": "salem",
      "section_title": "Marker Smoke Test",
      "marker_date": "2026-04-23T18:44:47.262171+00:00",
      "andrew_action": "confirm" | "reject" | "contest" | "auto_confirm",
      "action_at": "2026-04-24T09:14:32+00:00",
      "andrew_note": "",                            # any free-text reasoning
      "original_section_content": "...",            # only on reject;
                                                    # preserved so the audit
                                                    # trail isn't lossy
      "confirmed_via": "operator" | "timeout_24h" | "backfill" | ""
    }

#63a added the last two action shapes. ``auto_confirm`` rows are machine-written
(the 24h sweep); ``contest`` rows are the operator's correction signal, which is
the reason the contest door exists at all — a contest that only re-tiered a card
without landing here would be a judgment the system never learns from.

Append-only; never rewritten. Future analysis (Phase 3 — agent prompts
distinguishing inferred vs confirmed) will read this corpus to weight
how heavily an agent should rely on a given inferred rule.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterator


@dataclass
class AttributionCorpusEntry:
    """One row of the attribution-audit corpus.

    ``original_section_content`` is only populated on reject — the
    preserved-content rule is load-bearing for the audit trail (we
    want to be able to recover what Andrew rejected, not just that he
    rejected something).
    """

    type: str  # "attribution_confirm" / "_reject" / "_auto_confirm" / "_contest"
    marker_id: str
    record_path: str
    agent: str
    section_title: str
    marker_date: str
    # What was recorded. "confirm" / "reject" / "contest" are OPERATOR actions;
    # "auto_confirm" explicitly is not — the field name is historical, and the
    # one thing it must never do is claim Andrew acted when he didn't.
    andrew_action: str
    action_at: str
    andrew_note: str = ""
    original_section_content: str = ""
    # #63a — which path confirmed the entry (one of ``attribution
    # .CONFIRMED_VIA_*``), so the trail is readable without re-walking the
    # vault. Empty on reject/contest rows (nothing was confirmed) and on rows
    # written before #63a.
    confirmed_via: str = ""
    # #72 item 4 — the section the OPERATOR named when contesting, from a small
    # controlled vocabulary he taps (Topics / Decisions / Action Items / ...).
    #
    # Deliberately NOT ``section_title`` above. That one is free text chosen by
    # whichever producer wrote the marker — ``spec.title``, ``"Structured
    # Summary"``, ``f"Calibration — {sub}"``, even ``name or rel_path or
    # "instructor-write"``. Keying per-section rates on it would yield a long
    # tail of one-off strings and never surface "this section stands out",
    # which is the entire point of the tracking.
    #
    # Empty when the operator contested the card as a whole, which stays
    # allowed; the stats file that under ``SECTION_UNKNOWN`` rather than
    # dropping it from the denominator.
    section: str = ""


def append_entry(corpus_path: str | Path, entry: AttributionCorpusEntry) -> None:
    """Append one entry to the attribution-audit corpus JSONL.

    Creates the file (and parent directory) if absent. One write per
    Andrew action — the dispatcher serialises per-Telegram-chat so
    there are no concurrent writers.
    """
    path = Path(corpus_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# A row is gated TWICE, and this tuple is only the first gate.
#
# These FOUR keys are checked explicitly below — the ones without which a row
# says nothing at all (which marker, which record, what was done, when). They
# are not the requirement. ``AttributionCorpusEntry`` has EIGHT fields with no
# default: these four plus ``type``, ``agent``, ``section_title`` and
# ``marker_date``. A row missing any of those four raises ``TypeError`` at
# construction, and the ``except TypeError: continue`` below turns that into a
# skip — so the EFFECTIVE requirement is all eight, and this tuple is a
# readable subset of it rather than the boundary.
#
# Load-bearing when hand-building a fixture: a row carrying only these four
# reads back ZERO rows (verified by running it, not by reading the code).
# Build fixtures from the no-default field set.
#
# What the tolerance actually buys is the fields that DO have defaults
# (``andrew_note``, ``original_section_content``, ``confirmed_via``,
# ``section``). That is why a pre-#63a row with no ``confirmed_via`` still
# loads — which is the history the stat line has to read — and it is not
# something this tuple's length controls.
_REQUIRED_KEYS = ("marker_id", "record_path", "andrew_action", "action_at")


@dataclass
class CorpusReadStats:
    """How many rows the reader DECLINED, for callers that report a metric.

    The reader skips unreadable rows by design (see below). That silence is
    fine for a caller that just wants the good rows and wrong for one that
    publishes a count off them: a quality metric whose denominator quietly
    shrank looks like a quiet fortnight rather than like a broken writer.

    Blank lines are not counted — they are whitespace, not declined data.

    Pass an instance to :func:`iter_attribution_rows` and read ``skipped``
    AFTER the generator is exhausted. It is filled in as rows are consumed, so
    a caller that breaks out of the loop early gets a partial count.
    """

    skipped: int = 0


def iter_attribution_rows(
    corpus_path: str | Path,
    *,
    stats: CorpusReadStats | None = None,
) -> Iterator[AttributionCorpusEntry]:
    """Yield every corpus row, OLDEST FIRST. The read side of #63a's append.

    #63a shipped capture with no reader, which left the platform's
    self-correcting loop half-closed: the signal was durable and nothing
    consumed it. This is the ONE seam every #72 consumer reads through — the
    stat line, the demotion trigger, and the per-section rates all come through
    here rather than each re-parsing the JSONL, so they cannot disagree about
    what the corpus says.

    Tolerant in both directions, and the tolerance is load-bearing rather than
    tidy:

    * A row that will not parse is SKIPPED, not raised. A quality metric that
      dies on one corrupt line stops being computed on the day something went
      wrong enough to write one.
    * A row missing a field added later loads with that field's default. Those
      rows are the bulk of the history the first stat line reads; dropping them
      would make a busy instance look quiet.
    * A row carrying a field from a NEWER build is loaded without it, rather
      than exploding — the house schema-tolerance contract, both directions.

    Mirrors ``daily_sync/corpus.py::iter_corrections`` deliberately; the two
    corpora stay independently auditable but read the same way.

    Pass a :class:`CorpusReadStats` as ``stats`` to learn how many rows were
    declined — a caller that publishes a number off these rows should say when
    its denominator shrank.
    """
    path = Path(corpus_path)
    if not path.exists():
        return
    # Local throwaway when the caller does not want the count, so the four
    # decline paths below stay unconditional one-liners.
    tally = stats if stats is not None else CorpusReadStats()
    known = {f.name for f in fields(AttributionCorpusEntry)}
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                tally.skipped += 1
                continue
            if not isinstance(data, dict):
                tally.skipped += 1
                continue
            if any(k not in data for k in _REQUIRED_KEYS):
                tally.skipped += 1
                continue
            try:
                yield AttributionCorpusEntry(
                    **{k: v for k, v in data.items() if k in known}
                )
            except TypeError:
                tally.skipped += 1
                continue


__all__ = [
    "AttributionCorpusEntry",
    "CorpusReadStats",
    "append_entry",
    "iter_attribution_rows",
]
