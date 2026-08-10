"""#72 item 1 — the attribution corpus gets its read side.

#63a shipped capture only: `AttributionCorpusEntry` + `append_entry`, and no
reader anywhere. I flagged that in the #63a ship report as the half-closed loop
— the platform's self-correcting standard wants capture AND feed-back AND
operator-visible surfacing, and only the first existed.

This is the seam everything else in #72 reads through: the stat line, the
demotion trigger, and the per-section rates all consume this iterator rather
than each re-parsing the JSONL. One reader, one answer — the same argument that
put the auto-confirm sweep on the review batch's existing walk.

Pattern deliberately mirrors `daily_sync/corpus.py::iter_corrections`: yield
oldest-first, skip unparseable rows rather than raising. A quality metric that
crashes on one corrupt line is a metric that stops being computed on the day it
matters most.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.daily_sync.attribution_corpus import (
    AttributionCorpusEntry,
    append_entry,
    iter_attribution_rows,
)


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )


def _row(**over) -> dict:
    row = {
        "type": "attribution_contest",
        "marker_id": "inf-1",
        "record_path": "session/A.md",
        "agent": "salem",
        "section_title": "Structured Summary",
        "marker_date": "2026-08-01T00:00:00+00:00",
        "andrew_action": "contest",
        "action_at": "2026-08-02T00:00:00+00:00",
        "andrew_note": "",
        "original_section_content": "",
        "confirmed_via": "",
    }
    row.update(over)
    return row


def test_a_missing_corpus_yields_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """An instance that has never had a contest is the steady state, not an
    error. The consumer must be able to ask before anything has happened."""
    assert list(iter_attribution_rows(tmp_path / "absent.jsonl")) == []


def test_rows_come_back_oldest_first(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    _write(path, [_row(marker_id="a"), _row(marker_id="b"), _row(marker_id="c")])
    assert [r.marker_id for r in iter_attribution_rows(path)] == ["a", "b", "c"]


def test_the_round_trip_holds(tmp_path: Path) -> None:
    """What `append_entry` writes is what the reader gets back — the two halves
    of one contract, pinned together rather than each against a fixture."""
    path = tmp_path / "c.jsonl"
    append_entry(path, AttributionCorpusEntry(
        type="attribution_auto_confirm",
        marker_id="inf-9", record_path="session/B.md", agent="salem",
        section_title="Topics", marker_date="2026-08-01T00:00:00+00:00",
        andrew_action="auto_confirm", action_at="2026-08-02T00:00:00+00:00",
        confirmed_via="timeout_24h",
    ))
    rows = list(iter_attribution_rows(path))
    assert len(rows) == 1
    assert rows[0].marker_id == "inf-9"
    assert rows[0].confirmed_via == "timeout_24h"
    assert rows[0].andrew_action == "auto_confirm"


# --- tolerance, both directions ---------------------------------------------


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A metric that dies on one bad row stops being computed exactly when
    something has gone wrong enough to write a bad row."""
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(_row(marker_id="good1")) + "\n"
        + "{not json at all\n"
        + json.dumps(_row(marker_id="good2")) + "\n",
        encoding="utf-8",
    )
    assert [r.marker_id for r in iter_attribution_rows(path)] == ["good1", "good2"]


def test_blank_lines_and_a_json_non_object_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(_row(marker_id="a")) + "\n\n[1,2,3]\n   \n"
        + json.dumps(_row(marker_id="b")) + "\n",
        encoding="utf-8",
    )
    assert [r.marker_id for r in iter_attribution_rows(path)] == ["a", "b"]


def test_a_pre_63a_row_loads_with_defaults(tmp_path: Path) -> None:
    """BACKWARD: rows written before #63a have no `confirmed_via`, and rows
    written before #72 have no section field. They are the bulk of the history
    the first stat line will read — dropping them would make the metric look
    quiet on an instance that simply has old data."""
    path = tmp_path / "c.jsonl"
    _write(path, [{
        "type": "attribution_confirm", "marker_id": "old",
        "record_path": "session/A.md", "agent": "salem",
        "section_title": "S", "marker_date": "2026-04-01T00:00:00+00:00",
        "andrew_action": "confirm", "action_at": "2026-04-02T00:00:00+00:00",
    }])
    row = list(iter_attribution_rows(path))[0]
    assert row.marker_id == "old"
    assert row.confirmed_via == ""


def test_an_unknown_future_field_is_ignored_not_fatal(tmp_path: Path) -> None:
    """FORWARD: a newer build's extra column must not break an older reader —
    the house schema-tolerance contract, both directions."""
    path = tmp_path / "c.jsonl"
    _write(path, [_row(marker_id="x", something_from_2027={"nested": 1})])
    rows = list(iter_attribution_rows(path))
    assert len(rows) == 1 and rows[0].marker_id == "x"


def test_a_row_missing_required_keys_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    _write(path, [{"nothing": "useful"}, _row(marker_id="ok")])
    assert [r.marker_id for r in iter_attribution_rows(path)] == ["ok"]


def test_the_reader_is_lazy(tmp_path: Path) -> None:
    """An iterator, not a list: the corpus is append-only and unbounded, and
    every consumer here filters by date anyway."""
    import types

    path = tmp_path / "c.jsonl"
    _write(path, [_row()])
    assert isinstance(iter_attribution_rows(path), types.GeneratorType)
