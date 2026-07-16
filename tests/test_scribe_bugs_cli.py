"""Task #4 — `alfred scribe bugs {list|show|resolve}` CLI contract tests.

Local file ops over the box-local bug-report dir (no vault write, no egress). Drives the real
parser + handler (build_parser → cmd_scribe)."""

from __future__ import annotations

import pytest
import yaml

from alfred.cli import build_parser, cmd_scribe
from alfred.scribe import bug as bug_mod
from alfred.scribe.config import load_from_unified

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _write_config(tmp_path, bug_dir):
    cfg = {"scribe": {
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
        "bug": {"dir": str(bug_dir)},
    }}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def _seed(bug_dir, summary, detail="detail body"):
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "bug": {"dir": str(bug_dir)}}})
    _, bug_id = bug_mod.write_bug_report(cfg, summary=summary, detail=detail)
    return bug_id


def _run(config, *argv):
    args = build_parser().parse_args(["--config", config, "scribe", "bugs", *argv])
    cmd_scribe(args)


def test_bugs_list_empty(tmp_path, capsys):
    config = _write_config(tmp_path, tmp_path / "bugs")
    _run(config, "list")
    assert "No bug reports." in capsys.readouterr().out       # intentionally-left-blank


def test_bugs_list_shows_seeded(tmp_path, capsys):
    bug_dir = tmp_path / "bugs"
    bid = _seed(bug_dir, "Dead create button")
    config = _write_config(tmp_path, bug_dir)
    _run(config, "list")
    out = capsys.readouterr().out
    assert bid in out and "Dead create button" in out


def test_bugs_show_prints_report(tmp_path, capsys):
    bug_dir = tmp_path / "bugs"
    bid = _seed(bug_dir, "Something broke", detail="the thing did the bad")
    config = _write_config(tmp_path, bug_dir)
    _run(config, "show", bid)
    out = capsys.readouterr().out
    assert f"id: {bid}" in out and "the thing did the bad" in out


def test_bugs_show_unknown_exits_nonzero(tmp_path):
    config = _write_config(tmp_path, tmp_path / "bugs")
    with pytest.raises(SystemExit) as e:
        _run(config, "show", "nope")
    assert e.value.code == 1


def test_bugs_resolve_moves_and_hides_from_default_list(tmp_path, capsys):
    bug_dir = tmp_path / "bugs"
    bid = _seed(bug_dir, "Resolve me")
    config = _write_config(tmp_path, bug_dir)
    _run(config, "resolve", bid)
    assert "Resolved" in capsys.readouterr().out
    # default list no longer shows it...
    _run(config, "list")
    assert bid not in capsys.readouterr().out
    # ...but --all does, flagged resolved.
    _run(config, "list", "--all")
    out = capsys.readouterr().out
    assert bid in out and "[resolved]" in out


def test_bugs_resolve_unknown_exits_nonzero(tmp_path):
    config = _write_config(tmp_path, tmp_path / "bugs")
    with pytest.raises(SystemExit) as e:
        _run(config, "resolve", "does-not-exist")
    assert e.value.code == 1
