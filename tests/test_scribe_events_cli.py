"""CLI query-surface pins (event-store design §10 / §15.5): alfred scribe events|audit.

  events list — tolerant filtered JSON + ILB empty ('no events match');
  events tip — {stream, seq, entry_sha};
  events verify — ok true appends store.verified; a mid-file tamper → ok false + exit 1;
                  --rebuild-index; --deep reports post-attest edits (REPORT-only, no emit);
  events anchor — off-box export;
  audit encounter — the cross-family timeline;
  no `emit` verb exists.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import frontmatter
import pytest
import structlog
import yaml

from alfred import cli
from alfred.scribe.attest import attest
from alfred.scribe.events import ScribeEvents
from alfred.vault.ops import vault_create

_CLOCK = "2026-07-16T12:00:00+00:00"
_CLINICIANS = {"np_jamie"}


@pytest.fixture(autouse=True)
def _capture_structlog():
    """Keep the store's ``scribe.events.appended`` structlog OFF stdout so the CLI's JSON
    ``print`` is the sole stdout content (capsys parses it clean)."""
    with structlog.testing.capture_logs():
        yield


def _write_cfg(tmp_path):
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p)


def _seed(tmp_path, clock=_CLOCK):
    raw = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
           "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical"}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "data"), clock=lambda: clock)


def _ns(config, events_cmd=None, audit_cmd=None, **kw):
    d = dict(config=config, scribe_cmd="events", events_cmd=events_cmd,
             audit_cmd=audit_cmd, stream="clinical", family=None, kind=None,
             encounter=None, actor=None, since=None, until=None, path=None,
             limit=None, deep=False, rebuild_index=False)
    d.update(kw)
    return argparse.Namespace(**d)


def _stdout_json(capsys):
    out = capsys.readouterr().out
    return json.loads(out)


# --- list -------------------------------------------------------------------

def test_events_list_returns_rows_and_ilb_empty(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.note_draft_created(subject_id="enc-1", body_sha="a")
    ev.encounter_opened(subject_id="enc-1")

    cli.cmd_scribe(_ns(cfg, "list"))
    rows = _stdout_json(capsys)
    kinds = {r["kind"] for r in rows}
    assert {"note.draft_created", "encounter.opened"} <= kinds

    # a filter with no match → [] + the ILB stderr line.
    cli.cmd_scribe(_ns(cfg, "list", kind="note.ready"))
    cap = capsys.readouterr()
    assert json.loads(cap.out) == []
    assert "no events match" in cap.err


# --- tip --------------------------------------------------------------------

def test_events_tip(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.note_draft_created(subject_id="enc-1", body_sha="a")
    cli.cmd_scribe(_ns(cfg, "tip"))
    tip = _stdout_json(capsys)
    assert tip["stream"] == "clinical" and tip["seq"] >= 2 and len(tip["entry_sha"]) == 64


# --- verify -----------------------------------------------------------------

def test_events_verify_ok_appends_store_verified(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.note_draft_created(subject_id="enc-1", body_sha="a")
    cli.cmd_scribe(_ns(cfg, "verify"))
    out = _stdout_json(capsys)
    assert out["ok"] is True
    # store.verified was appended on success (chain-answerable "when did you last verify").
    assert len(ev.query("clinical", kind="store.verified")) == 1


def test_events_verify_tamper_exits_1(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.note_draft_created(subject_id="enc-1", body_sha="a")
    ev.note_draft_created(subject_id="enc-2", body_sha="b")
    # flip a byte mid-file (NOT the final line) → a continuity break.
    jsonl = tmp_path / "data" / "events" / "clinical.jsonl"
    lines = jsonl.read_text().splitlines()
    lines[1] = lines[1].replace('"a"', '"Z"', 1) if '"a"' in lines[1] else lines[1] + " "
    jsonl.write_text("\n".join(lines) + "\n")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_scribe(_ns(cfg, "verify"))
    assert exc.value.code == 1
    assert _stdout_json(capsys)["ok"] is False


def test_events_verify_rebuild_index(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.attest_recorded(subject_id="enc-9", attester="np_jamie", from_status="d",
                       to_status="a", creator="c", forced=False, completeness="complete",
                       body_sha="cafe", grounding_flag_count=0, grounding_reasons=[])
    ev._atomic_write_index({})  # drop the index
    cli.cmd_scribe(_ns(cfg, "verify", rebuild_index=True))
    out = capsys.readouterr().out
    assert '"rebuilt_index_entries": 1' in out


# --- verify --deep (post-attest-edit report) --------------------------------

def test_events_verify_deep_reports_post_attest_edit(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    vault = tmp_path / "vault"
    rel = vault_create(
        vault, "clinical_note", "Enc deep",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": "enc-deep0000001", "drafted_by": "stayc_scribe",
                    "encounter_completeness": {"protocol": 1, "complete": True}},
        body="## S\nchest pain\n", scope="stayc_clinical")["path"]
    attest(vault, rel, new_status="attested", attester="np_jamie", clinician_ids=_CLINICIANS,
           audit_path=tmp_path / "a.jsonl", now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
           events=ev)
    # edit the SIGNED note body out-of-band.
    post = frontmatter.load(str(vault / rel))
    post.content += "\nSNEAKY EDIT"
    (vault / rel).write_text(frontmatter.dumps(post), encoding="utf-8")

    cli.cmd_scribe(_ns(cfg, "verify", deep=True))
    out = _stdout_json(capsys)
    assert out["ok"] is True
    assert len(out["post_attest_edits"]) == 1
    assert out["post_attest_edits"][0]["subject_id"] == "enc-deep0000001"
    # REPORT-only: --deep must NOT emit a note.post_attest_edit_detected event.
    assert ev.query("clinical", kind="note.post_attest_edit_detected") == []


# --- anchor -----------------------------------------------------------------

def test_events_anchor_exports(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.note_draft_created(subject_id="enc-1", body_sha="a")
    cli.cmd_scribe(_ns(cfg, "anchor"))
    rec = _stdout_json(capsys)
    assert rec["stream"] == "clinical" and "head_sha" in rec
    assert (tmp_path / "data" / "events" / "anchors").is_dir()


# --- audit encounter --------------------------------------------------------

def test_audit_encounter_timeline(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    ev.encounter_opened(subject_id="enc-x")
    ev.access_read(subject_id="enc-x", record_type="clinical_note", status="attested",
                   path_digest="pd", via="cli", actor="op", actor_kind="operator")
    args = argparse.Namespace(config=cfg, scribe_cmd="audit", audit_cmd="encounter",
                              encounter="enc-x")
    cli.cmd_scribe(args)
    rows = _stdout_json(capsys)
    kinds = {r["kind"] for r in rows}
    assert {"encounter.opened", "access.read"} <= kinds  # cross-stream merge
    assert all(r["subject_id"] == "enc-x" for r in rows)


def test_no_emit_verb(tmp_path):
    # There is DELIBERATELY no `events emit` — the facade has no emit either (§2.2).
    assert not hasattr(ScribeEvents, "emit")


def test_verify_deep_with_rebuild_index_still_reports_edit(tmp_path, capsys):
    # R-A regression (the false all-clear): `verify --deep --rebuild-index` runs rebuild FIRST
    # (wiping rel_path to "") then the deep scan — which used to `continue` and report [] regardless
    # of on-disk tampering. The scan's source_id re-derivation must keep the AG-Rec-6 query truthful.
    cfg = _write_cfg(tmp_path)
    ev = _seed(tmp_path)
    vault = tmp_path / "vault"
    rel = vault_create(
        vault, "clinical_note", "Enc rebuild-deep",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": "enc-cli-rebuild01", "drafted_by": "stayc_scribe",
                    "encounter_completeness": {"protocol": 1, "complete": True}},
        body="## S\nchest pain\n", scope="stayc_clinical")["path"]
    attest(vault, rel, new_status="attested", attester="np_jamie", clinician_ids=_CLINICIANS,
           audit_path=tmp_path / "a.jsonl", now=datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
           events=ev)
    post = frontmatter.load(str(vault / rel))
    post.content += "\nSNEAKY EDIT AFTER SIGNATURE"
    (vault / rel).write_text(frontmatter.dumps(post), encoding="utf-8")

    cli.cmd_scribe(_ns(cfg, "verify", deep=True, rebuild_index=True))
    # --rebuild-index prints its own {"rebuilt_index_entries": N} blob first, then the report.
    text = capsys.readouterr().out.strip()
    _, idx = json.JSONDecoder().raw_decode(text)   # the rebuild blob
    out = json.loads(text[idx:].strip())           # the verify report
    assert out["ok"] is True
    assert len(out["post_attest_edits"]) == 1  # NOT a false all-clear despite the rebuild
    assert out["post_attest_edits"][0]["subject_id"] == "enc-cli-rebuild01"
