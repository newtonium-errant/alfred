"""CLI pins for ``alfred scribe retention schedule {publish|show}`` (task #13 slice 13c, design §4).

publish — validate → DURABLE sha pin [D] BEFORE the write (fail-closed: a store-down publish writes
          NOTHING) → atomic write of the exact pinned bytes; malformed → refuse, nothing published.
show    — present schedule + on-disk-vs-chain-pin drift; ILB explicit empty when none published.
"""
from __future__ import annotations

import argparse
import json

import pytest
import structlog
import yaml

from alfred import cli
from alfred.evstore import sha256_hex
from alfred.scribe import schedule as sched_mod
from alfred.scribe.events import CLINICAL, ScribeEvents


@pytest.fixture(autouse=True)
def _capture_structlog():
    # keep the store's append structlog OFF stdout so the CLI JSON print is the sole stdout content.
    with structlog.testing.capture_logs():
        yield


def _write_cfg(tmp_path, *, mode="clinical"):
    sched_path = tmp_path / "seal" / "retention_schedule.json"
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "clinicians": ["np_jamie"], "encounter_salt": "s", "mode": mode,
            "retention": {"schedule_path": str(sched_path)},
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p), sched_path


def _events(tmp_path, *, mode="clinical"):
    raw = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
           "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": mode}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "data"))


def _ns(config, schedule_cmd, file=None):
    return argparse.Namespace(
        config=config, scribe_cmd="retention", retention_cmd="schedule",
        schedule_cmd=schedule_cmd, file=file)


def _write_schedule_file(tmp_path, data=None, name="src_schedule.json"):
    data = data if data is not None else sched_mod.default_schedule_v1()
    p = tmp_path / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


def _stdout_json(capsys):
    return json.loads(capsys.readouterr().out)


# ============================ publish ============================


def test_publish_pins_sha_then_writes_exact_bytes(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    src = _write_schedule_file(tmp_path)

    cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))

    out = _stdout_json(capsys)
    assert out["published"] is True and out["schedule_version"] == "v1"
    # the file was written with EXACTLY the canonical bytes, and the reported sha is over them
    on_disk = sched_path.read_bytes()
    assert on_disk == sched_mod.canonical_schedule_bytes(sched_mod.default_schedule_v1())
    assert out["schedule_sha256"] == sha256_hex(on_disk)
    # a durable retention.schedule_published [D] row landed pinning that sha
    row = _events(tmp_path).latest(CLINICAL, family="retention", kind="retention.schedule_published")
    assert row is not None
    assert row["payload"]["schedule_sha256"] == out["schedule_sha256"]
    assert row["payload"]["schedule_version"] == "v1"
    assert row["payload"]["effective_date"] == "2026-07-19"


def test_publish_refuses_malformed_writes_nothing(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    bad = sched_mod.default_schedule_v1()
    del bad["classes"]["consent_events"]                   # malformed
    src = _write_schedule_file(tmp_path, bad)

    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))

    assert "error" in _stdout_json(capsys)
    assert not sched_path.exists()                          # nothing published (fail-closed)
    assert _events(tmp_path).latest(
        CLINICAL, family="retention", kind="retention.schedule_published") is None


def test_publish_fail_closed_when_pin_raises(tmp_path, capsys, monkeypatch):
    # the durable-before-act ordering: if the retention.schedule_published [D] pin RAISES (store down),
    # NOTHING is written to disk — never an unpinned schedule file.
    from alfred.evstore import EventStoreError
    cfg, sched_path = _write_cfg(tmp_path)
    src = _write_schedule_file(tmp_path)

    def _boom(self, **kw):
        raise EventStoreError("store down at retention.schedule_published")

    monkeypatch.setattr(ScribeEvents, "retention_schedule_published", _boom)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))

    assert "error" in _stdout_json(capsys)
    assert not sched_path.exists()                          # pin raised BEFORE the write → nothing published


def test_publish_refuses_unreadable_source(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_ns(cfg, "publish", file=str(tmp_path / "nope.json")))
    assert "error" in _stdout_json(capsys)
    assert not sched_path.exists()


# ============================ show ============================


def test_show_reports_present_and_pin_matches(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    src = _write_schedule_file(tmp_path)
    cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))
    capsys.readouterr()                                    # drain the publish output

    cli._cmd_scribe_retention(_ns(cfg, "show"))

    out = _stdout_json(capsys)
    assert out["schedule_present"] is True
    assert out["schedule_version"] == "v1"
    assert out["pin_matches"] is True                      # on-disk bytes match the chain-pinned sha
    assert set(out["classes"]) == set(sched_mod.SCHEDULE_CLASSES)


def test_show_ilb_when_absent(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    cli._cmd_scribe_retention(_ns(cfg, "show"))
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["schedule_present"] is False
    assert "no valid schedule" in out.err                  # ILB explicit empty


def test_show_detects_on_disk_drift(tmp_path, capsys):
    cfg, sched_path = _write_cfg(tmp_path)
    src = _write_schedule_file(tmp_path)
    cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))
    capsys.readouterr()
    # hand-edit the published file WITHOUT re-publishing → its bytes no longer match the chain pin
    tampered = json.loads(sched_path.read_text())
    tampered["effective_date"] = "2099-01-01"
    sched_path.write_text(json.dumps(tampered, indent=2) + "\n", encoding="utf-8")

    cli._cmd_scribe_retention(_ns(cfg, "show"))

    out = _stdout_json(capsys)
    assert out["schedule_present"] is True
    assert out["pin_matches"] is False                     # drift surfaced


def test_unset_schedule_path_errors(tmp_path, capsys):
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_ns(str(p), "show"))
    assert "unset" in json.dumps(_stdout_json(capsys))
