"""Tests for the KAL-LE distiller-radar Phase 3a daily wrapper.

Covers:
- ``rank_day`` reuses the synthesis ranker on a 1-day window.
- Surfaced-log dedup excludes already-flagged paths from subsequent
  daily fires (the load-bearing Phase 3a contract).
- ``min_score`` floor drops items below threshold even within top_n.
- ``render_daily_file`` produces explicit empty-state copy when items
  is empty (per ``feedback_intentionally_left_blank.md``).
- ``run_daily_radar`` end-to-end: file written, surfaced log appended.
- ``--dry-run`` path computes but does NOT write file or append log.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.distiller.radar_day import (
    DailyRadarResult,
    append_surfaced,
    latest_daily_path,
    load_surfaced_paths,
    rank_day,
    render_daily_file,
    run_daily_radar,
)


NOW = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


def _record(
    *,
    name: str,
    record_type: str,
    created: str = "2026-05-02",
    claim: str = "Default claim text.",
    source_links: list[str] | None = None,
    entity_links: list[str] | None = None,
    body: str = "",
) -> str:
    """Build a synthetic learn-record markdown string (mirror of the
    helper in test_synthesis_ranker.py)."""
    fm_lines = [f"name: {name}", f"type: {record_type}", f"claim: {claim}"]
    if created is not None:
        fm_lines.append(f"created: '{created}'")
    if source_links:
        fm_lines.append("source_links:")
        for link in source_links:
            fm_lines.append(f"  - '{link}'")
    if entity_links:
        fm_lines.append("entity_links:")
        for link in entity_links:
            fm_lines.append(f"  - '{link}'")
    head = "---\n" + "\n".join(fm_lines) + "\n---\n"
    return head + "\n" + body


@pytest.fixture
def vault_with_one_record(tmp_path: Path) -> Path:
    """Vault with a single synthesis record dated today (2026-05-02)."""
    for d in ("synthesis", "decision", "contradiction"):
        (tmp_path / d).mkdir()
    rec = _record(
        name="Test Synthesis",
        record_type="synthesis",
        created="2026-05-02",
        claim="Andrew prefers explicit over implicit empty-state.",
        source_links=["[[session/X]]", "[[session/Y]]"],
        entity_links=["[[person/Andrew Newton]]", "[[project/Alfred]]"],
    )
    (tmp_path / "synthesis" / "Test Synthesis.md").write_text(
        rec, encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """Vault with the three ranked dirs but no records."""
    for d in ("synthesis", "decision", "contradiction"):
        (tmp_path / d).mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# rank_day — basic + dedup + min_score
# ---------------------------------------------------------------------------


def test_rank_day_one_record(vault_with_one_record: Path) -> None:
    items, raw_count = rank_day(vault_with_one_record, top_n=5, now=NOW)
    assert raw_count == 1
    assert len(items) == 1
    assert items[0].record_type == "synthesis"
    # Score should be the sum of all four terms — non-zero because
    # both source_links and entity_links exist and the record is
    # dated today (recency=1.0 at age 0, 0.5**0).
    assert items[0].score > 0


def test_rank_day_empty(empty_vault: Path) -> None:
    items, raw_count = rank_day(empty_vault, top_n=5, now=NOW)
    assert items == []
    assert raw_count == 0


def test_rank_day_dedup_excludes_surfaced(vault_with_one_record: Path) -> None:
    record_path = vault_with_one_record / "synthesis" / "Test Synthesis.md"
    surfaced = {str(record_path)}
    items, raw_count = rank_day(
        vault_with_one_record, top_n=5,
        surfaced_paths=surfaced, now=NOW,
    )
    # Ranker still saw the record (raw_count=1), but the dedup gate
    # filtered it out (items=0).
    assert raw_count == 1
    assert items == []


def test_rank_day_min_score_floor_filters(vault_with_one_record: Path) -> None:
    # Force a floor that the synthetic record can't clear. The single
    # record's score will be ~16 (3+2 sources*3 + 2 entities*2 + 1*1
    # recency + 3*1 type), so 100.0 is well above.
    items, raw_count = rank_day(
        vault_with_one_record, top_n=5, min_score=100.0, now=NOW,
    )
    assert raw_count == 1
    assert items == []


def test_rank_day_window_is_one_day(tmp_path: Path) -> None:
    """Records older than 1 day should miss the recency window — but
    the ranker keeps them on the cross_source/entity/type terms.

    This test demonstrates the window_days=1 hardcoding: a 3-day-old
    record still shows up in the candidate pool with a non-zero score
    on the other three terms; recency just collapses to 0. Phase 3
    callers explicitly want 1-day window so they get the freshest
    signals first."""
    for d in ("synthesis", "decision", "contradiction"):
        (tmp_path / d).mkdir()
    old = _record(
        name="Old Record",
        record_type="synthesis",
        created="2026-04-29",  # 3 days before NOW (2026-05-02)
        source_links=["[[session/Z]]"],
    )
    (tmp_path / "synthesis" / "Old Record.md").write_text(
        old, encoding="utf-8",
    )
    items, raw_count = rank_day(tmp_path, top_n=5, now=NOW)
    assert raw_count == 1
    # Recency term should be zero (outside 1-day window) but other
    # terms should still produce a positive score.
    assert items[0].breakdown.recency == 0.0
    assert items[0].score > 0


# ---------------------------------------------------------------------------
# Surfaced log — read + write
# ---------------------------------------------------------------------------


def test_load_surfaced_paths_missing_returns_empty_set(tmp_path: Path) -> None:
    # Log file doesn't exist — first-run behavior.
    paths = load_surfaced_paths(tmp_path / "radar_surfaced.jsonl")
    assert paths == set()


def test_load_surfaced_paths_reads_existing(tmp_path: Path) -> None:
    log_path = tmp_path / "radar_surfaced.jsonl"
    log_path.write_text(
        '{"date":"2026-05-01","path":"/v/synthesis/A.md","score":9.0,"type":"synthesis"}\n'
        '{"date":"2026-05-01","path":"/v/decision/B.md","score":7.0,"type":"decision"}\n',
        encoding="utf-8",
    )
    paths = load_surfaced_paths(log_path)
    assert paths == {"/v/synthesis/A.md", "/v/decision/B.md"}


def test_load_surfaced_paths_skips_malformed_rows(tmp_path: Path) -> None:
    log_path = tmp_path / "radar_surfaced.jsonl"
    log_path.write_text(
        '{"date":"2026-05-01","path":"/v/A.md"}\n'
        'NOT JSON\n'
        '\n'  # blank line tolerated
        '{"date":"2026-05-01","path":"/v/B.md"}\n',
        encoding="utf-8",
    )
    paths = load_surfaced_paths(log_path)
    assert paths == {"/v/A.md", "/v/B.md"}


def test_append_surfaced_writes_one_row_per_item(
    tmp_path: Path, vault_with_one_record: Path,
) -> None:
    items, _ = rank_day(vault_with_one_record, top_n=5, now=NOW)
    log_path = tmp_path / "radar_surfaced.jsonl"
    append_surfaced(log_path, items, date(2026, 5, 2))
    assert log_path.is_file()
    rows = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 1
    parsed = json.loads(rows[0])
    assert parsed["date"] == "2026-05-02"
    assert parsed["type"] == "synthesis"
    assert "Test Synthesis.md" in parsed["path"]


def test_append_surfaced_appends_to_existing(
    tmp_path: Path, vault_with_one_record: Path,
) -> None:
    log_path = tmp_path / "radar_surfaced.jsonl"
    log_path.write_text(
        '{"date":"2026-05-01","path":"/v/old.md","score":5.0,"type":"decision"}\n',
        encoding="utf-8",
    )
    items, _ = rank_day(vault_with_one_record, top_n=5, now=NOW)
    append_surfaced(log_path, items, date(2026, 5, 2))
    rows = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 2  # 1 prior + 1 appended


def test_append_surfaced_empty_items_is_noop(tmp_path: Path) -> None:
    log_path = tmp_path / "radar_surfaced.jsonl"
    append_surfaced(log_path, [], date(2026, 5, 2))
    # Empty items → no file created, no rows written.
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# render_daily_file — empty-state + happy path
# ---------------------------------------------------------------------------


def test_render_daily_file_empty_state_explicit() -> None:
    """Per feedback_intentionally_left_blank.md — explicit empty state."""
    out = render_daily_file([], date(2026, 5, 2), ranker_count=0)
    assert "# Daily radar — 2026-05-02" in out
    assert "no radar items today" in out
    assert "synthesis/, decision/, contradiction/" in out
    # Even at 0 ranker_count, the line should still acknowledge the
    # corpus was scanned. Distinguishing "ran-and-found-nothing" from
    # "didn't run" is the contract.
    assert "ranker scanned 0 candidate(s)" in out


def test_render_daily_file_empty_state_with_dedup_filter() -> None:
    """Empty after dedup — the line should still render, and the
    'ranker scanned' tail acknowledges the candidates were considered.
    """
    out = render_daily_file([], date(2026, 5, 2), ranker_count=4)
    assert "no radar items today" in out
    assert "ranker scanned 4 candidate(s)" in out


def test_render_daily_file_with_items(vault_with_one_record: Path) -> None:
    items, raw_count = rank_day(vault_with_one_record, top_n=5, now=NOW)
    out = render_daily_file(items, date(2026, 5, 2), ranker_count=raw_count)
    assert "# Daily radar — 2026-05-02" in out
    assert "## Top 1 (1 ranked)" in out
    # Item heading: "### 1. Synthesis: ..."
    assert "### 1. Synthesis:" in out
    # Score breakdown line.
    assert "cross_source=" in out
    assert "type_weight=" in out


def test_render_daily_file_dedup_count_in_header() -> None:
    """When ranker_count > items, the 'deduped' tail appears."""
    # We don't need a real RankedRecord for the header check — just an
    # empty list with a non-zero ranker_count would be the empty case;
    # so synthesise a fake item via the fixture path.
    from alfred.distiller.synthesis_ranker import RankedRecord, ScoreBreakdown
    fake = RankedRecord(
        path=Path("/v/synthesis/Fake.md"),
        record_type="synthesis",
        score=10.0,
        frontmatter={"claim": "fake"},
        body="",
        breakdown=ScoreBreakdown(
            cross_source=6.0, entity_diversity=2.0,
            recency=1.0, type_weight=3.0,
        ),
        source_count=2,
        entity_count=1,
        age_days=0.5,
    )
    out = render_daily_file([fake], date(2026, 5, 2), ranker_count=4)
    # ranker_count=4, items=1 → "3 deduped" tail.
    assert "## Top 1 (4 ranked, 3 deduped)" in out


# ---------------------------------------------------------------------------
# run_daily_radar — end-to-end
# ---------------------------------------------------------------------------


def test_run_daily_radar_writes_file_and_log(
    tmp_path: Path, vault_with_one_record: Path,
) -> None:
    digests_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = run_daily_radar(
        vault_with_one_record,
        digests_dir,
        state_dir,
        top_n=5,
        today=date(2026, 5, 2),
        now=NOW,
    )

    # Result summary
    assert result.date == "2026-05-02"
    assert len(result.items) == 1
    assert result.ranker_count == 1
    assert result.dry_run is False

    # Daily file written under digests/daily/.
    daily_file = digests_dir / "daily" / "2026-05-02.md"
    assert daily_file.is_file()
    text = daily_file.read_text(encoding="utf-8")
    assert "# Daily radar — 2026-05-02" in text
    assert "### 1. Synthesis:" in text

    # Surfaced log appended.
    log_path = state_dir / "radar_surfaced.jsonl"
    assert log_path.is_file()
    rows = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["date"] == "2026-05-02"


def test_run_daily_radar_dry_run_writes_nothing(
    tmp_path: Path, vault_with_one_record: Path,
) -> None:
    digests_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = run_daily_radar(
        vault_with_one_record,
        digests_dir,
        state_dir,
        top_n=5,
        today=date(2026, 5, 2),
        now=NOW,
        dry_run=True,
    )

    # Result still carries the items + counts.
    assert result.dry_run is True
    assert len(result.items) == 1

    # But neither the daily file nor the surfaced log were written.
    daily_file = digests_dir / "daily" / "2026-05-02.md"
    log_path = state_dir / "radar_surfaced.jsonl"
    assert not daily_file.exists()
    assert not log_path.exists()


def test_run_daily_radar_dedup_round_trip(
    tmp_path: Path, vault_with_one_record: Path,
) -> None:
    """First fire surfaces, second fire dedups. The load-bearing
    Phase 3a contract: a record surfaced today doesn't re-surface
    tomorrow when it stays in the top-N."""
    digests_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    first = run_daily_radar(
        vault_with_one_record, digests_dir, state_dir,
        top_n=5, today=date(2026, 5, 2), now=NOW,
    )
    assert len(first.items) == 1

    # Second fire same day — surfaced log now contains the record, so
    # it should NOT re-surface. Empty-state line should appear in the
    # rendered file.
    second = run_daily_radar(
        vault_with_one_record, digests_dir, state_dir,
        top_n=5, today=date(2026, 5, 2), now=NOW,
    )
    assert len(second.items) == 0
    # Ranker still saw the record (raw count = 1); dedup hid it.
    assert second.ranker_count == 1

    # The day's file got rewritten with the empty-state copy. The log
    # was NOT appended (no items to append).
    log_path = state_dir / "radar_surfaced.jsonl"
    rows = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 1


def test_run_daily_radar_empty_vault_writes_empty_state_file(
    tmp_path: Path, empty_vault: Path,
) -> None:
    digests_dir = tmp_path / "digests"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = run_daily_radar(
        empty_vault, digests_dir, state_dir,
        top_n=5, today=date(2026, 5, 2), now=NOW,
    )

    assert len(result.items) == 0
    daily_file = digests_dir / "daily" / "2026-05-02.md"
    assert daily_file.is_file()
    text = daily_file.read_text(encoding="utf-8")
    # Explicit empty-state per intentionally-left-blank principle.
    assert "no radar items today" in text


# ---------------------------------------------------------------------------
# latest_daily_path helper (used by Phase 3b's section provider)
# ---------------------------------------------------------------------------


def test_latest_daily_path_returns_none_when_missing(tmp_path: Path) -> None:
    digests_dir = tmp_path / "digests"
    assert latest_daily_path(digests_dir, today=date(2026, 5, 2)) is None


def test_latest_daily_path_returns_file_when_present(tmp_path: Path) -> None:
    digests_dir = tmp_path / "digests"
    (digests_dir / "daily").mkdir(parents=True)
    target = digests_dir / "daily" / "2026-05-02.md"
    target.write_text("# Daily radar — 2026-05-02\n", encoding="utf-8")
    found = latest_daily_path(digests_dir, today=date(2026, 5, 2))
    assert found == target
