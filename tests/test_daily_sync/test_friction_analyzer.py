"""Tests for the K3 c1 friction analyzer + its scheduled-fire daemon.

Covers:
- AuditEntry parser tolerates malformed rows (no exception, just skip).
- detect_failed_patterns: 3 failures → event; 2 → no event; threshold
  configurable; command_not_found EXCLUDED from this category.
- detect_repeated_patterns: 5 successes of EXACT same command → event;
  near-equal commands stay separate.
- detect_missing_tools: any reason==command_not_found → event with
  tool name parsed; same tool deduped within a single fire.
- filter_window: entries outside last N hours dropped.
- run_friction_analysis: end-to-end happy + empty-log + idempotency.
- daemon fire_once: emits scheduled_fire_complete log even on
  empty-corpus days (intentionally-left-blank principle).
- Orchestrator wiring: auto-start gate + TOOL_RUNNERS + signature list
  + exit-78-on-disabled all source-pinned.

Log assertions use ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync.friction_analyzer import (
    AuditEntry,
    _command_prefix,
    _event_id,
    _local_day_bucket,
    append_events,
    detect_failed_patterns,
    detect_missing_tools,
    detect_repeated_patterns,
    filter_window,
    load_audit_entries,
    load_existing_event_ids,
    run_friction_analysis,
)


NOW = datetime(2026, 5, 5, 7, 30, 0, tzinfo=timezone.utc)


def _audit_row(
    *,
    ts: datetime,
    command: str,
    exit_code: int = 0,
    reason: str = "",
    cwd: str = "/home/andrew",
    duration_ms: int = 100,
    session_id: str = "sess-1",
) -> dict:
    """Build a bash_exec.jsonl-shaped row dict."""
    return {
        "ts": ts.isoformat(),
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "session_id": session_id,
        "reason": reason,
    }


def _seed_audit_log(path: Path, rows: list[dict]) -> None:
    """Write a JSONL audit log with the given rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _entry(
    *,
    ts: datetime,
    command: str,
    exit_code: int = 0,
    reason: str = "",
) -> AuditEntry:
    """Build an AuditEntry directly (skipping the row → dict round-trip)."""
    return AuditEntry(
        ts=ts,
        command=command,
        cwd="/home/andrew",
        exit_code=exit_code,
        duration_ms=100,
        session_id="sess-1",
        reason=reason,
    )


# ---------------------------------------------------------------------------
# AuditEntry parser
# ---------------------------------------------------------------------------


class TestAuditEntryParser:
    def test_parses_canonical_row(self):
        row = _audit_row(ts=NOW, command="uv sync", exit_code=1)
        entry = AuditEntry.from_dict(row)
        assert entry is not None
        assert entry.command == "uv sync"
        assert entry.exit_code == 1
        assert entry.ts.tzinfo is not None

    def test_skips_row_missing_ts(self):
        row = {"command": "uv sync", "exit_code": 0}
        assert AuditEntry.from_dict(row) is None

    def test_skips_row_missing_command(self):
        row = _audit_row(ts=NOW, command="")
        assert AuditEntry.from_dict(row) is None

    def test_z_suffix_iso_timestamp_tolerated(self):
        row = _audit_row(ts=NOW, command="x")
        row["ts"] = "2026-05-05T07:30:00Z"
        entry = AuditEntry.from_dict(row)
        assert entry is not None
        assert entry.ts.tzinfo is not None


class TestLoadAuditEntries:
    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        assert load_audit_entries(tmp_path / "nonexistent.jsonl") == []

    def test_skips_malformed_lines(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        path.write_text(
            json.dumps(_audit_row(ts=NOW, command="ok")) + "\n"
            "NOT JSON\n"
            "\n"
            + json.dumps(_audit_row(ts=NOW, command="also ok")) + "\n",
            encoding="utf-8",
        )
        entries = load_audit_entries(path)
        assert len(entries) == 2
        assert entries[0].command == "ok"
        assert entries[1].command == "also ok"


# ---------------------------------------------------------------------------
# Tokenization helper
# ---------------------------------------------------------------------------


class TestCommandPrefix:
    def test_two_token_prefix(self):
        assert _command_prefix("uv sync --no-cache") == "uv sync"

    def test_single_token_command(self):
        assert _command_prefix("pytest") == "pytest"

    def test_empty_command(self):
        assert _command_prefix("") == ""

    def test_compound_command_collapses_to_cd_x(self):
        """``cd X && Y`` → ``cd X`` under the 2-token rule. Coarser
        than ideal; documented behavior so the operator can spot the
        ``cd X`` prefix and read sample_command for the full text."""
        assert _command_prefix("cd /tmp && ls -la") == "cd /tmp"

    def test_unbalanced_quotes_falls_back_to_split(self):
        """shlex raises on unbalanced quotes; whitespace fallback
        keeps the analyzer running."""
        assert _command_prefix('echo "unbalanced') == "echo"


# ---------------------------------------------------------------------------
# detect_failed_patterns
# ---------------------------------------------------------------------------


class TestDetectFailedPatterns:
    def test_three_failures_emit_event(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=1), command="uv sync", exit_code=1),
            _entry(ts=NOW - timedelta(hours=2), command="uv sync --no-cache", exit_code=1),
            _entry(ts=NOW - timedelta(hours=3), command="uv sync", exit_code=1),
        ]
        events = detect_failed_patterns(
            entries, threshold=3, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "failed_pattern"
        assert ev["prefix"] == "uv sync"
        assert ev["count"] == 3
        # Newest entry's command surfaces as sample.
        assert "uv sync" in ev["sample_command"]
        assert ev["surfaced_at"] is None

    def test_two_failures_no_event(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=1), command="uv sync", exit_code=1),
            _entry(ts=NOW - timedelta(hours=2), command="uv sync", exit_code=1),
        ]
        events = detect_failed_patterns(
            entries, threshold=3, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert events == []

    def test_command_not_found_excluded_from_failed_pattern(self):
        """A failure with reason=command_not_found is routed to
        missing_tool detection only — not double-counted as a failed
        pattern."""
        entries = [
            _entry(
                ts=NOW - timedelta(hours=i),
                command="rg foo bar",
                exit_code=-1,
                reason="command_not_found",
            )
            for i in range(5)
        ]
        events = detect_failed_patterns(
            entries, threshold=3, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert events == []

    def test_threshold_configurable(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=i), command="x y", exit_code=1)
            for i in range(2)
        ]
        # threshold=2 → one event.
        events = detect_failed_patterns(
            entries, threshold=2, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert len(events) == 1


# ---------------------------------------------------------------------------
# detect_repeated_patterns
# ---------------------------------------------------------------------------


class TestDetectRepeatedPatterns:
    def test_five_identical_successes_emit_event(self):
        cmd = "pytest tests/test_foo.py -v"
        entries = [
            _entry(ts=NOW - timedelta(hours=i), command=cmd, exit_code=0)
            for i in range(5)
        ]
        events = detect_repeated_patterns(
            entries, threshold=5, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "repeated_pattern"
        assert ev["command"] == cmd
        assert ev["count"] == 5

    def test_four_successes_no_event(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=i), command="x", exit_code=0)
            for i in range(4)
        ]
        events = detect_repeated_patterns(
            entries, threshold=5, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert events == []

    def test_near_equal_commands_stay_separate(self):
        """``pytest -v`` and ``pytest --verbose`` are NOT collapsed —
        exact-equality grouping by design."""
        entries = [
            _entry(ts=NOW - timedelta(hours=i), command="pytest -v", exit_code=0)
            for i in range(3)
        ] + [
            _entry(ts=NOW - timedelta(hours=i), command="pytest --verbose", exit_code=0)
            for i in range(3)
        ]
        events = detect_repeated_patterns(
            entries, threshold=5, day_bucket="2026-05-05", now_utc=NOW,
        )
        # Neither hits 5 individually → no events even though combined
        # they would.
        assert events == []

    def test_failures_excluded_from_repeated_pattern(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=i), command="x", exit_code=1)
            for i in range(7)
        ]
        events = detect_repeated_patterns(
            entries, threshold=5, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert events == []


# ---------------------------------------------------------------------------
# detect_missing_tools
# ---------------------------------------------------------------------------


class TestDetectMissingTools:
    def test_command_not_found_emits_event(self):
        entries = [
            _entry(
                ts=NOW - timedelta(hours=1),
                command="rg foo bar",
                exit_code=-1,
                reason="command_not_found",
            ),
        ]
        events = detect_missing_tools(
            entries, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "missing_tool"
        assert ev["tool"] == "rg"
        assert ev["failed_command"] == "rg foo bar"

    def test_same_tool_deduplicated_per_fire(self):
        """Three command_not_found rows for the same tool → one event,
        not three. (Re-detection across fires still uses event_id
        idempotency.)"""
        entries = [
            _entry(
                ts=NOW - timedelta(hours=i),
                command="rg foo",
                exit_code=-1,
                reason="command_not_found",
            )
            for i in range(3)
        ]
        events = detect_missing_tools(
            entries, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert len(events) == 1

    def test_distinct_tools_each_emit_event(self):
        entries = [
            _entry(
                ts=NOW,
                command="rg foo",
                exit_code=-1,
                reason="command_not_found",
            ),
            _entry(
                ts=NOW,
                command="fd bar",
                exit_code=-1,
                reason="command_not_found",
            ),
        ]
        events = detect_missing_tools(
            entries, day_bucket="2026-05-05", now_utc=NOW,
        )
        tools = {e["tool"] for e in events}
        assert tools == {"rg", "fd"}

    def test_other_failure_reasons_ignored(self):
        """A normal failure (exit_code=1) with no reason set is NOT
        a missing tool event."""
        entries = [
            _entry(
                ts=NOW, command="pytest", exit_code=1, reason="",
            ),
        ]
        events = detect_missing_tools(
            entries, day_bucket="2026-05-05", now_utc=NOW,
        )
        assert events == []


# ---------------------------------------------------------------------------
# Window filter
# ---------------------------------------------------------------------------


class TestFilterWindow:
    def test_window_drops_old_entries(self):
        entries = [
            _entry(ts=NOW - timedelta(hours=12), command="recent"),
            _entry(ts=NOW - timedelta(hours=25), command="old"),
            _entry(ts=NOW - timedelta(hours=23, minutes=59), command="just inside"),
        ]
        kept = filter_window(entries, window_hours=24, now_utc=NOW)
        commands = {e.command for e in kept}
        assert "recent" in commands
        assert "just inside" in commands
        assert "old" not in commands

    def test_empty_entries_returns_empty(self):
        assert filter_window([], window_hours=24, now_utc=NOW) == []


# ---------------------------------------------------------------------------
# Day bucket + event_id
# ---------------------------------------------------------------------------


class TestDayBucket:
    def test_local_tz_bucket_late_evening_does_not_roll_over(self):
        """A detection at 22:00 ADT should bucket as ADT-local 'today',
        not UTC 'tomorrow'."""
        # 2026-05-05 22:00 ADT = 2026-05-06 01:00 UTC.
        utc_after_local_midnight = datetime(
            2026, 5, 6, 1, 0, 0, tzinfo=timezone.utc,
        )
        bucket = _local_day_bucket(
            utc_after_local_midnight, "America/Halifax",
        )
        assert bucket == "2026-05-05"


class TestEventId:
    def test_deterministic_for_same_inputs(self):
        a = _event_id(kind="failed_pattern", key="uv sync", day_bucket="2026-05-05")
        b = _event_id(kind="failed_pattern", key="uv sync", day_bucket="2026-05-05")
        assert a == b

    def test_changes_with_day_bucket(self):
        a = _event_id(kind="failed_pattern", key="uv sync", day_bucket="2026-05-05")
        b = _event_id(kind="failed_pattern", key="uv sync", day_bucket="2026-05-06")
        assert a != b


# ---------------------------------------------------------------------------
# run_friction_analysis — end-to-end
# ---------------------------------------------------------------------------


class TestRunFrictionAnalysis:
    def test_end_to_end_writes_events(self, tmp_path: Path):
        audit = tmp_path / "bash_exec.jsonl"
        log_path = tmp_path / "friction.jsonl"
        rows = [
            _audit_row(
                ts=NOW - timedelta(hours=i),
                command="uv sync",
                exit_code=1,
            )
            for i in range(3)
        ]
        _seed_audit_log(audit, rows)

        result = run_friction_analysis(
            audit, log_path,
            failed_pattern_threshold=3,
            repeated_pattern_threshold=5,
            window_hours=24,
            schedule_timezone="America/Halifax",
            now_utc=NOW,
        )
        assert len(result.events) == 1
        assert log_path.is_file()
        loaded = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(loaded) == 1
        assert loaded[0]["kind"] == "failed_pattern"

    def test_idempotent_per_day(self, tmp_path: Path):
        """Re-running on the same data produces zero new events
        (existing event_ids dedup the second pass)."""
        audit = tmp_path / "bash_exec.jsonl"
        log_path = tmp_path / "friction.jsonl"
        rows = [
            _audit_row(
                ts=NOW - timedelta(hours=i),
                command="uv sync",
                exit_code=1,
            )
            for i in range(3)
        ]
        _seed_audit_log(audit, rows)

        first = run_friction_analysis(
            audit, log_path, failed_pattern_threshold=3, now_utc=NOW,
        )
        assert len(first.events) == 1

        second = run_friction_analysis(
            audit, log_path, failed_pattern_threshold=3, now_utc=NOW,
        )
        assert len(second.events) == 0
        assert second.skipped == 1
        # Log file still has only one row.
        loaded = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(loaded) == 1

    def test_empty_audit_log_no_events_no_error(self, tmp_path: Path):
        audit = tmp_path / "bash_exec.jsonl"  # never created
        log_path = tmp_path / "friction.jsonl"
        result = run_friction_analysis(
            audit, log_path, now_utc=NOW,
        )
        assert result.events == []
        assert result.audit_entries_scanned == 0
        # No log file written when there's nothing to write.
        assert not log_path.exists()

    def test_window_horizon_drops_old_entries(self, tmp_path: Path):
        audit = tmp_path / "bash_exec.jsonl"
        log_path = tmp_path / "friction.jsonl"
        # 5 failures, but all 25h old.
        rows = [
            _audit_row(
                ts=NOW - timedelta(hours=25 + i),
                command="uv sync",
                exit_code=1,
            )
            for i in range(5)
        ]
        _seed_audit_log(audit, rows)

        result = run_friction_analysis(
            audit, log_path,
            failed_pattern_threshold=3,
            window_hours=24,
            now_utc=NOW,
        )
        assert result.events == []
        assert result.audit_entries_scanned == 5
        assert result.audit_entries_in_window == 0


# ---------------------------------------------------------------------------
# Daemon fire_once — log event contract
# ---------------------------------------------------------------------------


class TestDaemonFireOnce:
    def test_fire_once_emits_log_with_events(self, tmp_path: Path):
        from alfred.daily_sync.config import load_from_unified
        from alfred.daily_sync.friction_analyzer_daemon import fire_once

        audit = tmp_path / "bash_exec.jsonl"
        log_path = tmp_path / "friction.jsonl"
        rows = [
            _audit_row(
                ts=datetime.now(timezone.utc) - timedelta(hours=i),
                command="uv sync",
                exit_code=1,
            )
            for i in range(3)
        ]
        _seed_audit_log(audit, rows)

        raw = {
            "daily_sync": {
                "enabled": True,
                "friction_analyzer": {
                    "enabled": True,
                    "audit_log_path": str(audit),
                    "log_path": str(log_path),
                    "thresholds": {
                        "failed_pattern_count": 3,
                        "repeated_pattern_count": 5,
                        "window_hours": 24,
                    },
                },
            },
        }
        config = load_from_unified(raw)

        with structlog.testing.capture_logs() as captured:
            result = asyncio.run(fire_once(config, raw_config=raw))

        assert result["ok"] is True
        assert result["events_count"] == 1
        fire_events = [
            e for e in captured
            if e["event"] == "friction_analyzer.scheduled_fire_complete"
        ]
        assert len(fire_events) == 1
        assert fire_events[0]["events_count"] == 1
        assert fire_events[0]["by_kind"] == {"failed_pattern": 1}

    def test_fire_once_emits_log_on_empty_corpus(self, tmp_path: Path):
        """Per intentionally-left-blank: empty audit → log event still
        fires so the daemon's silence is observable."""
        from alfred.daily_sync.config import load_from_unified
        from alfred.daily_sync.friction_analyzer_daemon import fire_once

        log_path = tmp_path / "friction.jsonl"
        raw = {
            "daily_sync": {
                "enabled": True,
                "friction_analyzer": {
                    "enabled": True,
                    "audit_log_path": str(tmp_path / "missing.jsonl"),
                    "log_path": str(log_path),
                },
            },
        }
        config = load_from_unified(raw)

        with structlog.testing.capture_logs() as captured:
            result = asyncio.run(fire_once(config, raw_config=raw))

        assert result["events_count"] == 0
        fire_events = [
            e for e in captured
            if e["event"] == "friction_analyzer.scheduled_fire_complete"
        ]
        assert len(fire_events) == 1
        assert fire_events[0]["events_count"] == 0
        assert fire_events[0]["audit_entries_scanned"] == 0


# ---------------------------------------------------------------------------
# audit_log_path resolution — fallback to telegram.bash_exec.audit_path
# ---------------------------------------------------------------------------


class TestAuditLogPathResolution:
    def test_explicit_audit_log_path_wins(self, tmp_path: Path):
        from alfred.daily_sync.config import (
            FrictionAnalyzerConfig, FrictionThresholdsConfig,
        )
        from alfred.daily_sync.friction_analyzer_daemon import (
            _resolve_audit_log_path,
        )
        explicit = tmp_path / "explicit.jsonl"
        fa = FrictionAnalyzerConfig(
            enabled=True,
            audit_log_path=str(explicit),
            thresholds=FrictionThresholdsConfig(),
        )
        raw = {
            "telegram": {"bash_exec": {"audit_path": str(tmp_path / "telegram.jsonl")}},
        }
        resolved = _resolve_audit_log_path(fa, raw)
        assert resolved == explicit.resolve()

    def test_falls_back_to_telegram_bash_exec_audit_path(self, tmp_path: Path):
        from alfred.daily_sync.config import FrictionAnalyzerConfig
        from alfred.daily_sync.friction_analyzer_daemon import (
            _resolve_audit_log_path,
        )
        telegram_path = tmp_path / "telegram_audit.jsonl"
        fa = FrictionAnalyzerConfig(enabled=True)  # audit_log_path=""
        raw = {
            "telegram": {"bash_exec": {"audit_path": str(telegram_path)}},
        }
        resolved = _resolve_audit_log_path(fa, raw)
        assert resolved == telegram_path.resolve()

    def test_default_when_neither_configured(self):
        from alfred.daily_sync.config import FrictionAnalyzerConfig
        from alfred.daily_sync.friction_analyzer_daemon import (
            _resolve_audit_log_path,
        )
        fa = FrictionAnalyzerConfig(enabled=True)  # audit_log_path=""
        resolved = _resolve_audit_log_path(fa, raw_config=None)
        assert resolved.name == "bash_exec.jsonl"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_block_absent_defaults_disabled(self):
        from alfred.daily_sync.config import load_from_unified
        cfg = load_from_unified({"daily_sync": {"enabled": True}})
        assert cfg.friction_analyzer.enabled is False
        # Default schedule 07:30 ADT.
        assert cfg.friction_analyzer.schedule.time == "07:30"
        assert cfg.friction_analyzer.schedule.timezone == "America/Halifax"
        # Default thresholds match spec.
        assert cfg.friction_analyzer.thresholds.failed_pattern_count == 3
        assert cfg.friction_analyzer.thresholds.repeated_pattern_count == 5
        assert cfg.friction_analyzer.thresholds.window_hours == 24

    def test_partial_block_merges_over_defaults(self):
        from alfred.daily_sync.config import load_from_unified
        raw = {
            "daily_sync": {
                "enabled": True,
                "friction_analyzer": {"enabled": True},
            },
        }
        cfg = load_from_unified(raw)
        assert cfg.friction_analyzer.enabled is True
        assert cfg.friction_analyzer.schedule.time == "07:30"

    def test_full_block_overrides(self):
        from alfred.daily_sync.config import load_from_unified
        raw = {
            "daily_sync": {
                "enabled": True,
                "friction_analyzer": {
                    "enabled": True,
                    "schedule": {"time": "06:00", "timezone": "America/Toronto"},
                    "log_path": "/tmp/friction.jsonl",
                    "thresholds": {
                        "failed_pattern_count": 5,
                        "repeated_pattern_count": 10,
                        "window_hours": 12,
                    },
                },
            },
        }
        cfg = load_from_unified(raw)
        assert cfg.friction_analyzer.schedule.time == "06:00"
        assert cfg.friction_analyzer.thresholds.failed_pattern_count == 5
        assert cfg.friction_analyzer.thresholds.window_hours == 12


# ---------------------------------------------------------------------------
# Orchestrator wiring — source-pin
# ---------------------------------------------------------------------------


class TestOrchestratorWiring:
    def test_orchestrator_source_pins(self):
        """Source-pin: orchestrator must (a) read the nested
        daily_sync.friction_analyzer.enabled gate, (b) register
        _run_friction_analyzer in TOOL_RUNNERS, (c) include
        friction_analyzer in the no-skills-dir signature list."""
        here = Path(__file__).resolve().parents[1]
        orch_path = here.parent / "src" / "alfred" / "orchestrator.py"
        src = orch_path.read_text(encoding="utf-8")

        assert 'raw.get("daily_sync") or {}).get("friction_analyzer")' in src
        assert '"friction_analyzer": _run_friction_analyzer' in src
        sig_line = next(
            line for line in src.splitlines()
            if 'tool in ("surveyor"' in line
        )
        assert '"friction_analyzer"' in sig_line

    def test_run_friction_analyzer_exits_78_when_disabled(
        self, tmp_path: Path,
    ):
        from alfred.orchestrator import _run_friction_analyzer
        raw = {
            "logging": {"dir": str(tmp_path)},
            "daily_sync": {"enabled": True},  # no friction_analyzer block
        }
        with pytest.raises(SystemExit) as exc_info:
            _run_friction_analyzer(raw, suppress_stdout=True)
        assert exc_info.value.code == 78

    def test_run_friction_analyzer_exits_78_when_explicitly_disabled(
        self, tmp_path: Path,
    ):
        from alfred.orchestrator import _run_friction_analyzer
        raw = {
            "logging": {"dir": str(tmp_path)},
            "daily_sync": {
                "enabled": True,
                "friction_analyzer": {"enabled": False},
            },
        }
        with pytest.raises(SystemExit) as exc_info:
            _run_friction_analyzer(raw, suppress_stdout=True)
        assert exc_info.value.code == 78
