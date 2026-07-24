"""Tests for the per-tier confidence flag persistence.

Covers:
- list_confidence with no state file uses seed defaults.
- set_confidence flips the flag and persists.
- Round-trip across multiple set_confidence calls.
- Unknown tier raises ValueError.
- format_confidence_report output shape.
- save/load_state atomicity (corrupt file doesn't crash).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.daily_sync.config import ConfidenceConfig
from alfred.daily_sync.confidence import (
    format_confidence_report,
    list_confidence,
    load_state,
    save_state,
    set_confidence,
)


def _seed() -> ConfidenceConfig:
    return ConfidenceConfig(high=False, medium=False, low=False, spam=False)


def test_list_confidence_no_state_uses_seed(tmp_path: Path):
    state_path = tmp_path / "state.json"
    flags = list_confidence(state_path, _seed())
    # #7 7c-i lockstep: the four priority tiers are unperturbed; ``filing`` (the topical-filing axis gate)
    # is the additive fifth key, defaulting False.
    assert flags == {"high": False, "medium": False, "low": False, "spam": False, "filing": False}


def test_set_confidence_persists_and_returns(tmp_path: Path):
    state_path = tmp_path / "state.json"
    flags = set_confidence(state_path, "high", True, seed=_seed())
    assert flags["high"] is True
    # Round-trip — reload from disk
    flags2 = list_confidence(state_path, _seed())
    assert flags2["high"] is True
    assert flags2["medium"] is False


def test_set_confidence_multiple_tiers(tmp_path: Path):
    state_path = tmp_path / "state.json"
    set_confidence(state_path, "high", True, seed=_seed())
    set_confidence(state_path, "spam", True, seed=_seed())
    flags = list_confidence(state_path, _seed())
    assert flags == {
        "high": True, "medium": False, "low": False, "spam": True, "filing": False,
    }


def test_set_confidence_unknown_tier_raises(tmp_path: Path):
    state_path = tmp_path / "state.json"
    with pytest.raises(ValueError, match="unknown tier"):
        set_confidence(state_path, "urgent", True, seed=_seed())


def test_load_state_corrupt_falls_back_to_empty(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    assert load_state(state_path) == {}


def test_save_state_round_trip(tmp_path: Path):
    state_path = tmp_path / "state.json"
    payload = {
        "confidence": {"high": True, "medium": False, "low": False, "spam": False},
        "last_fired_date": "2026-04-22",
    }
    save_state(state_path, payload)
    loaded = load_state(state_path)
    assert loaded == payload


def test_format_confidence_report_contains_all_tiers():
    flags = {"high": True, "medium": False, "low": False, "spam": True}
    out = format_confidence_report(flags)
    assert "high" in out
    assert "medium" in out
    assert "low" in out
    assert "spam" in out
    # ✅ for True, ⏳ for False
    lines = out.splitlines()
    high_line = next(line for line in lines if "high" in line)
    assert "✅" in high_line
    medium_line = next(line for line in lines if "medium" in line)
    assert "⏳" in medium_line


# ---------------------------------------------------------------------------
# #7 7c-i — the ``filing`` axis gate: additive, defaults False, flippable, tiers unperturbed
# ---------------------------------------------------------------------------


def test_filing_flag_defaults_false_and_tiers_unperturbed(tmp_path: Path):
    # Exact-set lockstep pin: the confidence flag set is EXACTLY the four priority tiers + filing.
    flags = list_confidence(tmp_path / "state.json", ConfidenceConfig())
    assert set(flags) == {"high", "medium", "low", "spam", "filing"}
    assert flags == {"high": False, "medium": False, "low": False, "spam": False, "filing": False}


def test_filing_flag_flippable_via_set_confidence(tmp_path: Path):
    # ``/calibration_ok filing`` routes here; the gate must be flippable + persist, independent of tiers.
    state_path = tmp_path / "state.json"
    flags = set_confidence(state_path, "filing", True, seed=ConfidenceConfig())
    assert flags["filing"] is True
    # Persists across reload; the four priority tiers are untouched.
    reloaded = list_confidence(state_path, ConfidenceConfig())
    assert reloaded["filing"] is True
    assert reloaded["high"] is False and reloaded["spam"] is False


def test_filing_flag_in_report():
    out = format_confidence_report({"high": False, "medium": False, "low": False, "spam": False, "filing": True})
    assert "filing" in out
