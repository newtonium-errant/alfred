"""Daemon-level tests for the inbox-stage preference filter (P10 / Ship 3).

Covers the curator daemon's wiring of
:func:`alfred.curator.pipeline._apply_inbox_preference_filter`:

    * ``mark_filtered`` writes sidecar frontmatter (status,
      filtered_at, filtered_by_preference, filtered_reason) and moves
      the file to processed/
    * The daily-summary stats bucket bumps + drains correctly
    * ``_maybe_emit_daily_filter_summary`` respects the
      Halifax-midnight first-tick semantics
    * ``_count_active_inbox_filter_prefs`` counts ONLY the
      inbox-filter rule, not other curator-domain prefs
    * The daily summary log emits with the correct shape and
      fires even on zero-drop days (intentionally-left-blank
      contract)

The full live ``_process_file`` integration (which depends on the
ClaudeBackend + Anthropic SDK + the inbox watcher) is exercised by
the live daemon at deployment time; these unit-level tests pin the
contract of each new helper directly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.curator import daemon as daemon_mod
from alfred.curator.writer import mark_filtered
from alfred.preferences.loader import Preference


@pytest.fixture(autouse=True)
def _reset_filter_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the daemon module's filter-stats + last-emit globals.

    Both are module-level singletons; without isolation, test order
    determines outcomes. The pattern mirrors the talker heartbeat
    test fixture.
    """
    daemon_mod._inbox_filter_stats = {}
    daemon_mod._last_summary_emit = None
    yield
    daemon_mod._inbox_filter_stats = {}
    daemon_mod._last_summary_emit = None


# ---------------------------------------------------------------------------
# mark_filtered — sidecar frontmatter contract
# ---------------------------------------------------------------------------


def test_mark_filtered_writes_sidecar_frontmatter(tmp_path: Path) -> None:
    """Filtered inbox file gets status + filtered_at + filtered_by_preference + filtered_reason."""
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()

    inbox_file = inbox / "email-substack-sample.md"
    inbox_file.write_text(
        "---\n"
        "type: email\n"
        "from: writer@substack.com\n"
        "---\n\n"
        "(empty body)\n"
    )

    dest = mark_filtered(
        inbox_file,
        processed,
        preference_slug="skip-substack",
        reason="skip_inbox_if_sender_matches: sender 'writer@substack.com' matches pattern '*@substack.com'",
    )

    assert dest.exists()
    assert dest.parent == processed
    assert not inbox_file.exists()

    post = frontmatter.load(str(dest))
    assert post.metadata["status"] == "filtered_by_preference"
    assert post.metadata["filtered_by_preference"] == "skip-substack"
    assert "skip_inbox_if_sender_matches" in post.metadata["filtered_reason"]
    assert "filtered_at" in post.metadata
    # Original frontmatter preserved.
    assert post.metadata["from"] == "writer@substack.com"


def test_mark_filtered_handles_name_collision(tmp_path: Path) -> None:
    """Two filtered files with the same name → second gets ``_1`` suffix."""
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()

    # First file already moved.
    (processed / "substack-spam.md").write_text("existing")

    inbox_file = inbox / "substack-spam.md"
    inbox_file.write_text("---\nfoo: bar\n---\n\nbody\n")

    dest = mark_filtered(
        inbox_file,
        processed,
        preference_slug="slug",
        reason="reason",
    )
    assert dest.name == "substack-spam_1.md"
    assert (processed / "substack-spam.md").exists()  # original preserved
    assert dest.exists()


def test_mark_filtered_binary_file_skips_frontmatter(tmp_path: Path) -> None:
    """Binary inbox files (rare) move without frontmatter mutation.

    Binary files would have failed the sender-extract upstream so this
    path shouldn't fire in practice — but the contract matches
    :func:`mark_processed` for uniformity.
    """
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()

    inbox_file = inbox / "rogue.bin"
    inbox_file.write_bytes(b"\x00\x01\x02\xff\xfe" * 100)

    dest = mark_filtered(
        inbox_file,
        processed,
        preference_slug="bin",
        reason="r",
    )
    # File moved, content unchanged.
    assert dest.exists()
    assert not inbox_file.exists()
    assert dest.read_bytes().startswith(b"\x00\x01\x02")


# ---------------------------------------------------------------------------
# _filter_stats_bump
# ---------------------------------------------------------------------------


def test_filter_stats_bump_first_time_creates_entry() -> None:
    """First bump for a slug creates the entry at 1."""
    daemon_mod._filter_stats_bump("slug-a")
    assert daemon_mod._inbox_filter_stats == {"slug-a": 1}


def test_filter_stats_bump_repeats_increment() -> None:
    """Three bumps for the same slug land as count=3."""
    daemon_mod._filter_stats_bump("slug-a")
    daemon_mod._filter_stats_bump("slug-a")
    daemon_mod._filter_stats_bump("slug-a")
    assert daemon_mod._inbox_filter_stats == {"slug-a": 3}


def test_filter_stats_bump_distinct_slugs() -> None:
    """Distinct slugs track independently."""
    daemon_mod._filter_stats_bump("slug-a")
    daemon_mod._filter_stats_bump("slug-b")
    daemon_mod._filter_stats_bump("slug-a")
    assert daemon_mod._inbox_filter_stats == {"slug-a": 2, "slug-b": 1}


# ---------------------------------------------------------------------------
# _count_active_inbox_filter_prefs — decision flag #4
# ---------------------------------------------------------------------------


def _write_pref(
    dir_: Path,
    slug: str,
    *,
    rule: str = "skip_inbox_if_sender_matches",
    domain: str = "curator",
    status: str = "active",
    shape: str = "action",
) -> None:
    """Write a Shape A preference file with the given matcher rule."""
    body = (
        "---\n"
        f"type: preference\n"
        f"status: {status}\n"
        f"name: {slug}\n"
        f"shape: {shape}\n"
        f"scope: universal\n"
        f"matcher:\n"
        f"  domain: {domain}\n"
        f"  rule: {rule}\n"
        f"  args:\n"
        f"    sender_patterns:\n"
        f"      - '*@substack.com'\n"
        f"---\n\n"
        f"## Policy\nstub\n"
    )
    (dir_ / f"{slug}.md").write_text(body)


def test_count_active_inbox_filter_prefs_only_counts_inbox_rule(
    tmp_path: Path,
) -> None:
    """Decision flag #4 (operator-approved): ONLY the inbox-filter rule counts.

    Mixed-rule vault → summary reports only the inbox-filter prefs.
    """
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "inbox-one")
    _write_pref(pref_dir, "inbox-two")
    _write_pref(pref_dir, "event-pref", rule="skip_event_if")
    _write_pref(pref_dir, "brief-pref", rule="skip_brief_event_if")

    count = daemon_mod._count_active_inbox_filter_prefs(tmp_path)
    assert count == 2  # only inbox-one + inbox-two


def test_count_active_inbox_filter_prefs_excludes_revoked(
    tmp_path: Path,
) -> None:
    """Revoked prefs don't count even if they have the right rule."""
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "active-one")
    _write_pref(pref_dir, "revoked-one", status="revoked")

    count = daemon_mod._count_active_inbox_filter_prefs(tmp_path)
    assert count == 1


def test_count_active_inbox_filter_prefs_missing_preference_dir(
    tmp_path: Path,
) -> None:
    """No preference/ directory → returns 0 cleanly (no crash)."""
    count = daemon_mod._count_active_inbox_filter_prefs(tmp_path)
    assert count == 0


# ---------------------------------------------------------------------------
# _emit_daily_filter_summary — log shape + intentionally-left-blank
# ---------------------------------------------------------------------------


def test_emit_daily_summary_with_drops_has_full_shape(
    tmp_path: Path,
) -> None:
    """Populated stats dict → full event shape in the log emit."""
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "skip-substack")

    daemon_mod._filter_stats_bump("skip-substack")
    daemon_mod._filter_stats_bump("skip-substack")
    daemon_mod._filter_stats_bump("skip-other")

    with structlog.testing.capture_logs() as captured:
        daemon_mod._emit_daily_filter_summary(date(2026, 6, 6), tmp_path)

    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert len(matches) == 1
    event = matches[0]
    assert event["date"] == "2026-06-06"
    assert event["drops_today"] == 3
    assert event["drops_by_pref"] == {"skip-substack": 2, "skip-other": 1}
    assert event["prefs_active"] == 1


def test_emit_daily_summary_resets_stats(tmp_path: Path) -> None:
    """After emit, the stats bucket is empty so tomorrow starts fresh."""
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "skip-substack")

    daemon_mod._filter_stats_bump("skip-substack")
    daemon_mod._emit_daily_filter_summary(date(2026, 6, 6), tmp_path)
    assert daemon_mod._inbox_filter_stats == {}


def test_emit_daily_summary_zero_drops_still_fires(tmp_path: Path) -> None:
    """Intentionally-left-blank: zero-drop days still emit the summary.

    Per ``feedback_intentionally_left_blank.md``, an operator must be
    able to distinguish "filter alive, nothing to drop" from "filter
    silently broken." Without the empty-emit, a quiet day looks
    identical to a broken daemon.
    """
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "skip-substack")

    with structlog.testing.capture_logs() as captured:
        daemon_mod._emit_daily_filter_summary(date(2026, 6, 6), tmp_path)

    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert len(matches) == 1
    assert matches[0]["drops_today"] == 0
    assert matches[0]["drops_by_pref"] == {}


# ---------------------------------------------------------------------------
# _maybe_emit_daily_filter_summary — Halifax-midnight boundary
# ---------------------------------------------------------------------------


def test_first_tick_seeds_marker_without_emitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call after daemon start: seed _last_summary_emit, NO emit.

    Booting with an empty stats bucket and immediately emitting a
    summary for "whichever calendar day the daemon happens to start
    on" would replay a meaningless empty summary every restart.
    """
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()

    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 7),
    )

    with structlog.testing.capture_logs() as captured:
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)

    # No summary emit on first tick.
    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert matches == []
    # But the marker is seeded.
    assert daemon_mod._last_summary_emit == date(2026, 6, 7)


def test_same_day_subsequent_ticks_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-day ticks after the first one do not emit."""
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()

    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 7),
    )
    daemon_mod._maybe_emit_daily_filter_summary(tmp_path)  # seed

    with structlog.testing.capture_logs() as captured:
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)

    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert matches == []


def test_halifax_day_rolls_emit_fires_with_yesterday_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Day rolls → emit fires labeled with YESTERDAY (the stats accumulation day).

    Semantic: a drop at 23:59 Halifax-local on June 6 lands in the
    June 6 summary, NOT in the June 7 summary. The summary covers
    the day the stats accumulated against.
    """
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()
    _write_pref(pref_dir, "skip-substack")

    # Boot day: June 6.
    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 6),
    )
    daemon_mod._maybe_emit_daily_filter_summary(tmp_path)  # seed

    # Add some drops during June 6.
    daemon_mod._filter_stats_bump("skip-substack")
    daemon_mod._filter_stats_bump("skip-substack")

    # Roll to June 7.
    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 7),
    )
    with structlog.testing.capture_logs() as captured:
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)

    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert len(matches) == 1
    # Summary dated June 6, NOT June 7 — covers yesterday's drops.
    assert matches[0]["date"] == "2026-06-06"
    assert matches[0]["drops_today"] == 2
    # Marker advances to today (June 7).
    assert daemon_mod._last_summary_emit == date(2026, 6, 7)
    # Stats reset.
    assert daemon_mod._inbox_filter_stats == {}


def test_multi_day_skip_emits_once_for_most_recent_previous_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the daemon was paused for several days, ONE emit fires for the previous tracked day.

    Operational reality: a paused daemon (laptop suspend, network
    outage) wakes up days later. The summary fires ONCE labeled with
    the last-known day; intermediate days are not back-filled (no
    stats accumulated for them).
    """
    pref_dir = tmp_path / "preference"
    pref_dir.mkdir()

    # Seed on June 1.
    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 1),
    )
    daemon_mod._maybe_emit_daily_filter_summary(tmp_path)

    # Roll to June 5 directly.
    monkeypatch.setattr(
        daemon_mod, "_halifax_today", lambda: date(2026, 6, 5),
    )
    with structlog.testing.capture_logs() as captured:
        daemon_mod._maybe_emit_daily_filter_summary(tmp_path)

    matches = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_inbox_summary"
    ]
    assert len(matches) == 1
    # The marker was June 1 (the seeded day); summary dates that day.
    assert matches[0]["date"] == "2026-06-01"
