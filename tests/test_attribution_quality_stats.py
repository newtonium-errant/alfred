"""#72 items 2 + 6 — the quality stat line, and how each confirmed_via weighs.

Item 2 is the visible half: a periodic "attribution: N auto-confirmed, M
contested (14-day)" wherever the operator already looks. Item 6 is the half that
decides what those numbers MEAN, and it is the one worth arguing about.

## The weighting call (item 6) — tighter than the stated prior

The demotion trigger answers exactly one question: *is auto-confirm letting
wrong inferences through?* Only one kind of contest is evidence about that.

  * ``timeout_24h`` — the machine confirmed it unreviewed and it was WRONG.
    This is the whole case for demotion. COUNTS.
  * unconfirmed (contested while still inside its window) — the operator caught
    it in the FYI tier before the clock ran out. That is the current design
    WORKING; counting it would demote the tier for succeeding. Stats only.
  * ``backfill`` — swept in at deploy, never offered under these rules (the
    ratified #63a argument). Evidence about historical inference quality, not
    about the policy. Stats only.
  * ``operator`` — he reviewed it and approved it himself, then changed his
    mind. Returning these cards to review would not have caught it, because
    review is exactly what already happened. Stats only, and arguably evidence
    AGAINST demotion.

The stated prior excluded only ``backfill``. This excludes ``operator`` and the
unconfirmed case too, on the same principle: the trigger must count only
contests that REVIEW WOULD HAVE PREVENTED. Flagged for overrule — it is a
judgement about what the operator is being asked to approve, not a detail.

Per-section stats count EVERY contest regardless of via, because that question
is different: "which section produces bad inferences" doesn't care how the entry
was later confirmed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync.attribution_quality import (
    QUALITY_EVENT,
    attribution_quality_stats,
    render_quality_line,
)

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(action: str, *, days_ago: float = 1, via: str = "", section: str = "") -> dict:
    return {
        "type": f"attribution_{action}",
        "marker_id": f"m-{action}-{days_ago}-{via}-{section}",
        "record_path": "session/A.md",
        "agent": "salem",
        "section_title": "Structured Summary",
        "marker_date": _iso(NOW - timedelta(days=days_ago + 1)),
        "andrew_action": action,
        "action_at": _iso(NOW - timedelta(days=days_ago)),
        "confirmed_via": via,
        "section": section,
    }


def _stats(tmp_path: Path, rows: list[dict], **kw):
    path = tmp_path / "corpus.jsonl"
    _rows(path, rows)
    return attribution_quality_stats(path, now=NOW, **kw)


# --- the counts -------------------------------------------------------------


def test_counts_auto_confirms_and_contests_in_the_window(tmp_path: Path) -> None:
    s = _stats(tmp_path, [
        _row("auto_confirm", days_ago=1, via="timeout_24h"),
        _row("auto_confirm", days_ago=2, via="timeout_24h"),
        _row("contest", days_ago=3, via="timeout_24h"),
    ])
    assert s.auto_confirmed == 2
    assert s.contested == 1
    assert s.window_days == 14


def test_activity_outside_the_window_is_excluded(tmp_path: Path) -> None:
    s = _stats(tmp_path, [
        _row("auto_confirm", days_ago=1, via="timeout_24h"),
        _row("auto_confirm", days_ago=40, via="timeout_24h"),
        _row("contest", days_ago=40, via="timeout_24h"),
    ])
    assert s.auto_confirmed == 1
    assert s.contested == 0


def test_the_window_is_a_parameter_not_a_literal(tmp_path: Path) -> None:
    """Contract item 3: defaults are config-backed. A caller passing 30 must
    get 30 — otherwise the config knob is decorative."""
    rows = [_row("contest", days_ago=20, via="timeout_24h")]
    assert _stats(tmp_path, rows).contested == 0
    assert _stats(tmp_path, rows, window_days=30).contested == 1


def test_an_unparseable_action_date_is_skipped_not_fatal(tmp_path: Path) -> None:
    bad = _row("contest", via="timeout_24h")
    bad["action_at"] = "not-a-date"
    s = _stats(tmp_path, [bad, _row("contest", days_ago=1, via="timeout_24h")])
    assert s.contested == 1


# --- item 6: what counts toward DEMOTION vs toward stats ---------------------


def test_only_timeout_contests_drive_the_demotion_trigger(tmp_path: Path) -> None:
    """The load-bearing weighting pin. All four contests are real and all four
    show in `contested`; only the timeout one is evidence that REVIEW WOULD
    HAVE PREVENTED anything."""
    s = _stats(tmp_path, [
        _row("contest", days_ago=1, via="timeout_24h"),
        _row("contest", days_ago=2, via="backfill"),
        _row("contest", days_ago=3, via="operator"),
        _row("contest", days_ago=4, via=""),  # contested while still open
    ])
    assert s.contested == 4, "every contest is real and is reported"
    assert s.demotion_contests == 1, "only the unreviewed-and-wrong one counts"


def test_backfill_contests_still_reach_the_per_section_stats(tmp_path: Path) -> None:
    """Excluded from the trigger, INCLUDED in per-section rates — the two
    questions are different, and a backfill contest is still evidence about
    which section produces bad inferences."""
    s = _stats(tmp_path, [
        _row("contest", days_ago=1, via="backfill", section="Topics"),
        _row("contest", days_ago=2, via="operator", section="Topics"),
    ])
    assert s.demotion_contests == 0
    assert s.contests_by_section["Topics"] == 2


def test_a_contest_with_no_section_is_tracked_as_unknown(tmp_path: Path) -> None:
    """Card-level contest stays allowed (contract item 4) — it must not vanish
    from the stats just because the operator didn't name a section."""
    s = _stats(tmp_path, [_row("contest", days_ago=1, via="timeout_24h")])
    assert s.contests_by_section["unknown"] == 1


# --- ILB --------------------------------------------------------------------


def test_a_quiet_window_still_renders_a_line(tmp_path: Path) -> None:
    """ILB, contract item 2: every render including zero-activity. A metric
    that prints only when it has news is indistinguishable from one that
    stopped running."""
    s = _stats(tmp_path, [])
    line = render_quality_line(s)
    assert line
    assert "14" in line
    assert "0" in line


def test_the_line_reports_both_numbers(tmp_path: Path) -> None:
    s = _stats(tmp_path, [
        _row("auto_confirm", days_ago=1, via="timeout_24h"),
        _row("contest", days_ago=1, via="timeout_24h"),
    ])
    line = render_quality_line(s)
    assert "1 auto-confirmed" in line
    assert "1 contested" in line


def test_the_stats_emit_one_grepable_event(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as cap:
        _stats(tmp_path, [_row("contest", days_ago=1, via="timeout_24h")])
    matches = [c for c in cap if c.get("event") == QUALITY_EVENT]
    assert len(matches) == 1
    assert matches[0]["contested"] == 1
    assert matches[0]["demotion_contests"] == 1


def test_the_event_fires_on_the_zero_path_too(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as cap:
        _stats(tmp_path, [])
    matches = [c for c in cap if c.get("event") == QUALITY_EVENT]
    assert len(matches) == 1
    assert matches[0]["auto_confirmed"] == 0


def test_a_missing_corpus_is_a_quiet_window_not_an_error(tmp_path: Path) -> None:
    s = attribution_quality_stats(tmp_path / "absent.jsonl", now=NOW)
    assert s.auto_confirmed == 0 and s.contested == 0
    assert render_quality_line(s)


# --- the denominator can't shrink in silence --------------------------------


def test_rows_the_reader_declined_are_counted_and_logged(tmp_path: Path) -> None:
    """A broken writer and a quiet fortnight both produce low counts. Only this
    field tells them apart, so it has to reach the event."""
    path = tmp_path / "corpus.jsonl"
    path.write_text(
        "\n".join([
            "{not json",
            json.dumps({"nothing": "useful"}),
            json.dumps(_row("contest", days_ago=1, via="timeout_24h")),
        ]) + "\n",
        encoding="utf-8",
    )
    with structlog.testing.capture_logs() as cap:
        s = attribution_quality_stats(path, now=NOW)

    assert s.contested == 1
    assert s.unreadable_rows == 2
    matches = [c for c in cap if c.get("event") == QUALITY_EVENT]
    assert len(matches) == 1
    assert matches[0]["unreadable_rows"] == 2


def test_an_undated_row_is_counted_apart_from_an_unreadable_one(
    tmp_path: Path,
) -> None:
    """Different causes, different fixes: bad JSON is a writer emitting garbage,
    a bad timestamp is a format drifting. One number would hide which."""
    bad = _row("contest", via="timeout_24h")
    bad["action_at"] = "not-a-date"
    with structlog.testing.capture_logs() as cap:
        s = _stats(tmp_path, [bad, _row("contest", days_ago=1, via="timeout_24h")])

    assert s.contested == 1
    assert s.undated_rows == 1
    assert s.unreadable_rows == 0, "it parsed fine — only its date didn't"
    matches = [c for c in cap if c.get("event") == QUALITY_EVENT]
    assert matches[0]["undated_rows"] == 1


def test_a_row_merely_outside_the_window_is_not_a_decline(tmp_path: Path) -> None:
    """The window excluding old activity is the metric working. Counting that
    as a declined row would make every healthy corpus look damaged."""
    s = _stats(tmp_path, [_row("contest", days_ago=40, via="timeout_24h")])
    assert s.contested == 0
    assert s.undated_rows == 0 and s.unreadable_rows == 0


def test_both_decline_counts_ride_the_zero_path(tmp_path: Path) -> None:
    """ILB: the healthy answer is an explicit zero, not an absent key."""
    with structlog.testing.capture_logs() as cap:
        _stats(tmp_path, [])
    matches = [c for c in cap if c.get("event") == QUALITY_EVENT]
    assert matches[0]["unreadable_rows"] == 0
    assert matches[0]["undated_rows"] == 0


# --- the provider actually renders it (the gate-param trap) ------------------


def test_the_section_provider_renders_the_line_end_to_end(tmp_path: Path) -> None:
    """Everything above tests the helper. All of it stays green if the section
    provider never calls it — a computed-but-unrendered metric is the
    accepted-then-ignored shape. This drives the REAL provider.
    """
    from alfred.daily_sync import attribution_section as asec
    from alfred.daily_sync.config import AttributionConfig, DailySyncConfig

    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    corpus = tmp_path / "corpus.jsonl"
    _rows(corpus, [_row("auto_confirm", days_ago=1, via="timeout_24h")])

    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution = AttributionConfig(
        enabled=True, batch_size=5, scan_paths=[], corpus_path=str(corpus),
    )
    asec.set_vault_path(vault)

    out = asec.attribution_audit_section(cfg, date(2026, 8, 10))

    assert out is not None
    assert "Attribution quality:" in out, "the provider never rendered the metric"
    assert "1 auto-confirmed" in out


def test_the_config_window_reaches_the_provider(tmp_path: Path) -> None:
    """The knob is real, not decorative: a 30-day window must include activity
    a 14-day one excludes, THROUGH the provider."""
    from alfred.daily_sync import attribution_section as asec
    from alfred.daily_sync.config import AttributionConfig, DailySyncConfig

    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    corpus = tmp_path / "corpus.jsonl"
    _rows(corpus, [_row("contest", days_ago=20, via="timeout_24h")])

    def _render(window: int) -> str:
        cfg = DailySyncConfig(enabled=True, batch_size=5)
        cfg.state.path = str(tmp_path / "state.json")
        cfg.attribution = AttributionConfig(
            enabled=True, batch_size=5, scan_paths=[], corpus_path=str(corpus),
        )
        cfg.attribution.quality_window_days = window
        asec.set_vault_path(vault)
        return asec.attribution_audit_section(cfg, date(2026, 8, 10)) or ""

    assert "0 contested" in _render(14)
    assert "1 contested" in _render(30)
