"""Tests for the BIT curator probes — specifically the
``last-successful-process`` daemon-liveness probe added 2026-05-10
as part of the cross-daemon BIT probe arc.

Mirrors ``test_brief_probes.py`` (yesterday's commit `5e28885`'s
shape) but accounts for curator's inotify-driven cadence: a quiet
inbox is the legitimate idle state, so the probe combines
``last_run`` age with the inbox-non-empty signal to distinguish
"daemon dead" from "no work to do."

Per ``feedback_intentionally_left_blank.md`` — silence (curator
idle, inbox piling up, no log signal) is ambiguous between healthy-
quiet and broken; the probe disambiguates.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.curator.health import (
    _CURATOR_STALE_FAIL_HOURS,
    _CURATOR_STALE_WARN_HOURS,
    _check_last_successful_process,
    _inbox_has_pending_files,
    _read_curator_last_run,
    _resolve_curator_state_path,
)
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(state_path: Path, last_run: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 2, "last_run": last_run, "processed": {}}, indent=2),
        encoding="utf-8",
    )


def _hours_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _raw(
    tmp_path: Path,
    *,
    state_path: Path | None = None,
    inbox_dir: str = "inbox",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "curator": {"inbox_dir": inbox_dir},
    }
    if state_path is not None:
        raw["curator"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_curator_state_path
# ---------------------------------------------------------------------------


class TestResolveCuratorStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "curator_state.json"
        raw = _raw(tmp_path, state_path=explicit)
        assert _resolve_curator_state_path(raw) == explicit

    def test_fallback_to_default(self, tmp_path: Path) -> None:
        raw: dict[str, Any] = {"curator": {}}
        # Mirrors the dataclass default; pin the literal so a config-
        # default rename surfaces as a test failure.
        assert str(_resolve_curator_state_path(raw)) == "data/curator_state.json"

    def test_no_curator_section_falls_back_to_default(self) -> None:
        # Defensive: curator probe must not crash on configs that
        # entirely omit the curator section.
        raw: dict[str, Any] = {}
        assert str(_resolve_curator_state_path(raw)) == "data/curator_state.json"


# ---------------------------------------------------------------------------
# _read_curator_last_run
# ---------------------------------------------------------------------------


class TestReadCuratorLastRun:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_curator_last_run(tmp_path / "missing.json") is None

    def test_empty_last_run_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="")
        assert _read_curator_last_run(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid {{{", encoding="utf-8")
        assert _read_curator_last_run(state_path) is None

    def test_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        ts = "2026-05-10T14:00:00+00:00"
        _write_state(state_path, last_run=ts)
        assert _read_curator_last_run(state_path) == ts

    def test_last_run_not_string_returns_none(self, tmp_path: Path) -> None:
        # Defensive against a future schema migration.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({"version": 2, "last_run": 12345}),
            encoding="utf-8",
        )
        assert _read_curator_last_run(state_path) is None


# ---------------------------------------------------------------------------
# _inbox_has_pending_files — the inotify-aware heuristic
# ---------------------------------------------------------------------------


class TestInboxHasPendingFiles:
    def test_missing_inbox_returns_false(self, tmp_path: Path) -> None:
        # No inbox dir — treat as "no work" so the probe doesn't
        # cascade FAIL on the wrong probe (inbox-dir surfaces this).
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is False

    def test_empty_inbox_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "inbox").mkdir()
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is False

    def test_inbox_with_only_gitkeep_returns_false(self, tmp_path: Path) -> None:
        # ``.gitkeep`` is the standard "keep the dir present" file;
        # don't count it as pending work.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / ".gitkeep").write_text("")
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is False

    def test_inbox_with_real_file_returns_true(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "raw.md").write_text("---\n---\n")
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is True

    def test_inbox_with_processed_subdir_only_returns_false(
        self, tmp_path: Path,
    ) -> None:
        # ``processed/`` is curator's audit trail; don't count its
        # contents as pending work (the source of false-positive
        # FAILs if we did).
        inbox = tmp_path / "inbox"
        (inbox / "processed").mkdir(parents=True)
        (inbox / "processed" / "old-record.md").write_text("---\n---\n")
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is False

    def test_inbox_with_subdir_files_returns_true(self, tmp_path: Path) -> None:
        # Mail accounts land in ``inbox/<mailbox>/`` (per the mail
        # daemon's writer). The probe must surface those as pending.
        inbox = tmp_path / "inbox"
        (inbox / "mail-live").mkdir(parents=True)
        (inbox / "mail-live" / "msg.md").write_text("---\n---\n")
        raw = _raw(tmp_path)
        assert _inbox_has_pending_files(raw) is True

    def test_no_vault_path_returns_false(self) -> None:
        # Defensive against missing vault.path config.
        assert _inbox_has_pending_files({}) is False

    def test_custom_inbox_dir_respected(self, tmp_path: Path) -> None:
        # Operator-configured ``inbox_dir`` (per-instance variation).
        custom = tmp_path / "my-inbox"
        custom.mkdir()
        (custom / "x.md").write_text("---\n---\n")
        raw = _raw(tmp_path, inbox_dir="my-inbox")
        assert _inbox_has_pending_files(raw) is True


# ---------------------------------------------------------------------------
# _check_last_successful_process — full probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulProcessProbe:
    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        state_path = tmp_path / "missing.json"
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_empty_last_run_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.SKIP
        assert "no last_run" in result.detail

    def test_unparseable_last_run_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="not-a-timestamp")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.SKIP

    def test_quiet_inbox_recent_run_ok(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(2))
        raw = _raw(tmp_path, state_path=state_path)
        # No inbox files.
        result = _check_last_successful_process(raw)
        assert result.status == Status.OK
        assert "inbox empty" in result.detail

    def test_quiet_inbox_old_run_still_ok(self, tmp_path: Path) -> None:
        # The inotify-driven cadence: quiet inbox legitimately means
        # no work, REGARDLESS of last_run age. A 10-day-old last_run
        # with empty inbox is healthy idle, not a silent failure.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(240))  # 10 days
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.OK

    def test_pending_inbox_recent_run_ok(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(2))
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "x.md").write_text("---\n---\n")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.OK
        assert "pending files" in result.detail

    def test_pending_inbox_stale_run_warn(self, tmp_path: Path) -> None:
        # Inbox non-empty + 30h old last_run — single-ingest hiccup
        # window. WARN, not FAIL.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(30))
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "x.md").write_text("---\n---\n")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.WARN
        assert "stale" in result.detail

    def test_pending_inbox_very_old_run_fail(self, tmp_path: Path) -> None:
        # The bug-of-record shape: inbox piling up, daemon silently
        # not making progress. >48h with pending = FAIL.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(72))  # 3 days
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "x.md").write_text("---\n---\n")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_process(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail

    def test_threshold_constants_match_dispatch_calibration(self) -> None:
        # Pin the thresholds Andrew specified in the dispatch — so a
        # future tuning surfaces in this test rather than a silent
        # change.
        assert _CURATOR_STALE_WARN_HOURS == 24
        assert _CURATOR_STALE_FAIL_HOURS == 48


# ---------------------------------------------------------------------------
# health_check integration — wired into rollup
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_process_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        from alfred.curator.health import health_check

        state_path = tmp_path / "curator_state.json"
        _write_state(state_path, last_run=_hours_ago_iso(2))

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "agent": {"backend": "openclaw"},  # skip anthropic-auth
            "curator": {
                "inbox_dir": "inbox",
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-process" in names
        last = next(r for r in rollup.results if r.name == "last-successful-process")
        # Quiet inbox + recent last_run → OK.
        assert last.status == Status.OK

    async def test_skips_when_curator_section_absent(
        self, tmp_path: Path,
    ) -> None:
        """KAL-LE peer-digest regression-pin (2026-05-16).

        Instances that don't run curator (e.g. KAL-LE — surveyor watches
        its inbox; no curator daemon) must surface a tool-level SKIP,
        not a stale ``last-successful-process`` FAIL. Mirrors the
        gating already in place on surveyor / brief / mail / etc.

        Bug-of-record shape: KAL-LE's BIT showed
        ``[ FAIL ] curator.last-successful-process`` for 5 days because
        the probe consulted an absent state file via the dataclass
        default path and treated the missing-file as "fresh install"
        SKIP at the probe level — but ALSO ran ``_check_vault`` and
        ``_check_backend`` which produced OK results. The rollup's
        worst-of stayed OK on KAL-LE, but Salem's peer-digest pulled
        KAL-LE's BIT record and the per-probe SKIP detail rendered as
        a 'red — 1 fail' surface in the morning brief. Tool-level SKIP
        ahead of any probe is the canonical fix.
        """
        from alfred.curator.health import health_check

        rollup = await health_check({}, mode="quick")
        assert rollup.status == Status.SKIP
        assert rollup.results == []
        assert "no curator section" in (rollup.detail or "")

    async def test_skips_when_only_other_sections_present(
        self, tmp_path: Path,
    ) -> None:
        """Defensive: raw has vault + surveyor but no curator → SKIP.

        Specifically pins the KAL-LE config shape (surveyor configured,
        curator absent) so a future refactor that conflates
        ``vault`` / ``curator`` keys surfaces here rather than silently
        on KAL-LE's morning brief.
        """
        from alfred.curator.health import health_check

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "surveyor": {"watcher": {"debounce_seconds": 30}},
        }
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.SKIP
        assert rollup.results == []
