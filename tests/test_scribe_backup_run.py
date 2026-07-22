"""STAY-C dedicated backup_run — seal-before-backup orchestration (slice 13d-4b).

backup_run seals each encounter's transcript+note to the sealed-staging dir BEFORE restic, then
restic-backs up the retained tree + staging (enrollment structurally excluded). Malformed clinical_note
= skip-loud-and-count (backup is NON-destructive, distinct from the destroy WARN-1 refuse). restic is
mocked (no live repo).
"""
from __future__ import annotations

import argparse
import json
import subprocess

import pytest
import structlog
import yaml

from alfred import cli
from alfred.scribe import backup as backup_mod
from alfred.scribe import retention as ret
from alfred.scribe.config import load_from_unified
from alfred.scribe.events import ScribeEvents

_ENC = "enc-bbbb5555cccc6666"
_FAKE_PUB = b"age1fakerecipientforbackuprun"


@pytest.fixture(autouse=True)
def _capture_structlog():
    with structlog.testing.capture_logs():
        yield


class _FakeSealer:
    cipher = "fake-xor-test"

    def seal(self, plaintext, recipient_public_key):
        return b"FAKESEAL1" + plaintext

    def verify_wellformed(self, blob):
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob, private_key):
        return blob[9:]


def _cfg(tmp_path, *, enrollment=True):
    scribe = {
        "mode": "clinical", "encounter_salt": "s", "input_dir": str(tmp_path / "data" / "inbox"),
        "retention": {"retained_dir": str(tmp_path / "retained")},
    }
    if enrollment:
        scribe["diarize"] = {"enrollment_dir": str(tmp_path / "data" / "enrollment")}
    return load_from_unified({"scribe": scribe})


def _events(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"))


def _seal_encounter(tmp_path, *, enc=_ENC):
    ev = _events(tmp_path)
    enc_dir = tmp_path / "data" / "inbox" / "jane-doe"
    enc_dir.mkdir(parents=True, exist_ok=True)
    for seq in (1, 2):
        (enc_dir / f"chunk_{seq}.webm").write_bytes(f"a{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text("{}", encoding="utf-8")
    (enc_dir / "_CLOSED").write_text("{}", encoding="utf-8")
    (enc_dir / f"{enc}.transcript.json").write_text('{"segments":[]}', encoding="utf-8")
    out = ret.seal_encounter(enc_dir, enc, events=ev, sealer=_FakeSealer(),
                             recipient_public_key=_FAKE_PUB, retained_dir=tmp_path / "retained")
    assert out.status == ret.SEAL_STATUS_SEALED


def _make_note(tmp_path, enc, *, name=None, source_id=None, body="SOAP (PHI)"):
    nd = tmp_path / "vault" / "clinical_note"
    nd.mkdir(parents=True, exist_ok=True)
    p = nd / (name or f"note-{enc}.md")
    p.write_text(f"---\ntitle: N\ntype: clinical_note\nsource_id: {source_id or enc}\n---\n{body}\n",
                 encoding="utf-8")
    return p


def _record_restic(monkeypatch, returncode=0):
    calls = []

    def _stub(args, env):
        calls.append(args)
        return subprocess.CompletedProcess(args=["restic"], returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(backup_mod, "_run_restic", _stub)
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/restic")
    monkeypatch.setenv(backup_mod.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup_mod.ENV_RESTIC_PASSWORD, "pw")
    return calls


# ============================ backup_run ============================


def test_backup_run_seals_before_restic(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg = _cfg(tmp_path)
    calls = _record_restic(monkeypatch)
    t_dest, n_dest = backup_mod.sealed_backup_paths(cfg, _ENC)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB)
    assert res.encounters == 1 and res.transcripts_sealed == 1 and res.notes_sealed == 1
    assert res.restic_ran is True and res.malformed_notes == 0
    # the sealed staging copies exist (sealed BEFORE restic) and are age blobs, not plaintext.
    assert t_dest.read_bytes().startswith(b"FAKESEAL1")
    assert n_dest.read_bytes().startswith(b"FAKESEAL1") and b"PHI" not in n_dest.read_bytes()[:9]
    # restic backup ran with the tag + the enrollment exclude, and enrollment is NOT an include.
    assert calls[0][0] == "backup" and "--tag" in calls[0] and backup_mod.RESTIC_TAG in calls[0]
    assert "**/enrollment" in calls[0]
    assert str(tmp_path / "data" / "enrollment") in calls[0]
    assert str(tmp_path / "data" / "enrollment") not in _includes(calls[0])


def _includes(restic_args):
    """The positional include paths of a `restic backup <paths...> --exclude … --tag …` arg vector."""
    out = []
    i = 1
    while i < len(restic_args):
        a = restic_args[i]
        if a in ("--tag", "--exclude"):
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def test_backup_run_seals_strictly_before_restic_ORDER(tmp_path, monkeypatch):
    """NOTE-1: bind the seal-BEFORE-restic ORDER, not just 'both ran'. The restic stub records the
    staging-blob existence AT CALL TIME — a future reorder (restic before the seal loop) would capture
    them ABSENT here and fail, where the post-hoc 'both exist' assertion would still pass."""
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg = _cfg(tmp_path)
    t_dest, n_dest = backup_mod.sealed_backup_paths(cfg, _ENC)
    seen = {}

    def _stub(args, env):
        seen["transcript"] = t_dest.exists()      # FS state at the MOMENT restic is invoked
        seen["note"] = n_dest.exists()
        return subprocess.CompletedProcess(args=["restic"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup_mod, "_run_restic", _stub)
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: "/usr/bin/restic")
    monkeypatch.setenv(backup_mod.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup_mod.ENV_RESTIC_PASSWORD, "pw")
    backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(), recipient_public_key=_FAKE_PUB)
    assert seen == {"transcript": True, "note": True}   # the staging blobs existed BEFORE restic ran


def test_backup_run_malformed_note_skip_loud_and_count(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    (tmp_path / "vault" / "clinical_note" / "corrupt.md").write_text("---\nbad: [x\n---\nb", encoding="utf-8")
    cfg = _cfg(tmp_path)
    _record_restic(monkeypatch)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB)
    # NON-destructive: the run completes + backs up; the malformed note is counted + surfaced, not fatal.
    assert res.malformed_notes == 1 and res.restic_ran is True and res.notes_sealed == 1


def test_backup_run_multi_note_encounter_counted(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC, name="orig.md")
    _make_note(tmp_path, _ENC, name="amended.md")             # same source_id (amended)
    cfg = _cfg(tmp_path)
    _record_restic(monkeypatch)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB)
    assert res.multi_note_encounters == 1 and res.notes_sealed == 1   # only the first sealed


def test_backup_run_dry_run_seals_nothing(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg = _cfg(tmp_path)
    calls = _record_restic(monkeypatch)
    t_dest, n_dest = backup_mod.sealed_backup_paths(cfg, _ENC)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB, dry_run=True)
    assert res.dry_run is True and res.restic_ran is False and res.encounters == 1
    assert not t_dest.exists() and not n_dest.exists() and calls == []   # nothing sealed, no restic


def test_backup_run_seals_but_flags_restic_unavailable(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)   # no restic binary
    monkeypatch.setenv(backup_mod.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup_mod.ENV_RESTIC_PASSWORD, "pw")
    t_dest, _n = backup_mod.sealed_backup_paths(cfg, _ENC)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB)
    assert res.transcripts_sealed == 1 and res.restic_ran is False and "restic binary" in res.reason
    assert t_dest.exists()                                    # staging sealed even though restic absent


def test_backup_run_seal_is_idempotent_across_runs(tmp_path, monkeypatch):
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg = _cfg(tmp_path)
    _record_restic(monkeypatch)
    backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(), recipient_public_key=_FAKE_PUB)
    res2 = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(), recipient_public_key=_FAKE_PUB)
    # 2nd run: staging blobs already present → seal-if-absent skips re-sealing (age is non-deterministic;
    # re-sealing would churn restic dedup).
    assert res2.transcripts_sealed == 0 and res2.notes_sealed == 0 and res2.restic_ran is True


def test_backup_run_no_encounters_is_ilb(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_path / "retained").mkdir(parents=True, exist_ok=True)
    _record_restic(monkeypatch)
    res = backup_mod.backup_run(cfg, tmp_path / "vault", sealer=_FakeSealer(),
                                recipient_public_key=_FAKE_PUB)
    assert res.encounters == 0 and res.notes_sealed == 0     # ran, nothing to seal


# ============================ CLI entry (retention backup-run) ============================


def _cli_cfg_real_pubkey(tmp_path):
    """A CLI config with a REAL age recipient at seal_public_key_path (the CLI validates it via
    is_valid_age_recipient before sealing). Returns the config path."""
    pub, _priv = ret.generate_keypair()
    seal_pub = tmp_path / "seal" / "seal_pub.age"
    seal_pub.parent.mkdir(parents=True, exist_ok=True)
    seal_pub.write_text(pub.decode("utf-8"), encoding="utf-8")
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical",
                   "input_dir": str(tmp_path / "data" / "inbox"),
                   "retention": {"retained_dir": str(tmp_path / "retained"),
                                 "seal_public_key_path": str(seal_pub)}},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(cfg_path)


def _backup_run_ns(cfg_path, *, dry_run=False):
    return argparse.Namespace(config=cfg_path, scribe_cmd="retention", retention_cmd="backup-run",
                              dry_run=dry_run)


def test_backup_run_cli_real_crypto(tmp_path, capsys, monkeypatch):
    pytest.importorskip("pyrage")
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg_path = _cli_cfg_real_pubkey(tmp_path)
    _record_restic(monkeypatch)
    cli._cmd_scribe_retention(_backup_run_ns(cfg_path))
    out = json.loads(capsys.readouterr().out)
    assert out["backup_run"] is True and out["notes_sealed"] == 1 and out["restic_ran"] is True


def test_backup_run_cli_restic_failure_exits_1_WARN1(tmp_path, capsys, monkeypatch):
    """WARN-1: a restic-backup non-zero → the CLI exits 1 (the whole reason the timer's OnFailure
    surfaces). Unpinned before this — a regression that swallowed the failure would go green."""
    pytest.importorskip("pyrage")
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg_path = _cli_cfg_real_pubkey(tmp_path)
    _record_restic(monkeypatch, returncode=1)               # restic backup FAILS
    with pytest.raises(SystemExit) as exc:
        cli._cmd_scribe_retention(_backup_run_ns(cfg_path))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["restic_ran"] is False


def test_backup_run_cli_restic_unavailable_exits_1_WARN1(tmp_path, capsys, monkeypatch):
    """WARN-1 sibling: restic binary missing (repo un-runnable) → sealed the staging copies but the CLI
    still exits 1 (the backup did not complete — the timer must surface it)."""
    pytest.importorskip("pyrage")
    _seal_encounter(tmp_path)
    _make_note(tmp_path, _ENC)
    cfg_path = _cli_cfg_real_pubkey(tmp_path)
    monkeypatch.setattr(backup_mod.shutil, "which", lambda name: None)   # no restic binary
    with pytest.raises(SystemExit) as exc:
        cli._cmd_scribe_retention(_backup_run_ns(cfg_path))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["restic_ran"] is False


def test_backup_run_cli_errors_without_pubkey(tmp_path, capsys):
    body = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
            "scribe": {"clinicians": ["np_jamie"], "encounter_salt": "s", "mode": "clinical",
                       "retention": {"retained_dir": str(tmp_path / "retained")}}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(body), encoding="utf-8")
    ns = argparse.Namespace(config=str(cfg_path), scribe_cmd="retention",
                            retention_cmd="backup-run", dry_run=False)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(ns)
    assert "seal_public_key_path is unset" in json.loads(capsys.readouterr().out)["error"]
