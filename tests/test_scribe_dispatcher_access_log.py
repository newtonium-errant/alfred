"""Dispatcher access-log pins (event-store design §7.1.2c / §7.1.3 / Q3 / §15.5).

The top-level ``cmd_vault`` dispatcher registers the PHIA s.63 read hook when the LOADED CONFIG
identifies a STAY-C clinical instance (scribe block + mode==clinical + vault path) — NOT the
env-derived scope. Pins:

  * a clinical instance's ``vault read`` emits ``access.read`` (via="cli"), actor="operator" by
    default and the named clinician under ``--as`` (Q3, honest fallback — never fabricated);
  * a NON-clinical (synthetic / no-scribe) instance NEVER registers → no access.read;
  * the one-shot dispatcher leaves NO process-global read hook (cleared in finally).
"""

from __future__ import annotations

import argparse

import pytest
import yaml

from alfred import cli
from alfred.scribe.events import ScribeEvents
from alfred.vault import ops as vault_ops
from alfred.vault.ops import vault_create


@pytest.fixture(autouse=True)
def _env_hygiene(monkeypatch):
    for v in ("ALFRED_VAULT_SCOPE", "ALFRED_VAULT_SESSION", "ALFRED_VAULT_AUDIT_LOG"):
        monkeypatch.delenv(v, raising=False)
    vault_ops.clear_read_hooks()
    yield
    vault_ops.clear_read_hooks()


def _write_cfg(tmp_path, mode):
    body = {"vault": {"path": str(tmp_path / "vault")},
            "logging": {"dir": str(tmp_path / "data")},
            "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": mode}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p)


def _seed_note(tmp_path):
    vault = tmp_path / "vault"
    return vault_create(
        vault, "clinical_note", "Enc view",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": "enc-view00000001", "drafted_by": "stayc_scribe"},
        body="## S\nchest pain\n", scope="stayc_clinical")["path"]


def _store(tmp_path):
    raw = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
           "scribe": {"mode": "clinical", "encounter_salt": "s"}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "data"))


def _read_args(cfg, rel, *, as_clinician=None):
    return argparse.Namespace(config=cfg, vault_cmd="read", path=rel, as_clinician=as_clinician)


def test_clinical_read_logs_access_operator_default(tmp_path, monkeypatch):
    rel = _seed_note(tmp_path)
    monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_path / "vault"))
    cli.cmd_vault(_read_args(_write_cfg(tmp_path, "clinical"), rel))
    rows = _store(tmp_path).query("access", kind="access.read")
    assert len(rows) == 1
    r = rows[0]
    assert r["actor"] == "operator" and r["actor_kind"] == "operator"
    assert r["payload"]["via"] == "cli" and r["payload"]["record_type"] == "clinical_note"
    assert r["subject_id"] == "enc-view00000001"
    assert vault_ops._READ_HOOKS == []  # the one-shot dispatcher left no process-global hook


def test_clinical_read_as_clinician_attributes_named(tmp_path, monkeypatch):
    rel = _seed_note(tmp_path)
    monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_path / "vault"))
    cli.cmd_vault(_read_args(_write_cfg(tmp_path, "clinical"), rel, as_clinician="np_jamie"))
    rows = _store(tmp_path).query("access", kind="access.read")
    assert len(rows) == 1
    assert rows[0]["actor"] == "np_jamie" and rows[0]["actor_kind"] == "clinician"


def test_non_clinical_instance_never_registers(tmp_path, monkeypatch):
    rel = _seed_note(tmp_path)
    monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_path / "vault"))
    cli.cmd_vault(_read_args(_write_cfg(tmp_path, "synthetic"), rel))  # mode != clinical
    assert _store(tmp_path).query("access", kind="access.read") == []
    assert vault_ops._READ_HOOKS == []


def test_no_scribe_block_never_registers(tmp_path, monkeypatch):
    rel = _seed_note(tmp_path)
    monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_path / "vault"))
    body = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    cli.cmd_vault(_read_args(str(p), rel))
    assert _store(tmp_path).query("access", kind="access.read") == []
    assert vault_ops._READ_HOOKS == []
