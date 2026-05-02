"""Smoke tests for the ``alfred distiller rank-week`` CLI handler.

The handler is read-only — it reads frontmatter from the configured
vault and prints a ranked table. These tests verify:
- It runs without error against a small fixture vault
- It surfaces records in the expected ordering
- It handles the empty-vault case cleanly
- Missing vault path produces a friendly error, not a crash
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.distiller.cli import cmd_rank_week
from alfred.distiller.config import DistillerConfig, VaultConfig


def _config_for(vault: Path) -> DistillerConfig:
    return DistillerConfig(vault=VaultConfig(path=str(vault)))


def _write_record(vault: Path, record_type: str, name: str) -> None:
    type_dir = vault / record_type
    type_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\n"
        f"name: {name}\n"
        f"type: {record_type}\n"
        f"claim: claim for {name}\n"
        f"created: '2026-04-30'\n"
        f"source_links:\n"
        f"  - '[[session/A]]'\n"
        f"  - '[[session/B]]'\n"
        f"entity_links:\n"
        f"  - '[[project/Alfred]]'\n"
        f"---\n\nbody\n"
    )
    (type_dir / f"{name}.md").write_text(body, encoding="utf-8")


def test_rank_week_runs_against_fixture_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two records, ranker prints both with breakdown table."""
    _write_record(tmp_path, "synthesis", "Alpha")
    _write_record(tmp_path, "decision", "Beta")
    cmd_rank_week(_config_for(tmp_path), top_n=10, window_days=7)
    out = capsys.readouterr().out
    assert "Synthesis Ranker" in out
    assert "Alpha.md" in out
    assert "Beta.md" in out
    assert "Score breakdowns" in out
    # Synthesis (type weight 3) outranks decision (type weight 1) at
    # equal source/entity counts.
    assert out.index("Alpha") < out.index("Beta")


def test_rank_week_empty_vault_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """No records → explicit "No records ranked." line."""
    cmd_rank_week(_config_for(tmp_path), top_n=10, window_days=7)
    out = capsys.readouterr().out
    assert "Synthesis Ranker" in out
    assert "No records ranked." in out


def test_rank_week_missing_vault_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Vault path that doesn't exist → friendly error, no crash."""
    missing = tmp_path / "nope"
    cmd_rank_week(_config_for(missing), top_n=10, window_days=7)
    out = capsys.readouterr().out
    assert "Vault path does not exist" in out


def test_rank_week_top_n_caps_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    for i in range(5):
        _write_record(tmp_path, "synthesis", f"rec{i}")
    cmd_rank_week(_config_for(tmp_path), top_n=2, window_days=7)
    out = capsys.readouterr().out
    # Header line + 2 rows visible in the rank table.
    table_lines = [ln for ln in out.splitlines() if ln.startswith(("1 ", "2 ", "3 ", "4 ", "5 "))]
    assert len(table_lines) == 2
