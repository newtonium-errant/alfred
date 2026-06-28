"""Self-correcting matcher — Phase 1 Daily Sync surface pins.

Covers the read-only ``routine_match`` section: renders pending low-confidence
matches when enabled, the intentionally-left-blank sentinel when enabled-empty,
omits (None) when disabled; plus the config drift-guard (the section's pending
path default MUST equal the routine tool's).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog

from alfred.daily_sync import routine_match_section as rms
from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.routine import match_calibration as mc


def _cfg(pending_path: Path, *, enabled: bool = True) -> DailySyncConfig:
    return DailySyncConfig(
        enabled=True,
        routine_match=RoutineMatchConfig(
            enabled=enabled, pending_path=str(pending_path),
        ),
    )


def _seed_pending(p: Path, *entries: mc.PendingMatch) -> None:
    for e in entries:
        mc.append_pending(p, e)


def test_disabled_omits_section(tmp_path: Path) -> None:
    out = rms.routine_match_section(
        _cfg(tmp_path / "pending.jsonl", enabled=False), date(2026, 6, 28),
    )
    assert out is None


def test_enabled_empty_emits_ilb_sentinel(tmp_path: Path) -> None:
    """Enabled but nothing to review → explicit sentinel, NOT silent omit
    (intentionally-left-blank)."""
    with structlog.testing.capture_logs() as cap:
        out = rms.routine_match_section(
            _cfg(tmp_path / "pending.jsonl"), date(2026, 6, 28),
        )
    assert out is not None
    assert "No low-confidence routine matches to review" in out
    # ILB log pinned (per feedback_log_emission_test_pattern).
    assert [c for c in cap if c.get("event") == "routine_match.no_pending"]


def test_enabled_with_pending_renders_numbered_list(tmp_path: Path) -> None:
    p = tmp_path / "pending.jsonl"
    _seed_pending(
        p,
        mc.PendingMatch(query="walk doggo", matched_to="Walk dog",
                        record="Daily", confidence=0.40),
        mc.PendingMatch(query="meds", matched_to="Take meds",
                        record="Health", confidence=0.33),
    )
    with structlog.testing.capture_logs() as cap:
        out = rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=1)
    assert out is not None
    assert "walk doggo" in out and "Walk dog" in out
    assert "0.40" in out and "0.33" in out
    assert "1." in out and "2." in out
    surfaced = [c for c in cap if c.get("event") == "routine_match.surfaced"]
    assert len(surfaced) == 1 and surfaced[0]["count"] == 2


def test_start_index_offsets_numbering(tmp_path: Path) -> None:
    """Item numbering honours the assembler's global start_index so it stays
    continuous after earlier sections."""
    p = tmp_path / "pending.jsonl"
    _seed_pending(p, mc.PendingMatch(
        query="q", matched_to="m", record="R", confidence=0.2))
    out = rms.routine_match_section(_cfg(p), date(2026, 6, 28), start_index=7)
    assert out is not None
    assert "7." in out
    # peek count drives item_count_after → next section starts at 8.
    assert rms.peek_last_batch_count() == 1


def test_register_adds_provider(tmp_path: Path) -> None:
    from alfred.daily_sync import assembler

    assembler.clear_providers()
    rms.register()
    rms.register()  # idempotent — the daemon re-registers every fire
    assert assembler.registered_providers().count("routine_match") == 1
    assembler.clear_providers()


def test_pending_path_default_matches_routine_tool() -> None:
    """Drift-guard: the Daily Sync section's pending-path default MUST equal the
    routine tool's capture default (same file — the CLI writes, the section
    reads). Both bind the shared module constant."""
    assert RoutineMatchConfig().pending_path == mc.DEFAULT_PENDING_PATH


def test_daily_sync_loads_routine_match_block() -> None:
    from alfred.daily_sync.config import load_from_unified

    cfg = load_from_unified({
        "daily_sync": {
            "enabled": True,
            "routine_match": {"enabled": True, "pending_path": "/x/p.jsonl"},
        },
    })
    assert cfg.routine_match.enabled is True
    assert cfg.routine_match.pending_path == "/x/p.jsonl"
