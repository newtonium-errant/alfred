"""#7 7c-i — corpus TOPICAL-FILING axis extension (additive, orthogonal to the priority axis).

Pins: the category fields round-trip + load tolerantly (old rows default ""); is_category_correction
semantics; recent_category_corrections filters to real category corrections and ignores priority-only
rows. The priority axis (is_correction / recent_corrections) is unaffected — pinned here too.
"""

from __future__ import annotations

import json
from pathlib import Path

from alfred.daily_sync.corpus import (
    CorpusEntry,
    append_correction,
    iter_corrections,
    recent_category_corrections,
    recent_corrections,
)


def _cat_entry(path: str, *, c_cat: str = "", a_cat: str = "") -> CorpusEntry:
    return CorpusEntry(
        record_path=path, classifier_priority="", classifier_action_hint=None,
        classifier_reason="", andrew_priority="",
        classifier_category=c_cat, andrew_category=a_cat,
        sender=f"s@{path}", subject=f"subj-{path}",
    )


def _pri_entry(path: str, *, c_pri: str, a_pri: str) -> CorpusEntry:
    return CorpusEntry(
        record_path=path, classifier_priority=c_pri, classifier_action_hint=None,
        classifier_reason="", andrew_priority=a_pri,
    )


def test_category_fields_round_trip(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _cat_entry("A", c_cat="", a_cat="Finance/Personal"))
    got = list(iter_corrections(target))
    assert len(got) == 1
    assert got[0].andrew_category == "Finance/Personal"
    assert got[0].classifier_category == ""
    # Serialized row carries the new keys.
    row = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert row["andrew_category"] == "Finance/Personal"
    assert "classifier_category" in row


def test_old_rows_without_category_fields_load_tolerantly(tmp_path: Path):
    # A pre-7c-i row has no category keys → they default to "".
    target = tmp_path / "corpus.jsonl"
    target.write_text(json.dumps({
        "record_path": "old", "classifier_priority": "medium", "classifier_action_hint": None,
        "classifier_reason": "", "andrew_priority": "low",
    }) + "\n", encoding="utf-8")
    got = list(iter_corrections(target))
    assert got[0].classifier_category == "" and got[0].andrew_category == ""


def test_is_category_correction_semantics():
    assert _cat_entry("A", c_cat="", a_cat="Finance/Personal").is_category_correction() is True
    assert _cat_entry("B", c_cat="Business/Invoices", a_cat="Finance/Tax").is_category_correction() is True
    # Same category = confirmation, not a correction.
    assert _cat_entry("C", c_cat="Finance/Tax", a_cat="Finance/Tax").is_category_correction() is False
    # No andrew_category = no signal.
    assert _cat_entry("D", c_cat="Finance/Tax", a_cat="").is_category_correction() is False


def test_recent_category_corrections_filters_and_orders(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _cat_entry("1", a_cat="Finance/Personal"))          # correction
    append_correction(target, _pri_entry("2", c_pri="medium", a_pri="low"))        # priority-only, ignored
    append_correction(target, _cat_entry("3", c_cat="Finance/Tax", a_cat="Finance/Tax"))  # confirmation, ignored
    append_correction(target, _cat_entry("4", a_cat="Business/Receipts"))          # correction
    out = recent_category_corrections(target, limit=10)
    assert [e.record_path for e in out] == ["1", "4"]  # only category corrections, oldest-first


def test_recent_category_corrections_respects_limit(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    for i in range(5):
        append_correction(target, _cat_entry(f"c{i}", a_cat="Finance/Personal"))
    out = recent_category_corrections(target, limit=2)
    assert [e.record_path for e in out] == ["c3", "c4"]  # most-recent 2, oldest-first


def test_priority_axis_unaffected_by_category_extension(tmp_path: Path):
    # A category-only correction is NOT a priority correction, and vice-versa — the axes are orthogonal.
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _cat_entry("cat", a_cat="Finance/Personal"))
    append_correction(target, _pri_entry("pri", c_pri="medium", a_pri="low"))
    pri = recent_corrections(target, limit=10)
    assert [e.record_path for e in pri] == ["pri"]  # priority axis sees only the priority correction
