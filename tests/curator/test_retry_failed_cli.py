"""#34 ``alfred curator retry-failed`` — re-queue quarantined files.

The operator-triggered recovery: after the backend recovers (Monitor B / the
BIT ``claude-cli-auth`` probe goes green), move quarantined files back into
inbox with a fresh retry budget. Per intentionally-left-blank, an empty
quarantine prints an explicit "nothing to retry" rather than nothing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from alfred import cli


def _patch(monkeypatch, raw) -> None:
    monkeypatch.setattr(cli, "_load_unified_config", lambda p: raw)
    monkeypatch.setattr(cli, "_setup_logging_from_config", lambda *a, **k: None)


def _args() -> argparse.Namespace:
    return argparse.Namespace(config="unused", curator_cmd="retry-failed")


def test_retry_failed_requeues_and_clears_counter(tmp_path: Path, monkeypatch, capsys) -> None:
    vault = tmp_path / "v"
    failed = vault / "inbox" / "failed"
    failed.mkdir(parents=True)
    (failed / "email-gmail-x.md").write_text("---\nstatus: failed\n---\nx\n", encoding="utf-8")
    state_path = vault / "curator_state.json"
    state_path.write_text(
        json.dumps({"version": 2, "processed": {}, "failed_attempts": {"email-gmail-x.md": 2}}),
        encoding="utf-8",
    )
    raw = {"vault": {"path": str(vault)}, "curator": {"state": {"path": str(state_path)}}}
    _patch(monkeypatch, raw)

    cli.cmd_curator(_args())

    # Moved back to inbox top-level; gone from failed/.
    assert (vault / "inbox" / "email-gmail-x.md").exists()
    assert not (failed / "email-gmail-x.md").exists()
    assert "Re-queued 1" in capsys.readouterr().out
    # Counter cleared → a fresh max_retries budget on reprocess.
    st = json.loads(state_path.read_text(encoding="utf-8"))
    assert st.get("failed_attempts", {}) == {}


def test_retry_failed_empty_dir_is_ilb(tmp_path: Path, monkeypatch, capsys) -> None:
    vault = tmp_path / "v"
    (vault / "inbox" / "failed").mkdir(parents=True)
    _patch(monkeypatch, {"vault": {"path": str(vault)}})
    cli.cmd_curator(_args())
    assert "nothing to retry" in capsys.readouterr().out


def test_retry_failed_no_dir_is_ilb(tmp_path: Path, monkeypatch, capsys) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    _patch(monkeypatch, {"vault": {"path": str(vault)}})
    cli.cmd_curator(_args())
    assert "nothing to retry" in capsys.readouterr().out
