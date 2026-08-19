"""Peer-route bleed-stop tests — §1a confirm-guard + §2 stickiness.

Covers the 2026-06-16 incident fix (design doc
``PEER_ROUTE_DESIGN_2026-06-16.md``, §1+§2 subset; §3 round-trip is a
separate later arc and is NOT exercised here).

Incident: "Check Vera gh#7 confirmed closed in peer digest" was
mis-classified ``peer_route target=kal-le`` and the opened peer_route
session swallowed the next two confirm messages, force-forwarding them to
KAL-LE which never worked them.

Surviving surface (Telegram retirement 2026-08-19):

§1a — deterministic confirm-guard (``router.is_brief_or_status_confirm``):
    a brief/status confirm returns a matched-pattern label and is forced
    local; legit peer work is NOT eaten.

The §1a-wiring and §2-stickiness tests drove ``bot._open_routed_session``
/ ``bot.handle_message`` and were removed with ``alfred.telegram.bot``
(the stickiness machinery itself lived in bot.py and is gone with the
surface). The pure-function guard below is the part the router module
still owns.
"""

from __future__ import annotations


import pytest

from alfred.telegram import router


# --- §1a: is_brief_or_status_confirm pure-function guard -------------------


@pytest.mark.parametrize(
    "message,expected_label",
    [
        # Canonical tier grammar (source-of-truth brief/tier_section.py).
        ("T1 confirm", "tier_grammar"),
        ("T2 confirm", "tier_grammar"),
        ("T3 confirm walk Fergus", "tier_grammar"),
        ("T2 add eggs and milk", "tier_grammar"),
        ("T3 drop the gym thing", "tier_grammar"),
        ("t1 done", "tier_grammar"),  # lowercase + done verb
        ("  T2 keep  ", "tier_grammar"),  # leading whitespace tolerant
        # Bare leading status verbs.
        ("confirmed", "status_verb"),
        ("confirm", "status_verb"),
        ("done", "status_verb"),
        ("closed", "status_verb"),
        ("Done!", "status_verb"),
        ("  closed the issue", "status_verb"),
    ],
)
def test_confirm_guard_forces_local(message: str, expected_label: str) -> None:
    """Canonical confirm grammar returns its matched-pattern label."""
    assert router.is_brief_or_status_confirm(message) == expected_label


@pytest.mark.parametrize(
    "message",
    [
        # The exact incident phrasing — leads with "Check", so the TIGHT
        # regex deliberately does NOT match (the §1b prompt block owns this
        # fuzzy shape). Pinned so a future regex-widening can't silently
        # start eating it without updating this expectation.
        "Check Vera gh#7 confirmed closed in peer digest",
        # Legit peer work — must NOT be eaten by the guard.
        "KAL-LE, run pytest on the new branch",
        "run the tests and tell me what fails",
        "why is the transport scheduler firing twice",
        "refactor the dispatch helper",
        # Verb mid-sentence (not leading) — must NOT match the status rule.
        "I want KAL-LE to confirm the build passed",
        "ask kal-le whether the test is done",
        # Longer word starting with a guard verb — \b prevents a match.
        "confirmation needed on the deploy",
        "closure of the ticket is pending",
        # "T1" not followed by a guard verb.
        "T1 is the imminent tier, right?",
        # Empty / whitespace.
        "",
        "   ",
    ],
)
def test_confirm_guard_does_not_eat_legit_peer_work(message: str) -> None:
    """Non-confirm phrasings (incl. the incident text) return None.

    The incident text leads with "Check" so the TIGHT §1a regex skips it
    on purpose — the §1b ``_ROUTER_PROMPT`` exclusion block is the layer
    that catches that fuzzy class. This pin documents the deliberate
    boundary.
    """
    assert router.is_brief_or_status_confirm(message) is None


def test_confirm_guard_log_emission_in_handle_message_path() -> None:
    """The matched-pattern label is the correction-signal substrate.

    ``is_brief_or_status_confirm`` returns the LABEL (not a bare bool) so
    the caller can log WHICH pattern fired. Pin the label values so a
    refactor can't silently collapse them to a bool.
    """
    assert router.is_brief_or_status_confirm("T1 confirm") == "tier_grammar"
    assert router.is_brief_or_status_confirm("done") == "status_verb"
    assert router.is_brief_or_status_confirm("hello there") is None


# --- §1a wiring: force_local_note bypasses the router ----------------------


# --- §2 helper: _is_reply_to_peer_relay -----------------------------------


# --- §2: handle_message stickiness harness --------------------------------


# --- §2 log-emission pins -------------------------------------------------


