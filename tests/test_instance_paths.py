"""Pins for the shared instance-data-dir resolver (#74 batch 1).

Four modules derive their default data paths through
``alfred.common.instance_paths``. The load-bearing properties are (1) Salem's
resolved string is BYTE-IDENTICAL to the cwd-relative literal it replaced —
that identity is the whole reason the retrofit needs no data migration on the
box — and (2) co-located instances resolve DIFFERENT paths, which is the
property the cwd-relative literal did not have.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from alfred.common.instance_paths import (
    LEGACY_DATA_DIR,
    configured_logging_dir,
    instance_data_dir,
    instance_data_path,
)

# A KAL-LE-shaped absolute data dir, named so a reviewer can see the fixture is
# a second instance rather than an arbitrary tmp path.
_KALLE_DATA_DIR = "/home/andrew/.alfred/kalle/data"


# --- configured_logging_dir: "configured" vs "absent" -----------------------

def test_reads_logging_dir() -> None:
    assert configured_logging_dir({"logging": {"dir": _KALLE_DATA_DIR}}) == _KALLE_DATA_DIR


def test_absent_block_is_none() -> None:
    assert configured_logging_dir({}) is None


def test_non_dict_logging_block_is_none() -> None:
    # A malformed config must not crash the resolver (``logging: somestring``).
    assert configured_logging_dir({"logging": "nonsense"}) is None
    assert configured_logging_dir({"logging": None}) is None


def test_blank_dir_is_none_not_empty_string() -> None:
    # The hole in the inline ``.get("dir", "./data")`` idiom: an explicitly
    # blank dir comes back as "" and joins into the ROOT-anchored "/scribe".
    assert configured_logging_dir({"logging": {"dir": ""}}) is None
    assert configured_logging_dir({"logging": {"dir": "   "}}) is None


def test_surrounding_whitespace_stripped() -> None:
    assert configured_logging_dir({"logging": {"dir": "  /x/data  "}}) == "/x/data"


# --- instance_data_dir: the fallback ----------------------------------------

def test_falls_back_to_legacy_when_unconfigured() -> None:
    assert instance_data_dir({}) == LEGACY_DATA_DIR == "./data"
    assert instance_data_dir({"logging": {"dir": ""}}) == "./data"


# --- instance_data_path: byte-identity + per-instance distinctness ----------

def test_salem_path_is_byte_identical_to_the_literal() -> None:
    # Salem's logging.dir IS "./data", so every derived default reproduces the
    # exact cwd-relative string it replaced — zero data movement on the box.
    # Mutation check: joining via pathlib normalises "./data" to "data" and
    # reddens this.
    raw = {"logging": {"dir": "./data"}}
    assert instance_data_path(raw, "voice_calibration") == "./data/voice_calibration"
    assert instance_data_path(raw, "canonical_audit.jsonl") == "./data/canonical_audit.jsonl"
    assert instance_data_path(raw, "scribe", "inbox") == "./data/scribe/inbox"


def test_unconfigured_path_is_byte_identical_too() -> None:
    # A config with no logging block at all (the minimal-test-fixture shape)
    # must also reproduce the literal, or the retrofit changes suite behaviour.
    assert instance_data_path({}, "voice_calibration") == "./data/voice_calibration"


def test_paths_are_instance_scoped_and_distinct() -> None:
    salem = instance_data_path({"logging": {"dir": "./data"}}, "canonical_audit.jsonl")
    kalle = instance_data_path({"logging": {"dir": _KALLE_DATA_DIR}}, "canonical_audit.jsonl")
    assert kalle == f"{_KALLE_DATA_DIR}/canonical_audit.jsonl"
    # The invariant the cwd-relative literal lacked: co-located instances
    # resolve DIFFERENT files.
    assert salem != kalle


def test_trailing_slash_does_not_double() -> None:
    assert instance_data_path({"logging": {"dir": "/x/data/"}}, "y") == "/x/data/y"


def test_empty_parts_dropped() -> None:
    assert instance_data_path({"logging": {"dir": "/x"}}, "", "y") == "/x/y"


# --- feed keeps its own extra rung, on top of the shared read ---------------

def test_feed_still_prefers_logging_dir_then_daily_sync() -> None:
    # feed layers a daily_sync rung under logging.dir; pin that delegating the
    # logging.dir read did not drop it. There is no third rung — an unanchored
    # config yields None, and the feed turns itself off rather than guessing
    # the cwd (see tests/feed/test_feed_config.py).
    from alfred.feed.config import _instance_data_dir

    assert _instance_data_dir({"logging": {"dir": _KALLE_DATA_DIR}}) == _KALLE_DATA_DIR
    assert _instance_data_dir(
        {"daily_sync": {"state": {"path": f"{_KALLE_DATA_DIR}/daily_sync_state.json"}}},
    ) == _KALLE_DATA_DIR
    assert _instance_data_dir({}) is None
    # And the blank-dir hole is closed for feed too, via the shared read — a
    # blank dir falls THROUGH to the daily_sync rung rather than joining "".
    assert _instance_data_dir({"logging": {"dir": "  "}}) is None
    assert _instance_data_dir({
        "logging": {"dir": "  "},
        "daily_sync": {"state": {"path": f"{_KALLE_DATA_DIR}/s.json"}},
    }) == _KALLE_DATA_DIR
