"""The swallowed belt — a feed failure must never reach the producer.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from alfred.feed.belt import try_feed_reconcile
from alfred.feed.model import FeedItem
from alfred.feed.store import FeedStore


class _RaisingStore(FeedStore):
    def reconcile(self, kind, open_items):  # type: ignore[override]
        raise RuntimeError("disk on fire")


def _item(kind: str, key: str) -> FeedItem:
    return FeedItem.create(kind=kind, stable_key=key, instance="salem", title="t")


def test_success_returns_counts_and_logs_ok(tmp_path: Path) -> None:
    store = FeedStore(tmp_path / "feed.jsonl")
    with structlog.testing.capture_logs() as cap:
        counts = try_feed_reconcile(store, "proposal", [_item("proposal", "c1")])
    assert counts == {"open": 1, "acted": 0, "suppressed": 0}
    ok = [c for c in cap if c.get("event") == "feed.reconcile"]
    assert len(ok) == 1
    assert ok[0]["ok"] is True
    assert ok[0]["kind"] == "proposal"
    assert ok[0]["open"] == 1
    assert ok[0]["acted"] == 0
    # Always emitted, including 0 — a card that stops re-appearing must be
    # explicable as a kept decision, not a silent producer.
    assert ok[0]["suppressed"] == 0


def test_ilb_ok_line_fires_even_on_empty_reconcile(tmp_path: Path) -> None:
    store = FeedStore(tmp_path / "feed.jsonl")
    with structlog.testing.capture_logs() as cap:
        try_feed_reconcile(store, "radar", [])
    ok = [c for c in cap if c.get("event") == "feed.reconcile"]
    assert len(ok) == 1 and ok[0]["open"] == 0  # ran, nothing to do — still emits


def test_belt_swallows_exception_and_never_raises(tmp_path: Path) -> None:
    """The load-bearing pin: a raising reconcile returns None + logs the failure,
    and does NOT propagate. Mutation-verify: strip the try/except in belt.py and
    this test reddens (the RuntimeError propagates out of try_feed_reconcile)."""
    store = _RaisingStore(tmp_path / "feed.jsonl")
    with structlog.testing.capture_logs() as cap:
        result = try_feed_reconcile(store, "proposal", [_item("proposal", "c1")])
    assert result is None  # swallowed, no raise
    failed = [c for c in cap if c.get("event") == "feed.reconcile_failed"]
    assert len(failed) == 1
    assert failed[0]["kind"] == "proposal"
    assert failed[0]["error_type"] == "RuntimeError"
