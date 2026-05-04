"""Tests for the daily-radar auto-fire daemon (Phase 3a-on-a-scheduler).

Covers:
- ``RadarDayConfig`` loads cleanly from the unified-config dict, with
  defaults for an absent block (enabled=False, 08:00 ADT default).
- Partial dict (just ``enabled: true``) merges over the dataclass
  default so the schedule doesn't have to be re-stated.
- Orchestrator auto-start gate appends ``radar_day`` to the tools list
  ONLY when ``distiller.radar_day.enabled`` is true.
- Orchestrator omits ``radar_day`` when the block is missing OR when
  ``enabled: false``.
- ``_run_radar_day`` exit-78s when block is disabled (matches every
  other optional daemon).
- ``fire_once`` emits the load-bearing
  ``radar_day.scheduled_fire_complete`` log event with item count +
  path so a no-radar-items day is observably distinct from a daemon
  that never ran (per ``feedback_intentionally_left_blank.md``).
- Empty-corpus case still emits the log event.

Log assertions use ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md`` — async daemon code paths.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import structlog


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_radar_day_config_block_absent_defaults_to_disabled():
    """No ``distiller.radar_day:`` block → enabled=False, default
    schedule (08:00 ADT)."""
    from alfred.distiller.config import load_from_unified
    cfg = load_from_unified(
        {"distiller": {"vault": {"path": "/tmp/v"}}, "vault": {"path": "/tmp/v"}},
    )
    assert cfg.radar_day.enabled is False
    assert cfg.radar_day.schedule.time == "08:00"
    assert cfg.radar_day.schedule.timezone == "America/Halifax"
    assert cfg.radar_day.top_n == 5
    assert cfg.radar_day.min_score is None


def test_radar_day_partial_block_merges_over_defaults():
    """Just ``enabled: true`` in YAML → schedule stays at 08:00 ADT."""
    from alfred.distiller.config import load_from_unified
    raw = {
        "vault": {"path": "/tmp/v"},
        "distiller": {
            "vault": {"path": "/tmp/v"},
            "radar_day": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.radar_day.enabled is True
    # Schedule defaults preserved.
    assert cfg.radar_day.schedule.time == "08:00"
    assert cfg.radar_day.schedule.timezone == "America/Halifax"


def test_radar_day_full_block_overrides_defaults():
    """Operator can override schedule + top_n + min_score."""
    from alfred.distiller.config import load_from_unified
    raw = {
        "vault": {"path": "/tmp/v"},
        "distiller": {
            "vault": {"path": "/tmp/v"},
            "radar_day": {
                "enabled": True,
                "schedule": {"time": "07:30", "timezone": "America/Toronto"},
                "top_n": 8,
                "min_score": 6.0,
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.radar_day.enabled is True
    assert cfg.radar_day.schedule.time == "07:30"
    assert cfg.radar_day.schedule.timezone == "America/Toronto"
    assert cfg.radar_day.top_n == 8
    assert cfg.radar_day.min_score == 6.0


def test_radar_day_explicit_disabled_false():
    """``enabled: false`` is honored — block present but daemon stays off."""
    from alfred.distiller.config import load_from_unified
    raw = {
        "vault": {"path": "/tmp/v"},
        "distiller": {
            "vault": {"path": "/tmp/v"},
            "radar_day": {"enabled": False},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.radar_day.enabled is False


# ---------------------------------------------------------------------------
# _resolve_dirs — fallback derivation
# ---------------------------------------------------------------------------


def test_resolve_dirs_default_uses_vault_digests_and_state_parent(tmp_path: Path):
    """No explicit overrides → digests=<vault>/digests, state=<state.path
    parent>. Mirrors the Phase 3a CLI's fallback derivation."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.radar_day_daemon import _resolve_dirs
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "distiller": {
            "vault": {"path": str(tmp_path / "vault")},
            "state": {"path": str(state_dir / "distiller_state.json")},
            "radar_day": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    digests, state = _resolve_dirs(cfg)
    assert digests == (tmp_path / "vault" / "digests").resolve()
    assert state == state_dir.resolve()


def test_resolve_dirs_explicit_overrides_win(tmp_path: Path):
    """``radar_day.digests_dir`` and ``state_dir`` short-circuit the
    fallback derivation."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.radar_day_daemon import _resolve_dirs
    explicit_digests = tmp_path / "explicit_digests"
    explicit_state = tmp_path / "explicit_state"
    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "distiller": {
            "vault": {"path": str(tmp_path / "vault")},
            "state": {"path": str(tmp_path / "ignored.json")},
            "radar_day": {
                "enabled": True,
                "digests_dir": str(explicit_digests),
                "state_dir": str(explicit_state),
            },
        },
    }
    cfg = load_from_unified(raw)
    digests, state = _resolve_dirs(cfg)
    assert digests == explicit_digests.resolve()
    assert state == explicit_state.resolve()


# ---------------------------------------------------------------------------
# fire_once — log-event contract
# ---------------------------------------------------------------------------


def _seed_synthesis_record(vault: Path, name: str, claim: str) -> Path:
    """Write a minimal synthesis record dated today so it ranks."""
    today_iso = date.today().isoformat()
    rec = (
        "---\n"
        f"name: {name}\n"
        "type: synthesis\n"
        f"claim: {claim}\n"
        f"created: '{today_iso}'\n"
        "source_links:\n"
        "  - '[[session/X]]'\n"
        "  - '[[session/Y]]'\n"
        "entity_links:\n"
        "  - '[[person/Andrew]]'\n"
        "  - '[[project/Alfred]]'\n"
        "---\n\nbody\n"
    )
    path = vault / "synthesis" / f"{name}.md"
    path.write_text(rec, encoding="utf-8")
    return path


def test_fire_once_emits_scheduled_fire_complete_log_with_items(tmp_path: Path):
    """Happy path: ranker finds an item, daily file written, log event
    carries items_count > 0 + output_path."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.radar_day_daemon import fire_once

    vault = tmp_path / "vault"
    for d in ("synthesis", "decision", "contradiction"):
        (vault / d).mkdir(parents=True)
    _seed_synthesis_record(vault, "Test Item", "Andrew prefers explicit.")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    raw = {
        "vault": {"path": str(vault)},
        "distiller": {
            "vault": {"path": str(vault)},
            "state": {"path": str(state_dir / "distiller_state.json")},
            "radar_day": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)

    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(fire_once(cfg))

    assert result["ok"] is True
    assert result["items_count"] == 1
    assert "digests/daily/" in result["output_path"]

    fire_events = [
        e for e in captured if e["event"] == "radar_day.scheduled_fire_complete"
    ]
    assert len(fire_events) == 1
    assert fire_events[0]["items_count"] == 1
    assert fire_events[0]["ranker_count"] == 1
    assert fire_events[0]["deduped"] == 0
    assert "digests/daily/" in fire_events[0]["output_path"]


def test_fire_once_emits_log_on_empty_day(tmp_path: Path):
    """Per intentionally-left-blank: zero ranked items still emits the
    log event so the daemon's silence is observable."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.radar_day_daemon import fire_once

    vault = tmp_path / "vault"
    for d in ("synthesis", "decision", "contradiction"):
        (vault / d).mkdir(parents=True)
    # No records seeded.

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    raw = {
        "vault": {"path": str(vault)},
        "distiller": {
            "vault": {"path": str(vault)},
            "state": {"path": str(state_dir / "distiller_state.json")},
            "radar_day": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)

    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(fire_once(cfg))

    assert result["items_count"] == 0
    fire_events = [
        e for e in captured if e["event"] == "radar_day.scheduled_fire_complete"
    ]
    assert len(fire_events) == 1
    # items_count=0 logged explicitly.
    assert fire_events[0]["items_count"] == 0
    assert fire_events[0]["ranker_count"] == 0
    # Daily file still written (with the "no radar items today" body).
    daily_file = Path(fire_events[0]["output_path"])
    assert daily_file.is_file()
    assert "no radar items today" in daily_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestrator auto-start gate
# ---------------------------------------------------------------------------


def test_orchestrator_includes_radar_day_when_enabled():
    """Source-pin: the orchestrator's auto-start gate must check
    ``distiller.radar_day.enabled`` and append 'radar_day' to tools."""
    here = Path(__file__).resolve().parent
    orch_path = here.parent / "src" / "alfred" / "orchestrator.py"
    src = orch_path.read_text(encoding="utf-8")

    # Auto-start gate must reference the nested distiller.radar_day path.
    assert 'raw.get("distiller") or {}).get("radar_day")' in src, (
        "orchestrator's auto-start gate must read "
        "(raw.get('distiller') or {}).get('radar_day') so KAL-LE's "
        "config.kalle.yaml block is picked up. Without this, the "
        "daemon never starts even with enabled: true in config."
    )

    # Must be in TOOL_RUNNERS.
    assert '"radar_day": _run_radar_day' in src, (
        "orchestrator's TOOL_RUNNERS dict must register "
        "_run_radar_day under the 'radar_day' key."
    )

    # Must be in the no-skills-dir signature list (radar_day takes
    # only (raw, suppress_stdout)).
    sig_line = next(
        line for line in src.splitlines()
        if 'tool in ("surveyor"' in line and "no skills" not in line
    )
    assert '"radar_day"' in sig_line, (
        "radar_day must be in the no-skills-dir signature list "
        "(line starting with 'if tool in (\"surveyor\", ...)'). "
        "Otherwise the orchestrator passes 3 args to a 2-arg runner "
        "and start_process crashes with TypeError."
    )


def test_run_radar_day_exits_78_when_disabled(tmp_path: Path, monkeypatch):
    """``_run_radar_day`` invoked against a disabled config exits 78
    so auto-restart skips. Mirrors every other optional daemon."""
    from alfred.orchestrator import _run_radar_day

    # Block missing entirely → exit 78. Use a vault path that exists
    # and a state path under tmp_path so config loading doesn't choke.
    raw = {
        "vault": {"path": str(tmp_path)},
        "logging": {"dir": str(tmp_path)},
        "distiller": {
            "vault": {"path": str(tmp_path)},
            "state": {"path": str(tmp_path / "ds.json")},
            # No radar_day block.
        },
    }
    with pytest.raises(SystemExit) as exc_info:
        _run_radar_day(raw, suppress_stdout=True)
    assert exc_info.value.code == 78


def test_run_radar_day_exits_78_when_explicitly_disabled(
    tmp_path: Path,
):
    """``radar_day.enabled: false`` also exits 78."""
    from alfred.orchestrator import _run_radar_day

    raw = {
        "vault": {"path": str(tmp_path)},
        "logging": {"dir": str(tmp_path)},
        "distiller": {
            "vault": {"path": str(tmp_path)},
            "state": {"path": str(tmp_path / "ds.json")},
            "radar_day": {"enabled": False},
        },
    }
    with pytest.raises(SystemExit) as exc_info:
        _run_radar_day(raw, suppress_stdout=True)
    assert exc_info.value.code == 78
