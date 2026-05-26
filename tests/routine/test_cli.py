"""``alfred routine`` CLI handler tests.

Covers the dispatch ratified `done` verb + supporting verbs (run-now,
status). Salem-only enforcement is pinned independently — a non-Salem
instance config raises ScopeError before any vault mutation occurs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest
import structlog
import yaml

from alfred.routine.cli import cmd_done, cmd_run_now, cmd_status
from alfred.routine.config import RoutineConfig
from alfred.vault.scope import ScopeError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(vault_path: Path, tmp_path: Path, *, instance: str = "salem") -> RoutineConfig:
    config = RoutineConfig(
        vault_path=str(vault_path),
        instance_name=instance,
    )
    config.state.path = str(tmp_path / "routine_state.json")
    return config


def _write_routine(vault_path: Path, name: str, payload: dict) -> Path:
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# done — happy path
# ---------------------------------------------------------------------------


def test_done_appends_today_to_completion_log(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "For Self Health", {
        "type": "routine",
        "name": "For Self Health",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Reading for pleasure", "priority": "aspirational"},
            {"text": "Dog Walk", "priority": "tracked"},
        ],
        "completion_log": {
            "Reading for pleasure": ["2026-05-22", "2026-05-24"],
        },
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "For Self Health", "Reading for pleasure",
        today_override="2026-05-26",
    )
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "For Self Health.md"))
    log = post.metadata["completion_log"]
    assert log["Reading for pleasure"] == ["2026-05-22", "2026-05-24", "2026-05-26"]


def test_done_idempotent_same_day(tmp_path: Path) -> None:
    """Calling ``done`` twice with the same item on the same day yields
    one log entry (no duplicates within a single day)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code1 = cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    code2 = cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    assert code1 == code2 == 0

    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata["completion_log"]
    assert log["Brush Teeth"] == ["2026-05-26"]


def test_done_creates_completion_log_when_absent(tmp_path: Path) -> None:
    """First-ever completion on a routine without a completion_log key."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Fresh", {
        "type": "routine",
        "name": "Fresh",
        "cadence": {"type": "daily"},
        "items": [{"text": "New Habit", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(config, "Fresh", "New Habit", today_override="2026-05-26")
    assert code == 0

    post = frontmatter.load(str(vault / "routine" / "Fresh.md"))
    log = post.metadata["completion_log"]
    assert log["New Habit"] == ["2026-05-26"]


def test_done_emits_log_event(tmp_path: Path) -> None:
    """Per intentionally-left-blank + log-emission-tests-must-drive-prod
    discipline: pin the emission."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")

    matches = [c for c in captured if c.get("event") == "routine.cli.done"]
    assert len(matches) == 1
    m = matches[0]
    assert m.get("record") == "Daily"
    assert m.get("item") == "Brush Teeth"
    assert m.get("date") == "2026-05-26"
    assert m.get("appended") is True

    # Second call — appended should be False.
    with structlog.testing.capture_logs() as captured2:
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    matches2 = [c for c in captured2 if c.get("event") == "routine.cli.done"]
    assert len(matches2) == 1
    assert matches2[0].get("appended") is False


# ---------------------------------------------------------------------------
# done — error paths
# ---------------------------------------------------------------------------


def test_done_unknown_item_returns_1(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Real Item", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_done(
        config, "Daily", "Typo Item", today_override="2026-05-26",
    )
    assert code == 1
    # File should be unchanged.
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    assert "completion_log" not in post.metadata or not post.metadata.get("completion_log")


def test_done_unknown_record_raises_file_not_found(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)
    with pytest.raises(FileNotFoundError):
        cmd_done(config, "Nonexistent", "Anything", today_override="2026-05-26")


# ---------------------------------------------------------------------------
# Salem-only enforcement (CLI guard)
# ---------------------------------------------------------------------------


def test_done_non_salem_instance_raises_scope_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path, instance="hypatia")
    with pytest.raises(ScopeError):
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")


def test_done_empty_instance_raises_scope_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="")
    with pytest.raises(ScopeError):
        cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")


def test_run_now_non_salem_instance_raises(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="kalle")
    with pytest.raises(ScopeError):
        cmd_run_now(config, today_override="2026-05-26")


def test_status_non_salem_instance_raises(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = _config(vault, tmp_path, instance="hypatia")
    with pytest.raises(ScopeError):
        cmd_status(config)


# ---------------------------------------------------------------------------
# run-now + status smoke tests
# ---------------------------------------------------------------------------


def test_run_now_writes_aggregator_note(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine",
        "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_run_now(config, today_override="2026-05-26")
    assert code == 0
    assert (vault / "daily" / "2026-05-26.md").exists()


def test_status_with_no_runs_prints_never(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    config = _config(vault, tmp_path)

    code = cmd_status(config)
    assert code == 0
    captured = capsys.readouterr()
    # Intentionally-left-blank — visible "never" rather than silence.
    assert "Last run:" in captured.out
    assert "never" in captured.out


# ---------------------------------------------------------------------------
# Frontmatter preservation — done shouldn't reorder or drop other fields
# ---------------------------------------------------------------------------


def test_done_preserves_other_frontmatter_fields(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    payload = {
        "type": "routine",
        "status": "active",
        "name": "Daily",
        "created": "2026-05-01",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush Teeth", "priority": "tracked"}],
        "tags": ["habits", "morning"],
    }
    _write_routine(vault, "Daily", payload)
    config = _config(vault, tmp_path)

    cmd_done(config, "Daily", "Brush Teeth", today_override="2026-05-26")
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    fm = post.metadata
    assert fm["type"] == "routine"
    assert fm["status"] == "active"
    assert fm["name"] == "Daily"
    assert fm["tags"] == ["habits", "morning"]
    # And completion_log got the new entry.
    assert fm["completion_log"]["Brush Teeth"] == ["2026-05-26"]
