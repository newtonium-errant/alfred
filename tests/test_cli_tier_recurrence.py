"""#20 P5 B2 — the `alfred tier-recurrence` approve/reject/list CLI (the precise approval path).

Drives the REAL CLI (build_parser → _cmd_tier_recurrence): approve promotes to a routine + records the
decision; reject records only; the no-junk-default guard refuses approve with no --routine and no
configured promote_routine. list materializes + prints.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import frontmatter
import yaml

from alfred.cli import build_parser
from alfred.cli import _cmd_tier_recurrence
from alfred.tier import promote


def _cfg_file(tmp_path: Path, *, promote_routine: str = "") -> Path:
    cfg = {
        "vault": {"path": str(tmp_path / "vault")},
        "daily_sync": {
            "tier_recurrence": {
                "enabled": True,
                "pending_path": str(tmp_path / "pending.jsonl"),
                "decided_path": str(tmp_path / "decided.jsonl"),
                "threshold_done_days": 3,
                "window_days": 30,
                "promote_routine": promote_routine,
            }
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _seed(tmp_path: Path, text: str = "Rake leaves") -> str:
    pid = promote.proposal_id(promote.query_key(text))
    promote.append_pending(str(tmp_path / "pending.jsonl"), promote.RecurrenceProposal(
        proposal_id=pid, query_key=promote.query_key(text), sample_text=text,
        done_days=4, window_days=30, first_seen="2026-05-01", last_seen="2026-05-22"))
    return pid


def _run(config: Path, *argv) -> tuple[int, str]:
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            ns = build_parser().parse_args(["--config", str(config), "tier-recurrence", *argv])
            _cmd_tier_recurrence(ns)
        except SystemExit as e:  # includes argparse's parse-time exit (e.g. missing required --operator)
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def _routine_items(tmp_path: Path, record: str) -> list:
    post = frontmatter.load(str(tmp_path / "vault" / "routine" / f"{record}.md"))
    return post.metadata.get("items") or []


def test_approve_promotes_and_records(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path)
    pid = _seed(tmp_path)
    code, out = _run(cfg, "approve", pid, "--operator", "andrew", "--routine", "Recurring Chores")
    assert code == 0
    res = json.loads(out)
    assert res["approved"] == pid and res["routine"] == "Recurring Chores" and res["cadence_days"] == 7
    items = _routine_items(tmp_path, "Recurring Chores")
    assert len(items) == 1 and items[0]["text"] == "Rake leaves" and items[0]["target_cadence_days"] == 7
    assert promote.decided_ids(str(tmp_path / "decided.jsonl")) == {pid}


def test_approve_cadence_override(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path)
    pid = _seed(tmp_path)
    _run(cfg, "approve", pid, "--operator", "a", "--routine", "Chores", "--cadence", "14")
    assert _routine_items(tmp_path, "Chores")[0]["target_cadence_days"] == 14


def test_approve_uses_configured_promote_routine(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path, promote_routine="Home Routine")
    pid = _seed(tmp_path)
    code, out = _run(cfg, "approve", pid, "--operator", "a")   # no --routine → uses configured default
    assert code == 0 and json.loads(out)["routine"] == "Home Routine"
    assert len(_routine_items(tmp_path, "Home Routine")) == 1


def test_approve_no_junk_default_refuses(tmp_path: Path) -> None:
    """No --routine AND no configured promote_routine → REFUSE (never a blind placement)."""
    cfg = _cfg_file(tmp_path)   # promote_routine unset
    pid = _seed(tmp_path)
    code, out = _run(cfg, "approve", pid, "--operator", "a")
    assert code == 1 and "no routine target" in json.loads(out)["error"]
    assert not (tmp_path / "vault" / "routine").exists()       # NO routine written
    assert promote.decided_ids(str(tmp_path / "decided.jsonl")) == set()   # NO decision recorded


def test_reject_records_only(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path)
    pid = _seed(tmp_path)
    code, out = _run(cfg, "reject", pid, "--operator", "andrew")
    assert code == 0 and json.loads(out)["rejected"] == pid
    assert promote.decided_ids(str(tmp_path / "decided.jsonl")) == {pid}
    assert not (tmp_path / "vault" / "routine").exists()       # NO routine touch


def test_operator_required(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path)
    pid = _seed(tmp_path)
    code, out = _run(cfg, "reject", pid)   # missing --operator → argparse... actually required=True
    # argparse enforces --operator required → SystemExit(2) at parse. Guard via a direct handler check too.
    assert code != 0


def test_list_renders_and_json(tmp_path: Path) -> None:
    cfg = _cfg_file(tmp_path)
    _seed(tmp_path)
    code, out = _run(cfg, "list")
    assert "Rake leaves" in out and "suggest every ~7d" in out
    code2, out2 = _run(cfg, "list", "--json")
    js = json.loads(out2)
    assert js[0]["sample_text"] == "Rake leaves" and js[0]["suggested_cadence_days"] == 7
