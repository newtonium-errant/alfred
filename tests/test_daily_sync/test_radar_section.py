"""Tests for the Daily Sync radar section provider — Phase 3b.

Covers:
- Section returns ``None`` when the daily file is missing (instances
  that don't run radar stay unaffected).
- Section renders verbatim when the daily file is present, with the
  Phase 3a ``# Daily radar`` heading stripped.
- Empty-state daily file (Phase 3a wrote it but with the
  "no radar items today" body) still renders — explicit empty-state
  observability per ``feedback_intentionally_left_blank.md``.
- Item-summary parsing extracts (item_number, type, path, score,
  summary) and renumbers against ``start_index`` so global numbering
  stays continuous across sections.
- Provider registration is idempotent.
- Daemon wiring: the daily file's items show up in
  ``last_batch.radar_items`` after a fire.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alfred.daily_sync import assembler, radar_section
from alfred.daily_sync.config import DailySyncConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    assembler.clear_providers()
    radar_section._LAST_BATCH_HOLDER["items"] = []
    radar_section._DIGESTS_DIR_HOLDER.clear()
    yield
    assembler.clear_providers()
    radar_section._LAST_BATCH_HOLDER["items"] = []
    radar_section._DIGESTS_DIR_HOLDER.clear()


@pytest.fixture
def config() -> DailySyncConfig:
    return DailySyncConfig(enabled=True)


# ---------------------------------------------------------------------------
# build_batch + render_batch
# ---------------------------------------------------------------------------


def test_section_returns_none_when_digests_dir_unset(config) -> None:
    """No daemon wiring → the section omits silently. Defensive guard
    for tests that exercise the provider without going through the
    daemon's set_digests_dir() call."""
    out = radar_section.radar_section(config, date(2026, 5, 2))
    assert out is None


def test_section_returns_none_when_daily_file_missing(
    tmp_path: Path, config,
) -> None:
    """Phase 3a daemon hasn't fired yet today → section omits.
    Distinguishes 'radar disabled / not yet fired' from 'radar fired
    and found nothing'."""
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    radar_section.set_digests_dir(digests_dir)
    out = radar_section.radar_section(config, date(2026, 5, 2))
    assert out is None


def test_section_renders_empty_state_file(
    tmp_path: Path, config,
) -> None:
    """Phase 3a wrote a 'no radar items today' file → section still
    renders so operator sees radar ran-and-found-nothing."""
    digests_dir = tmp_path / "digests"
    daily_dir = digests_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-02.md").write_text(
        "# Daily radar — 2026-05-02\n\n"
        "no radar items today (corpus checked: synthesis/, "
        "decision/, contradiction/)\n\n"
        "_ranker scanned 0 candidate(s)._\n",
        encoding="utf-8",
    )
    radar_section.set_digests_dir(digests_dir)
    out = radar_section.radar_section(config, date(2026, 5, 2))
    assert out is not None
    # Header rendered.
    assert "## Distiller radar" in out
    # Phase 3a's top-line heading stripped (Daily Sync banner already
    # carries the date).
    assert "# Daily radar" not in out
    # Empty-state body preserved verbatim.
    assert "no radar items today" in out


def test_section_renders_items_file(tmp_path: Path, config) -> None:
    """Phase 3a wrote a populated daily file → section renders the
    body verbatim and parses items into the batch holder."""
    digests_dir = tmp_path / "digests"
    daily_dir = digests_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-02.md").write_text(
        "# Daily radar — 2026-05-02\n\n"
        "## Top 2 (5 ranked, 3 deduped)\n\n"
        '### 1. Synthesis: "Andrew prefers explicit over implicit." '
        "(score 16.30)\n"
        "    type: synthesis  src: 3  ent: 4  age: 0.42d\n"
        "    path: /v/synthesis/A.md\n"
        "    cross_source=9.00  entity_diversity=8.00  "
        "recency=0.30  type_weight=3.00\n\n"
        '### 2. Decision: "Use Claude Opus for KAL-LE coding." '
        "(score 8.50)\n"
        "    type: decision  src: 1  ent: 2  age: 0.10d\n"
        "    path: /v/decision/B.md\n"
        "    cross_source=3.00  entity_diversity=4.00  "
        "recency=0.50  type_weight=1.00\n",
        encoding="utf-8",
    )
    radar_section.set_digests_dir(digests_dir)
    out = radar_section.radar_section(config, date(2026, 5, 2))
    assert out is not None
    # Header reflects item count.
    assert "## Distiller radar (2 items)" in out
    # Body content preserved.
    assert "Andrew prefers explicit over implicit" in out
    assert "Use Claude Opus for KAL-LE coding" in out
    assert "## Top 2 (5 ranked, 3 deduped)" in out
    # Phase 3a's top-line heading stripped.
    assert "# Daily radar" not in out
    # Items captured for state persistence.
    items = radar_section.consume_last_batch()
    assert len(items) == 2
    assert items[0].record_type == "synthesis"
    assert items[0].record_path == "/v/synthesis/A.md"
    assert items[0].score == pytest.approx(16.30)
    assert "Andrew prefers explicit" in items[0].summary
    assert items[1].record_type == "decision"
    assert items[1].record_path == "/v/decision/B.md"


def test_section_singular_items_header(tmp_path: Path, config) -> None:
    digests_dir = tmp_path / "digests"
    daily_dir = digests_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-02.md").write_text(
        "# Daily radar — 2026-05-02\n\n"
        "## Top 1 (1 ranked)\n\n"
        '### 1. Synthesis: "single item" (score 12.00)\n'
        "    type: synthesis  src: 2  ent: 2  age: 0.5d\n"
        "    path: /v/synthesis/Solo.md\n"
        "    cross_source=6.00  entity_diversity=4.00  "
        "recency=0.50  type_weight=3.00\n",
        encoding="utf-8",
    )
    radar_section.set_digests_dir(digests_dir)
    out = radar_section.radar_section(config, date(2026, 5, 2))
    assert out is not None
    # Singular form when len(items) == 1.
    assert "## Distiller radar (1 item)" in out
    assert "(1 items)" not in out


# ---------------------------------------------------------------------------
# start_index renumbering — global numbering across sections
# ---------------------------------------------------------------------------


def test_items_renumbered_against_start_index(tmp_path: Path, config) -> None:
    """Daily file uses 1-based item numbers; the section provider
    re-stamps them against the global ``start_index`` so e.g. when
    email rendered 5 items above, radar's first item becomes #6."""
    digests_dir = tmp_path / "digests"
    daily_dir = digests_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-02.md").write_text(
        "# Daily radar — 2026-05-02\n\n"
        '### 1. Synthesis: "first" (score 10.00)\n'
        "    type: synthesis  src: 1  ent: 1  age: 0.1d\n"
        "    path: /v/A.md\n"
        '### 2. Decision: "second" (score 5.00)\n'
        "    type: decision  src: 1  ent: 1  age: 0.1d\n"
        "    path: /v/B.md\n",
        encoding="utf-8",
    )
    radar_section.set_digests_dir(digests_dir)
    out = radar_section.radar_section(config, date(2026, 5, 2), start_index=6)
    assert out is not None
    items = radar_section.consume_last_batch()
    assert len(items) == 2
    # When email renders items 1..5 above, radar items become #6 + #7.
    assert items[0].item_number == 6
    assert items[1].item_number == 7


# ---------------------------------------------------------------------------
# Registration + assembler integration
# ---------------------------------------------------------------------------


def test_register_idempotent() -> None:
    radar_section.register()
    radar_section.register()  # second call does NOT raise.
    names = assembler.registered_providers()
    assert names.count("radar") == 1


def test_radar_priority_between_proposals_and_attribution() -> None:
    """Priority 22 — between canonical proposals at 15 and attribution
    at 25. Asserting the priority pin keeps this contract grep-able."""
    radar_section.register()
    # Walk the internal registry to find our entry's priority.
    entry = next(
        (e for e in assembler._REGISTRY if e.name == "radar"),
        None,
    )
    assert entry is not None
    assert entry.priority == 22


def test_assembler_includes_radar_section(tmp_path: Path, config) -> None:
    """End-to-end: the assembler runs the radar provider and includes
    its rendered section in the message body."""
    digests_dir = tmp_path / "digests"
    daily_dir = digests_dir / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-02.md").write_text(
        "# Daily radar — 2026-05-02\n\n"
        "## Top 1 (1 ranked)\n\n"
        '### 1. Synthesis: "test" (score 10.00)\n'
        "    type: synthesis  src: 1  ent: 1  age: 0.1d\n"
        "    path: /v/A.md\n",
        encoding="utf-8",
    )
    radar_section.set_digests_dir(digests_dir)
    radar_section.register()
    body = assembler.assemble_message(config, date(2026, 5, 2))
    # Daily Sync banner.
    assert "Daily Sync — 2026-05-02" in body
    # Radar section rendered.
    assert "## Distiller radar (1 item)" in body
    assert "test" in body


def test_assembler_omits_radar_when_no_daily_file(
    tmp_path: Path, config,
) -> None:
    """When the daily file is absent, the assembler omits the section
    entirely — instances that don't run radar stay clean."""
    digests_dir = tmp_path / "digests"
    digests_dir.mkdir()
    radar_section.set_digests_dir(digests_dir)
    radar_section.register()
    body = assembler.assemble_message(config, date(2026, 5, 2))
    # No radar header; full message is the empty-Daily-Sync header.
    assert "## Distiller radar" not in body
    # Empty-Daily-Sync body fires when EVERY provider returned None.
    assert "No items today" in body
