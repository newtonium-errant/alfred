"""Cross-instance recall EDGE pins (#30 half 2).

The recall code path is generic; wiring an edge is a config act. That
makes the shipped ``*.yaml.example`` files the actual contract surface an
operator copies from — so they are pinned here as executable
documentation rather than prose. A silently-broken example is worse than
no example: it gets copied to the box.

Two failure modes these guard, both silent:
  * the example stops PARSING (a peer/type key typo) — nobody notices
    until an operator copies it;
  * the example parses but the loader DROPS a type as unknown-for-scope
    (it warns and continues, by design), so an edge quietly narrows.

The STAY-C fence is pinned in both directions alongside, since these
edges are exactly where someone would be tempted to add it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alfred.transport.config import (
    RecallConfigError,
    load_from_unified,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_example(name: str):
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} missing from repo root"
    return load_from_unified(yaml.safe_load(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------
# config.instance.yaml.example — the per-instance template (KAL-LE shape)
# --------------------------------------------------------------------

def test_instance_template_recall_answers_and_asks() -> None:
    """The template ships BOTH sides of the recall lane wired."""
    tc = _load_example("config.instance.yaml.example")
    assert tc.recall.enabled is True
    assert set(tc.recall.peers) == {"salem", "hypatia"}
    assert set(tc.recall.ask.peers) == {"salem", "hypatia"}


def test_instance_template_kalle_hypatia_edge_is_bidirectional() -> None:
    """#30: the KAL-LE↔Hypatia edge, the deferred one this closes.

    Hypatia must appear on BOTH sides — as an asking peer KAL-LE will
    answer, and as a peer KAL-LE may ask. A one-sided edge is the bug
    shape here (the SKILLs promise both directions).
    """
    tc = _load_example("config.instance.yaml.example")
    assert "hypatia" in tc.recall.peers, "KAL-LE must ANSWER Hypatia"
    assert "hypatia" in tc.recall.ask.peers, "KAL-LE must be able to ASK Hypatia"
    assert tc.recall.peers["hypatia"].types
    assert tc.recall.ask.peers["hypatia"].types


def test_instance_template_recall_peers_are_reachable_transport_peers() -> None:
    """Every recall peer must also be a configured ``transport.peers`` key.

    An ask edge naming a peer with no base_url/token can never actually
    send — it would fail at dispatch, not at load, so pin it here.
    """
    tc = _load_example("config.instance.yaml.example")
    for name in tc.recall.ask.peers:
        assert name in tc.peers, (
            f"recall.ask.peers['{name}'] has no transport.peers entry — "
            "unreachable edge"
        )


def test_instance_template_drops_no_types(caplog: pytest.LogCaptureFixture) -> None:
    """No configured type may be silently dropped as unknown-for-scope.

    The loader's drop-unknown-with-warn is correct behaviour for operator
    typos, but in the SHIPPED example a drop means the template advertises
    a disclosure the instance will never actually make.
    """
    tc = _load_example("config.instance.yaml.example")
    for side, peers in (
        ("answer", tc.recall.peers), ("ask", tc.recall.ask.peers),
    ):
        for name, rules in peers.items():
            assert rules.types, (
                f"{side} edge '{name}' resolved to an EMPTY type list — "
                "every configured type was dropped as unknown for this "
                "instance's scope"
            )


def test_instance_template_peer_keys_use_canonical_hyphenated_form() -> None:
    """Peer keys are the WIRE namespace, spelled canonically.

    #30: a sender-local alias that diverges (``kalle`` for ``kal-le``)
    still resolves at runtime, but it defeats grep across instances. The
    shipped template must not model the divergent spelling.
    """
    tc = _load_example("config.instance.yaml.example")
    assert "kalle" not in tc.peers, (
        "template must not model the non-canonical `kalle` peer key; "
        "the canonical wire form is `kal-le`"
    )


# --------------------------------------------------------------------
# STAY-C fence — both directions, at the edge surface
# --------------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["stay-c", "stayc", "STAY_C", "Stay-C"])
def test_stayc_refused_as_answer_peer_in_any_spelling(spelling: str) -> None:
    raw = {
        "transport": {
            "recall": {"enabled": True, "peers": {spelling: {"types": ["note"]}}},
        },
    }
    with pytest.raises(RecallConfigError):
        load_from_unified(raw)


@pytest.mark.parametrize("spelling", ["stay-c", "stayc", "STAY_C", "Stay-C"])
def test_stayc_refused_as_ask_peer_in_any_spelling(spelling: str) -> None:
    raw = {
        "transport": {
            "recall": {"ask": {"peers": {spelling: {"types": ["note"]}}}},
        },
    }
    with pytest.raises(RecallConfigError):
        load_from_unified(raw)
