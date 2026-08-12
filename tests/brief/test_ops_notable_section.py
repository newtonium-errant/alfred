"""Pins for §5 Operations as NOTABLE-EVENTS-ONLY (Phase C, item 4).

The delta predicate that already governed the ops feed cards now governs the
markdown section too. What is pinned:

  * the three quiet-looking outcomes stay distinguishable — unreadable, first
    render, and genuinely-nothing-moved are three different claims and only one
    of them is evidence the instance is fine;
  * a notable actually renders (the positive control for all of the above);
  * the #27 "medium emails waiting" line survives the cut on every path,
    because it is an open-item count and not a delta;
  * the baseline advances EXACTLY ONCE per brief run — the trap this design is
    shaped around, since a second call would compare against the first call's
    own writes and report a quiet morning on a day something moved.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from alfred.brief.ops_notable import (
    FIRST_RENDER_LINE,
    NOTHING_NOTABLE_LINE,
    UNREADABLE_LINE,
    OpsBaseline,
    load_baseline,
    ops_notable_feed_items,
    render_ops_notable_section,
    save_baseline,
)
from alfred.feed import FeedItem, FeedStore


def _item(title: str) -> FeedItem:
    return FeedItem.create(
        kind="ops_notable", stable_key="k", instance="salem", title=title,
        evidence={},
    )


# --- the three quiet outcomes stay distinguishable --------------------------


def test_unreadable_never_reads_as_quiet() -> None:
    """A source we could not read must not claim the metrics are steady. This
    is the direction the whole module is shaped against."""
    body = render_ops_notable_section(None, had_baseline=True)
    assert body == UNREADABLE_LINE
    assert NOTHING_NOTABLE_LINE not in body


def test_first_render_says_so_rather_than_claiming_quiet() -> None:
    """No baseline ⟹ nothing to compare against. "We compared and found
    nothing" would be a claim we have not earned on day one."""
    assert render_ops_notable_section([], had_baseline=False) == FIRST_RENDER_LINE


def test_genuinely_quiet_gets_the_ilb_one_liner() -> None:
    """Silence is not an option: an empty Operations section is
    indistinguishable from one that failed to run."""
    assert render_ops_notable_section([], had_baseline=True) == NOTHING_NOTABLE_LINE


def test_notables_render_as_anomaly_lines() -> None:
    """THE positive control for the three tests above — without it they would
    pass identically against a renderer that could never emit anything."""
    body = render_ops_notable_section(
        [_item("Spam quarantine up 3 (now 12 this week)"),
         _item("New agent-failure kind: rate_limit")],
        had_baseline=True,
    )
    assert body == (
        "- Spam quarantine up 3 (now 12 this week)\n"
        "- New agent-failure kind: rate_limit"
    )
    assert NOTHING_NOTABLE_LINE not in body


# --- the medium-waiting line survives the notable-only cut ------------------


def _feed_store_with_medium(tmp_path: Path, n: int) -> str:
    """Seed via the real ``FeedStore`` rather than hand-rolled JSONL — a
    hand-written record that silently fails to load would make every
    assertion below pass against a zero count."""
    path = str(tmp_path / "feed.jsonl")
    store = FeedStore(path)
    for i in range(n):
        store.upsert(FeedItem.create(
            kind="email_tier", stable_key=f"email-{i}", instance="salem",
            title=f"mail {i}",
            evidence={"classifier_priority": "medium", "record_path": f"e{i}"},
        ))
    return path


def test_medium_waiting_line_rides_on_a_quiet_morning(tmp_path: Path) -> None:
    """An open-item COUNT is not a delta. A medium email that has waited three
    days is not news by the notable predicate and is exactly what the operator
    needs told — so it renders even when nothing moved."""
    body = render_ops_notable_section(
        [], had_baseline=True,
        feed_store_path=_feed_store_with_medium(tmp_path, 2),
    )
    assert NOTHING_NOTABLE_LINE in body
    assert "2 medium emails waiting" in body


def test_medium_waiting_line_rides_even_when_ops_is_unreadable(
    tmp_path: Path,
) -> None:
    """Held email must never be silently held, whatever the ops metrics did."""
    body = render_ops_notable_section(
        None, had_baseline=True,
        feed_store_path=_feed_store_with_medium(tmp_path, 1),
    )
    assert UNREADABLE_LINE in body
    assert "1 medium email waiting" in body


def test_medium_waiting_zero_state_is_explicit(tmp_path: Path) -> None:
    body = render_ops_notable_section(
        [], had_baseline=True,
        feed_store_path=_feed_store_with_medium(tmp_path, 0),
    )
    assert "No medium emails waiting" in body


# --- the once-per-run baseline advance --------------------------------------


def test_baseline_advances_once_and_a_second_call_sees_no_delta(
    tmp_path: Path,
) -> None:
    """THE trap this design is shaped around.

    ``ops_notable_feed_items`` advances the baseline as a side effect. If the
    markdown render and the feed projection each called it, the SECOND caller
    would compare against the first caller's own writes and report a quiet
    morning on a day something moved. This test documents that behaviour at the
    source so the "compute once, share the result" wiring in the daemon reads
    as necessary rather than as an optimisation.
    """
    data = tmp_path / "data"
    data.mkdir()
    vault = tmp_path / "vault"
    (vault / "quarantine").mkdir(parents=True)
    baseline = tmp_path / "ops_baseline.json"

    # A recorded baseline of zero, and one CRITICAL janitor issue now.
    save_baseline(baseline, OpsBaseline(
        quarantine_week=0, janitor_critical=0, curator_failed_files=0,
        seen_agent_failure_kinds=[], recorded=True,
    ))
    (data / "janitor_state.json").write_text(
        json.dumps({"sweeps": {"2026-08-12": {
            "timestamp": "2026-08-12T05:00:00+00:00",
            "issues_by_severity": {"CRITICAL": 1},
        }}}),
        encoding="utf-8",
    )

    first = ops_notable_feed_items(
        str(data), str(vault), str(baseline), instance="salem",
    )
    second = ops_notable_feed_items(
        str(data), str(vault), str(baseline), instance="salem",
    )

    assert first is not None and len(first) == 1, "the delta must fire once"
    assert second == [], "the SAME call again sees its own advanced baseline"
    assert load_baseline(baseline).janitor_critical == 1
