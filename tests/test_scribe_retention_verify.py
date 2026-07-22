"""STAY-C retention VERIFY — §12 integrity report (slice 13d-2).

verify reports, fail-closed on the inconsistency classes: incomplete destructions
(intent-without-destroyed), blob-without-sidecar / sidecar-without-blob orphans, a dangling schedule
pin (chain-pinned schedule absent/sha-mismatched on disk), and over-window-due (informational — does
NOT fail the exit). ILB explicit 'nothing to report' when clean. JSON output like the other verbs.
"""
from __future__ import annotations

import argparse
import json

import pytest
import structlog
import yaml

from alfred import cli
from alfred.scribe import schedule as sched_mod
from alfred.scribe.events import ScribeEvents


@pytest.fixture(autouse=True)
def _capture_structlog():
    with structlog.testing.capture_logs():
        yield


def _cfg_and_ev(tmp_path, *, schedule_path=None):
    retention = {"retained_dir": str(tmp_path / "retained")}
    if schedule_path is not None:
        retention["schedule_path"] = str(schedule_path)
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical",
            "events": {"dir": str(tmp_path / "ev")}, "retention": retention,
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "data"))
    return str(p), ev


def _verify_ns(config):
    return argparse.Namespace(config=config, scribe_cmd="retention", retention_cmd="verify")


def _run(cfg_path, capsys):
    exited = None
    try:
        cli._cmd_scribe_retention(_verify_ns(cfg_path))
    except SystemExit as e:
        exited = e.code
    return json.loads(capsys.readouterr().out), exited


def test_verify_clean_reports_nothing_and_exits_zero(tmp_path, capsys):
    cfg_path, _ev = _cfg_and_ev(tmp_path)
    (tmp_path / "retained").mkdir(parents=True, exist_ok=True)
    out, exited = _run(cfg_path, capsys)
    assert exited is None                                    # clean → exit 0
    assert out["incomplete_destructions"] == []
    assert out["blob_without_sidecar"] == [] and out["sidecar_without_blob"] == []
    assert out["dangling_schedule_pin"] is None
    assert out["over_window_due"] == 0 and out["inconsistent"] is False


def test_verify_flags_incomplete_destruction(tmp_path, capsys):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    # a destroy_intent with NO matching destroyed = a crash between the two phases.
    ev.retention_destroy_intent(subject_id="enc-crashed", schedule_version="v1", manifest_sha256="abc")
    out, exited = _run(cfg_path, capsys)
    assert exited == 1                                       # inconsistency → fail-closed
    assert out["incomplete_destructions"] == ["enc-crashed"]
    assert out["inconsistent"] is True


def test_verify_completed_destruction_is_not_flagged(tmp_path, capsys):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    ev.retention_destroy_intent(subject_id="enc-done", schedule_version="v1", manifest_sha256="abc")
    ev.retention_destroyed(subject_id="enc-done", schedule_version="v1", manifest_sha256="abc")
    out, exited = _run(cfg_path, capsys)
    assert out["incomplete_destructions"] == [] and exited is None


def test_verify_flags_blob_without_sidecar(tmp_path, capsys):
    cfg_path, _ev = _cfg_and_ev(tmp_path)
    retained = tmp_path / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    (retained / "enc-orphanblob.age").write_bytes(b"age-ciphertext")   # blob, no sidecar
    out, exited = _run(cfg_path, capsys)
    assert out["blob_without_sidecar"] == ["enc-orphanblob"]
    assert out["sidecar_without_blob"] == [] and exited == 1


def test_verify_flags_sidecar_without_blob(tmp_path, capsys):
    cfg_path, _ev = _cfg_and_ev(tmp_path)
    retained = tmp_path / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    (retained / "enc-orphansidecar.manifest.json").write_text("{}", encoding="utf-8")
    out, exited = _run(cfg_path, capsys)
    assert out["sidecar_without_blob"] == ["enc-orphansidecar"]
    assert out["blob_without_sidecar"] == [] and exited == 1


def test_verify_flags_dangling_schedule_pin(tmp_path, capsys):
    sched_path = tmp_path / "seal" / "schedule.json"
    cfg_path, ev = _cfg_and_ev(tmp_path, schedule_path=sched_path)
    (tmp_path / "retained").mkdir(parents=True, exist_ok=True)
    # Chain-pin a schedule but leave NOTHING on disk at schedule_path → dangling.
    ev.retention_schedule_published(schedule_version="v1", schedule_sha256="a" * 64,
                                    effective_date="2026-07-19")
    out, exited = _run(cfg_path, capsys)
    assert out["dangling_schedule_pin"] is not None
    assert "absent" in out["dangling_schedule_pin"]["reason"]
    assert exited == 1


def test_verify_over_window_due_is_informational_not_failing(tmp_path, capsys, monkeypatch):
    sched_path = tmp_path / "seal" / "schedule.json"
    cfg_path, ev = _cfg_and_ev(tmp_path, schedule_path=sched_path)
    retained = tmp_path / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    # Publish a schedule on disk (matching a chain pin so it's not dangling) with a 10yr window.
    data = sched_mod.default_schedule_v1()
    published = sched_mod.publish_schedule(sched_path, data)
    ev.retention_schedule_published(**published)
    # A sealed blob whose mtime is ~11 years old (no chain ts → mtime fallback) → over-window.
    blob = retained / "enc-ancient.age"
    blob.write_bytes(b"age-ct")
    (retained / "enc-ancient.manifest.json").write_text("{}", encoding="utf-8")  # paired (no orphan)
    import os
    import time
    old = time.time() - 11 * 365 * 86400
    os.utime(blob, (old, old))
    out, exited = _run(cfg_path, capsys)
    assert out["over_window_due"] == 1 and out["oldest_over_window"] == "enc-ancient"
    assert out["over_window_evaluated"] is True
    # Over-window alone is a normal review signal — it does NOT fail the exit (no other inconsistency).
    assert out["inconsistent"] is False and exited is None
