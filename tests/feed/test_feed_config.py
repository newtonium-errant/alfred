"""Feed config load pins — defaults, tool-scoped path, schema-tolerance.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from alfred.feed.config import FeedConfig, load_from_unified
from alfred.feed.store import DEFAULT_COMPACT_THRESHOLD_BYTES


def test_defaults_when_block_absent() -> None:
    cfg = load_from_unified({})
    assert cfg.enabled is True  # local write, belt-guarded → on by default
    assert cfg.store_path == "./data/feed_items.jsonl"  # tool-scoped default
    assert cfg.compact_threshold_bytes == DEFAULT_COMPACT_THRESHOLD_BYTES


def test_values_honoured() -> None:
    cfg = load_from_unified({
        "feed": {"enabled": False, "store_path": "/x/feed.jsonl", "compact_threshold_bytes": 123},
    })
    assert cfg.enabled is False
    assert cfg.store_path == "/x/feed.jsonl"
    assert cfg.compact_threshold_bytes == 123


def test_unknown_keys_ignored_forward_compat() -> None:
    cfg = load_from_unified({"feed": {"enabled": True, "future_knob": "x"}})
    assert isinstance(cfg, FeedConfig)
    assert cfg.enabled is True


def test_empty_block_is_defaults() -> None:
    assert load_from_unified({"feed": {}}).store_path == "./data/feed_items.jsonl"
