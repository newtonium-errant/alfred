"""NL-lane config layer — opt-in parsing + fail-closed back-compat pins.

The LLM-mediated opt-in lane (2026-06-10, ratified Decisions A-H) is
governed by TWO additive config surfaces:

  * ``transport.canonical.nl_broker`` — holder-global lane mechanics
    (master switch, model inherit, answer-shape limits). Absent block
    = default-constructed = DISABLED.
  * ``nl_query`` on each peer × type entry — per-pair enablement +
    compose-tier (``compose_fields``). Absent = ``None`` = DENIED.

BACK-COMPAT PIN (disclosure-safety ●): every pre-existing config —
which by definition carries neither block — must parse byte-identically
to pre-LLM-lane behavior: ``nl_query is None`` on every entry and
``nl_broker.enabled is False``.
"""

from __future__ import annotations

import structlog

from alfred.transport.config import (
    FILTER_LIMIT_CEILING,
    NLBrokerConfig,
    NLQueryRules,
    PeerQueryRules,
    load_from_unified,
)


def _perm_entry(**overrides: object) -> dict:
    """A minimal peer×type permissions entry with the P1 query block."""
    entry: dict = {
        "fields": ["name", "date", "participants"],
        "query": {
            "filter_dims": {
                "participants": {"op": ["eq", "contains"]},
                "date": {"op": ["gte", "lte", "between"]},
            },
            "sort": ["date"],
            "max_limit": 10,
            "default_limit": 5,
        },
    }
    entry.update(overrides)
    return entry


def _unified(event_entry: dict, nl_broker: dict | None = None) -> dict:
    canonical: dict = {
        "owner": True,
        "peer_permissions": {"hypatia": {"event": event_entry}},
    }
    if nl_broker is not None:
        canonical["nl_broker"] = nl_broker
    return {"transport": {"canonical": canonical}}


# ---------------------------------------------------------------------------
# Back-compat pins ● — existing configs parse identically
# ---------------------------------------------------------------------------


def test_existing_config_without_nl_blocks_parses_to_denied() -> None:
    """● PIN: a pre-LLM-lane config gets nl_query=None + broker disabled."""
    cfg = load_from_unified(_unified(_perm_entry()))
    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert rules.nl_query is None
    assert isinstance(rules.query, PeerQueryRules)  # P1 block untouched
    assert rules.fields == ["name", "date", "participants"]
    # Master switch defaults OFF.
    assert isinstance(cfg.canonical.nl_broker, NLBrokerConfig)
    assert cfg.canonical.nl_broker.enabled is False


def test_empty_unified_config_has_disabled_broker() -> None:
    cfg = load_from_unified({})
    assert cfg.canonical.nl_broker.enabled is False
    assert cfg.canonical.nl_broker.model == ""


# ---------------------------------------------------------------------------
# nl_broker block parsing
# ---------------------------------------------------------------------------


def test_nl_broker_block_parses_with_defaults_and_overrides() -> None:
    cfg = load_from_unified(_unified(
        _perm_entry(),
        nl_broker={
            "enabled": True,
            "max_answer_chars": 900,
            "verbatim_run_limit": 60,
        },
    ))
    broker = cfg.canonical.nl_broker
    assert broker.enabled is True
    assert broker.model == ""               # inherit-talker default (Decision D)
    assert broker.max_subqueries == 1       # Decision E
    assert broker.max_answer_chars == 900
    assert broker.verbatim_run_limit == 60
    assert broker.question_max_chars == 2000
    assert broker.compose_field_max_chars == 1500
    assert broker.llm_timeout_seconds == 30.0


def test_nl_broker_garbage_values_fall_back_to_defaults() -> None:
    cfg = load_from_unified(_unified(
        _perm_entry(),
        nl_broker={
            "enabled": True,
            "max_subqueries": "not-a-number",
            "max_answer_chars": 0,           # floored at 1
            "llm_timeout_seconds": "bogus",
        },
    ))
    broker = cfg.canonical.nl_broker
    assert broker.max_subqueries == 1
    assert broker.max_answer_chars == 1
    assert broker.llm_timeout_seconds == 30.0


# ---------------------------------------------------------------------------
# nl_query block parsing
# ---------------------------------------------------------------------------


def test_nl_query_block_parses_compose_fields_and_max_records() -> None:
    cfg = load_from_unified(_unified(_perm_entry(
        nl_query={"compose_fields": ["description"], "max_records": 3},
    )))
    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert isinstance(rules.nl_query, NLQueryRules)
    assert rules.nl_query.compose_fields == ["description"]
    assert rules.nl_query.max_records == 3


def test_nl_query_defaults_ship_empty_compose_tier() -> None:
    """Ratified Decision C: the mechanism ships with an EMPTY compose tier."""
    cfg = load_from_unified(_unified(_perm_entry(nl_query={})))
    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert rules.nl_query is not None
    assert rules.nl_query.compose_fields == []
    assert rules.nl_query.max_records == 5


def test_nl_query_max_records_clamped_to_ceiling() -> None:
    cfg = load_from_unified(_unified(_perm_entry(
        nl_query={"max_records": 9999},
    )))
    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert rules.nl_query is not None
    assert rules.nl_query.max_records == FILTER_LIMIT_CEILING


def test_nl_query_non_string_compose_fields_dropped() -> None:
    cfg = load_from_unified(_unified(_perm_entry(
        nl_query={"compose_fields": ["description", 42, "", None]},
    )))
    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert rules.nl_query is not None
    assert rules.nl_query.compose_fields == ["description"]


# ---------------------------------------------------------------------------
# Fail-closed consistency: nl_query without query
# ---------------------------------------------------------------------------


def test_nl_query_without_query_block_is_denied_and_warned() -> None:
    """nl_query without the sibling deterministic ``query`` = None + warning.

    The NL lane retrieves THROUGH the deterministic engine, so a
    deterministic policy must exist. The misconfiguration is surfaced
    via ``transport.config.nl_query_without_query`` (intentionally-
    left-blank: visible, not silent) and the lane stays fail-closed.
    """
    entry = _perm_entry(nl_query={"compose_fields": ["description"]})
    del entry["query"]

    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified(_unified(entry))

    rules = cfg.canonical.peer_permissions["hypatia"]["event"]
    assert rules.query is None
    assert rules.nl_query is None  # fail-closed despite block present

    matches = [
        c for c in captured
        if c.get("event") == "transport.config.nl_query_without_query"
    ]
    assert len(matches) == 1
    assert matches[0].get("peer") == "hypatia"
    assert matches[0].get("type") == "event"
