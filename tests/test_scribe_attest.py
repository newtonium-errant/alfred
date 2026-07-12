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
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create

_CLINICIANS = {"np_jamie", "dr_synthetic"}
_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _scrub_cloud_key_env(monkeypatch):
    """TEST-HYGIENE (CLAUDE.md dispatcher/boundary env-hygiene contract): the
    ``cmd_scribe`` attest-CLI tests call ``validate_sovereign_boundary(raw)`` with
    NO ``env=`` → barrier-c reads LIVE ``os.environ``. A PRIOR test in the full
    suite that leaks a cloud credential (e.g. ANTHROPIC_API_KEY) into
    ``os.environ`` and doesn't scrub it makes barrier-c refuse the attest
    (correct fail-closed production behavior, but env-bleed in tests). Scrub EVERY
    ``CLOUD_KEY_ENV_VARS`` entry at setup — imported (not hardcoded) so it stays in
    lockstep with the boundary — so every attest-CLI test runs cloud-key-free
    regardless of prior-test bleed."""
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _make_ai_draft(tmp_path, *, source_id="enc-abc0123456789d", drafted_by=SCRIBE_DRAFTER_IDENTITY):
    """Create a born-ai_draft clinical_note under the agent scope; return rel_path."""
    result = vault_create(
        tmp_path, "clinical_note", "Synthetic encounter chest pain",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": source_id, "drafted_by": drafted_by,
            # #58 — these tests attest a COMPLETE encounter (they gate on
            # lifecycle/attester); carry the completeness marker so the #58
            # precondition passes and the tests exercise what they target.
            "encounter_completeness": {"protocol": 1, "complete": True},
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


def _scribe_args(config_path, note="clinical_note/x.md", *,
                 force_incomplete=False, reason=None, attester="np_jamie",
                 new_status="attested"):
    import argparse
    return argparse.Namespace(config=config_path, scribe_cmd="attest", note=note,
                              attester=attester, new_status=new_status,
                              force_incomplete=force_incomplete, reason=reason)


def test_attest_cli_installs_guard_before_attest_for_sovereign(tmp_path, monkeypatch):
    # FIX 3 + N1: for a SOVEREIGN instance, cmd_scribe must arm the http guard
    # BEFORE calling scribe_attest. Patch the attest writer to assert the guard is
    # already installed at call time.
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
        "sovereign": {"enabled": True},                    # sovereign → guard arms
        "vault": {"path": str(tmp_path)}, "logging": {"dir": str(tmp_path)},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s",
                   "stt": {"provider": "fake"},            # barrier-a OK (local)
                   "llm": {"base_url": "http://127.0.0.1:11434"}},  # barrier-b OK (loopback)
    })
    try:
        cli.cmd_scribe(_scribe_args(cfg))
        assert seen.get("guard_installed") is True         # guard armed BEFORE attest
    finally:
        uninstall_sovereign_http_guard()


def test_attest_cli_non_sovereign_does_not_install_guard(tmp_path, monkeypatch):
    # FIX N1 (precision): a NON-sovereign instance's attest must NOT monkeypatch the
    # transport — the guard is a sovereign-scope control. The boundary still runs
    # (no-op for non-sovereign), attest still proceeds, but the guard stays OFF.
    import importlib
    from alfred import cli
    attest_mod = importlib.import_module("alfred.scribe.attest")
    from alfred.sovereign import (
        is_sovereign_http_guard_installed,
        uninstall_sovereign_http_guard,
    )
    assert is_sovereign_http_guard_installed() is False

    seen = {}

    def _fake_attest(*a, **k):
        seen["guard_installed"] = is_sovereign_http_guard_installed()
        return {"path": "clinical_note/x.md"}

    monkeypatch.setattr(attest_mod, "attest", _fake_attest)
    cfg = _write_config(tmp_path, {                        # NO sovereign block
        "vault": {"path": str(tmp_path)}, "logging": {"dir": str(tmp_path)},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s"},
    })
    try:
        cli.cmd_scribe(_scribe_args(cfg))
        assert seen.get("guard_installed") is False        # guard NOT armed (non-sovereign)
        assert is_sovereign_http_guard_installed() is False
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


# ---------------------------------------------------------------------------
# #58 D1/D2 — the CLI --force-incomplete override + reason → vault audit
# ---------------------------------------------------------------------------

def _cli_config(tmp_path, *, mode="synthetic"):
    return _write_config(tmp_path, {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": mode,
                   "stt": {"provider": "fake"}, "llm": {"base_url": "http://127.0.0.1:11434"}},
    })


def _markerless_draft(tmp_path, *, source_id="enc-abc0123456789d"):
    from alfred.vault.ops import vault_create
    return vault_create(
        tmp_path / "vault", "clinical_note", f"Synthetic enc {source_id}",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": source_id, "drafted_by": "stayc_scribe"},
        body="## Subjective\nReports chest pain.\n", scope="stayc_clinical",
    )["path"]


def test_cli_force_incomplete_e2e_reason_to_vault_audit_not_attest_audit(tmp_path):
    from alfred import cli
    from alfred.vault.ops import vault_read
    cfg = _cli_config(tmp_path)
    rel = _markerless_draft(tmp_path)
    reason = "recorder died mid-visit"
    cli.cmd_scribe(_scribe_args(cfg, note=rel, force_incomplete=True, reason=reason))

    # attested.
    assert vault_read(tmp_path / "vault", rel)["frontmatter"]["status"] == "attested"
    data = tmp_path / "data"
    attest_audit = (data / "clinical_attest_audit.jsonl").read_text()
    vault_audit = (data / "vault_audit.log").read_text()
    # the free-text reason is in the VAULT audit, NOT the (PHI-free) attest audit.
    assert reason in vault_audit and reason not in attest_audit
    aa = json.loads(attest_audit.strip())
    assert aa["forced"] is True and aa["completeness"] == "absent"
    assert "reason" not in aa and "override_reason" not in aa       # PHI-free pin


def test_cli_force_incomplete_without_reason_refuses_non_zero_exit(tmp_path):
    from alfred import cli
    from alfred.vault.ops import vault_read
    cfg = _cli_config(tmp_path)
    rel = _markerless_draft(tmp_path)
    for bad in (None, "", "   "):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_scribe(_scribe_args(cfg, note=rel, force_incomplete=True, reason=bad))
        assert exc.value.code == 1
    # never attested, no triad.
    assert vault_read(tmp_path / "vault", rel)["frontmatter"]["status"] == "ai_draft"


def test_cli_strict_refuse_without_force(tmp_path):
    # No --force-incomplete on a markerless note → strict refuse (encounter_incomplete).
    from alfred import cli
    from alfred.vault.ops import vault_read
    cfg = _cli_config(tmp_path)
    rel = _markerless_draft(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_scribe(_scribe_args(cfg, note=rel))
    assert exc.value.code == 1
    assert vault_read(tmp_path / "vault", rel)["frontmatter"]["status"] == "ai_draft"


def test_cli_force_incomplete_works_in_clinical_mode(tmp_path):
    # Q1 — the flag is NOT mode-gated; it works in clinical mode too.
    from alfred import cli
    from alfred.vault.ops import vault_read
    cfg = _cli_config(tmp_path, mode="clinical")
    rel = _markerless_draft(tmp_path)
    cli.cmd_scribe(_scribe_args(cfg, note=rel, force_incomplete=True, reason="clinical override"))
    assert vault_read(tmp_path / "vault", rel)["frontmatter"]["status"] == "attested"
    assert "clinical override" in (tmp_path / "data" / "vault_audit.log").read_text()


@pytest.mark.parametrize("phi_reason", [
    "patient John Doe DOB 1980-01-01 coded before signature",
    "MRN 4432211 — device failed",
])
def test_cli_attest_audit_never_contains_reason_phi_free_invariant(tmp_path, phi_reason):
    # PHI-FREE INVARIANT: whatever the --reason text, the clinical_attest_audit.jsonl
    # NEVER carries it — it lands ONLY in the vault audit.
    from alfred import cli
    cfg = _cli_config(tmp_path)
    rel = _markerless_draft(tmp_path)
    cli.cmd_scribe(_scribe_args(cfg, note=rel, force_incomplete=True, reason=phi_reason))
    data = tmp_path / "data"
    assert phi_reason not in (data / "clinical_attest_audit.jsonl").read_text()   # PHI-free
    # the reason IS in the vault audit — decode the JSONL (on-disk JSON escapes
    # non-ASCII like the em-dash, so check the decoded ``detail`` value).
    va_details = [json.loads(ln)["detail"]
                  for ln in (data / "vault_audit.log").read_text().splitlines() if ln.strip()]
    assert any(phi_reason in d for d in va_details)
