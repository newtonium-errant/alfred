"""Per-instance calibration corpus — append-only JSONL.

Schema for one row::

    {
      "record_path": "note/Acme Confirmation.md",
      "classifier_priority": "medium",
      "classifier_action_hint": "calendar",
      "classifier_reason": "Future appointment confirmation",
      "andrew_priority": "low",
      "andrew_action_hint": null,
      "andrew_reason": "marketing — auto-archive",
      "timestamp": "2026-04-22T13:00:00+00:00",
      "daily_sync_message_id": 12345
    }

Append-only; never rewritten. Phase 2 (deferred) will derive standing
prompt rules from accumulated corrections; today the classifier just
rotates the tail of this file into its few-shot example slots.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class CorpusEntry:
    """One row of the calibration corpus.

    All ``andrew_*`` fields are optional (Andrew may confirm with no
    correction — in that case ``andrew_priority`` echoes
    ``classifier_priority`` and ``andrew_reason`` may be empty).
    """

    record_path: str
    classifier_priority: str
    classifier_action_hint: str | None
    classifier_reason: str
    andrew_priority: str
    andrew_action_hint: str | None = None
    andrew_reason: str = ""
    timestamp: str = ""
    daily_sync_message_id: int | None = None
    # Optional cached display fields so few-shot rotation can render the
    # example without re-reading the original record. None when the
    # writer didn't capture them — the few-shot renderer falls back to
    # ``record_path`` in that case.
    sender: str = ""
    subject: str = ""
    snippet: str = ""

    def is_correction(self) -> bool:
        """Return True when Andrew's call differed from the classifier's."""
        return self.andrew_priority != self.classifier_priority


def append_correction(corpus_path: str | Path, entry: CorpusEntry) -> None:
    """Append one entry to the corpus JSONL. Creates the file if absent.

    Atomic enough for a daemon's purposes — one append per Andrew reply
    item, no concurrent writers (the bot serialises per-chat). The
    parent directory is auto-created so a fresh install doesn't need
    bootstrap steps.
    """
    path = Path(corpus_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def iter_corrections(corpus_path: str | Path) -> Iterable[CorpusEntry]:
    """Yield every entry in the corpus, oldest first.

    Lines that fail to parse (corrupt write, schema drift) are skipped
    silently — the calibration loop stays usable even if one row is
    malformed.
    """
    path = Path(corpus_path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                yield _entry_from_dict(data)
            except (TypeError, KeyError):
                continue


def _entry_from_dict(data: dict) -> CorpusEntry:
    """Build a CorpusEntry from a dict, tolerant of missing optional fields."""
    return CorpusEntry(
        record_path=data.get("record_path", ""),
        classifier_priority=data.get("classifier_priority", ""),
        classifier_action_hint=data.get("classifier_action_hint"),
        classifier_reason=data.get("classifier_reason", ""),
        andrew_priority=data.get("andrew_priority", ""),
        andrew_action_hint=data.get("andrew_action_hint"),
        andrew_reason=data.get("andrew_reason", ""),
        timestamp=data.get("timestamp", ""),
        daily_sync_message_id=data.get("daily_sync_message_id"),
        sender=data.get("sender", ""),
        subject=data.get("subject", ""),
        snippet=data.get("snippet", ""),
    )


def recent_corrections(
    corpus_path: str | Path,
    *,
    limit: int = 10,
    diversify_by_tier: bool = True,
) -> list[CorpusEntry]:
    """Return the most recent N entries, optionally diversified by tier.

    ``diversify_by_tier`` (default True) tries to keep each tier
    represented in the result rather than letting one noisy tier
    dominate. The algorithm is greedy: walk the tail of the corpus
    newest-first, take every entry until we've seen at least one from
    each tier (or until we hit ``limit``), then take any remaining
    entries newest-first to fill up to ``limit``.

    Deterministic for a given corpus — the rotation must produce the
    same prompt across processes (Salem and a one-off ``alfred bit
    classifier`` re-run should agree on the few-shot examples).
    """
    if limit <= 0:
        return []
    all_entries = list(iter_corrections(corpus_path))
    if not all_entries:
        return []

    # Newest-first traversal of the most recent ``limit * 4`` rows.
    # Cap so we don't read a huge corpus end-to-end every classifier call.
    window_size = max(limit * 4, limit)
    window = all_entries[-window_size:]
    newest_first = list(reversed(window))

    if not diversify_by_tier:
        return list(reversed(newest_first[:limit]))

    # Greedy diversification.
    seen_tiers: set[str] = set()
    chosen: list[CorpusEntry] = []
    chosen_indices: set[int] = set()
    for idx, entry in enumerate(newest_first):
        tier = entry.andrew_priority
        if tier and tier not in seen_tiers:
            chosen.append(entry)
            chosen_indices.add(idx)
            seen_tiers.add(tier)
            if len(chosen) >= limit:
                break

    # Fill remaining slots newest-first from un-chosen entries.
    if len(chosen) < limit:
        for idx, entry in enumerate(newest_first):
            if idx in chosen_indices:
                continue
            chosen.append(entry)
            if len(chosen) >= limit:
                break

    # Return oldest-first so the few-shot block reads chronologically.
    return list(reversed(chosen))
