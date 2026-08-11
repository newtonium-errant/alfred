"""Feed config load pins — defaults, tool-scoped path, schema-tolerance.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from alfred.feed.config import FeedConfig, load_from_unified
from alfred.feed.store import DEFAULT_COMPACT_THRESHOLD_BYTES


def test_defaults_when_block_absent() -> None:
    # A config that names an instance data dir but no feed block: on by
    # default (local write, belt-guarded), path derived from logging.dir.
    cfg = load_from_unified({"logging": {"dir": "./data"}})
    assert cfg.enabled is True
    assert cfg.store_path == "./data/feed_items.jsonl"
    assert cfg.compact_threshold_bytes == DEFAULT_COMPACT_THRESHOLD_BYTES


def test_unanchored_config_is_off_not_cwd_relative() -> None:
    # #74: the resolver used to fall back to a cwd-relative "./data" when a
    # config named no data dir, which is how the suite wrote
    # data/feed_items.jsonl into the repo tree (the daily-sync fire path loads
    # this config from a raw dict with no logging block). There is no correct
    # answer for such a config, so the guess is gone and the feed goes OFF.
    cfg = load_from_unified({})
    assert cfg.store_path == ""
    assert cfg.enabled is False  # __post_init__ coercion — never a cwd write
    assert cfg.compact_threshold_bytes == DEFAULT_COMPACT_THRESHOLD_BYTES


def test_values_honoured() -> None:
    cfg = load_from_unified({
        "feed": {"enabled": False, "store_path": "/x/feed.jsonl", "compact_threshold_bytes": 123},
    })
    assert cfg.enabled is False
    assert cfg.store_path == "/x/feed.jsonl"
    assert cfg.compact_threshold_bytes == 123


def test_unknown_keys_ignored_forward_compat() -> None:
    # logging.dir present so the config is anchored — otherwise the #74
    # coercion would flip ``enabled``, and this test is about the schema
    # filter, not about anchoring.
    cfg = load_from_unified({
        "logging": {"dir": "./data"},
        "feed": {"enabled": True, "future_knob": "x"},
    })
    assert isinstance(cfg, FeedConfig)
    assert cfg.enabled is True


def test_empty_block_is_defaults() -> None:
    raw = {"logging": {"dir": "./data"}, "feed": {}}
    assert load_from_unified(raw).store_path == "./data/feed_items.jsonl"


# --- instance-scoped default store path (2026-07-31 cross-instance fix) -------
# On the box all instances share one cwd + differ only by --config, so a
# cwd-relative default was ONE shared file → KAL-LE's sync polluted Salem's feed.
# The default now anchors to the instance's own data dir (logging.dir).

# A KAL-LE-shaped config anchors its data dir off the box cwd; used across the
# distinct-path pins. Reused as a named constant so a reviewer sees the fixtures.
_KALLE_DATA_DIR = "/home/andrew/.alfred/kalle/data"


def test_salem_default_unchanged_zero_migration() -> None:
    # Salem's logging.dir is the legacy ./data → the resolved default is
    # BYTE-IDENTICAL to before, so its existing store carries over untouched.
    cfg = load_from_unified({"logging": {"dir": "./data"}})
    assert cfg.store_path == "./data/feed_items.jsonl"


def test_default_is_instance_scoped_and_distinct_across_instances() -> None:
    salem = load_from_unified({"logging": {"dir": "./data"}}).store_path
    kalle = load_from_unified({"logging": {"dir": _KALLE_DATA_DIR}}).store_path
    assert kalle == f"{_KALLE_DATA_DIR}/feed_items.jsonl"
    # The load-bearing invariant: co-located instances resolve DIFFERENT files.
    # Mutation check — revert to a cwd-relative default and both collapse to
    # "./data/feed_items.jsonl", reddening this.
    assert salem != kalle


def test_default_falls_back_to_daily_sync_state_dir() -> None:
    # No logging.dir → anchor on the primary producer's own per-instance state.
    cfg = load_from_unified(
        {"daily_sync": {"state": {"path": f"{_KALLE_DATA_DIR}/daily_sync_state.json"}}},
    )
    assert cfg.store_path == f"{_KALLE_DATA_DIR}/feed_items.jsonl"


def test_explicit_store_path_wins_over_instance_scope() -> None:
    cfg = load_from_unified(
        {"logging": {"dir": _KALLE_DATA_DIR}, "feed": {"store_path": "/x/feed.jsonl"}},
    )
    assert cfg.store_path == "/x/feed.jsonl"


def test_brief_producer_shares_the_one_resolver() -> None:
    # The three feed callers (daily_sync producer, brief producer, talker/transport
    # wiring) MUST resolve one path per instance. daily_sync + talker call
    # alfred.feed.load_from_unified inline; the brief config WRAPS it — pin that it
    # is the very same resolver (not a divergent copy) so brief can't drift.
    import alfred.brief.config as brief_config

    assert brief_config._load_feed_config is load_from_unified
    # And behaviourally: the same raw yields the same store path either way.
    raw = {"logging": {"dir": _KALLE_DATA_DIR}}
    assert brief_config._load_feed_config(raw).store_path == load_from_unified(raw).store_path
