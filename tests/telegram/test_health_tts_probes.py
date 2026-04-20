"""Tests for wk2b commit 6 — TTS-related BIT probes.

Covers:
    * ``tts-key`` probe: OK on populated key, FAIL on unresolved
      placeholder / empty, SKIP when tts section absent.
    * ``capture-handler-registered`` probe: OK when both modules
      import successfully.
    * ``elevenlabs-auth`` probe: OK on 200, FAIL on non-200 or
      network error, SKIP when tts section absent, only runs in full
      mode.
    * Overall talker health_check result includes all probes and
      SKIP-safely degrades when tts is absent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from alfred.health.types import Status
from alfred.telegram import health


# --- _check_tts_key -------------------------------------------------------


def test_tts_key_skip_when_section_absent() -> None:
    r = health._check_tts_key(None)
    assert r.status == Status.SKIP
    assert "absent" in r.detail.lower() or "opt-in" in r.detail.lower()


def test_tts_key_fail_on_empty() -> None:
    r = health._check_tts_key({"provider": "elevenlabs", "api_key": ""})
    assert r.status == Status.FAIL
    assert "missing" in r.detail.lower()


def test_tts_key_fail_on_unresolved_placeholder() -> None:
    r = health._check_tts_key({
        "provider": "elevenlabs",
        "api_key": "${ELEVENLABS_API_KEY}",
    })
    assert r.status == Status.FAIL
    assert "placeholder" in r.detail.lower()


def test_tts_key_ok_when_populated() -> None:
    r = health._check_tts_key({
        "provider": "elevenlabs",
        "api_key": "DUMMY_ELEVENLABS_TEST_KEY",
    })
    assert r.status == Status.OK
    assert r.data["length"] == len("DUMMY_ELEVENLABS_TEST_KEY")


# --- _check_capture_handlers ---------------------------------------------


def test_capture_handlers_ok_when_modules_import() -> None:
    r = health._check_capture_handlers()
    assert r.status == Status.OK
    assert "importable" in r.detail


# --- _check_elevenlabs_auth ----------------------------------------------


@pytest.mark.asyncio
async def test_elevenlabs_auth_ok_on_200(monkeypatch) -> None:
    async def _fake_get(self, url, **kwargs):
        assert url.endswith("/v1/user")
        assert kwargs["headers"]["xi-api-key"] == "DUMMY_ELEVENLABS_TEST_KEY"
        return httpx.Response(200, json={"subscription": {"tier": "starter"}})
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    r = await health._check_elevenlabs_auth({"api_key": "DUMMY_ELEVENLABS_TEST_KEY"})
    assert r.status == Status.OK
    assert "200" in r.detail


@pytest.mark.asyncio
async def test_elevenlabs_auth_fail_on_401(monkeypatch) -> None:
    async def _fake_get(self, url, **kwargs):
        return httpx.Response(401, text="unauthorized")
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    r = await health._check_elevenlabs_auth({"api_key": "DUMMY_ELEVENLABS_BAD_KEY"})
    assert r.status == Status.FAIL
    assert "401" in r.detail


@pytest.mark.asyncio
async def test_elevenlabs_auth_skip_when_tts_absent() -> None:
    r = await health._check_elevenlabs_auth(None)
    assert r.status == Status.SKIP


@pytest.mark.asyncio
async def test_elevenlabs_auth_skip_when_key_missing() -> None:
    r = await health._check_elevenlabs_auth({"api_key": ""})
    assert r.status == Status.SKIP


# --- Full health_check rollup --------------------------------------------


@pytest.mark.asyncio
async def test_health_check_without_tts_section_does_not_fail(monkeypatch) -> None:
    """No tts section → tts-key + elevenlabs-auth SKIP, overall unaffected."""
    raw = {
        "telegram": {
            "bot_token": "test-token-not-empty",
            "allowed_users": [1],
            "stt": {"provider": "groq", "api_key": "DUMMY_GROQ_TEST_KEY"},
            "anthropic": {"api_key": "DUMMY_ANTHROPIC_TEST_KEY"},
        },
    }

    # Mock anthropic auth so we don't hit the real API.
    from alfred.health import anthropic_auth
    from alfred.health.types import CheckResult

    async def _fake_auth(api_key, model):
        return CheckResult(
            name="anthropic-auth", status=Status.OK, detail="ok",
        )
    monkeypatch.setattr(anthropic_auth, "check_anthropic_auth", _fake_auth)
    monkeypatch.setattr(health, "check_anthropic_auth", _fake_auth)

    result = await health.health_check(raw, mode="full")

    # Collect results by name.
    by_name = {r.name: r for r in result.results}
    assert by_name["tts-key"].status == Status.SKIP
    assert by_name["capture-handler-registered"].status == Status.OK
    assert by_name["elevenlabs-auth"].status == Status.SKIP

    # Overall status shouldn't be FAIL just because of missing opt-in tts.
    assert result.status != Status.FAIL


@pytest.mark.asyncio
async def test_health_check_quick_mode_skips_remote_elevenlabs_probe(
    monkeypatch,
) -> None:
    """Quick mode: tts-key runs, but elevenlabs-auth remote probe is skipped entirely."""
    raw = {
        "telegram": {
            "bot_token": "test-token",
            "allowed_users": [1],
            "stt": {"api_key": "DUMMY_GROQ_TEST_KEY"},
            "anthropic": {"api_key": "DUMMY_ANTHROPIC_TEST_KEY"},
            "tts": {"api_key": "DUMMY_ELEVENLABS_TEST_KEY"},
        },
    }
    from alfred.health import anthropic_auth
    from alfred.health.types import CheckResult

    async def _fake_auth(api_key, model):
        return CheckResult(name="anthropic-auth", status=Status.OK)
    monkeypatch.setattr(anthropic_auth, "check_anthropic_auth", _fake_auth)
    monkeypatch.setattr(health, "check_anthropic_auth", _fake_auth)

    result = await health.health_check(raw, mode="quick")

    names = {r.name for r in result.results}
    assert "tts-key" in names
    assert "capture-handler-registered" in names
    # Quick mode: the remote probe isn't in the result list at all.
    assert "elevenlabs-auth" not in names
