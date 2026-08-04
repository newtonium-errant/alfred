"""Pins for the suite egress guard itself (#16).

The guard in ``tests/conftest.py`` is infrastructure that fails other people's
tests, so it needs its own. Two classes of pin here:

  * the CREDENTIAL pin team-lead asked for — collection must not leave real
    credentials in ``os.environ``. Without it the de-pollution can rot silently
    and the suite quietly resumes spending money on third-party APIs, which is
    exactly the failure it was added to stop;
  * UNIT pins on the exemption predicate, each covering a false positive that
    was found by measurement against real traffic rather than by review.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import socket

from tests.conftest import (
    _collection_injected,
    _egress_exempt,
    _guard_is_live,
    _is_ip_literal,
)


# --- the credential pin -----------------------------------------------------


def test_collection_leaves_no_credential_env_vars() -> None:
    """Importing the test suite must not inject credentials into os.environ.

    ``pymilvus/settings.py`` runs a bare ``load_dotenv()`` at import;
    ``find_dotenv`` walks up from cwd to the repo's REAL .env (it finds the main
    repo's file even from inside a worktree). So importing
    ``alfred.surveyor.embedder`` used to inject GROQ_API_KEY, ANTHROPIC_API_KEY,
    ELEVENLABS_API_KEY and TELEGRAM_BOT_TOKEN — with real values — into the
    whole pytest process before a single test ran.

    That silently defeated ``skipif(not os.environ.get(...))`` on the Groq and
    ElevenLabs integration tests, so they executed against real paid APIs on
    every full-suite run.

    This asserts the OUTCOME, not the mechanism, so it holds whichever way the
    fix is implemented: nothing credential-shaped may appear in os.environ that
    was not there when conftest was imported. An operator who deliberately
    exports a key still can — that value is in the baseline.

    Scoped to COLLECTION deliberately, by reading the recording the conftest
    takes at ``pytest_collection_finish`` rather than inspecting live
    ``os.environ`` here. A live check would also fail on credentials left behind
    by an earlier TEST — and the CLI dispatchers inject ``ALFRED_TRANSPORT_TOKEN``
    and friends by documented design (CLAUDE.md, "Dispatcher env-var
    injection"), so the pin would fail for a reason it is not about and the
    obvious fix would be to weaken it. Measured: the live-check version passed
    on its own file and failed in the full suite for exactly that reason.

    Mutation: remove the ``dotenv.load_dotenv`` suppression from
    tests/conftest.py → this fails, naming the injected vars.
    """
    assert _collection_injected == [], (
        f"collection injected credential env vars: {_collection_injected}. "
        f"Something imported at collection time loaded a .env into os.environ. "
        f"This un-gates every skipif(not os.environ.get(...)) integration test, "
        f"so the suite starts calling real paid APIs."
    )


def test_guard_is_installed() -> None:
    """ILB: a guard that never installed looks identical to a clean suite.

    Pins that our socket hook is the live one at the moment a normal test runs.
    """
    assert _guard_is_live(), (
        "the egress guard's socket.socket.connect patch is not live — the guard "
        "is reporting clean results it did not actually observe"
    )
    assert socket.getaddrinfo.__module__ == "tests.conftest"


# --- exemption predicate ----------------------------------------------------


def test_loopback_is_exempt() -> None:
    for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "", "0.0.0.0", "::"):
        assert _egress_exempt(host) is True, host


def test_bytes_loopback_is_exempt() -> None:
    """Bytes hosts are real and must be decoded before comparison.

    telegram's getaddrinfo passes ``b'api.telegram.org'`` and the surveyor's
    ollama probe passes ``b'localhost'``. Comparing bytes against str prefixes
    silently yields False, which classifies ``b'localhost'`` as egress — the
    guard would fail a test for talking to a LOCAL model. Found in the wild:
    the first sweep flagged ``b'localhost':11434``.

    Mutation: drop the bytes-decode branch from ``_egress_exempt`` → this fails.
    """
    assert _egress_exempt(b"localhost") is True
    assert _egress_exempt(b"127.0.0.1") is True
    assert _egress_exempt(b"api.telegram.org") is False


def test_test_net_ranges_are_exempt() -> None:
    """RFC 5737 documentation ranges are reserved and non-routable.

    The scribe egress firewall dials 192.0.2.1 as its CANARY — that is an egress
    control verifying itself, not a leak, and flagging it would pressure someone
    into weakening a real safety check.

    Mutation: ``_TEST_NET_PREFIXES = ()`` → this fails, and 4 scribe tests error.
    """
    assert _egress_exempt("192.0.2.1") is True
    assert _egress_exempt("198.51.100.7") is True
    assert _egress_exempt("203.0.113.9") is True


def test_real_destinations_are_not_exempt() -> None:
    """Preservation pin: the exemptions must not swallow actual egress."""
    for host in ("api.anthropic.com", "api.telegram.org", "8.8.8.8", "1.1.1.1"):
        assert _egress_exempt(host) is False, host


def test_unknown_host_types_fail_closed() -> None:
    """A destination the guard cannot identify is reported, not waved through."""
    assert _egress_exempt(None) is False
    assert _egress_exempt(12345) is False


def test_ip_literal_detection() -> None:
    """Resolving an IP literal is a local parse, not a DNS query.

    alfred.sovereign's own http_guard calls getaddrinfo on the target to
    CLASSIFY it before refusing, so counting that as DNS flagged the guard for
    doing its job.
    """
    assert _is_ip_literal("8.8.8.8") is True
    assert _is_ip_literal("::1") is True
    assert _is_ip_literal(b"192.0.2.1") is True
    assert _is_ip_literal("api.anthropic.com") is False
    assert _is_ip_literal(None) is False
