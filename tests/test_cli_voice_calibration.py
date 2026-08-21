"""R4 — the ``alfred voice-calibration`` list/approve/reject CLI (the apply door).

Drives the REAL CLI (build_parser → _cmd_voice_calibration). This is a
production call site of ``calibration_store.approve_proposal``, so these pins
exist for the threading class specifically: the store's guards are unit-tested
elsewhere, and what is proven HERE is that the CLI actually reaches them with
the target record threaded — the failure mode where a gate is tested by direct
invocation and production never supplies the argument.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from alfred.cli import _cmd_voice_calibration, build_parser
from alfred.telegram import calibration, calibration_store
from alfred.telegram.calibration import Proposal

PRIMARY_USER_REL = "person/Andrew Newton"


def _cfg_file(tmp_path: Path, *, primary_users=(PRIMARY_USER_REL,), capture=True) -> Path:
    cfg = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {
            "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
            "allowed_users": [1],
            "instance": {"name": "Salem"},
            "primary_users": list(primary_users),
            "calibration": {
                "capture_enabled": capture,
                "pending_path": str(tmp_path / "pending.jsonl"),
                "decided_path": str(tmp_path / "decided.jsonl"),
            },
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _person_record(tmp_path: Path, body: str = "- existing line.") -> Path:
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True, exist_ok=True)
    rec = vault / f"{PRIMARY_USER_REL}.md"
    rec.write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n# Andrew Newton\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n"
        f"## Communication Style\n\n{body}\n"
        f"{calibration.CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )
    return rec


def _seed(tmp_path: Path, bullet: str = "Prefers bottom-line-up-front.") -> str:
    rows = calibration_store.record_proposals(
        str(tmp_path / "pending.jsonl"),
        str(tmp_path / "decided.jsonl"),
        [Proposal("Communication Style", bullet, 0.9, "session/A")],
    )
    return rows[0].proposal_id


def _run(config: Path, *argv) -> tuple[int, str]:
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            ns = build_parser().parse_args(
                ["--config", str(config), "voice-calibration", *argv]
            )
            _cmd_voice_calibration(ns)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def test_list_empty_says_it_ran_and_names_the_capture_state(tmp_path: Path) -> None:
    """ILB. An empty list must be distinguishable from a capture door that
    never ran — so the sentinel NAMES which of the two it is."""
    cfg = _cfg_file(tmp_path, capture=False)
    code, out = _run(cfg, "list")
    assert code == 0
    assert "No calibration proposals pending." in out
    assert "Capture is OFF" in out

    code, out = _run(_cfg_file(tmp_path, capture=True), "list")
    assert "Capture is on" in out


def test_list_shows_pending_proposals_and_the_target_record(tmp_path: Path) -> None:
    pid = _seed(tmp_path)
    code, out = _run(_cfg_file(tmp_path), "list")
    assert code == 0
    assert pid in out
    assert "Prefers bottom-line-up-front." in out
    # The operator is told WHERE an approve would write before he approves.
    assert PRIMARY_USER_REL in out


def test_list_json_is_machine_readable(tmp_path: Path) -> None:
    pid = _seed(tmp_path)
    code, out = _run(_cfg_file(tmp_path), "list", "--json")
    assert code == 0
    rows = json.loads(out)
    assert [r["proposal_id"] for r in rows] == [pid]


def test_approve_threads_the_target_record_and_applies(tmp_path: Path) -> None:
    """THE THREADING PIN. The CLI must reach the store WITH the person record.

    ``approve_proposal`` refuses a blank ``user_rel_path``; a CLI that forgot to
    thread it would refuse every approve while every store-level unit test
    stayed green. This drives the real CLI and asserts the bullet landed in the
    real record.
    """
    rec = _person_record(tmp_path)
    pid = _seed(tmp_path)

    code, out = _run(_cfg_file(tmp_path), "approve", pid, "--operator", "andrew")
    assert code == 0, out
    result = json.loads(out)
    assert result["approved"] == pid
    assert result["applied_to"] == PRIMARY_USER_REL
    assert result["operator"] == "andrew"

    body = rec.read_text(encoding="utf-8")
    assert "Prefers bottom-line-up-front." in body
    assert "existing line." in body   # the block was appended to, not replaced


def test_approve_with_no_primary_user_refuses_and_names_the_reason(
    tmp_path: Path,
) -> None:
    """The threading pin's other end: no configured target → a NAMED refusal.

    Pairs with the test above as its positive control — the same proposal, the
    same CLI, differing only in the config field under test.
    """
    rec = _person_record(tmp_path)
    pid = _seed(tmp_path)
    before = rec.read_text(encoding="utf-8")

    code, out = _run(
        _cfg_file(tmp_path, primary_users=()), "approve", pid, "--operator", "andrew"
    )
    assert code == 1
    assert "primary_users" in json.loads(out)["error"]
    assert rec.read_text(encoding="utf-8") == before


def test_approve_without_operator_is_refused_by_argparse(tmp_path: Path) -> None:
    """``--operator`` is REQUIRED at the parser, so the anonymous approve cannot
    even be spelled. The store's own guard is the second layer (unit-tested), not
    the only one."""
    _person_record(tmp_path)
    pid = _seed(tmp_path)
    code, _ = _run(_cfg_file(tmp_path), "approve", pid)
    assert code != 0
    assert calibration_store.decided_ids(str(tmp_path / "decided.jsonl")) == set()


def test_reject_records_the_verdict_and_writes_no_vault(tmp_path: Path) -> None:
    rec = _person_record(tmp_path)
    pid = _seed(tmp_path)
    before = rec.read_text(encoding="utf-8")

    code, out = _run(_cfg_file(tmp_path), "reject", pid, "--operator", "andrew")
    assert code == 0, out
    assert json.loads(out)["rejected"] == pid
    assert rec.read_text(encoding="utf-8") == before
    # And it leaves the review list for good.
    code, out = _run(_cfg_file(tmp_path), "list")
    assert pid not in out


def test_approving_a_record_with_no_calibration_block_refuses_cleanly(
    tmp_path: Path,
) -> None:
    """The defect this lane's own pin caught, driven end-to-end through the CLI.

    ``apply_proposals`` reports success on a record with no marker pair (the
    frontmatter write lands while the body rewriter no-ops), so the first cut
    recorded a decision for a bullet that never landed. The proposal must stay
    PENDING and the operator must be told what to fix.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / f"{PRIMARY_USER_REL}.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n# Andrew Newton\n",
        encoding="utf-8",
    )
    pid = _seed(tmp_path)

    code, out = _run(_cfg_file(tmp_path), "approve", pid, "--operator", "andrew")
    assert code == 1
    assert "no calibration block" in json.loads(out)["error"]
    assert calibration_store.decided_ids(str(tmp_path / "decided.jsonl")) == set()
    # Still reviewable — the observation was not silently lost.
    code, out = _run(_cfg_file(tmp_path), "list")
    assert pid in out
