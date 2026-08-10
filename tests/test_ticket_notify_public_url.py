"""#63b — ticket-created notifications must not hand the operator a dead link.

The forge is on-box (forgejo), so the issue URL its API returns is whatever
ROOT_URL it was configured with — in practice ``http://localhost:3001/...``.
That URL travels verbatim into the ticket-created notice, which lands on the
operator's PHONE via the Telegram relay and the PWA notification tray. Tapping
it there goes nowhere.

Two halves, and the second is the one that matters most:

  * When a PUBLIC base URL is configured, rewrite the origin so the link works
    from anywhere. Config-driven — never a hardcoded hostname.
  * When one ISN'T, say so. A box-local link presented as if it were live is
    the actual defect; a link the operator is told is box-local is merely a
    limitation. Honest degradation beats a silent dead end.

The URL is only rewritten/labelled when it really is box-local. A
GitHub-backed instance's ``https://github.com/...`` link is genuinely public,
and labelling THAT "box-local" would be a new lie in the opposite direction.
"""

from __future__ import annotations

import pytest

from alfred.transport.peer_handlers import (
    _is_box_local_url,
    _operator_facing_issue_url,
)
from alfred.transport.ticket_intake import (
    TicketIntakeConfig,
    load_ticket_intake_config,
)

BOX_ISSUE = "http://localhost:3001/andrew/algernon/issues/7"
PUBLIC_BASE = "https://forge.example.com"


# --- is this URL reachable from off the box? --------------------------------


@pytest.mark.parametrize("url", [
    "http://localhost:3001/andrew/algernon/issues/7",
    "http://127.0.0.1:3001/x/y/issues/1",
    "http://0.0.0.0:3001/x/y/issues/1",
    "http://[::1]:3001/x/y/issues/1",
    "http://192.168.1.40:3001/x/y/issues/1",   # private LAN
    "http://10.0.0.5:3001/x/y/issues/1",       # private LAN
    "http://algernon-box:3001/x/y/issues/1",   # single-label host: no public DNS
])
def test_box_local_urls_are_recognised(url) -> None:
    assert _is_box_local_url(url) is True


@pytest.mark.parametrize("url", [
    "https://github.com/acme/site/issues/7",
    "https://forge.example.com/andrew/algernon/issues/7",
])
def test_genuinely_public_urls_are_not_flagged(url) -> None:
    """The guard against a lie in the opposite direction. A GitHub-backed
    instance's link works fine from the phone; labelling it box-local would
    train the operator to distrust links that are actually good."""
    assert _is_box_local_url(url) is False


@pytest.mark.parametrize("url", ["", "   ", "not-a-url", "javascript:alert(1)"])
def test_unparseable_or_empty_urls_are_not_flagged(url) -> None:
    """Fail toward "say nothing" rather than toward a confident wrong claim."""
    assert _is_box_local_url(url) is False


# --- the operator-facing rewrite -------------------------------------------


def test_a_configured_public_base_rewrites_the_origin() -> None:
    """The path is preserved; only scheme+host+port are replaced. Rebuilding
    the path would be inventing a URL structure the forge owns."""
    url, note = _operator_facing_issue_url(BOX_ISSUE, PUBLIC_BASE)
    assert url == "https://forge.example.com/andrew/algernon/issues/7"
    assert note == "", "a working link needs no apology"


def test_a_trailing_slash_on_the_base_does_not_double_up() -> None:
    url, _ = _operator_facing_issue_url(BOX_ISSUE, PUBLIC_BASE + "/")
    assert url == "https://forge.example.com/andrew/algernon/issues/7"


def test_no_public_base_keeps_the_url_and_labels_it() -> None:
    """THE #63b pin. The link is still carried (it works when he's at the box)
    but it now arrives labelled, so a tap that fails is expected rather than
    baffling."""
    url, note = _operator_facing_issue_url(BOX_ISSUE, "")
    assert url == BOX_ISSUE, "the link is labelled, not withheld"
    assert note, "a box-local link MUST carry a note"
    assert "box" in note.lower()


def test_a_public_url_is_untouched_even_with_no_base_configured() -> None:
    """An instance whose forge is already public needs no config to behave."""
    gh = "https://github.com/acme/site/issues/7"
    url, note = _operator_facing_issue_url(gh, "")
    assert url == gh
    assert note == ""


def test_a_public_base_does_not_rewrite_an_already_public_url() -> None:
    """Rewriting the origin of a URL that was never box-local would point the
    operator at a host that doesn't serve that issue."""
    gh = "https://github.com/acme/site/issues/7"
    url, note = _operator_facing_issue_url(gh, PUBLIC_BASE)
    assert url == gh
    assert note == ""


def test_an_empty_issue_url_stays_empty_and_unlabelled() -> None:
    url, note = _operator_facing_issue_url("", PUBLIC_BASE)
    assert url == ""
    assert note == ""


def test_a_malformed_public_base_degrades_to_labelling() -> None:
    """A misconfigured base must not produce a mangled URL — fall back to the
    honest box-local label, which is the same place an unset base lands."""
    url, note = _operator_facing_issue_url(BOX_ISSUE, "not a url")
    assert url == BOX_ISSUE
    assert note


# --- config wiring ----------------------------------------------------------


def test_public_base_url_is_config_driven_and_defaults_empty() -> None:
    """No hardcoded hostname anywhere: an instance that sets nothing gets the
    labelling path, not somebody else's forge."""
    assert TicketIntakeConfig().public_base_url == ""
    cfg = load_ticket_intake_config({"ticket_intake": {
        "enabled": True, "public_base_url": PUBLIC_BASE,
    }})
    assert cfg.public_base_url == PUBLIC_BASE


def test_public_base_url_survives_an_absent_ticket_intake_block() -> None:
    assert load_ticket_intake_config({}).public_base_url == ""
