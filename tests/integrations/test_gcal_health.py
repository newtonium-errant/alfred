"""Tests for ``alfred.integrations.gcal_health``.

Covers the four-state probe (OK / WARN / FAIL / SKIP) plus the
underlying log-scanning helper. The probe is a thin mtime-inspector
+ log-tail-grepper; we mock the token file mtime via ``os.utime`` and
synthesise log files with known timestamps rather than spinning up
real OAuth or daemon flows.

Status mapping pinned here (matches the module docstring):

  * OK   — token mtime < 24h ago
  * WARN — 24h <= mtime < 72h
  * FAIL — mtime >= 72h
  * SKIP — gcal.enabled=false OR token file missing OR section absent
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alfred.integrations import gcal_health as gh
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_token(tmp_path: Path, age_seconds: float) -> Path:
    """Create a token file with mtime ``age_seconds`` ago.

    Returns the absolute path. Caller passes this path into the config
    dict so the probe consults it instead of the default.
    """
    token_path = tmp_path / "gcal_token.json"
    token_path.write_text("{}", encoding="utf-8")
    target_mtime = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(token_path, (target_mtime, target_mtime))
    return token_path


def _make_log(tmp_path: Path, lines: list[str], name: str = "talker.log") -> Path:
    """Write a log file with the given lines (one per element) and
    return its path. Lines should be pre-formatted as the structlog
    ConsoleRenderer would emit them (without ANSI for readability —
    the probe's regex matches both with and without ANSI since the
    stripper runs unconditionally)."""
    log_dir = tmp_path / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / name
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _iso(dt: datetime) -> str:
    """Format a datetime as the structlog TimeStamper(fmt='iso') would."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        """``gcal.enabled: true`` but no token file → SKIP (fresh install)."""
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


# ---------------------------------------------------------------------------
# OK / WARN / FAIL paths via mtime thresholds
# ---------------------------------------------------------------------------

class TestThresholdMapping:
    """The three live status paths driven purely by token file mtime."""

    def test_ok_path_recent_mtime(self, tmp_path: Path) -> None:
        """mtime < 24h ago → Status.OK."""
        token_path = _make_token(tmp_path, age_seconds=0.3 * 3600)  # ~18min
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs_here")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK
        assert "last gcal sync" in result.detail
        assert "token last refreshed" in result.detail
        # Hours format: "0.3h" or similar.
        assert "h ago" in result.detail
        # No error suffix on OK (per module docstring).
        assert "last error" not in result.detail

    def test_warn_path_mid_threshold(self, tmp_path: Path) -> None:
        """24h <= mtime < 72h → Status.WARN."""
        token_path = _make_token(tmp_path, age_seconds=36.5 * 3600)
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs_here")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "may be stale" in result.detail
        # Hours format check.
        assert "36.5h ago" in result.detail or "36" in result.detail

    def test_fail_path_above_72h(self, tmp_path: Path) -> None:
        """mtime >= 72h → Status.FAIL with auth-revoked hint."""
        token_path = _make_token(tmp_path, age_seconds=216.7 * 3600)
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs_here")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "auth likely revoked" in result.detail
        assert "alfred gcal authorize" in result.detail

    def test_fail_path_exactly_72h(self, tmp_path: Path) -> None:
        """Boundary case: ``age == 72h`` → FAIL (>= boundary)."""
        token_path = _make_token(tmp_path, age_seconds=72 * 3600 + 5)
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs_here")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL

    def test_warn_boundary_just_over_24h(self, tmp_path: Path) -> None:
        """Boundary case: ``age == 24h + 1m`` → WARN."""
        token_path = _make_token(tmp_path, age_seconds=24 * 3600 + 60)
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "no_logs_here")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN


# ---------------------------------------------------------------------------
# Last-error suffix
# ---------------------------------------------------------------------------

class TestLastErrorSuffix:
    """The ``; last error: <code>`` suffix from log-scanning."""

    def test_recent_failure_appends_to_warn(self, tmp_path: Path) -> None:
        """Recent ``gcal.sync_create_failed`` within 24h → suffix on WARN."""
        token_path = _make_token(tmp_path, age_seconds=30 * 3600)
        # Log line ~2h ago — well within the 24h scan window.
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
        line = (
            f"{ts} [warning  ] gcal.sync_create_failed       "
            f"correlation_id=abc error=token_expired error_code=auth_failed"
        )
        _make_log(tmp_path, [line], name="talker.log")
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "; last error: auth_failed" in result.detail
        assert result.data["last_error"]["error_code"] == "auth_failed"
        assert result.data["last_error"]["event"] == "gcal.sync_create_failed"

    def test_recent_failure_appends_to_fail(self, tmp_path: Path) -> None:
        """FAIL status also gets the last-error suffix when recent."""
        token_path = _make_token(tmp_path, age_seconds=100 * 3600)
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        line = (
            f"{ts} [warning  ] gcal.sync_update_failed       "
            f"error_code='invalid_grant'"
        )
        _make_log(tmp_path, [line], name="talker.log")
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.FAIL
        assert "; last error: invalid_grant" in result.detail

    def test_ok_status_skips_error_suffix(self, tmp_path: Path) -> None:
        """OK detail stays clean even when an old error is in the log.

        The OK detail line is a strong-enough green signal that stale
        errors are noise. Suffix only fires on WARN/FAIL.
        """
        token_path = _make_token(tmp_path, age_seconds=0.5 * 3600)
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
        line = (
            f"{ts} [warning  ] gcal.sync_create_failed       "
            f"error_code=quota_exceeded"
        )
        _make_log(tmp_path, [line], name="talker.log")
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.OK
        assert "last error" not in result.detail

    def test_old_failure_outside_window_ignored(self, tmp_path: Path) -> None:
        """A failure ``>24h`` ago is not surfaced — could be already
        resolved transient."""
        token_path = _make_token(tmp_path, age_seconds=30 * 3600)
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
        line = (
            f"{ts} [warning  ] gcal.sync_create_failed       "
            f"error_code=stale_old_error"
        )
        _make_log(tmp_path, [line], name="talker.log")
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "last error" not in result.detail
        assert "last_error" not in result.data

    def test_no_log_files_warn_clean(self, tmp_path: Path) -> None:
        """Missing log files → no suffix, WARN/FAIL still rendered."""
        token_path = _make_token(tmp_path, age_seconds=40 * 3600)
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "nonexistent_logs")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert result.status == Status.WARN
        assert "last error" not in result.detail

    def test_error_code_truncated_at_cap(self, tmp_path: Path) -> None:
        """Very long error_code values are capped at 150 chars with
        ``...`` sentinel.

        The cap exists so a wall-of-text error message doesn't blow up
        the BIT detail line into multi-line garbage. Full structured
        last_error always survives in ``result.data["last_error"]``
        regardless of the cap.
        """
        token_path = _make_token(tmp_path, age_seconds=30 * 3600)
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        long_code = "x" * 300
        line = (
            f"{ts} [warning  ] gcal.sync_create_failed       "
            f"error_code='{long_code}'"
        )
        _make_log(tmp_path, [line], name="talker.log")
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        # Suffix is "; last error: <147 chars>...". Body of suffix is
        # the capped string.
        suffix_idx = result.detail.find("; last error: ")
        assert suffix_idx >= 0
        suffix_body = result.detail[suffix_idx + len("; last error: "):]
        assert suffix_body.endswith("...")
        # Cap is 150, including the trailing "..." sentinel.
        assert len(suffix_body) == 150
        # Data dict retains the full (untruncated) error_code.
        assert result.data["last_error"]["error_code"] == long_code

    def test_most_recent_failure_wins_across_files(self, tmp_path: Path) -> None:
        """When both ``talker.log`` and ``alfred.log`` have qualifying
        entries, the most-recent timestamp wins."""
        token_path = _make_token(tmp_path, age_seconds=30 * 3600)
        ts_old = _iso(datetime.now(timezone.utc) - timedelta(hours=5))
        ts_new = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        _make_log(
            tmp_path,
            [f"{ts_old} [warning  ] gcal.sync_create_failed       error_code=old_one"],
            name="talker.log",
        )
        _make_log(
            tmp_path,
            [f"{ts_new} [warning  ] gcal.sync_update_failed       error_code=new_one"],
            name="alfred.log",
        )
        raw = {
            "gcal": {"enabled": True, "token_path": str(token_path)},
            "logging": {"dir": str(tmp_path / "data")},
        }
        result = gh._check_last_successful_gcal_sync(raw)
        assert "; last error: new_one" in result.detail
        assert result.data["last_error"]["event"] == "gcal.sync_update_failed"


# ---------------------------------------------------------------------------
# Log-scanning unit coverage
# ---------------------------------------------------------------------------

class TestScanRecentFailures:
    """Direct exercise of ``_scan_recent_failures`` parser quirks."""

    def test_returns_none_when_no_log_files(self, tmp_path: Path) -> None:
        """No log files → None (not an exception)."""
        result = gh._scan_recent_failures(
            [tmp_path / "nope.log", tmp_path / "also_nope.log"]
        )
        assert result is None

    def test_returns_none_when_no_matching_lines(self, tmp_path: Path) -> None:
        """Log files exist but contain no gcal.sync_*_failed entries."""
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
        """ANSI-coloured lines are matched after the escape stripper."""
        ts = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        # Real-world structlog dev/console renderer output.
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
        """A very old qualifying entry in the head of the file is
        skipped when the tail window doesn't reach it.

        We pin this so a long-running install doesn't keep surfacing a
        stale error from weeks ago — the tail-bytes cap is both a
        latency optimisation and a recency filter.
        """
        ts_old = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        # First line: matching entry near the start of the file.
        head_line = (
            f"{ts_old} [warning  ] gcal.sync_create_failed       "
            f"error_code=head_entry"
        )
        # Padding: ~1KB of unrelated lines to push the head_line out
        # of a tiny tail window.
        padding = ["x" * 100 for _ in range(20)]
        log_path = _make_log(tmp_path, [head_line, *padding])
        # Tail window of 100 bytes — small enough to skip head_line.
        result = gh._scan_recent_failures([log_path], tail_bytes=100)
        assert result is None

    def test_quoted_and_bare_error_codes(self, tmp_path: Path) -> None:
        """Three error_code value forms parse: bare, single-quoted, double-quoted."""
        now = datetime.now(timezone.utc)
        lines = [
            f"{_iso(now - timedelta(hours=3))} [warning  ] "
            f"gcal.sync_create_failed       error_code=bare_form",
            f"{_iso(now - timedelta(hours=2))} [warning  ] "
            f"gcal.sync_update_failed       error_code='single_quoted'",
            f"{_iso(now - timedelta(hours=1))} [warning  ] "
            f"gcal.sync_delete_failed       error_code=\"double_quoted\"",
        ]
        log_path = _make_log(tmp_path, lines)
        # Most recent wins.
        result = gh._scan_recent_failures([log_path])
        assert result is not None
        assert result["error_code"] == "double_quoted"

    def test_unparseable_timestamp_skipped(self, tmp_path: Path) -> None:
        """A malformed-timestamp line doesn't crash the scan."""
        line = (
            "not-a-timestamp [warning  ] gcal.sync_create_failed       "
            "error_code=bad_ts"
        )
        log_path = _make_log(tmp_path, [line])
        result = gh._scan_recent_failures([log_path])
        # Regex won't match without a valid leading ISO timestamp → None.
        assert result is None


# ---------------------------------------------------------------------------
# Top-level health_check integration
# ---------------------------------------------------------------------------

def test_health_check_returns_tool_health_with_single_result(
    tmp_path: Path,
) -> None:
    """The ``health_check`` entry point returns a ToolHealth with the
    probe's single result mirrored into ``results``."""
    token_path = _make_token(tmp_path, age_seconds=2 * 3600)
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
    # Importing the module triggers register_check.
    import importlib
    import alfred.integrations.gcal_health as mod
    importlib.reload(mod)
    from alfred.health.aggregator import _REGISTRY
    assert "gcal" in _REGISTRY


# ---------------------------------------------------------------------------
# Tilde-expansion in token_path
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
    # Force recent mtime so the probe returns OK.
    target_mtime = datetime.now(timezone.utc).timestamp() - 60
    os.utime(token, (target_mtime, target_mtime))

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
