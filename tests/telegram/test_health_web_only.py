"""Talker health probes quiet down on a web-only instance.

An instance running ``web.enabled`` + ``web.web_only`` has no Telegram bot
by explicit operator declaration. Before this fix its three Telegram
prerequisite probes reported an empty token as FAIL and empty
allowlist/STT as WARN — all attention statuses — which would have given
every quiesced instance daily BIT / digest / narration noise.

This is a DELIBERATE quieting, the opposite of the fail-OPEN default the
rest of the health layer holds. It is justified only because ``web_only``
is an explicit operator declaration rather than an inferred condition, so
the tests below pin both directions: quiet when declared, loud otherwise,
and loud when the declaration cannot be read.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.health.types import Status
from alfred.telegram.health import (
    WEB_ONLY_SKIP_DETAIL,
    _check_allowed_users,
    _check_bot_token,
    _check_stt_key,
    _web_only_enabled,
    health_check,
)

WEB_ONLY_RAW = {"web": {"enabled": True, "web_only": True}}


# ---------------------------------------------------------------------------
# _web_only_enabled — the derivation, and its strict fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"web": {"enabled": True, "web_only": True}}, True),
        ({"web": {"enabled": True, "web_only": False}}, False),
        ({"web": {"enabled": False, "web_only": True}}, False),
        ({"web": {}}, False),
        ({}, False),
    ],
)
def test_web_only_requires_both_flags(raw: dict, expected: bool) -> None:
    """Both flags, both explicit — mirrors the daemon's own probe. A
    standard instance (either flag absent) keeps every Telegram
    prerequisite enforced, byte for byte."""
    assert _web_only_enabled(raw) is expected


def test_web_only_fails_toward_strict_on_parse_error(monkeypatch) -> None:
    """A web-config problem must NEVER be able to quiet a check.

    The dangerous direction is asymmetric: falling back to strict costs a
    spurious FAIL on a genuinely web-only instance, which is visible and
    gets fixed. Falling back to quiet turns a config bug into invisible
    health, which is not.
    """
    import alfred.web.config as webcfg

    def _boom(_raw):
        raise ValueError("malformed web block")

    monkeypatch.setattr(webcfg, "load_from_unified", _boom)
    assert _web_only_enabled(WEB_ONLY_RAW) is False


def test_web_only_parse_error_is_logged(monkeypatch) -> None:
    """The fallback is observable, not assumed — otherwise a silently
    strict instance and a correctly strict one look identical."""
    import alfred.web.config as webcfg

    def _boom(_raw):
        raise ValueError("malformed web block")

    monkeypatch.setattr(webcfg, "load_from_unified", _boom)
    with structlog.testing.capture_logs() as captured:
        _web_only_enabled(WEB_ONLY_RAW)

    assert [
        c for c in captured
        if c.get("event") == "talker.health.web_only_probe_failed"
    ]


# ---------------------------------------------------------------------------
# The three probes — quiet under web_only, loud otherwise
# ---------------------------------------------------------------------------


def test_bot_token_skips_when_empty_under_web_only() -> None:
    r = _check_bot_token("", web_only=True)
    assert r.status is Status.SKIP
    assert r.detail == WEB_ONLY_SKIP_DETAIL


def test_bot_token_skips_when_placeholder_unresolved_under_web_only() -> None:
    """The second branch, which is the one that gets forgotten.

    Re-commenting the token line without setting the env var — the
    ordinary way an operator half-removes a bot — resolves to a ``${...}``
    placeholder. If only the empty branch were quieted, that would restore
    the daily noise on an instance that is still, by declaration,
    web-only.
    """
    r = _check_bot_token("${TELEGRAM_BOT_TOKEN}", web_only=True)
    assert r.status is Status.SKIP
    assert r.detail == WEB_ONLY_SKIP_DETAIL


def test_bot_token_still_fails_both_branches_when_not_web_only() -> None:
    """Positive control, both branches. Without this the SKIP pins would
    pass just as happily against a probe that never reports anything."""
    assert _check_bot_token("", web_only=False).status is Status.FAIL
    assert (
        _check_bot_token("${TELEGRAM_BOT_TOKEN}", web_only=False).status
        is Status.FAIL
    )


def test_bot_token_ok_path_untouched_when_not_web_only() -> None:
    r = _check_bot_token("1234567:realish-looking-value", web_only=False)
    assert r.status is Status.OK


def test_allowed_users_skips_under_web_only_and_warns_otherwise() -> None:
    assert _check_allowed_users([], web_only=True).status is Status.SKIP
    # Positive control — WARN, not FAIL. Both are attention statuses, so
    # both are digest noise; the distinction matters for describing the
    # fix accurately, not for whether it is needed.
    assert _check_allowed_users([], web_only=False).status is Status.WARN
    assert _check_allowed_users([123], web_only=False).status is Status.OK


def test_stt_key_skips_under_web_only_and_warns_otherwise() -> None:
    assert _check_stt_key({}, web_only=True).status is Status.SKIP
    assert _check_stt_key({}, web_only=False).status is Status.WARN
    assert (
        _check_stt_key({"api_key": "test-stt-key"}, web_only=False).status
        is Status.OK
    )


def test_all_three_skips_use_the_one_wording() -> None:
    """Single-spelled, so a grep finds every quieted check at once and the
    three cannot drift into three different explanations of the same
    state."""
    details = {
        _check_bot_token("", web_only=True).detail,
        _check_allowed_users([], web_only=True).detail,
        _check_stt_key({}, web_only=True).detail,
    }
    assert details == {WEB_ONLY_SKIP_DETAIL}
    # The wording has to answer "quiet or broken?" for a digest reader.
    assert "web_only" in WEB_ONLY_SKIP_DETAIL
    assert "intentionally disabled" in WEB_ONLY_SKIP_DETAIL


# ---------------------------------------------------------------------------
# health_check — driving the production entry point
# ---------------------------------------------------------------------------


def _results_by_name(health) -> dict:
    return {c.name: c for c in health.results}


async def test_health_check_quiets_the_three_on_a_web_only_instance() -> None:
    """Drives ``health_check``, not the helpers — a probe that skips only
    when called directly is a fix nothing runs."""
    raw = {
        "telegram": {"bot_token": "", "allowed_users": [], "stt": {}},
        "web": {"enabled": True, "web_only": True},
    }
    got = _results_by_name(await health_check(raw))
    for name in ("bot-token", "allowed-users", "stt-key"):
        assert got[name].status is Status.SKIP, name
        assert got[name].detail == WEB_ONLY_SKIP_DETAIL


async def test_health_check_still_fails_on_a_standard_instance() -> None:
    """Positive control through the same entry point: identical telegram
    config, no web_only declaration, and the probes stay loud."""
    raw = {"telegram": {"bot_token": "", "allowed_users": [], "stt": {}}}
    got = _results_by_name(await health_check(raw))
    assert got["bot-token"].status is Status.FAIL
    assert got["allowed-users"].status is Status.WARN
    assert got["stt-key"].status is Status.WARN


async def test_health_check_logs_that_it_quieted_checks() -> None:
    """Quieting a check is exactly where silence and breakage look alike,
    so the run says so out loud, at production log level."""
    raw = {
        "telegram": {"bot_token": "", "allowed_users": [], "stt": {}},
        "web": {"enabled": True, "web_only": True},
    }
    with structlog.testing.capture_logs() as captured:
        await health_check(raw)

    matches = [
        c for c in captured
        if c.get("event") == "talker.health.web_only_checks_skipped"
    ]
    assert len(matches) == 1
    assert matches[0]["skipped"] == ["bot-token", "allowed-users", "stt-key"]
    assert matches[0]["detail"] == WEB_ONLY_SKIP_DETAIL


async def test_health_check_does_not_log_quieting_on_a_standard_instance(
) -> None:
    """Negative control — a signal that fires unconditionally means
    nothing."""
    raw = {"telegram": {"bot_token": "", "allowed_users": [], "stt": {}}}
    with structlog.testing.capture_logs() as captured:
        await health_check(raw)

    assert [
        c for c in captured
        if c.get("event") == "talker.health.web_only_checks_skipped"
    ] == []
