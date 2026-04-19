"""Unit tests for surveyor / brief / mail / talker health modules (BIT c3).

We patch ``httpx.AsyncClient`` at the module level where each health
module imports it (``alfred.surveyor.health`` and ``alfred.brief.health``
both do ``import httpx`` lazily inside the check function). Our stub
client lets a test control the response status code and any raised
exception without a real network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.brief import health as brief_health
from alfred.health.types import CheckResult, Status
from alfred.mail import health as mail_health
from alfred.surveyor import health as surveyor_health
from alfred.telegram import health as talker_health


# ---------------------------------------------------------------------------
# httpx fakes — shared
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Drop-in async context manager that returns a scripted response."""

    def __init__(self, status_code: int = 200, raises: Exception | None = None):
        self._status = status_code
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    async def get(self, url):  # noqa: ANN001
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._status)


class _FakeHttpxModule:
    """Minimal httpx shim — only AsyncClient is referenced by checks."""

    def __init__(self, client_factory):  # noqa: ANN001
        self._factory = client_factory

    def AsyncClient(self, *, timeout):  # noqa: ANN001, N802
        return self._factory()


def _patch_httpx(monkeypatch, module, client_factory):  # noqa: ANN001
    """Replace httpx inside a module's lazy import path.

    Each health module does ``import httpx`` inside its probe function,
    so we patch ``sys.modules`` and, for defensiveness, the module
    attribute if present.
    """
    import sys
    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpxModule(client_factory))


# ---------------------------------------------------------------------------
# Surveyor
# ---------------------------------------------------------------------------

class TestSurveyorHealth:
    async def test_missing_section_returns_skip(self) -> None:
        result = await surveyor_health.health_check({})
        assert result.status == Status.SKIP
        assert "no surveyor section" in result.detail

    async def test_happy_path_ok(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, surveyor_health, lambda: _FakeClient(status_code=200))
        raw = {
            "surveyor": {
                "ollama": {"base_url": "http://localhost:11434"},
                "milvus": {"uri": str(tmp_path / "milvus.db")},
                "openrouter": {"api_key": "or-xxx", "model": "x-ai/grok"},
            }
        }
        result = await surveyor_health.health_check(raw)
        assert result.tool == "surveyor"
        assert result.status == Status.OK
        names = {r.name for r in result.results}
        assert "ollama-reachable" in names
        assert "milvus-lite" in names
        assert "openrouter-key" in names

    async def test_ollama_unreachable_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        def _factory():
            return _FakeClient(raises=ConnectionError("refused"))
        _patch_httpx(monkeypatch, surveyor_health, _factory)
        raw = {
            "surveyor": {
                "ollama": {"base_url": "http://localhost:11434"},
                "milvus": {"uri": str(tmp_path / "m.db")},
                "openrouter": {"api_key": "or-xxx", "model": "x"},
            }
        }
        result = await surveyor_health.health_check(raw)
        ollama = next(r for r in result.results if r.name == "ollama-reachable")
        assert ollama.status == Status.WARN

    async def test_empty_ollama_url_is_fail(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, surveyor_health, lambda: _FakeClient())
        raw = {
            "surveyor": {
                "ollama": {"base_url": ""},
                "milvus": {"uri": str(tmp_path / "m.db")},
                "openrouter": {"api_key": "k", "model": "m"},
            }
        }
        result = await surveyor_health.health_check(raw)
        ollama = next(r for r in result.results if r.name == "ollama-reachable")
        assert ollama.status == Status.FAIL

    async def test_milvus_missing_parent_warns(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, surveyor_health, lambda: _FakeClient())
        raw = {
            "surveyor": {
                "ollama": {"base_url": "http://x"},
                "milvus": {"uri": str(tmp_path / "nope" / "m.db")},
                "openrouter": {"api_key": "k", "model": "m"},
            }
        }
        result = await surveyor_health.health_check(raw)
        mv = next(r for r in result.results if r.name == "milvus-lite")
        assert mv.status == Status.WARN

    async def test_openrouter_unresolved_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, surveyor_health, lambda: _FakeClient())
        raw = {
            "surveyor": {
                "ollama": {"base_url": "http://x"},
                "milvus": {"uri": str(tmp_path / "m.db")},
                "openrouter": {"api_key": "${OPENROUTER_API_KEY}", "model": "m"},
            }
        }
        result = await surveyor_health.health_check(raw)
        orc = next(r for r in result.results if r.name == "openrouter-key")
        assert orc.status == Status.WARN


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

class TestBriefHealth:
    async def test_missing_section_returns_skip(self) -> None:
        result = await brief_health.health_check({})
        assert result.status == Status.SKIP

    async def test_happy_path_ok(self, monkeypatch, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        (vault / "run").mkdir(parents=True)
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(200))
        raw = {
            "vault": {"path": str(vault)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "America/Halifax"},
                "output": {"directory": "run"},
                "weather": {"stations": [{"id": "CYZX", "name": "Greenwood"}]},
            },
        }
        result = await brief_health.health_check(raw)
        assert result.tool == "brief"
        assert result.status == Status.OK

    async def test_invalid_schedule_time_fails(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(200))
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {"schedule": {"time": "25:00", "timezone": "UTC"}},
        }
        result = await brief_health.health_check(raw)
        assert result.status == Status.FAIL
        st = next(r for r in result.results if r.name == "schedule-time")
        assert st.status == Status.FAIL

    async def test_unknown_timezone_fails(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(200))
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {"schedule": {"time": "06:00", "timezone": "Mars/Olympus"}},
        }
        result = await brief_health.health_check(raw)
        tz = next(r for r in result.results if r.name == "schedule-timezone")
        assert tz.status == Status.FAIL

    async def test_empty_stations_skips_weather_probe(self, monkeypatch, tmp_path: Path) -> None:
        # The weather probe should not be called when stations is empty —
        # assert by making httpx raise if it is called.
        def _factory():
            return _FakeClient(raises=RuntimeError("should not be called"))
        _patch_httpx(monkeypatch, brief_health, _factory)
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "UTC"},
                "weather": {"stations": []},
            },
        }
        result = await brief_health.health_check(raw)
        weather = next(r for r in result.results if r.name == "weather-api")
        assert weather.status == Status.SKIP

    async def test_weather_api_unreachable_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        def _factory():
            return _FakeClient(raises=TimeoutError("too slow"))
        _patch_httpx(monkeypatch, brief_health, _factory)
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "UTC"},
                "weather": {"stations": [{"id": "CYZX"}]},
            },
        }
        result = await brief_health.health_check(raw)
        weather = next(r for r in result.results if r.name == "weather-api")
        assert weather.status == Status.WARN


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

class TestMailHealth:
    async def test_missing_section_returns_skip(self) -> None:
        result = await mail_health.health_check({})
        assert result.status == Status.SKIP

    async def test_empty_accounts_warns(self, tmp_path: Path) -> None:
        raw = {"vault": {"path": str(tmp_path)}, "mail": {"accounts": []}}
        result = await mail_health.health_check(raw)
        accounts = next(r for r in result.results if r.name == "mail-accounts")
        assert accounts.status == Status.WARN

    async def test_account_missing_fields_fails(self, tmp_path: Path) -> None:
        raw = {
            "vault": {"path": str(tmp_path)},
            "mail": {"accounts": [{"name": "personal"}]},  # missing email, imap_host
        }
        result = await mail_health.health_check(raw)
        assert result.status == Status.FAIL
        entries = [r for r in result.results if r.name.startswith("account:")]
        assert len(entries) == 1
        assert entries[0].status == Status.FAIL
        assert "email" in entries[0].detail
        assert "imap_host" in entries[0].detail

    async def test_good_account_is_ok(self, tmp_path: Path) -> None:
        (tmp_path / "inbox").mkdir()
        raw = {
            "vault": {"path": str(tmp_path)},
            "mail": {
                "accounts": [
                    {
                        "name": "primary",
                        "email": "a@b.com",
                        "imap_host": "imap.example.com",
                    }
                ]
            },
        }
        result = await mail_health.health_check(raw)
        assert result.status == Status.OK
        assert any(r.name == "account:primary" for r in result.results)

    async def test_missing_inbox_warns(self, tmp_path: Path) -> None:
        raw = {
            "vault": {"path": str(tmp_path)},
            "mail": {
                "accounts": [
                    {
                        "name": "x",
                        "email": "a@b.com",
                        "imap_host": "imap.example.com",
                    }
                ]
            },
        }
        result = await mail_health.health_check(raw)
        inbox = next(r for r in result.results if r.name == "inbox-dir")
        assert inbox.status == Status.WARN


# ---------------------------------------------------------------------------
# Talker
# ---------------------------------------------------------------------------

async def _ok_auth_stub(api_key, model="claude-haiku-4-5"):  # noqa: ANN001
    return CheckResult(name="anthropic-auth", status=Status.OK, detail="stubbed")


class TestTalkerHealth:
    async def test_missing_section_returns_skip(self) -> None:
        result = await talker_health.health_check({})
        assert result.status == Status.SKIP

    async def test_empty_bot_token_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {"telegram": {"bot_token": "", "anthropic": {"api_key": "sk-x"}}}
        result = await talker_health.health_check(raw)
        assert result.status == Status.FAIL
        bot = next(r for r in result.results if r.name == "bot-token")
        assert bot.status == Status.FAIL

    async def test_unresolved_bot_token_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "${TELEGRAM_BOT_TOKEN}",
                "anthropic": {"api_key": "sk-x"},
            }
        }
        result = await talker_health.health_check(raw)
        bot = next(r for r in result.results if r.name == "bot-token")
        assert bot.status == Status.FAIL
        assert "placeholder" in bot.detail

    async def test_empty_allowed_users_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "123:abc",
                "allowed_users": [],
                "anthropic": {"api_key": "sk-x"},
                "stt": {"api_key": "gsk-x"},
            }
        }
        result = await talker_health.health_check(raw)
        au = next(r for r in result.results if r.name == "allowed-users")
        assert au.status == Status.WARN

    async def test_missing_stt_key_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "123:abc",
                "allowed_users": [1],
                "anthropic": {"api_key": "sk-x"},
                "stt": {"provider": "groq", "api_key": ""},
            }
        }
        result = await talker_health.health_check(raw)
        stt = next(r for r in result.results if r.name == "stt-key")
        assert stt.status == Status.WARN

    async def test_missing_anthropic_key_fails(self) -> None:
        raw = {
            "telegram": {
                "bot_token": "123:abc",
                "allowed_users": [1],
                "anthropic": {"api_key": ""},
                "stt": {"api_key": "gsk-x"},
            }
        }
        result = await talker_health.health_check(raw)
        # Anthropic missing is FAIL
        ath = next(r for r in result.results if r.name == "anthropic-auth")
        assert ath.status == Status.FAIL

    async def test_happy_path_ok(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "123:abc",
                "allowed_users": [1, 2],
                "anthropic": {"api_key": "sk-x", "model": "claude-sonnet-4-6"},
                "stt": {"provider": "groq", "api_key": "gsk-x"},
            }
        }
        result = await talker_health.health_check(raw)
        assert result.tool == "talker"
        assert result.status == Status.OK


# ---------------------------------------------------------------------------
# Aggregator integration — real tools load via _load_tool_checks
# ---------------------------------------------------------------------------

class TestAggregatorIntegration:
    async def test_load_tool_checks_populates_registry(self, tmp_path: Path) -> None:
        """Calling run_all_checks imports every tool module and registers it.

        Since c3 lands checks for all 7 known tools, the registry should
        be full after a run.
        """
        from alfred.health import aggregator as agg
        agg.clear_registry()
        # Build a config that exercises the "section missing → SKIP"
        # path for optional tools — they'll still register.
        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "agent": {"backend": "zo"},
        }
        report = await agg.run_all_checks(raw, mode="quick")
        registered = set(agg._REGISTRY.keys())
        # c2 + c3 land seven tools
        expected = {"curator", "janitor", "distiller", "surveyor", "brief", "mail", "talker"}
        assert expected <= registered
        # Every probed tool shows up in the report
        tools_in_report = {t.tool for t in report.tools}
        assert expected <= tools_in_report
