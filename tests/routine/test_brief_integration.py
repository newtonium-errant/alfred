"""Brief integration tests — ``render_routine_section`` + brief insertion.

The brief section is loose-coupled via filesystem: the routine daemon
writes ``vault/daily/<today>.md`` at 05:59 Halifax; the brief reads
that file at 06:00. Tests:

  - Aggregator writes daily note → brief section produces expected body.
  - No daily note → section emits sentinel (intentionally-left-blank).
  - Malformed daily note → section emits sentinel + warning log.
  - Empty body → section emits sentinel.
  - Brief daemon includes "Today's Routines" in its section list order.

The full brief daemon test path is heavy (network + state + transport);
we test the rendering pieces in isolation here and pin the section-list
membership separately so a future re-ordering catches.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog
import yaml

from alfred.brief.routine_section import render_routine_section
from alfred.routine.aggregator import run_aggregator_once
from alfred.routine.config import RoutineConfig


def _config(vault_path: Path, tmp_path: Path) -> RoutineConfig:
    cfg = RoutineConfig(vault_path=str(vault_path), instance_name="salem")
    cfg.state.path = str(tmp_path / "routine_state.json")
    return cfg


def _write_routine(vault_path: Path, name: str, payload: dict) -> None:
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    (routine_dir / f"{name}.md").write_text(
        f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Aggregator → brief section roundtrip
# ---------------------------------------------------------------------------


def test_brief_section_reads_aggregator_output(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush Teeth", "priority": "tracked"},
            {"text": "Kiki Insulin", "priority": "critical", "time": "12:00"},
        ],
    })

    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    section = render_routine_section(vault, today)

    assert "## Critical" in section
    assert "Kiki Insulin @ 12:00" in section
    assert "## Tracked" in section
    assert "Brush Teeth" in section
    assert "## Aspirational" in section


def test_brief_section_no_daily_note_emits_sentinel(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)
    # Empty vault, no aggregator run.
    with structlog.testing.capture_logs() as captured:
        section = render_routine_section(vault, today)

    assert "no routines due today" in section.lower()
    # Per intentionally-left-blank: a structured log MUST fire so
    # operators can tell "daemon hasn't run" from "render is broken."
    events = [c.get("event") for c in captured]
    assert "brief.routine_section.no_daily_note" in events


def test_brief_section_empty_body_emits_sentinel(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    daily_dir = vault / "daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-26.md").write_text(
        "---\ntype: daily\ndate: '2026-05-26'\n---\n\n",
        encoding="utf-8",
    )

    with structlog.testing.capture_logs() as captured:
        section = render_routine_section(vault, date(2026, 5, 26))

    assert "no routines due today" in section.lower()
    events = [c.get("event") for c in captured]
    assert "brief.routine_section.empty_body" in events


def test_brief_section_rendered_log_event(tmp_path: Path) -> None:
    """Log-emission pin: ``brief.routine_section.rendered`` fires on happy path."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "X", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)

    with structlog.testing.capture_logs() as captured:
        render_routine_section(vault, today)

    matches = [c for c in captured
               if c.get("event") == "brief.routine_section.rendered"]
    assert len(matches) == 1
    assert matches[0].get("body_chars", 0) > 0
    assert matches[0].get("date") == "2026-05-26"


# ---------------------------------------------------------------------------
# Brief daemon section ordering
# ---------------------------------------------------------------------------


def test_brief_daemon_has_no_standalone_routines_section() -> None:
    """Pin the §4 DISSOLUTION (Phase C): the brief no longer emits a
    standalone ``## Today's Routines`` section.

    This is the negative half only — on its own it would pass just as happily
    against a build where the routine data vanished entirely, which is exactly
    the failure this dissolution risks. Its positive control is the test
    directly below, which drives a real render and proves a routine item still
    reaches the operator inside its slot.
    """
    import inspect
    from alfred.brief import daemon

    src = inspect.getsource(daemon.generate_brief)
    assert 'SectionResult("Today\'s Routines"' not in src
    # The section it dissolved INTO is still assembled, in its ordered place.
    weather_idx = src.find('"Weather"')
    tier_idx = src.find("TIER_SECTION_HEADER")
    operations_idx = src.find('"Operations"')
    assert 0 < weather_idx < tier_idx < operations_idx


def test_dissolved_routine_item_renders_inside_its_slot(tmp_path: Path) -> None:
    """The POSITIVE control for the dissolution: a routine item that fires
    today and never escalates into a tier still reaches the operator — now
    inside its slot in the day plan rather than in its own section.

    Drives the real production render (``render_tier_section``), not a helper,
    because the thing at risk is the WIRING: the projection could classify
    perfectly while the render never asked it for routines.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from alfred.brief.tier_section import render_tier_section

    vault = tmp_path / "vault"
    # ``target_cadence_days`` with a fresh completion today ⟹ NOT overdue, so
    # the item does NOT hand off to T3 and lands in the routine complement —
    # the exact population this dissolution is responsible for.
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Morning pages",
                "priority": "tracked",
                "target_cadence_days": 3,
            },
        ],
        "completion_log": {"Morning pages": ["2026-05-26"]},
    })
    now = datetime(2026, 5, 26, 6, 0, tzinfo=ZoneInfo("America/Halifax"))

    body = render_tier_section(vault, now)

    assert "Morning pages" in body
    # Rhythm, because ``target_cadence_days`` is the classifier's practice
    # rule — not defaulted into Duty to fill the board.
    rhythm_idx = body.index("### Rhythm")
    fuel_idx = body.index("### Fuel")
    assert rhythm_idx < body.index("Morning pages") < fuel_idx
