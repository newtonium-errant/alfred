"""STAY-C retention DESTROY — §5.2 two-phase s.49 secure destruction (slice 13d-3).

The heaviest, most safety-critical slice: IRREVERSIBLE PHI destruction. Pins the crash-safe two-phase
order (intent [D] before ANY unlink), the purge-gates-destroyed contract (an incomplete backup purge
BLOCKS retention.destroyed — a destruction leaving a copy is not "destroyed"), crash-mid-destroy →
verify flags → re-run completes, withdrawal-never-triggers, dry-run mutates nothing, the note-location
helper, and the surviving PHI-free chain (audit_encounter ends in retention.destroyed).

restic is mocked (backup.purge_encounter monkeypatched — no live repo).
"""
from __future__ import annotations

import argparse
import json

import pytest
import structlog
import yaml

from alfred import cli
from alfred.scribe import backup as backup_mod
from alfred.scribe import retention as ret
from alfred.scribe.backup import PurgeResult
from alfred.scribe.events import CLINICAL, ScribeEvents

_ENC = "enc-dddd3333eeee4444"
_FAKE_PUB = b"age1fakerecipientfordestroytests"


@pytest.fixture(autouse=True)
def _capture_structlog():
    with structlog.testing.capture_logs():
        yield


class _FakeSealer:
    cipher = "fake-xor-test"

    def seal(self, plaintext, recipient_public_key):
        return b"FAKESEAL1" + bytes([len(recipient_public_key)]) + recipient_public_key + plaintext

    def verify_wellformed(self, blob):
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob, private_key):
        n = blob[9]
        return blob[10 + n:]


def _cfg_and_ev(tmp_path):
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical",
            "events": {"dir": str(tmp_path / "ev")},
            "retention": {"retained_dir": str(tmp_path / "retained")},
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "data"))
    return str(p), ev


def _seal(tmp_path, ev, *, enc=_ENC):
    enc_dir = tmp_path / "inbox" / "jane-doe-2026-07-19"
    enc_dir.mkdir(parents=True, exist_ok=True)
    for seq in (1, 2):
        (enc_dir / f"chunk_{seq}.webm").write_bytes(f"audio-{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text("{}", encoding="utf-8")
    (enc_dir / "_CLOSED").write_text("{}", encoding="utf-8")
    (enc_dir / f"{enc}.transcript.json").write_text("{}", encoding="utf-8")
    retained = tmp_path / "retained"
    out = ret.seal_encounter(enc_dir, enc, events=ev, sealer=_FakeSealer(),
                             recipient_public_key=_FAKE_PUB, retained_dir=retained)
    assert out.status == ret.SEAL_STATUS_SEALED
    return retained


def _make_note(tmp_path, enc, *, source_id=None):
    note_dir = tmp_path / "vault" / "clinical_note"
    note_dir.mkdir(parents=True, exist_ok=True)
    path = note_dir / f"note-{source_id or enc}.md"
    path.write_text(
        f"---\ntitle: Encounter note\ntype: clinical_note\nstatus: attested\n"
        f"source_id: {source_id or enc}\n---\n\nSOAP body (PHI).\n", encoding="utf-8")
    return path


def _destroy_ns(config, encounter, *, reason="patient_request", ticket="TCK-1",
                justification=None, dry_run=False, yes=True):
    return argparse.Namespace(
        config=config, scribe_cmd="retention", retention_cmd="destroy", encounter=encounter,
        reason=reason, ticket=ticket, justification=justification, dry_run=dry_run, yes=yes)


def _mock_purge(monkeypatch, *, complete=True, reason=""):
    calls = []

    def _stub(cfg, encounter_id, *, dry_run=False):
        calls.append((encounter_id, dry_run))
        return PurgeResult(complete=complete, encounter_id=encounter_id, dry_run=dry_run, reason=reason)

    monkeypatch.setattr(backup_mod, "purge_encounter", _stub)
    return calls


def _run(cfg_path, ns_kwargs_enc, capsys, **kw):
    exited = None
    try:
        cli._cmd_scribe_retention(_destroy_ns(cfg_path, ns_kwargs_enc, **kw))
    except SystemExit as e:
        exited = e.code
    return json.loads(capsys.readouterr().out), exited


# ============================ note-location helper ============================


def test_resolve_note_paths_matches_by_source_id(tmp_path):
    _make_note(tmp_path, _ENC)                                        # matches
    _make_note(tmp_path, "enc-other", source_id="enc-other")          # different source_id
    matches, malformed = ret.resolve_note_paths(tmp_path / "vault", _ENC)
    assert [p.name for p in matches] == [f"note-{_ENC}.md"] and malformed == []


def test_resolve_note_paths_collects_malformed_note(tmp_path):
    _make_note(tmp_path, _ENC)
    broken = tmp_path / "vault" / "clinical_note" / "broken.md"
    broken.write_text("---\nnot: [valid: yaml\n---\nbody", encoding="utf-8")
    matches, malformed = ret.resolve_note_paths(tmp_path / "vault", _ENC)   # collects, no raise
    assert [p.name for p in matches] == [f"note-{_ENC}.md"]
    assert [p.name for p in malformed] == ["broken.md"]


def test_resolve_note_paths_empty_when_no_dir(tmp_path):
    assert ret.resolve_note_paths(tmp_path / "vault", _ENC) == ([], [])


# ============================ two-phase order + happy path ============================


def test_destroy_happy_path_two_phase_and_chain_survives(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    note = _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    blob = retained / f"{_ENC}.age"
    sidecar = retained / f"{_ENC}.manifest.json"
    assert blob.exists() and sidecar.exists() and note.exists()

    out, exited = _run(cfg_path, _ENC, capsys)
    assert exited is None and out["destroyed"] is True
    assert out["clinical_notes_deleted"] == 1 and out["backup_purged"] is True
    # every PHI artifact gone.
    assert not blob.exists() and not sidecar.exists() and not note.exists()
    # the two-phase chain rows both landed.
    assert ev.retention_destroy_intent_row(_ENC) is not None
    assert ev.retention_destroyed_row(_ENC) is not None
    # the PHI-FREE chain SURVIVES — audit_encounter ends in retention.destroyed (proof-of-destruction).
    timeline = ev.audit_encounter(_ENC)
    assert timeline[-1]["kind"] == "retention.destroyed"
    assert {r["kind"] for r in timeline} >= {"retention.sealed", "retention.destroy_intent",
                                             "retention.destroyed"}


def test_destroy_intent_before_any_unlink(tmp_path, capsys, monkeypatch):
    """Crash-safety: a store-down destroy_intent RAISES → NOTHING is unlinked (intent precedes the
    first unlink). Proven by patching the intent emit to raise and asserting the blob survives."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    from alfred.scribe.events import EventStoreError
    monkeypatch.setattr(ScribeEvents, "retention_destroy_intent",
                        lambda self, **kw: (_ for _ in ()).throw(EventStoreError("store down")))
    out, exited = _run(cfg_path, _ENC, capsys)
    assert exited == 1 and "destroy_intent FAILED" in out["error"]
    assert (retained / f"{_ENC}.age").exists()                       # NOTHING unlinked
    assert ev.retention_destroyed_row(_ENC) is None


# ============================ purge gates destroyed ============================


def test_incomplete_backup_purge_blocks_destroyed(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=False, reason="restic find still returns the encounter")
    out, exited = _run(cfg_path, _ENC, capsys)
    assert exited == 1 and "INCOMPLETE" in out["error"]
    assert out["backup_purge_complete"] is False
    # retention.destroyed was NOT emitted — the incomplete-destruction state persists.
    assert ev.retention_destroy_intent_row(_ENC) is not None
    assert ev.retention_destroyed_row(_ENC) is None


# ============================ crash-mid-destroy → verify → re-run ============================


def test_crash_between_phases_flagged_by_verify_then_re_run_completes(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    # First run: purge fails → intent lands, destroyed does NOT (simulated crash-between-phases).
    _mock_purge(monkeypatch, complete=False, reason="transient repo error")
    _run(cfg_path, _ENC, capsys)
    assert ev.retention_destroy_intent_row(_ENC) is not None
    assert ev.retention_destroyed_row(_ENC) is None
    # verify flags the incomplete destruction (exit 1).
    verify_ns = argparse.Namespace(config=cfg_path, scribe_cmd="retention", retention_cmd="verify")
    exited = None
    try:
        cli._cmd_scribe_retention(verify_ns)
    except SystemExit as e:
        exited = e.code
    vout = json.loads(capsys.readouterr().out)
    assert _ENC in vout["incomplete_destructions"] and exited == 1
    # Re-run with a now-successful purge → completes idempotently (unlink tolerates already-gone).
    _mock_purge(monkeypatch, complete=True)
    out, exited2 = _run(cfg_path, _ENC, capsys)
    assert out["destroyed"] is True and exited2 is None
    assert ev.retention_destroyed_row(_ENC) is not None


def test_malformed_clinical_note_refuses_destroy_WARN1(tmp_path, capsys, monkeypatch):
    """WARN-1: an unparseable clinical_note (unknowable source_id — could BE the target) → the destroy
    REFUSES to emit retention.destroyed (never a false proof-of-destruction); nothing is destroyed."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    (tmp_path / "vault" / "clinical_note" / "corrupt.md").write_text(
        "---\nbad: [unclosed\n---\nSOAP body", encoding="utf-8")     # unparseable note in the dir
    _mock_purge(monkeypatch, complete=True)
    out, exited = _run(cfg_path, _ENC, capsys)
    assert exited == 1 and "REFUSING to destroy" in out["error"]
    assert out["unparseable_clinical_notes"] == 1
    # NOTHING destroyed — no intent, artifacts intact.
    assert (retained / f"{_ENC}.age").exists()
    assert ev.retention_destroy_intent_row(_ENC) is None
    assert ev.retention_destroyed_row(_ENC) is None


def test_dry_run_surfaces_unparseable_notes_WARN1(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    (tmp_path / "vault" / "clinical_note" / "corrupt.md").write_text("---\nbad: [x\n---\nb", encoding="utf-8")
    _mock_purge(monkeypatch, complete=True)
    out, _exited = _run(cfg_path, _ENC, capsys, dry_run=True)
    assert out["unparseable_clinical_notes"] == 1 and out["blocked_by_unparseable_notes"] is True


def test_reason_write_failure_blocks_destroyed_NOTE1(tmp_path, capsys, monkeypatch):
    """NOTE-1: the compliance --reason is routed to vault_audit BEFORE retention.destroyed. A reason
    write failure fail-louds WITHOUT emitting destroyed (the reason must be durable first), so the
    destruction never "stands" with the reason lost — a re-run re-routes + completes."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    monkeypatch.setattr(cli, "_route_destroy_reason",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("audit log unwritable")))
    out, exited = _run(cfg_path, _ENC, capsys)
    assert exited == 1 and "compliance --reason" in out["error"]
    assert ev.retention_destroyed_row(_ENC) is None              # destroyed NOT emitted


def test_already_destroyed_is_noop(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    _run(cfg_path, _ENC, capsys)                                     # first destroy
    out, exited = _run(cfg_path, _ENC, capsys)                      # second → no-op
    assert out == {"already_destroyed": True, "encounter_id": _ENC} and exited is None


# ============================ dry-run + confirm + reason routing ============================


def test_dry_run_mutates_nothing(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    note = _make_note(tmp_path, _ENC)
    calls = _mock_purge(monkeypatch, complete=True)
    out, exited = _run(cfg_path, _ENC, capsys, dry_run=True)
    assert out["dry_run"] is True and exited is None
    assert any(str(retained / f"{_ENC}.age") in w for w in out["would_unlink"])
    assert out["clinical_notes"] == 1
    # NOTHING mutated: artifacts present, no chain rows, the purge ran in dry-run mode only.
    assert (retained / f"{_ENC}.age").exists() and note.exists()
    assert ev.retention_destroy_intent_row(_ENC) is None
    assert ev.retention_destroyed_row(_ENC) is None
    assert calls == [(_ENC, True)]                                  # purge_encounter(dry_run=True) only


def test_confirmation_mismatch_aborts(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    monkeypatch.setattr("builtins.input", lambda *a: "WRONG-ID")     # operator typos the id
    out, exited = _run(cfg_path, _ENC, capsys, yes=False)
    assert exited == 1 and "did not match" in out["error"]
    assert (retained / f"{_ENC}.age").exists()                       # ABORTED — nothing destroyed
    assert ev.retention_destroy_intent_row(_ENC) is None


def test_destroy_reason_routes_to_vault_audit_not_chain(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    _seal(tmp_path, ev)
    _make_note(tmp_path, _ENC)
    _mock_purge(monkeypatch, complete=True)
    _run(cfg_path, _ENC, capsys, reason="legal_order", ticket="COURT-42",
         justification="court order 2026-07 re: patient X")
    # the frozen destroy payloads carry NO reason field — only {schedule_version, manifest_sha256}.
    intent = ev.retention_destroy_intent_row(_ENC)
    assert set(intent["payload"].keys()) == {"schedule_version", "manifest_sha256"}
    assert "legal_order" not in json.dumps(intent) and "COURT-42" not in json.dumps(intent)
    # reason + ticket + justification land in vault_audit.log.
    audit = (tmp_path / "data" / "vault_audit.log").read_text(encoding="utf-8")
    assert "legal_order" in audit and "COURT-42" in audit and "court order 2026-07" in audit


# ============================ withdrawal never triggers destroy ============================


def test_withdrawal_alone_never_destroys(tmp_path, capsys, monkeypatch):
    """A consent.withdrawn marker is destroy-ADDRESSABILITY only — it NEVER auto-triggers destruction.
    No code path turns a withdrawal into a retention.destroyed; only the explicit destroy CLI does."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    ev.consent_confirmed(subject_id=_ENC, captured_by="np_jamie")    # ∅ → confirmed
    _seal(tmp_path, ev)
    # Mark the encounter withdrawn (the #12 marker — confirmed → withdrawn).
    ev.consent_withdrawn(subject_id=_ENC, at_seq=1, actor="np_jamie")
    # No destroy CLI was invoked → the encounter is NOT destroyed; the marker is queryable (addressable).
    assert ev.retention_destroyed_row(_ENC) is None
    withdrawn = ev.query(CLINICAL, family="consent", kind="consent.withdrawn", subject_id=_ENC)
    assert len(withdrawn) == 1                                        # addressable, not destroyed
