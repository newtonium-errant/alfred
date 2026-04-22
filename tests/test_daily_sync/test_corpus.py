"""Tests for the calibration corpus (append-only JSONL).

Covers:
- append_correction creates the file and parent dir.
- iter_corrections yields entries in append order.
- iter_corrections tolerates corrupt lines.
- recent_corrections diversifies by tier.
- recent_corrections returns deterministic order (oldest first).
- recent_corrections respects limit on a small + large corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

from alfred.daily_sync.corpus import (
    CorpusEntry,
    append_correction,
    iter_corrections,
    recent_corrections,
)


def _entry(
    *, path: str, c_pri: str, a_pri: str, ts: str = "2026-04-22T00:00:00+00:00"
) -> CorpusEntry:
    return CorpusEntry(
        record_path=path,
        classifier_priority=c_pri,
        classifier_action_hint=None,
        classifier_reason="reason",
        andrew_priority=a_pri,
        andrew_reason="",
        timestamp=ts,
    )


def test_append_creates_file_and_dir(tmp_path: Path):
    target = tmp_path / "subdir" / "corpus.jsonl"
    e = _entry(path="note/A.md", c_pri="medium", a_pri="low")
    append_correction(target, e)
    assert target.exists()
    assert target.parent.is_dir()
    line = target.read_text(encoding="utf-8").strip().splitlines()[0]
    decoded = json.loads(line)
    assert decoded["record_path"] == "note/A.md"
    assert decoded["andrew_priority"] == "low"


def test_iter_returns_append_order(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="medium", a_pri="low"))
    append_correction(target, _entry(path="note/B.md", c_pri="high", a_pri="high"))
    append_correction(target, _entry(path="note/C.md", c_pri="low", a_pri="spam"))
    paths = [e.record_path for e in iter_corrections(target)]
    assert paths == ["note/A.md", "note/B.md", "note/C.md"]


def test_iter_skips_corrupt_lines(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="medium", a_pri="low"))
    # Inject a corrupt line + a valid one
    with target.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
        f.write(json.dumps({"record_path": "note/B.md", "classifier_priority": "high", "andrew_priority": "high"}) + "\n")
    paths = [e.record_path for e in iter_corrections(target)]
    assert paths == ["note/A.md", "note/B.md"]


def test_iter_missing_file_returns_empty(tmp_path: Path):
    target = tmp_path / "no_such.jsonl"
    assert list(iter_corrections(target)) == []


def test_recent_corrections_limit(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    for i in range(20):
        append_correction(target, _entry(path=f"note/{i}.md", c_pri="medium", a_pri="medium"))
    out = recent_corrections(target, limit=5)
    # Returns oldest-first, but only 5 entries
    assert len(out) == 5


def test_recent_corrections_diversifies_by_tier(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    # 10 medium entries, then 1 high, 1 low, 1 spam at the end
    for i in range(10):
        append_correction(target, _entry(path=f"note/m{i}.md", c_pri="medium", a_pri="medium"))
    append_correction(target, _entry(path="note/h.md", c_pri="medium", a_pri="high"))
    append_correction(target, _entry(path="note/l.md", c_pri="medium", a_pri="low"))
    append_correction(target, _entry(path="note/s.md", c_pri="medium", a_pri="spam"))
    out = recent_corrections(target, limit=4, diversify_by_tier=True)
    tiers = {e.andrew_priority for e in out}
    # All four tiers represented thanks to diversification
    assert tiers == {"high", "low", "spam", "medium"}


def test_recent_corrections_no_diversification(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    for i in range(5):
        append_correction(target, _entry(path=f"note/{i}.md", c_pri="low", a_pri="low"))
    out = recent_corrections(target, limit=3, diversify_by_tier=False)
    # Newest 3 oldest-first
    paths = [e.record_path for e in out]
    assert paths == ["note/2.md", "note/3.md", "note/4.md"]


def test_recent_corrections_empty_corpus(tmp_path: Path):
    target = tmp_path / "no_such.jsonl"
    assert recent_corrections(target, limit=5) == []


def test_recent_corrections_zero_limit(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="low", a_pri="low"))
    assert recent_corrections(target, limit=0) == []
