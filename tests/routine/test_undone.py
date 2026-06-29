"""Tests for ``cmd_undone`` — surgical single-date un-log (the inverse of done).

Pins the CLI handler contract:
  * happy-path single-date removal (other dates retained).
  * date-not-present → ``not_logged`` no-op, exit 0, file BYTES + mtime
    untouched (the aborted gate).
  * empty-list handling — removing the last date keeps ``completion_log[item]``
    as ``[]`` (mirror done; don't drop the key).
  * item-not-found / ambiguous → the shared resolver canaries (exit 1).
  * vault-wide fuzzy resolution (omit record).
  * invalid --date → invalid_field canary.
  * default date = today (config tz).
  * non-Salem instance → ScopeError (Salem-only).
  * pure data-fix boundary — un-log does NOT write the matcher corpus.

The handler lives in ``cli_items.py``; these mirror ``test_cli_items.py`` shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest
import yaml

from alfred.routine.cli import (
    ITEM_KIND_AMBIGUOUS_ITEM,
    ITEM_KIND_INVALID_FIELD,
    ITEM_KIND_NOT_LOGGED,
    ITEM_KIND_UNKNOWN_ITEM,
    ITEM_KIND_UNLOGGED,
    _today_iso,
    cmd_undone,
)
from alfred.routine.config import RoutineConfig
from alfred.vault.scope import ScopeError


# ---------------------------------------------------------------------------
# Helpers (mirror test_cli_items.py)
# ---------------------------------------------------------------------------


def _config(
    vault_path: Path, tmp_path: Path, *, instance: str = "salem",
) -> RoutineConfig:
    config = RoutineConfig(vault_path=str(vault_path), instance_name=instance)
    config.state.path = str(tmp_path / "routine_state.json")
    config.match_calibration.pending_path = str(tmp_path / "pending.jsonl")
    config.match_calibration.corpus_path = str(tmp_path / "corpus.jsonl")
    return config


def _write_routine(vault_path: Path, name: str, payload: dict) -> Path:
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


def _read_fm(vault_path: Path, name: str) -> dict:
    post = frontmatter.load(str(vault_path / "routine" / f"{name}.md"))
    return dict(post.metadata)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_undone_removes_single_date_retains_others(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-26", "2026-06-27", "2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "Daily", "Walk dog", date="2026-06-27")

    assert code == 0
    log = _read_fm(vault, "Daily")["completion_log"]
    assert log["Walk dog"] == ["2026-06-26", "2026-06-28"]


def test_undone_success_canary(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "Daily", "Walk dog", date="2026-06-28", wants_json=True)

    assert code == 0


def test_undone_empty_list_keeps_empty_key(tmp_path: Path) -> None:
    """Removing the last date keeps ``completion_log[item] == []`` (mirror
    cmd_done; only item-remove drops the whole key)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "Daily", "Walk dog", date="2026-06-28")

    assert code == 0
    log = _read_fm(vault, "Daily")["completion_log"]
    assert "Walk dog" in log  # key retained
    assert log["Walk dog"] == []


# ---------------------------------------------------------------------------
# date-not-present no-op (the ILB / aborted path)
# ---------------------------------------------------------------------------


def test_undone_date_not_present_is_noop_canary(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(
        config, "Daily", "Walk dog", date="2026-06-01", wants_json=True,
    )

    assert code == 0  # desired end-state already holds — idempotent no-op
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_NOT_LOGGED
    assert payload["removed"] is False
    assert "was not logged on 2026-06-01" in payload["message"]
    # The present date is untouched.
    assert _read_fm(vault, "Daily")["completion_log"]["Walk dog"] == ["2026-06-28"]


def test_undone_noop_leaves_file_bytes_and_mtime_untouched(tmp_path: Path) -> None:
    """The aborted gate: a date-not-present no-op MUST NOT rewrite the file
    (no mtime bump, no YAML reflow)."""
    vault = tmp_path / "vault"
    path = _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    cmd_undone(config, "Daily", "Walk dog", date="2026-06-01")

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# resolution canaries + date validation
# ---------------------------------------------------------------------------


def test_undone_unknown_item_canary(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "", "xyzzy nonexistent", wants_json=True)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["kind"] == ITEM_KIND_UNKNOWN_ITEM


def test_undone_ambiguous_item_canary(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "A", {
        "type": "routine", "name": "A", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    _write_routine(vault, "B", {
        "type": "routine", "name": "B", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "", "Walk dog", wants_json=True)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["kind"] == ITEM_KIND_AMBIGUOUS_ITEM


def test_undone_vault_wide_fuzzy_resolves(tmp_path: Path) -> None:
    """Omitting the record → vault-wide fuzzy match on the item text."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk the dog", "priority": "tracked"}],
        "completion_log": {"Walk the dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "", "Walk the dog", date="2026-06-28")

    assert code == 0
    assert _read_fm(vault, "Daily")["completion_log"]["Walk the dog"] == []


def test_undone_invalid_date_canary(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "Daily", "Walk dog", date="not-a-date", wants_json=True)

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == ITEM_KIND_INVALID_FIELD


def test_undone_default_date_is_today(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = _today_iso("America/Halifax")
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": [today]},
    })
    config = _config(vault, tmp_path)

    code = cmd_undone(config, "Daily", "Walk dog")  # no --date → today

    assert code == 0
    assert _read_fm(vault, "Daily")["completion_log"]["Walk dog"] == []


# ---------------------------------------------------------------------------
# scope + boundary
# ---------------------------------------------------------------------------


def test_undone_non_salem_instance_raises_scope_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path, instance="kalle")

    with pytest.raises(ScopeError, match="Salem-only"):
        cmd_undone(config, "Daily", "Walk dog", date="2026-06-28")


def test_undone_does_not_write_matcher_corpus(tmp_path: Path) -> None:
    """Boundary pin: un-log is a pure data-fix — it must NOT touch the matcher
    learned-glossary corpus (that's the Daily-Sync confirm/reject surface)."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily", "status": "active",
        "cadence": {"type": "daily"},
        "items": [{"text": "Walk dog", "priority": "tracked"}],
        "completion_log": {"Walk dog": ["2026-06-28"]},
    })
    config = _config(vault, tmp_path)

    cmd_undone(config, "Daily", "Walk dog", date="2026-06-28")

    assert not Path(config.match_calibration.corpus_path).exists()
    assert not Path(config.match_calibration.pending_path).exists()
