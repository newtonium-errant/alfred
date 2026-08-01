"""Tests for the cross-instance recall config layer (#20 S1).

The matrix-as-config: ``transport.recall`` (participation + per-asking-peer
type allowlist), loaded via the house ``load_from_unified`` pattern with the
STAY-C fence that FAILS LOUD at load in both directions.

Coverage (mandatory regression pins, run unconditionally):
    * Defaults — no ``recall`` section → disabled, empty peers (fail-closed).
    * Parsing — enabled + per-peer type allowlists; clamps + floors.
    * Schema tolerance both directions — unknown keys tolerated.
    * STAY-C fence — a config naming STAY-C as a recall peer, OR enabling
      participation on a STAY-C instance, raises ``RecallConfigError``.
"""

from __future__ import annotations

import pytest

from alfred.transport.config import (
    DEFAULT_RECALL_MAX_MATCHES,
    DEFAULT_RECALL_SNIPPET_MAX_CHARS,
    RECALL_MAX_MATCHES_CEILING,
    RecallConfig,
    RecallConfigError,
    _build_recall,
    is_stayc_peer_name,
    load_from_unified,
)


# ---------------------------------------------------------------------------
# is_stayc_peer_name — the single source of truth for the fence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["stay-c", "STAY-C", "stayc", "STAYC", "stay_c", " Stay-C ", "sTaY-c"],
)
def test_is_stayc_peer_name_matches_all_spellings(name: str) -> None:
    assert is_stayc_peer_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["salem", "kal-le", "hypatia", "vera", "stay", "staycation", "", "c"],
)
def test_is_stayc_peer_name_rejects_non_stayc(name: str) -> None:
    assert is_stayc_peer_name(name) is False


# ---------------------------------------------------------------------------
# Defaults + fail-closed
# ---------------------------------------------------------------------------


def test_recall_absent_defaults_disabled_empty() -> None:
    cfg = load_from_unified({"transport": {}})
    assert cfg.recall == RecallConfig()
    assert cfg.recall.enabled is False
    assert cfg.recall.peers == {}


def test_recall_default_constructed_is_fail_closed() -> None:
    r = RecallConfig()
    assert r.enabled is False
    assert r.peers == {}
    assert r.max_matches == DEFAULT_RECALL_MAX_MATCHES
    assert r.snippet_max_chars == DEFAULT_RECALL_SNIPPET_MAX_CHARS


# ---------------------------------------------------------------------------
# Parsing — the matrix
# ---------------------------------------------------------------------------


def test_recall_parses_enabled_and_peer_allowlists() -> None:
    cfg = load_from_unified({
        "transport": {
            "recall": {
                "enabled": True,
                "max_matches": 7,
                "snippet_max_chars": 300,
                "peers": {
                    "kal-le": {"types": ["person", "project", "task"]},
                    "hypatia": {"types": ["note", "session"]},
                },
            }
        }
    })
    r = cfg.recall
    assert r.enabled is True
    assert r.max_matches == 7
    assert r.snippet_max_chars == 300
    assert set(r.peers) == {"kal-le", "hypatia"}
    assert r.peers["kal-le"].types == ["person", "project", "task"]
    assert r.peers["hypatia"].types == ["note", "session"]


def test_recall_max_matches_clamped_to_ceiling() -> None:
    r = _build_recall({"enabled": True, "max_matches": 9999})
    assert r.max_matches == RECALL_MAX_MATCHES_CEILING


def test_recall_max_matches_floored_at_one() -> None:
    r = _build_recall({"enabled": True, "max_matches": 0})
    assert r.max_matches == 1


def test_recall_snippet_max_chars_floored_at_one() -> None:
    r = _build_recall({"enabled": True, "snippet_max_chars": -5})
    assert r.snippet_max_chars == 1


def test_recall_bad_numeric_falls_back_to_default() -> None:
    r = _build_recall({"enabled": True, "max_matches": "lots"})
    assert r.max_matches == DEFAULT_RECALL_MAX_MATCHES


def test_recall_peer_types_coerced_and_blanks_dropped() -> None:
    r = _build_recall({
        "enabled": True,
        "peers": {"kal-le": {"types": ["person", "", "  ", "task", 5]}},
    })
    # Non-str + blank entries dropped; str entries kept in order.
    assert r.peers["kal-le"].types == ["person", "task"]


def test_recall_peer_with_no_types_is_configured_but_empty() -> None:
    r = _build_recall({"enabled": True, "peers": {"kal-le": {}}})
    assert "kal-le" in r.peers
    assert r.peers["kal-le"].types == []


# ---------------------------------------------------------------------------
# Schema tolerance — both directions
# ---------------------------------------------------------------------------


def test_recall_tolerates_unknown_top_level_keys() -> None:
    # A newer version wrote a field this version doesn't know → ignored.
    r = _build_recall({
        "enabled": True,
        "future_field": {"nested": 1},
        "peers": {"kal-le": {"types": ["person"]}},
    })
    assert r.enabled is True
    assert r.peers["kal-le"].types == ["person"]


def test_recall_tolerates_unknown_peer_rule_keys() -> None:
    r = _build_recall({
        "enabled": True,
        "peers": {"kal-le": {"types": ["person"], "future_scope": "x"}},
    })
    assert r.peers["kal-le"].types == ["person"]


def test_recall_non_dict_block_yields_default() -> None:
    assert _build_recall("not-a-dict") == RecallConfig()
    assert _build_recall(None) == RecallConfig()


# ---------------------------------------------------------------------------
# STAY-C fence — fails loud at load, both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer_key", ["stay-c", "stayc", "STAY-C", "stay_c"])
def test_fence_rejects_stayc_named_as_peer(peer_key: str) -> None:
    with pytest.raises(RecallConfigError, match="STAY-C"):
        _build_recall({"enabled": True, "peers": {peer_key: {"types": ["note"]}}})


def test_fence_rejects_stayc_peer_even_when_disabled() -> None:
    # A latent STAY-C edge must never sit dormant waiting to be flipped on.
    with pytest.raises(RecallConfigError, match="STAY-C"):
        _build_recall({"enabled": False, "peers": {"stay-c": {"types": ["note"]}}})


def test_fence_rejects_participation_on_stayc_instance() -> None:
    with pytest.raises(RecallConfigError, match="STAY-C"):
        _build_recall({"enabled": True}, instance_name="STAY-C")


def test_fence_rejects_participation_on_stayc_via_load_from_unified() -> None:
    # The instance name is threaded from telegram.instance.name at load.
    with pytest.raises(RecallConfigError, match="STAY-C"):
        load_from_unified({
            "telegram": {"instance": {"name": "STAY-C"}},
            "transport": {"recall": {"enabled": True, "peers": {}}},
        })


def test_fence_allows_disabled_recall_on_stayc_instance() -> None:
    # STAY-C with NO participation legitimately answers nothing — no raise.
    cfg = load_from_unified({
        "telegram": {"instance": {"name": "STAY-C"}},
        "transport": {"recall": {"enabled": False}},
    })
    assert cfg.recall.enabled is False


def test_fence_allows_stayc_instance_with_no_recall_section() -> None:
    cfg = load_from_unified({
        "telegram": {"instance": {"name": "STAY-C"}},
        "transport": {},
    })
    assert cfg.recall == RecallConfig()


def test_fence_allows_normal_instance_enabling_recall() -> None:
    cfg = load_from_unified({
        "telegram": {"instance": {"name": "Salem"}},
        "transport": {
            "recall": {"enabled": True, "peers": {"kal-le": {"types": ["person"]}}}
        },
    })
    assert cfg.recall.enabled is True
    assert cfg.recall.peers["kal-le"].types == ["person"]
