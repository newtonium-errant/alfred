"""Tests for ``alfred.integrations.gcal_health``.

Active-call probe rework (queue #8, 2026-05-17). Status mapping pinned
here (matches the module docstring):

  * OK   — active API call
           (``events().list(calendarId=<sync_calendar>, maxResults=1)``)
           succeeded. mtime info is supporting context in detail, not
           the gate.
  * WARN — active call raised a transient / network / unknown error,
           OR an HttpError with a non-auth status code.
  * FAIL — active call raised auth-specific failure: ``RefreshError``
           OR ``HttpError`` with status 401 / 403. OR token file
           unreadable (corrupt JSON, etc.).
  * SKIP — gcal.enabled=false OR token file missing OR section absent
           OR google-* libs not installed.

The probe is mock-based: real OAuth + Google API calls aren't
exercised. We monkey-patch ``_build_calendar_service`` to return a
fake service whose
``events().list(calendarId=..., maxResults=1).execute()`` returns
dict-shaped success / raises specific exception classes for the
failure paths.

**Scope-match rationale (2026-05-18 regression-pin)** — the probe
uses ``events().list``, NOT ``calendarList().list``, because the
adapter authorizes only the ``calendar.events`` scope. The
``test_active_probe_uses_events_list_not_calendar_list_*`` tests in
``TestScopeMatch`` pin this so a future refactor that "simplifies"
the probe to ``calendarList`` resurfaces the false-negative FAIL.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.integrations import gcal_health as gh
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(tmp_path: Path, age_seconds: float = 60.0) -> Path:
    """Create a token file with mtime ``age_seconds`` ago.

    Returns the absolute path. Default age is 1 minute (fresh) so the
    detail-string mtime context is fresh-looking unless the test
    overrides. Post-rework, mtime is no longer the gate — but it
    still appears in the detail as supporting context.
    """
    token_path = tmp_path / "gcal_token.json"
    token_path.write_text("{}", encoding="utf-8")
    target_mtime = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(token_path, (target_mtime, target_mtime))
    return token_path


def _make_log(tmp_path: Path, lines: list[str], name: str = "talker.log") -> Path:
    """Write a log file with the given lines and return its path."""
    log_dir = tmp_path / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / name
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _iso(dt: datetime) -> str:
    """Format a datetime as the structlog TimeStamper(fmt='iso') would."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_service_mock(
    *,
    execute_return: dict | None = None,
    execute_raises: BaseException | None = None,
) -> MagicMock:
    """Build a mock ``service`` object that exposes
    ``.events().list(calendarId=..., maxResults=1).execute()``.

    Either ``execute_return`` (success-path payload) OR
    ``execute_raises`` (exception to raise on .execute()) — caller
    chooses which.

    Mirrors the production probe's call shape exactly, per the
    scope-match rationale in the module docstring. The mock chain
    accepts ANY ``calendarId`` value (the production code resolves
    ``raw['gcal']['alfred_calendar_id']`` with a ``'primary'``
    fallback); ``TestScopeMatch`` asserts on the exact ``calendarId``
    that gets threaded through.
    """
    service = MagicMock()
    list_op = MagicMock()
    if execute_raises is not None:
        list_op.execute.side_effect = execute_raises
    else:
        list_op.execute.return_value = execute_return or {"items": []}
    events_resource = MagicMock()
    events_resource.list.return_value = list_op
    service.events.return_value = events_resource
    return service


def _patch_service(
    monkeypatch: pytest.MonkeyPatch,
    service: MagicMock | None = None,
    *,
    build_raises: BaseException | None = None,
) -> MagicMock | None:
    """Monkey-patch ``gcal_health._build_calendar_service`` to return
    the supplied mock (or raise the supplied exception).

    Returns the patched service (or None when ``build_raises`` was
    set) so the test can assert on the mock's call history if needed.
    """
    if build_raises is not None:
        def _raise(*_args, **_kw):
            raise build_raises
        monkeypatch.setattr(
            gh, "_build_calendar_service", _raise,
        )
        return None
    monkeypatch.setattr(
        gh, "_build_calendar_service", lambda _path: service,
    )
    return service


# ---------------------------------------------------------------------------
# SKIP paths
# ---------------------------------------------------------------------------


class TestSkipPaths:
    """All the ways the probe legitimately returns SKIP."""

    def test_skip_when_section_absent(self) -> None:
        """No ``gcal`` section in config → ToolHealth.status=SKIP."""
        raw: dict = {"logging": {"dir": "./data"}}
        th = asyncio.run(gh.health_check(raw, mode="quick"))
        assert th.status == Status.SKIP
        assert th.tool == "gcal"
        assert "no gcal section" in th.detail

    def test_skip_when_disabled(self, tmp_path: Path) -> None:
        """``gcal.enabled: false`` → SKIP regardless of token state.

        Even if a stale token file is sitting on disk from a previous
        enabled install, the operator opted out — no signal needed.
        No active call is made (the probe short-circuits BEFORE
        ``_build_calendar_service`` runs).
        """
        # Plant a stale token to prove it's ignored.
        _make_token(tmp_path, age_seconds=100 * 3600)
        raw = {
            "gcal": {
                "enabled": False,
                "token_path": str(tmp_path / "gcal_token.json"),
            },
        }
        th = asyncio.run(gh.health_check(raw, mode="quick"))
        assert th.status == Status.SKIP
        assert len(th.results) == 1
        assert th.results[0].status == Status.SKIP
        assert "disabled" in th.results[0].detail

    def test_skip_when_token_file_missing(self, tmp_path: Path) -> None:
        """``gcal.enabled: true`` but no token file → SKIP (fresh install).

        No active call attempted — without a token file the build step
        couldn't succeed anyway. The probe distinguishes "no token"
        from "auth broken" via the detail string and result.data.
        """
        raw = {
            "gcal": {
                "enabled": True,
                "token_path": str(tmp_path / "nonexistent_token.json"),
            },
        }
        th = asyncio.run(gh.health_check(raw, mode="quick"))
        assert th.status == Status.SKIP
        assert len(th.results) == 1
        assert th.results[0].status == Status.SKIP
        assert "no token file yet" in th.results[0].detail
        assert th.results[0].data["exists"] is False

    def test_skip_when_google_libs_not_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_build_calendar_service`` raises ImportError → SKIP.

        Pre-rework, the probe didn't try to build a service at all
        (mtime-only), so missing libs never surfaced as a probe issue.
        Post-rework, the active-call path needs the libs; an install
        without the ``gcal`` extras short-circuits to SKIP rather than
        emitting a noisy FAIL on the absent dependency.
        """
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            build_raises=ImportError("No module named 'google.oauth2'"),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.SKIP
        assert "libraries not installed" in result.detail
        assert "alfred-vault[gcal]" in result.detail
        assert result.data["libs_installed"] is False


# ---------------------------------------------------------------------------
# OK path — active call succeeds
# ---------------------------------------------------------------------------


class TestOkPath:
    """Active call succeeds → Status.OK regardless of mtime."""

    def test_ok_when_active_call_succeeds_with_fresh_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """API call returns a result → OK. Token mtime is fresh."""
        token_path = _make_token(tmp_path, age_seconds=300)
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_return={"items": [{"id": "primary"}]}),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK
        assert "active probe ok" in result.detail
        # mtime appears as supporting context.
        assert "token last refreshed" in result.detail
        assert result.data["active_probe"] == "ok"

    def test_ok_when_active_call_succeeds_with_stale_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """**Core rework regression-pin** (queue #8):

        Pre-rework, a token mtime ≥72h ago surfaced as FAIL even when
        the refresh token was still valid. The 2026-05-07 → 2026-05-15
        silent-auth incident motivated the rework: idle days are NOT
        broken days.

        Post-rework: mtime is supporting context, NOT the gate. An
        active call succeeding overrides any "old mtime" signal. The
        probe returns OK.

        Setup: token mtime 100h ago (would be FAIL under old rules);
        active call returns successfully.
        Expectation: status OK.
        """
        token_path = _make_token(tmp_path, age_seconds=100 * 3600)  # 100h ≥ 72h
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_return={"items": []}),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        # CRITICAL: stale mtime + active probe ok → OK, not FAIL.
        assert result.status == Status.OK
        assert "active probe ok" in result.detail
        # Mtime context still shows up in the detail so the operator
        # can see the staleness as info.
        assert "token last refreshed" in result.detail
        # Detail mtime context shows hours.
        assert "h ago" in result.detail

    def test_ok_no_last_error_suffix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OK detail stays clean — no last-error suffix on success."""
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_return={"items": []}),
        )
        # Plant a recent failure in the log — should NOT appear on OK.
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
        _make_log(
            tmp_path,
            [f"{ts} [warning  ] gcal.sync_create_failed       "
             f"error_code=quota_exceeded"],
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK
        assert "last error" not in result.detail


# ---------------------------------------------------------------------------
# Scope-match regression-pin (2026-05-18 false-negative FAIL fix)
# ---------------------------------------------------------------------------


class TestScopeMatch:
    """Pin the probe's API call shape to ``events().list``, NOT
    ``calendarList().list``.

    **Why this class exists** — the 2026-05-18 BIT showed ``[FAIL]``
    on ``last-successful-gcal-sync`` with detail ``http_403; run
    alfred gcal authorize: "Request had insufficient authentication
    scopes"``, but the actual sync writer succeeded an hour later
    creating an event. Root cause: the adapter authorizes only the
    narrow ``calendar.events`` scope (see
    ``alfred.integrations.gcal.DEFAULT_SCOPES``); ``calendarList``
    requires the broader ``calendar.readonly`` scope. Probe was
    out-of-scope while real sync was in-scope → false-negative FAIL.

    These tests pin the production code to ``events().list``. A future
    refactor that switches back to ``calendarList`` resurfaces the
    bug — these tests would catch it.
    """

    def test_active_probe_uses_events_list_not_calendar_list_for_scope_match(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the probe calls ``service.events().list``, NOT
        ``service.calendarList().list``.

        Mocks ``_build_calendar_service`` to return a fresh service
        mock whose ``calendarList`` and ``events`` attributes are
        both observable. Runs the probe and asserts ``events.list``
        was called while ``calendarList`` was NOT.
        """
        token_path = _make_token(tmp_path)
        service = MagicMock()
        # Wire up events().list().execute() success path.
        list_op = MagicMock()
        list_op.execute.return_value = {"items": []}
        events_resource = MagicMock()
        events_resource.list.return_value = list_op
        service.events.return_value = events_resource
        # calendarList is also wired so attribute access works, but we
        # assert below it's never CALLED.
        cal_list_resource = MagicMock()
        service.calendarList.return_value = cal_list_resource

        monkeypatch.setattr(gh, "_build_calendar_service", lambda _p: service)

        raw = {
            "gcal": {
                "enabled": True,
                "token_path": str(token_path),
                "alfred_calendar_id": "primary",
            },
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)

        # Probe succeeded with the in-scope API.
        assert result.status == Status.OK

        # CORE PIN: events().list was called.
        assert service.events.called, (
            "Probe must call service.events() to stay inside the "
            "calendar.events scope"
        )
        assert events_resource.list.called, (
            "Probe must call .list() on the events resource"
        )

        # CORE PIN: calendarList() was NEVER called. A future refactor
        # that adds ``calendarList`` re-introduces the scope-mismatch
        # bug and trips this assertion.
        assert not service.calendarList.called, (
            "Probe must NOT call service.calendarList — that requires "
            "calendar.readonly scope which the adapter doesn't "
            "authorize. Use events.list instead (scope: calendar.events)."
        )

    def test_active_probe_passes_configured_calendar_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The probe threads ``raw['gcal']['alfred_calendar_id']`` into
        the ``calendarId`` kwarg of ``events().list``.

        Matches the same calendar the sync writers target, so probe
        outcome tracks sync outcome.
        """
        token_path = _make_token(tmp_path)
        service = _make_service_mock(execute_return={"items": []})
        monkeypatch.setattr(gh, "_build_calendar_service", lambda _p: service)

        configured_cal = "andrew.test@group.calendar.google.com"
        raw = {
            "gcal": {
                "enabled": True,
                "token_path": str(token_path),
                "alfred_calendar_id": configured_cal,
            },
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK

        # Inspect the call kwargs to the events resource's .list().
        events_resource = service.events.return_value
        events_resource.list.assert_called_once_with(
            calendarId=configured_cal, maxResults=1
        )

    def test_active_probe_falls_back_to_primary_when_calendar_id_unset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``alfred_calendar_id`` is missing or empty, fall back
        to ``'primary'``.

        Degraded-config path: probe still verifies auth works even when
        the operator hasn't configured a specific sync calendar yet.
        Better than blowing up with a KeyError.
        """
        token_path = _make_token(tmp_path)
        service = _make_service_mock(execute_return={"items": []})
        monkeypatch.setattr(gh, "_build_calendar_service", lambda _p: service)

        # No ``alfred_calendar_id`` key at all.
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK

        events_resource = service.events.return_value
        events_resource.list.assert_called_once_with(
            calendarId="primary", maxResults=1
        )

    def test_active_probe_falls_back_to_primary_when_calendar_id_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty-string ``alfred_calendar_id`` also falls back to
        ``'primary'``. Catches the misconfig where the field exists
        but was never populated.
        """
        token_path = _make_token(tmp_path)
        service = _make_service_mock(execute_return={"items": []})
        monkeypatch.setattr(gh, "_build_calendar_service", lambda _p: service)

        raw = {
            "gcal": {
                "enabled": True,
                "token_path": str(token_path),
                "alfred_calendar_id": "",  # explicitly empty
            },
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK

        events_resource = service.events.return_value
        events_resource.list.assert_called_once_with(
            calendarId="primary", maxResults=1
        )


# ---------------------------------------------------------------------------
# FAIL path — auth-specific failure
# ---------------------------------------------------------------------------


class TestFailPath:
    """Active call raises auth-broken errors → Status.FAIL."""

    def test_fail_on_refresh_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``google.auth.exceptions.RefreshError`` → FAIL.

        Lazy-import inside the classifier means this test must be able
        to import the real ``RefreshError``. Skip if google-auth isn't
        available (no point — the SKIP path covers that case).
        """
        try:
            from google.auth.exceptions import RefreshError  # type: ignore
        except ImportError:
            pytest.skip("google-auth not installed; RefreshError unavailable")

        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(
                execute_raises=RefreshError(
                    "invalid_grant: Token has been expired or revoked"
                ),
            ),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "alfred gcal authorize" in result.detail
        assert "refresh_failed" in result.detail
        assert result.data["error_class"] == "refresh_failed"
        assert result.data["exception_type"] == "RefreshError"

    def test_fail_on_http_401(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``HttpError`` with status 401 → FAIL (unauthorized)."""
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip(
                "google-api-python-client not installed; "
                "HttpError unavailable"
            )

        token_path = _make_token(tmp_path)
        # Build an HttpError. The googleapiclient API: HttpError(resp,
        # content, uri=None). resp needs ``.status``.
        fake_resp = MagicMock()
        fake_resp.status = 401
        fake_resp.reason = "Unauthorized"
        http_err = HttpError(fake_resp, b'{"error": "invalid_credentials"}')
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_raises=http_err),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "alfred gcal authorize" in result.detail
        assert "http_401" in result.detail
        assert result.data["error_class"] == "http_401"

    def test_fail_on_http_403(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``HttpError`` with status 403 → FAIL (forbidden, auth-rejected)."""
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")

        token_path = _make_token(tmp_path)
        fake_resp = MagicMock()
        fake_resp.status = 403
        fake_resp.reason = "Forbidden"
        http_err = HttpError(fake_resp, b'{"error": "forbidden"}')
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_raises=http_err),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "http_403" in result.detail

    def test_fail_on_unreadable_token_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Token file exists but ``_build_calendar_service`` raises on
        a non-ImportError (e.g. JSONDecodeError on a corrupt file) →
        FAIL with a clean operator message.
        """
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            build_raises=ValueError(
                "Expecting property name enclosed in double quotes"
            ),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "token file unreadable" in result.detail
        assert "alfred gcal authorize" in result.detail
        assert result.data["error_class"] == "token_load_failed"

    def test_fail_includes_last_error_suffix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FAIL status appends the last-error suffix when a recent
        failure shows up in the log."""
        try:
            from google.auth.exceptions import RefreshError  # type: ignore
        except ImportError:
            pytest.skip("google-auth not installed")

        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(
                execute_raises=RefreshError("Token revoked"),
            ),
        )
        # Recent log entry.
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        _make_log(
            tmp_path,
            [f"{ts} [warning  ] gcal.sync_update_failed       "
             f"error_code='invalid_grant'"],
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "; last error: invalid_grant" in result.detail


# ---------------------------------------------------------------------------
# WARN path — transient / non-auth failures
# ---------------------------------------------------------------------------


class TestWarnPath:
    """Active call raises transient / network / non-auth errors → WARN."""

    def test_warn_on_network_timeout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Network timeout (generic exception) → WARN, not FAIL.

        The 2026-05-07 silent-auth incident also taught us: don't burn
        the operator's OAuth flow on a transient. Network errors during
        the active probe are diagnostically valuable (something's
        wrong) but don't necessarily mean re-auth.
        """
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(
                execute_raises=TimeoutError("Connection timed out"),
            ),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "could not verify" in result.detail
        assert "transient" in result.detail
        # No "run alfred gcal authorize" hint on WARN — operator
        # should retry, not re-auth.
        assert "alfred gcal authorize" not in result.detail
        assert result.data["error_class"] == "transient"

    def test_warn_on_http_500(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``HttpError`` with 5xx status → WARN (server-side / transient)."""
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")

        token_path = _make_token(tmp_path)
        fake_resp = MagicMock()
        fake_resp.status = 500
        fake_resp.reason = "Internal Server Error"
        http_err = HttpError(fake_resp, b'{"error": "internal"}')
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_raises=http_err),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "http_500" in result.detail
        assert "alfred gcal authorize" not in result.detail

    def test_warn_on_http_429_quota(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``HttpError`` 429 quota exceeded → WARN (transient, retry)."""
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")

        token_path = _make_token(tmp_path)
        fake_resp = MagicMock()
        fake_resp.status = 429
        fake_resp.reason = "Too Many Requests"
        http_err = HttpError(fake_resp, b'{"error": "quota"}')
        _patch_service(
            monkeypatch,
            _make_service_mock(execute_raises=http_err),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "http_429" in result.detail

    def test_warn_on_dns_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OSError / socket-style network errors → WARN."""
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(
                execute_raises=OSError(
                    "[Errno -3] Temporary failure in name resolution"
                ),
            ),
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "transient" in result.detail

    def test_warn_includes_last_error_suffix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WARN also gets the last-error suffix from log scan."""
        token_path = _make_token(tmp_path)
        _patch_service(
            monkeypatch,
            _make_service_mock(
                execute_raises=TimeoutError("network down"),
            ),
        )
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
        _make_log(
            tmp_path,
            [f"{ts} [warning  ] gcal.sync_create_failed       "
             f"error_code=auth_failed"],
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "; last error: auth_failed" in result.detail


# ---------------------------------------------------------------------------
# Active-call classifier unit tests
# ---------------------------------------------------------------------------


class TestClassifier:
    """Direct unit tests on ``_classify_active_call_outcome``."""

    def test_classifier_refresh_error_is_fail(self) -> None:
        try:
            from google.auth.exceptions import RefreshError  # type: ignore
        except ImportError:
            pytest.skip("google-auth not installed")
        exc = RefreshError("invalid_grant")
        status, tag = gh._classify_active_call_outcome(exc)
        assert status == Status.FAIL
        assert tag == "refresh_failed"

    def test_classifier_http_401_is_fail(self) -> None:
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")
        resp = MagicMock()
        resp.status = 401
        exc = HttpError(resp, b"{}")
        status, tag = gh._classify_active_call_outcome(exc)
        assert status == Status.FAIL
        assert tag == "http_401"

    def test_classifier_http_403_is_fail(self) -> None:
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")
        resp = MagicMock()
        resp.status = 403
        exc = HttpError(resp, b"{}")
        status, tag = gh._classify_active_call_outcome(exc)
        assert status == Status.FAIL
        assert tag == "http_403"

    def test_classifier_http_500_is_warn(self) -> None:
        try:
            from googleapiclient.errors import HttpError  # type: ignore
        except ImportError:
            pytest.skip("googleapiclient unavailable")
        resp = MagicMock()
        resp.status = 500
        exc = HttpError(resp, b"{}")
        status, tag = gh._classify_active_call_outcome(exc)
        assert status == Status.WARN
        assert tag == "http_500"

    def test_classifier_generic_exception_is_warn_transient(self) -> None:
        status, tag = gh._classify_active_call_outcome(
            TimeoutError("timed out")
        )
        assert status == Status.WARN
        assert tag == "transient"

    def test_classifier_oserror_is_warn_transient(self) -> None:
        status, tag = gh._classify_active_call_outcome(
            OSError("network down")
        )
        assert status == Status.WARN
        assert tag == "transient"


# ---------------------------------------------------------------------------
# Last-error suffix log-scanning (carried over unchanged from prior tests)
# ---------------------------------------------------------------------------


class TestScanRecentFailures:
    """Direct exercise of ``_scan_recent_failures`` parser quirks.

    Unchanged from the pre-rework probe — the log-scanner is the
    same helper.
    """

    def test_returns_none_when_no_log_files(self, tmp_path: Path) -> None:
        result = gh._scan_recent_failures(
            [tmp_path / "nope.log", tmp_path / "also_nope.log"]
        )
        assert result is None

    def test_returns_none_when_no_matching_lines(self, tmp_path: Path) -> None:
        ts = _iso(datetime.now(timezone.utc))
        log_path = _make_log(
            tmp_path,
            [
                f"{ts} [info     ] gcal.sync_created             event_id=abc",
                f"{ts} [info     ] gcal.event_updated            event_id=def",
            ],
        )
        result = gh._scan_recent_failures([log_path])
        assert result is None

    def test_ansi_escape_stripped(self, tmp_path: Path) -> None:
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        ansi_line = (
            f"\x1b[2m{ts}\x1b[0m [\x1b[33m\x1b[1mwarning  \x1b[0m] "
            f"\x1b[1mgcal.sync_create_failed       \x1b[0m "
            f"\x1b[36merror_code\x1b[0m=\x1b[35mauth_failed\x1b[0m"
        )
        log_path = _make_log(tmp_path, [ansi_line])
        result = gh._scan_recent_failures([log_path])
        assert result is not None
        assert result["error_code"] == "auth_failed"
        assert result["event"] == "gcal.sync_create_failed"

    def test_tail_only_reads_last_bytes(self, tmp_path: Path) -> None:
        ts_old = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        head_line = (
            f"{ts_old} [warning  ] gcal.sync_create_failed       "
            f"error_code=head_entry"
        )
        padding = ["x" * 100 for _ in range(20)]
        log_path = _make_log(tmp_path, [head_line, *padding])
        result = gh._scan_recent_failures([log_path], tail_bytes=100)
        assert result is None

    def test_old_failure_outside_window_ignored(self, tmp_path: Path) -> None:
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
        log_path = _make_log(
            tmp_path,
            [f"{ts} [warning  ] gcal.sync_create_failed       "
             f"error_code=stale_old_error"],
        )
        result = gh._scan_recent_failures([log_path])
        assert result is None


# ---------------------------------------------------------------------------
# Top-level health_check integration
# ---------------------------------------------------------------------------


def test_health_check_returns_tool_health_with_single_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``health_check`` entry point returns a ToolHealth with the
    probe's single result mirrored into ``results``."""
    token_path = _make_token(tmp_path)
    _patch_service(
        monkeypatch,
        _make_service_mock(execute_return={"items": []}),
    )
    raw = {
        "gcal": {"enabled": True, "token_path": str(token_path)},
        "logging": {"dir": str(tmp_path / "no_logs")},
    }
    th = asyncio.run(gh.health_check(raw, mode="quick"))
    assert th.tool == "gcal"
    assert th.status == Status.OK
    assert len(th.results) == 1
    assert th.results[0].name == "last-successful-gcal-sync"


def test_health_check_registered_under_gcal_in_aggregator() -> None:
    """The module's top-level ``register_check`` call wires ``"gcal"``
    into the aggregator registry.

    Pins the registration contract so a future refactor that removes
    the side-effecting import-time call surfaces immediately rather
    than via silent SKIP in production.
    """
    import importlib
    import alfred.integrations.gcal_health as mod
    importlib.reload(mod)
    from alfred.health.aggregator import _REGISTRY
    assert "gcal" in _REGISTRY


# ---------------------------------------------------------------------------
# Tilde-expansion in token_path (carried over)
# ---------------------------------------------------------------------------


def test_token_path_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``~/...`` in ``gcal.token_path`` resolves to the real home dir.

    Mirrors ``GCalClient``'s own expansion so the probe and the
    adapter look at the same file. Without expansion, the probe would
    SKIP on every install that uses the default config (since ``~``
    as a literal directory rarely exists).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # Place a real token under the expanded location.
    secrets_dir = fake_home / "alfred" / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    token = secrets_dir / "gcal_token.json"
    token.write_text("{}", encoding="utf-8")

    # Active probe needs the service patched.
    _patch_service(
        monkeypatch,
        _make_service_mock(execute_return={"items": []}),
    )

    raw = {
        "gcal": {
            "enabled": True,
            "token_path": "~/alfred/data/secrets/gcal_token.json",
        },
        "logging": {"dir": str(tmp_path / "no_logs")},
    }
    result = gh._check_last_successful_gcal_sync(raw)
    assert result.status == Status.OK
    assert str(token) in result.data["token_path"]
