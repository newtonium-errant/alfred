"""Peer-key resolution across instance-name spellings (#30).

Background — the reframe this suite encodes. ``kalle`` and ``kal-le`` are
NOT one name spelled two ways; they are two different NAMESPACES that
legitimately coexist:

  * WIRE (peer key)   — ``transport.peers`` / ``auth.tokens`` keys:
                        ``salem``, ``kal-le``, ``hypatia``, ``vera``
  * SCOPE             — ``instance.tool_set`` / ``SCOPE_RULES`` keys:
                        ``talker``, ``kalle``, ``hypatia``, ``vera``

Salem's scope is ``talker`` while her peer key is ``salem`` — proof the
two cannot be collapsed into one form by renaming. So this is NOT a
migration; ``vault/scope.py`` and ``vault/schema.py`` are correct as-is.

The real defect is the DISPLAY→WIRE bridge. ``_normalize_instance_name``
preserves dashes, so ``KAL-LE`` → ``kal-le`` but ``K.A.L.L.E.`` →
``kalle`` — two spellings of one instance yielding two different keys.
Compounding it, sender-local aliases diverge on the box: VERA's
``transport.peers`` names KAL-LE ``kalle`` while every other instance
writes ``kal-le``, so a classifier emitting the canonical form failed a
literal membership test on VERA.

``collapse_peer_name`` + ``resolve_peer_key`` close both. See
``_compat.py`` for why ``_normalize_instance_name`` itself is FROZEN
(its output is the ``speed_pref`` store key — changing its collapse rule
would orphan every stored preference; pinned below).
"""

from __future__ import annotations

from typing import Any

import pytest

from alfred.telegram import router
from alfred.telegram._compat import (
    _normalize_instance_name,
    collapse_peer_name,
    resolve_peer_key,
)

# The live peer sets on algernon-box as of 2026-08-03. VERA's set is the
# one that carries the divergent alias; it is the reason this code exists.
SALEM_PEERS = {"kal-le", "hypatia"}
KALLE_PEERS = {"salem", "vera"}
HYPATIA_PEERS = {"salem"}
VERA_PEERS = {"salem", "kalle"}  # <- alias divergence: "kalle", not "kal-le"


# --------------------------------------------------------------------
# collapse_peer_name — the identity form
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "spelling",
    ["KAL-LE", "kal-le", "K.A.L.L.E.", "kalle", "Kal_Le", "  kal-le  "],
)
def test_collapse_converges_every_kalle_spelling(spelling: str) -> None:
    """Every legitimate spelling of KAL-LE collapses to one identity."""
    assert collapse_peer_name(spelling) == "kalle"


def test_collapse_converges_dotted_vera() -> None:
    """``V.E.R.A.`` is the LIVE dotted ``instance.name`` on the box."""
    assert collapse_peer_name("V.E.R.A.") == "vera"
    assert collapse_peer_name("vera") == "vera"


def test_collapse_maps_legacy_alfred_to_salem() -> None:
    """A default-named install still resolves to the ``salem`` peer key."""
    assert collapse_peer_name("Alfred") == "salem"
    assert collapse_peer_name("alfred") == "salem"


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_collapse_handles_empty_input(empty: Any) -> None:
    assert collapse_peer_name(empty) == ""


# --------------------------------------------------------------------
# resolve_peer_key — spelling -> CONFIGURED key
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "emitted", ["kal-le", "KAL-LE", "K.A.L.L.E.", "kalle"],
)
def test_resolve_any_spelling_against_veras_divergent_alias(
    emitted: str,
) -> None:
    """THE #30 BUG. VERA's configured alias for KAL-LE is ``kalle``.

    A classifier emitting the canonical ``kal-le`` (what every SKILL and
    every other instance writes) must still resolve — and must resolve to
    VERA's OWN key, since that is what ``peer_send`` looks up.
    """
    assert resolve_peer_key(emitted, VERA_PEERS) == "kalle"


@pytest.mark.parametrize(
    "emitted", ["kal-le", "KAL-LE", "K.A.L.L.E.", "kalle"],
)
def test_resolve_any_spelling_against_salems_canonical_key(
    emitted: str,
) -> None:
    """Mirror of the above: on Salem the same spellings yield ``kal-le``."""
    assert resolve_peer_key(emitted, SALEM_PEERS) == "kal-le"


def test_resolve_returns_the_configured_form_not_the_collapsed_form() -> None:
    """The return value must be usable as a ``transport.peers`` dict key.

    Returning the collapsed identity (``kalle``) would KeyError on Salem,
    whose configured key is ``kal-le``. This is the whole contract.
    """
    assert resolve_peer_key("K.A.L.L.E.", SALEM_PEERS) in SALEM_PEERS
    assert resolve_peer_key("kal-le", VERA_PEERS) in VERA_PEERS


def test_resolve_is_fail_closed_on_unconfigured_peer() -> None:
    """Never invent a key that isn't configured — refuse instead.

    Hypatia has only ``salem``; a classifier naming KAL-LE must not
    produce a route.
    """
    assert resolve_peer_key("kal-le", HYPATIA_PEERS) is None
    assert resolve_peer_key("vera", SALEM_PEERS) is None
    assert resolve_peer_key("nobody", KALLE_PEERS) is None


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_resolve_fail_closed_on_empty_name(empty: Any) -> None:
    assert resolve_peer_key(empty, SALEM_PEERS) is None


def test_resolve_on_empty_peer_set_is_none() -> None:
    """An instance with no configured peers routes nowhere."""
    assert resolve_peer_key("kal-le", set()) is None


def test_resolve_ambiguity_is_deterministic() -> None:
    """Two keys collapsing to one identity can only be one instance.

    Either is a correct route; the pick is sorted-first purely so the
    behaviour is stable rather than set-iteration-order dependent.
    """
    both = {"kalle", "kal-le"}
    assert resolve_peer_key("K.A.L.L.E.", both) == "kal-le"
    assert resolve_peer_key("kalle", both) == "kal-le"


def test_resolve_maps_legacy_alfred_to_salem_key() -> None:
    assert resolve_peer_key("Alfred", {"salem", "kal-le"}) == "salem"


# --------------------------------------------------------------------
# REGRESSION PIN — _normalize_instance_name must NOT change
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("KAL-LE", "kal-le"),
        ("K.A.L.L.E.", "kalle"),   # the lossy case — deliberately kept
        ("Salem", "salem"),
        ("Hypatia", "hypatia"),
        ("V.E.R.A.", "vera"),
        ("Stay C", "stay-c"),
        ("Alfred", "salem"),
        ("", ""),
    ],
)
def test_normalize_instance_name_behaviour_is_frozen(
    raw: str, expected: str,
) -> None:
    """``_normalize_instance_name`` output is a STORE KEY — do not change.

    ``speed_pref`` persists per-instance TTS preferences under this
    function's output. Widening its collapse rule (e.g. to also strip
    dashes, as ``collapse_peer_name`` does) would orphan every stored
    preference on the box. #30 deliberately added a SEPARATE helper
    rather than changing this one; this pin guards that decision.
    """
    assert _normalize_instance_name(raw) == expected


# --------------------------------------------------------------------
# Router — acceptance must be identity-based, not literal
# --------------------------------------------------------------------

def test_router_accepts_canonical_emission_against_divergent_alias() -> None:
    """On VERA, a ``kal-le`` classification must survive the router gate.

    Before #30 this failed a literal ``in`` against ``{salem, kalle}``
    and silently degraded to a local ``note`` — the peer route never ran.
    """
    decision = router._decision_from_parsed(
        {"session_type": "peer_route", "target": "kal-le"},
        recent=[],
        self_name="vera",
        valid_peer_targets=VERA_PEERS,
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kalle"  # VERA's own configured key


def test_router_still_refuses_an_unconfigured_peer() -> None:
    """Fail-closed is preserved: Hypatia has no KAL-LE peer."""
    decision = router._decision_from_parsed(
        {"session_type": "peer_route", "target": "kal-le"},
        recent=[],
        self_name="hypatia",
        valid_peer_targets=HYPATIA_PEERS,
    )
    assert decision.session_type == "note"
    assert decision.target is None


def test_router_self_target_guard_survives_spelling_mismatch() -> None:
    """A dotted self-name vs a dashed target must still read as SELF.

    ``_normalize_instance_name`` would give ``kalle`` vs ``kal-le`` here
    and let the self-route through; the collapsed comparison catches it.
    """
    decision = router._decision_from_parsed(
        {"session_type": "peer_route", "target": "kal-le"},
        recent=[],
        self_name="kalle",
        valid_peer_targets={"kal-le"},
    )
    assert decision.session_type == "note"
    assert decision.target is None


# --------------------------------------------------------------------
# E2E through the production entry point (_dispatch_peer_route)
# --------------------------------------------------------------------
#
# This is the pin class that catches the unthreaded-gate trap: per-layer
# unit pins above would all stay green if the resolver were never wired
# into the dispatcher. These drive the real function body.

