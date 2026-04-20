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

    async def test_weather_api_timeout_is_fail(self, monkeypatch, tmp_path: Path) -> None:
        """Timeouts / connection errors → FAIL.

        Post-BIT-hotfix (2026-04-19): timeouts mean the service is
        genuinely unreachable — not just "probe URL may be off". WARN
        is reserved for 4xx, which proves the service is up.
        """
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
        assert weather.status == Status.FAIL

    async def test_weather_api_200_is_ok(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(200))
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "UTC"},
                "weather": {"stations": [{"id": "CYZX"}]},
            },
        }
        result = await brief_health.health_check(raw)
        weather = next(r for r in result.results if r.name == "weather-api")
        assert weather.status == Status.OK
        assert "HTTP 200" in weather.detail

    async def test_weather_api_404_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        """4xx → WARN: the service answered, but our probe URL may be stale.

        This is the live-config case that surfaced on 2026-04-19: the
        previous probe hit ``/`` (the API root) and got HTTP 404, but
        the old status mapping coerced any response to OK. The hotfix
        both (a) changes the probe URL to match the real client's
        ``/metar?ids=...`` shape, and (b) returns WARN on 4xx so an
        endpoint regression doesn't hide behind a false OK.
        """
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(404))
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
        assert "HTTP 404" in weather.detail

    async def test_weather_api_500_is_fail(self, monkeypatch, tmp_path: Path) -> None:
        _patch_httpx(monkeypatch, brief_health, lambda: _FakeClient(500))
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "UTC"},
                "weather": {"stations": [{"id": "CYZX"}]},
            },
        }
        result = await brief_health.health_check(raw)
        weather = next(r for r in result.results if r.name == "weather-api")
        assert weather.status == Status.FAIL
        assert "HTTP 500" in weather.detail

    async def test_weather_api_probes_real_metar_endpoint(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """The probe must hit ``{api_base}/metar?ids=<first>&format=json``.

        Guarantees the probe exercises the same request shape the brief
        uses at runtime (see ``brief/weather.py::fetch_metars``).
        """
        captured: dict[str, str] = {}

        class _CapturingClient(_FakeClient):
            async def get(self, url):  # noqa: ANN001
                captured["url"] = url
                return _FakeResponse(200)

        _patch_httpx(monkeypatch, brief_health, lambda: _CapturingClient(200))
        raw = {
            "vault": {"path": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "UTC"},
                "weather": {
                    "api_base": "https://aviationweather.gov/api/data",
                    "stations": [{"id": "CYZX"}, {"id": "CYHZ"}],
                },
            },
        }
        await brief_health.health_check(raw)
        assert "metar" in captured["url"]
        assert "ids=CYZX" in captured["url"]


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
        raw = {"telegram": {"bot_token": "", "anthropic": {"api_key": "test-anthropic-key"}}}
        result = await talker_health.health_check(raw)
        assert result.status == Status.FAIL
        bot = next(r for r in result.results if r.name == "bot-token")
        assert bot.status == Status.FAIL

    async def test_unresolved_bot_token_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "${TELEGRAM_BOT_TOKEN}",
                "anthropic": {"api_key": "test-anthropic-key"},
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
                "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
                "allowed_users": [],
                "anthropic": {"api_key": "test-anthropic-key"},
                "stt": {"api_key": "test-stt-key"},
            }
        }
        result = await talker_health.health_check(raw)
        au = next(r for r in result.results if r.name == "allowed-users")
        assert au.status == Status.WARN

    async def test_missing_stt_key_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)
        raw = {
            "telegram": {
                "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
                "allowed_users": [1],
                "anthropic": {"api_key": "test-anthropic-key"},
                "stt": {"provider": "groq", "api_key": ""},
            }
        }
        result = await talker_health.health_check(raw)
        stt = next(r for r in result.results if r.name == "stt-key")
        assert stt.status == Status.WARN

    async def test_missing_anthropic_key_fails(self) -> None:
        raw = {
            "telegram": {
                "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
                "allowed_users": [1],
                "anthropic": {"api_key": ""},
                "stt": {"api_key": "test-stt-key"},
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
                "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
                "allowed_users": [1, 2],
                "anthropic": {"api_key": "test-anthropic-key", "model": "claude-sonnet-4-6"},
                "stt": {"provider": "groq", "api_key": "test-stt-key"},
                # wk2b c6: include tts section so the new tts-key +
                # elevenlabs-auth probes don't SKIP (which would bubble
                # up to mark the rollup SKIP rather than OK).
                "tts": {"api_key": "test-tts-key"},
            }
        }
        # Quick mode so the remote elevenlabs probe isn't attempted.
        result = await talker_health.health_check(raw, mode="quick")
        assert result.tool == "talker"
        assert result.status == Status.OK

    async def test_env_var_placeholders_are_expanded(self, monkeypatch) -> None:
        """Regression: bot_token / stt.api_key / anthropic.api_key all
        supplied via ``${VAR}`` placeholders must resolve to OK when the
        env vars are set.

        Before the 2026-04-19 hotfix, the talker health check inspected
        the pre-substitution raw config, so a user with
        ``bot_token: "${TELEGRAM_BOT_TOKEN}"`` would see FAIL even
        though the daemon's own config loader expanded the same
        placeholder at startup. The fix is to run ``_substitute_env``
        on the raw dict at the top of ``health_check``, matching what
        ``telegram/config.py::load_from_unified`` does.
        """
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "DUMMY_TELEGRAM_TEST_TOKEN")
        monkeypatch.setenv("GROQ_API_KEY", "DUMMY_GROQ_TEST_KEY")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "DUMMY_ANTHROPIC_TEST_KEY")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "DUMMY_ELEVENLABS_TEST_KEY")
        monkeypatch.setattr(talker_health, "check_anthropic_auth", _ok_auth_stub)

        raw = {
            "telegram": {
                "bot_token": "${TELEGRAM_BOT_TOKEN}",
                "allowed_users": [1],
                "anthropic": {"api_key": "${ANTHROPIC_API_KEY}"},
                "stt": {"provider": "groq", "api_key": "${GROQ_API_KEY}"},
                # wk2b c6: include tts so the rollup stays OK (tts-key
                # SKIP would bubble to SKIP otherwise).
                "tts": {"api_key": "${ELEVENLABS_API_KEY}"},
            }
        }
        result = await talker_health.health_check(raw, mode="quick")

        bot = next(r for r in result.results if r.name == "bot-token")
        assert bot.status == Status.OK
        assert "DUMMY_TELEGRAM_TEST_TOKEN" not in bot.detail  # secret shouldn't leak into detail
        stt = next(r for r in result.results if r.name == "stt-key")
        assert stt.status == Status.OK
        ath = next(r for r in result.results if r.name == "anthropic-auth")
        assert ath.status == Status.OK
        # wk2b c6: tts-key probe resolves the ELEVENLABS_API_KEY env var.
        tts = next(r for r in result.results if r.name == "tts-key")
        assert tts.status == Status.OK
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
