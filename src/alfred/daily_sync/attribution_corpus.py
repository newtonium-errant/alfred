"""Attribution-audit corpus — append-only JSONL of confirm/reject actions.

Separate from the email calibration corpus (``daily_sync.corpus``) so
the two audit streams stay independently auditable: the email corpus
is what the email classifier rotates into its few-shot prompt; the
attribution corpus is the audit trail for which agent-inferred
sections Andrew explicitly confirmed or rejected.

Schema for one row::

    {
      "type": "attribution_confirm" | "attribution_reject",
      "marker_id": "inf-20260423-salem-fc766c",
      "record_path": "note/Marker Smoke Test.md",
      "agent": "salem",
      "section_title": "Marker Smoke Test",
      "marker_date": "2026-04-23T18:44:47.262171+00:00",
      "andrew_action": "confirm" | "reject",
      "action_at": "2026-04-24T09:14:32+00:00",
      "andrew_note": "",                            # any free-text reasoning
      "original_section_content": "..."             # only on reject;
                                                    # preserved so the audit
                                                    # trail isn't lossy
    }

Append-only; never rewritten. Future analysis (Phase 3 — agent prompts
distinguishing inferred vs confirmed) will read this corpus to weight
how heavily an agent should rely on a given inferred rule.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AttributionCorpusEntry:
    """One row of the attribution-audit corpus.

    ``original_section_content`` is only populated on reject — the
    preserved-content rule is load-bearing for the audit trail (we
    want to be able to recover what Andrew rejected, not just that he
    rejected something).
    """

    type: str  # "attribution_confirm" or "attribution_reject"
    marker_id: str
    record_path: str
    agent: str
    section_title: str
    marker_date: str
    andrew_action: str  # "confirm" or "reject"
    action_at: str
    andrew_note: str = ""
    original_section_content: str = ""


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


__all__ = ["AttributionCorpusEntry", "append_entry"]
