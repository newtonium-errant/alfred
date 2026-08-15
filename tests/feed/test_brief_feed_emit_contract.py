"""Brief feed emit contract — failure ≠ emptiness, and event pref-alignment.

Pins the remediation-round contracts:
  * item 2: an extractor that returns None (or raises) leaves that kind's feed
    state UNTOUCHED (no spurious mass-acted); a genuine [] reconciles normally.
  * item 3: the event extractor threads the loaded operator prefs into
    _collect_items (so the feed matches the render, not a superset).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import structlog

from alfred.brief import daemon as brief_daemon
from alfred.brief import feed_producer as bfp
from alfred.brief.config import BriefConfig
from alfred.brief.section_result import SectionResult
from alfred.feed import (
    STATE_ACTED,
    STATE_OPEN,
    STATE_RETIRED,
    FeedConfig,
    FeedItem,
    FeedStore,
)

TODAY = date(2026, 7, 30)
NOW = datetime(2026, 7, 30, 6, 0, 0, tzinfo=timezone.utc)
TIER_HEADER = "Open Tasks by Tier"


def _slot(key: str) -> FeedItem:
    return FeedItem.create(kind="slot_suggestion", stable_key=key, instance="salem", title="t")


def _config_with_open_slot(tmp_path: Path) -> tuple[BriefConfig, FeedStore]:
    store_path = tmp_path / "feed.jsonl"
    store = FeedStore(store_path)
    store.reconcile("slot_suggestion", [_slot("task:task/X.md")])  # one open slot in the store
    assert store.load()["slot_suggestion:task:task/X.md"].state == STATE_OPEN
    cfg = BriefConfig(vault_path=str(tmp_path / "vault"), instance_name="salem")
    cfg.feed = FeedConfig(enabled=True, store_path=str(store_path))
    return cfg, store


def _emit(cfg: BriefConfig) -> list:
    with structlog.testing.capture_logs() as cap:
        brief_daemon._emit_brief_feed(cfg, [SectionResult(TIER_HEADER, "md", feed_kind="slot_suggestion")], TODAY, NOW)
    return cap


def test_extractor_none_leaves_kind_untouched(tmp_path: Path, monkeypatch) -> None:
    cfg, store = _config_with_open_slot(tmp_path)
    monkeypatch.setattr(bfp, "slot_suggestion_feed_items", lambda *a, **k: None)  # failure sentinel
    cap = _emit(cfg)
    # The previously-open slot must STILL be open — no reconcile happened.
    assert store.load()["slot_suggestion:task:task/X.md"].state == STATE_OPEN
    fails = [c for c in cap if c.get("event") == "brief.feed_extractor_failed"]
    assert len(fails) == 1 and fails[0]["kind"] == "slot_suggestion"


def test_extractor_raises_leaves_kind_untouched(tmp_path: Path, monkeypatch) -> None:
    cfg, store = _config_with_open_slot(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("compute blew up")

    monkeypatch.setattr(bfp, "slot_suggestion_feed_items", _boom)
    cap = _emit(cfg)
    assert store.load()["slot_suggestion:task:task/X.md"].state == STATE_OPEN  # untouched
    fails = [c for c in cap if c.get("event") == "brief.feed_extractor_failed"]
    assert len(fails) == 1 and fails[0]["error_type"] == "RuntimeError"


def test_genuine_empty_still_reconciles_to_acted(tmp_path: Path, monkeypatch) -> None:
    cfg, store = _config_with_open_slot(tmp_path)
    monkeypatch.setattr(bfp, "slot_suggestion_feed_items", lambda *a, **k: [])  # genuine empty read
    _emit(cfg)
    # Empty read → reconcile → the previously-open slot goes acted (decided).
    assert store.load()["slot_suggestion:task:task/X.md"].state == STATE_RETIRED


# --- item 3: event extractor threads operator prefs into _collect_items ------


def test_event_extractor_applies_operator_prefs(tmp_path: Path, monkeypatch) -> None:
    sentinel_prefs = ["PREF-A", "PREF-B"]
    captured: dict = {}

    monkeypatch.setattr("alfred.preferences.loader.load_active_preferences", lambda *a, **k: sentinel_prefs)

    def _fake_collect(vault_path, today, max_days, prefs=None):
        captured["prefs"] = prefs
        return ([], 0)

    monkeypatch.setattr("alfred.brief.upcoming_events._collect_items", _fake_collect)

    class _Cfg:
        max_days_ahead = 30

    result = bfp.event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    # The SAME prefs the render loads are passed to _collect_items (feed == render,
    # not a superset). Genuine-empty result is [] (a read, not a failure).
    assert captured["prefs"] == sentinel_prefs
    assert result == []
