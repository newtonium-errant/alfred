"""Tests for the structural attestation orchestrator (scribe P2-a, #41).

The ONLY sanctioned path to flip a clinical_note's attest triad. These pins
prove the primitives are now STRUCTURAL: the orchestrator enforces
authorize_attestation, writes under the privileged scope, and appends a durable
PHI-free audit — while the agent scope can never raw-flip the triad (pinned in
test_stayc_clinical_scope.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import frontmatter
import pytest

from alfred.scribe import SCRIBE_DRAFTER_IDENTITY, source_id_for
from alfred.scribe.attest import attest
from alfred.scribe.attestation import AttestationError
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie", "dr_synthetic"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def _make_ai_draft(tmp_path, *, source_id="enc-abc0123456789d", drafted_by=SCRIBE_DRAFTER_IDENTITY):
    """Create a born-ai_draft clinical_note under the agent scope; return rel_path."""
    result = vault_create(
        tmp_path, "clinical_note", "Synthetic encounter chest pain",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": source_id, "drafted_by": drafted_by,
        },
        body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope="stayc_clinical",
    )
    return result["path"]


# ---------------------------------------------------------------------------
# source_id — SALTED opaque id in EVERY mode (P3-b1 leak fix). The P2 verbatim-
# in-synthetic + "sha256:" colon-prefix behaviors are GONE.
# ---------------------------------------------------------------------------

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def test_source_id_is_salted_opaque_no_label_leak():
    sid = source_id_for("patient_jane_doe_2026-07-09.wav", salt=_SALT)
    assert sid.startswith("enc-")              # colon-free (no "sha256:" filename bug)
    assert "jane" not in sid and "doe" not in sid  # NO PHI-bearing label leaks
    assert len(sid) == len("enc-") + 16


def test_source_id_no_verbatim_leak_in_any_mode():
    # The P2 leak: synthetic mode returned the label VERBATIM. Now it is opaque.
    assert source_id_for("synth1.wav", salt=_SALT) != "synth1.wav"
    assert source_id_for("synth1.wav", salt=_SALT).startswith("enc-")


def test_source_id_deterministic_and_salt_sensitive():
    # Same label + same salt → same id (stable across an encounter's chunks);
    # different salt → different id (the salt is what makes it non-reversible).
    a = source_id_for("enc-label", salt=_SALT)
    assert a == source_id_for("enc-label", salt=_SALT)
    assert a != source_id_for("enc-label", salt="DIFFERENT_SALT")


def test_source_id_fail_loud_on_missing_salt():
    from alfred.scribe import EncounterIdentityError
    with pytest.raises(EncounterIdentityError):
        source_id_for("synth1.wav", salt="")


# ---------------------------------------------------------------------------
# Happy path — distinct clinician attests an ai_draft
# ---------------------------------------------------------------------------

def test_attest_happy_path_writes_triad_and_audit(tmp_path):
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    result = attest(
        tmp_path, rel, new_status="attested", attester="np_jamie",
        clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW,
    )
    assert result["path"] == rel
    post = frontmatter.load(str(tmp_path / rel))
    assert post["status"] == "attested"
    assert post["attested_by"] == "np_jamie"
    assert post["attested_at"] == _NOW.isoformat()
    # body unchanged (frozen)
    assert "chest pain" in post.content
    # durable audit written
    assert audit.exists()
    entry = json.loads(audit.read_text().strip())
    assert entry["op"] == "attest"
    assert entry["attester"] == "np_jamie"
    assert entry["from_status"] == "ai_draft"
    assert entry["to_status"] == "attested"


def test_attest_audit_is_phi_free(tmp_path):
    rel = _make_ai_draft(tmp_path, source_id="enc-deadbeefdeadbe")
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    raw = audit.read_text()
    # The audit carries the OPAQUE source_id (the salted enc- id), clinician id,
    # statuses — NEVER the note body / transcript / patient content.
    assert "enc-deadbeefdeadbe" in raw
    assert "chest pain" not in raw
    assert "Subjective" not in raw


# ---------------------------------------------------------------------------
# Self-attest refused — even via the orchestrator (defense-in-depth)
# ---------------------------------------------------------------------------

def test_scribe_self_attest_refused_via_orchestrator(tmp_path):
    # THE self-attest pin (mutation: allow self-attest => fails). The scribe
    # drafter cannot attest its own draft even by calling the orchestrator —
    # authorize_attestation refuses the drafter identity.
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested",
               attester=SCRIBE_DRAFTER_IDENTITY,
               clinician_ids=_CLINICIANS | {SCRIBE_DRAFTER_IDENTITY},
               audit_path=audit, now=_NOW)
    assert exc.value.reason == "scribe_self_attest"
    # note untouched, no audit line
    assert frontmatter.load(str(tmp_path / rel))["status"] == "ai_draft"
    assert not audit.exists()


def test_creator_self_attest_refused(tmp_path):
    # A clinician who was ALSO the drafter (creator) can't self-attest.
    rel = _make_ai_draft(tmp_path, drafted_by="np_jamie")
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "self_attest"


# ---------------------------------------------------------------------------
# Fail-closed refusals
# ---------------------------------------------------------------------------

def test_empty_clinicians_no_valid_attester(tmp_path):
    rel = _make_ai_draft(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="np_jamie",
               clinician_ids=set(), audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "attester_not_clinician"


def test_non_clinician_attester_refused(tmp_path):
    rel = _make_ai_draft(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="attested", attester="random_user",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "attester_not_clinician"


def test_forward_only_no_un_attest_via_orchestrator(tmp_path):
    # Attest to 'attested', then a revert to 'ai_draft' is refused (forward-only).
    rel = _make_ai_draft(tmp_path)
    audit = tmp_path / "clinical_attest_audit.jsonl"
    attest(tmp_path, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, rel, new_status="ai_draft", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=audit, now=_NOW)
    assert exc.value.reason == "illegal_status_transition"


def test_attest_refuses_non_clinical_note(tmp_path):
    # A non-clinical_note record can never be attested (unscoped create of a note).
    result = vault_create(tmp_path, "note", "Just a note", set_fields={}, scope=None)
    with pytest.raises(AttestationError) as exc:
        attest(tmp_path, result["path"], new_status="attested", attester="np_jamie",
               clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl", now=_NOW)
    assert exc.value.reason == "not_clinical_note"


# ---------------------------------------------------------------------------
# Audit FIX 3 — the attest CLI (cmd_scribe) runs behind the sovereign boundary
# + arms the http guard (daemon.py:76-79 parity), a privileged PHI writer
# ---------------------------------------------------------------------------

def _write_config(tmp_path, body: dict) -> str:
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p)


def _scribe_args(config_path, note="clinical_note/x.md"):
    import argparse
    return argparse.Namespace(config=config_path, scribe_cmd="attest", note=note,
                              attester="np_jamie", new_status="attested")


def test_attest_cli_installs_guard_before_attest(tmp_path, monkeypatch):
    # FIX 3: cmd_scribe must arm the http guard BEFORE calling scribe_attest. Patch
    # the attest writer to assert the guard is already installed at call time.
    import importlib
    from alfred import cli
    # The SUBMODULE (importlib.import_module returns sys.modules[...] — the package
    # __init__'s `from .attest import attest` shadows the plain attribute).
    attest_mod = importlib.import_module("alfred.scribe.attest")
    from alfred.sovereign import is_sovereign_http_guard_installed, uninstall_sovereign_http_guard

    seen = {}

    def _fake_attest(*a, **k):
        seen["guard_installed"] = is_sovereign_http_guard_installed()
        return {"path": k.get("note") or "clinical_note/x.md"}

    monkeypatch.setattr(attest_mod, "attest", _fake_attest)
    cfg = _write_config(tmp_path, {
        "vault": {"path": str(tmp_path)}, "logging": {"dir": str(tmp_path)},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s"},
    })
    try:
        cli.cmd_scribe(_scribe_args(cfg))
        assert seen.get("guard_installed") is True         # guard armed BEFORE attest
    finally:
        uninstall_sovereign_http_guard()


def test_attest_cli_refuses_on_sovereign_boundary_breach(tmp_path, monkeypatch):
    # FIX 3: a sovereign config with a boundary breach (cloud STT — barrier-a) must
    # REFUSE the attest at the CLI (fail-closed), BEFORE any attest write.
    import importlib
    from alfred import cli
    attest_mod = importlib.import_module("alfred.scribe.attest")
    from alfred.sovereign import uninstall_sovereign_http_guard

    called = {"attest": False}
    monkeypatch.setattr(attest_mod, "attest", lambda *a, **k: called.__setitem__("attest", True))
    cfg = _write_config(tmp_path, {
        "sovereign": {"enabled": True},
        "vault": {"path": str(tmp_path)}, "logging": {"dir": str(tmp_path)},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s",
                   "stt": {"provider": "groq"},                    # barrier-a breach
                   "llm": {"base_url": "http://127.0.0.1:11434"}},
    })
    try:
        with pytest.raises(SystemExit) as exc:
            cli.cmd_scribe(_scribe_args(cfg))
        assert exc.value.code == 1
        assert called["attest"] is False                   # refused BEFORE the write
    finally:
        uninstall_sovereign_http_guard()
