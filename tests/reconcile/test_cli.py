"""``alfred reconcile`` — the commands, and the wiring that reaches them.

Two layers are tested, and the second is the one that catches the failure
the first cannot:

  * the handlers in :mod:`alfred.reconcile.cli`, called directly;
  * the FULL path through ``alfred.cli.main`` with a real ``--config``
    file, because a feature reachable only by direct invocation is the
    standing trap — every unit pin stays green while production never
    calls it. The end-to-end seed/report tests below go through the actual
    argparse dispatcher and the actual config loader.

The exit codes are part of the contract: a lossy seed exits 2, so a
scripted caller cannot mistake a partial parse for a success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from alfred.reconcile import cli as rcli
from alfred.reconcile.attention import CLASS_DUPLICATE_DENIAL, CLASS_REVERSAL
from alfred.reconcile.config import ReconcileConfig, load_from_unified
from alfred.reconcile.ledger import load_ledger
from alfred.reconcile.paths import corrections_path_in, ledger_path_in

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN_NOTE = FIXTURES / "remittance_note_synthetic.md"
DAMAGED_NOTE = FIXTURES / "remittance_note_damaged.md"


@pytest.fixture
def cfg(tmp_path) -> ReconcileConfig:
    return load_from_unified({
        "logging": {"dir": str(tmp_path)},
        "telegram": {"instance": {"name": "Testbed"}},
    })


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# --- seed ---------------------------------------------------------------------


def test_seed_writes_the_ledger(cfg, capsys):
    code = rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    assert code == 0
    payload = _json_out(capsys)
    assert payload["claim_lines"] == 7
    assert payload["statements"] == 3
    assert payload["inserted"] == 14  # 3 statements + 7 claims + 4 subtotals
    contents = load_ledger(ledger_path_in(cfg.store_dir))
    assert len(contents.claim_lines) == 7


def test_seed_is_idempotent_and_says_so(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "0 inserted, 0 updated" in out
    assert "idempotent re-run looks like, not a failure" in out
    assert len(load_ledger(ledger_path_in(cfg.store_dir)).claim_lines) == 7


def test_seed_records_provenance(cfg, capsys):
    rcli.cmd_seed(
        cfg, note_path=str(CLEAN_NOTE), batch_id="b1", session="s1",
        capture_ref="scan-3", wants_json=True,
    )
    capsys.readouterr()
    line = load_ledger(ledger_path_in(cfg.store_dir)).claim_lines[0]
    assert (line.batch_id, line.session, line.capture_ref) == ("b1", "s1", "scan-3")


def test_dry_run_writes_nothing(cfg, capsys):
    code = rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), dry_run=True,
                         wants_json=False)
    assert code == 0
    assert "dry run" in capsys.readouterr().out
    assert not ledger_path_in(cfg.store_dir).exists()


def test_a_lossy_seed_exits_two_and_lists_what_it_lost(cfg, capsys):
    """A scripted caller must not read a partial parse as a success, and a
    human must be able to see WHICH rows were lost."""
    code = rcli.cmd_seed(cfg, note_path=str(DAMAGED_NOTE), wants_json=False)
    assert code == 2
    out = capsys.readouterr().out
    assert "7 row(s) were NOT parsed" in out
    assert "N0T-A-NUMBER" in out
    assert "ragged row" in out
    # Positive control: the well-formed row still landed.
    assert len(load_ledger(ledger_path_in(cfg.store_dir)).claim_lines) == 1


def test_a_clean_seed_says_nothing_was_skipped(cfg, capsys):
    """ILB: the absence of a skip list must be stated, or 'no skips' and
    'the skip reporting broke' look identical."""
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=False)
    assert "Every row in the note parsed" in capsys.readouterr().out


def test_seed_reports_key_collisions(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=False)
    out = capsys.readouterr().out
    assert "1 claim line(s) shared a key" in out
    assert "never silent" in out


def test_seed_refuses_a_missing_note(cfg, capsys):
    code = rcli.cmd_seed(cfg, note_path="/tmp/definitely/not/here.md",
                         wants_json=True)
    assert code == 1
    assert _json_out(capsys)["error"] == "note_not_found"


def test_seed_refuses_when_the_store_is_unresolved(capsys):
    """The refusal must name its own cause. A generic failure would be
    indistinguishable from a missing note."""
    disabled = load_from_unified({"telegram": {"instance": {"name": "X"}}})
    code = rcli.cmd_seed(disabled, note_path=str(CLEAN_NOTE), wants_json=True)
    assert code == 1
    payload = _json_out(capsys)
    assert payload["error"] == "store_unresolved"
    assert "logging.dir" in payload["detail"]


# --- render -------------------------------------------------------------------


def test_render_prints_the_regenerated_note(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_render(cfg, wants_json=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "Machine-generated" in out
    assert "Wren Alderly" in out
    assert "| Claim # |" in out


def test_render_on_an_empty_ledger_says_it_is_empty(cfg, capsys):
    code = rcli.cmd_render(cfg, wants_json=False)
    assert code == 0
    assert "ledger is empty" in capsys.readouterr().out


def test_render_writes_nothing_at_all(cfg, capsys):
    """P1 makes no vault write and no file write. The only thing render
    produces is stdout."""
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    before = set(Path(cfg.store_dir).rglob("*"))
    rcli.cmd_render(cfg, wants_json=False)
    capsys.readouterr()
    assert set(Path(cfg.store_dir).rglob("*")) == before


# --- report -------------------------------------------------------------------


def test_report_writes_both_halves_into_the_instance_store(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_report(cfg, wants_json=True)
    assert code == 0
    payload = _json_out(capsys)
    csv_path = Path(payload["csv_path"])
    summary_path = Path(payload["summary_path"])
    assert csv_path.is_file() and summary_path.is_file()
    assert csv_path.parent == Path(cfg.store_dir) / "reports"
    assert payload["total_lines"] == 7
    assert payload["flagged_lines"] == 2


def test_report_on_an_empty_ledger_still_produces_the_artifact(cfg, capsys):
    code = rcli.cmd_report(cfg, wants_json=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "The ledger is empty" in out
    assert "alfred reconcile seed" in out


def test_report_surfaces_cross_foot_findings(cfg, capsys, monkeypatch):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    # Tamper with a declared total so the cross-foot has something to find.
    ledger = ledger_path_in(cfg.store_dir)
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    for row in rows:
        if row.get("row_type") == "statement" and row.get("payment_total"):
            row["payment_total"] = "999.00"
            break
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    code = rcli.cmd_report(cfg, wants_json=False)
    assert code == 0
    assert "Cross-foot findings present" in capsys.readouterr().out


# --- status -------------------------------------------------------------------


def test_status_before_any_seed_says_pre_seed_not_broken(cfg, capsys):
    code = rcli.cmd_status(cfg, wants_json=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "No ledger file yet" in out
    assert "not a failure" in out


def test_status_reports_the_counts(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_status(cfg, wants_json=True)
    assert code == 0
    payload = _json_out(capsys)
    assert payload["claim_lines"] == 7
    assert payload["statements"] == 3
    assert payload["flagged"] == 2
    assert payload["ledger_exists"] is True


def test_status_explains_the_empty_eob_map(cfg, capsys):
    """The starting state is unusual enough to need saying: everything
    coded surfaces as unknown, and that is intended."""
    rcli.cmd_status(cfg, wants_json=False)
    out = capsys.readouterr().out
    assert "No EOB codes are mapped" in out
    assert "fail OPEN" in out


# --- correct ------------------------------------------------------------------


def test_correct_records_a_ruling_with_the_lines_eob_codes(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    target = [
        c for c in load_ledger(ledger_path_in(cfg.store_dir)).claim_lines
        if c.eob_code
    ][0]

    code = rcli.cmd_correct(
        cfg, line_key=target.key, classes=[CLASS_DUPLICATE_DENIAL],
        operator="andrew", note="already paid in March", wants_json=True,
    )
    assert code == 0
    payload = _json_out(capsys)
    assert payload["classes"] == [CLASS_DUPLICATE_DENIAL]
    assert payload["eob_codes"] == [target.eob_code.upper()]
    assert corrections_path_in(cfg.store_dir).is_file()


def test_correct_feeds_back_into_status(cfg, capsys):
    """The loop closing: a ruling changes what the next read reports."""
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    flagged = [
        c for c in load_ledger(ledger_path_in(cfg.store_dir)).claim_lines
        if c.eob_code
    ][0]
    rcli.cmd_correct(cfg, line_key=flagged.key, classes=[], operator="andrew",
                     wants_json=True)
    capsys.readouterr()
    rcli.cmd_status(cfg, wants_json=True)
    payload = _json_out(capsys)
    assert payload["flagged"] == 1  # was 2
    assert payload["corrections"] == 1


def test_correct_refuses_an_unknown_line_key(cfg, capsys):
    """A ruling against a line that does not exist would sit in the file
    teaching nothing. The positive control is the accepted ruling above."""
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_correct(cfg, line_key="no-such-key",
                            classes=[CLASS_REVERSAL], operator="andrew",
                            wants_json=True)
    assert code == 1
    assert _json_out(capsys)["error"] == "unknown_line_key"
    assert not corrections_path_in(cfg.store_dir).exists()


def test_correct_refuses_an_unknown_class(cfg, capsys):
    rcli.cmd_seed(cfg, note_path=str(CLEAN_NOTE), wants_json=True)
    capsys.readouterr()
    code = rcli.cmd_correct(cfg, line_key="k", classes=["invented"],
                            operator="andrew", wants_json=True)
    assert code == 1
    assert _json_out(capsys)["error"] == "unknown_class"


def test_correct_requires_an_operator(cfg, capsys):
    code = rcli.cmd_correct(cfg, line_key="k", classes=[], operator="",
                            wants_json=True)
    assert code == 1
    assert _json_out(capsys)["error"] == "missing_operator"


# --- the full path through the real CLI ---------------------------------------
#
# These are the pins that catch a feature wired only in tests. Everything
# above calls the handlers directly; these go through argparse and the real
# config loader, which is what production does.


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.test.yaml"
    path.write_text(yaml.safe_dump({
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Testbed"}},
    }), encoding="utf-8")
    return path


def _run(argv: list[str]) -> int:
    """Drive the real entry point exactly as a shell does.

    ``alfred.cli.main`` takes no arguments — it parses ``sys.argv`` — so the
    argv is installed rather than passed. Going through it (instead of
    calling ``build_parser`` and the handler by hand) is the whole point of
    these tests: it exercises the same dispatch production uses.
    """
    import sys

    from alfred.cli import main

    original = sys.argv
    sys.argv = ["alfred", *argv]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = original
    return 0


def test_reconcile_is_registered_in_the_real_parser():
    from alfred.cli import build_parser

    args = build_parser().parse_args(
        ["reconcile", "seed", "--note", "/tmp/x.md"]
    )
    assert args.command == "reconcile"
    assert args.reconcile_cmd == "seed"
    assert args.note == "/tmp/x.md"


def test_reconcile_is_registered_in_the_handlers_dict():
    """The parser accepting the verb is not the same as the dispatcher
    routing it — a subcommand can parse fine and then fall through to the
    usage text."""
    import inspect

    from alfred import cli as top_cli

    source = inspect.getsource(top_cli.main)
    assert '"reconcile": cmd_reconcile' in source


def test_end_to_end_seed_then_report_through_the_real_entry_point(tmp_path):
    """The pin that per-layer unit tests structurally cannot give: the
    whole chain, argparse -> config load -> handler -> ledger -> report."""
    config_path = _config_file(tmp_path)

    code = _run(["--config", str(config_path), "reconcile", "seed",
                 "--note", str(CLEAN_NOTE), "--json"])
    assert code == 0

    store = tmp_path / "data" / "remittance" / "testbed"
    assert (store / "ledger.jsonl").is_file()
    assert len(load_ledger(store / "ledger.jsonl").claim_lines) == 7

    code = _run(["--config", str(config_path), "reconcile", "report", "--json"])
    assert code == 0
    reports = list((store / "reports").glob("backlog-review-*.csv"))
    assert len(reports) == 1
    assert list((store / "reports").glob("backlog-review-*.md"))


def test_end_to_end_status_and_render(tmp_path, capsys):
    config_path = _config_file(tmp_path)
    _run(["--config", str(config_path), "reconcile", "seed",
          "--note", str(CLEAN_NOTE), "--json"])
    capsys.readouterr()

    assert _run(["--config", str(config_path), "reconcile", "status"]) == 0
    assert "3 statement(s), 7 claim line(s)" in capsys.readouterr().out

    assert _run(["--config", str(config_path), "reconcile", "render"]) == 0
    assert "| Claim # |" in capsys.readouterr().out


def test_end_to_end_a_lossy_seed_exits_two(tmp_path):
    config_path = _config_file(tmp_path)
    code = _run(["--config", str(config_path), "reconcile", "seed",
                 "--note", str(DAMAGED_NOTE)])
    assert code == 2


def test_end_to_end_empty_classes_rules_a_line_clear(tmp_path, capsys):
    """``--classes ''`` must reach the handler as an EMPTY list. Splitting
    it naively yields ``[""]``, which is then refused as an unknown class —
    and "rule this line clear", the whole retraction path, becomes
    unreachable from the command line while the unit tests stay green."""
    config_path = _config_file(tmp_path)
    _run(["--config", str(config_path), "reconcile", "seed",
          "--note", str(CLEAN_NOTE), "--json"])
    capsys.readouterr()

    store = tmp_path / "data" / "remittance" / "testbed"
    target = [
        c for c in load_ledger(store / "ledger.jsonl").claim_lines if c.eob_code
    ][0]

    code = _run(["--config", str(config_path), "reconcile", "correct",
                 "--line-key", target.key, "--operator", "andrew",
                 "--classes", "", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["classes"] == []


def test_end_to_end_bare_reconcile_prints_usage(tmp_path, capsys):
    config_path = _config_file(tmp_path)
    code = _run(["--config", str(config_path), "reconcile"])
    assert code == 1
    assert "Usage: alfred reconcile" in capsys.readouterr().out
