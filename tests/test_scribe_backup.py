"""STAY-C dedicated backup — code-side contract pins (task #13 slice 13d-4).

Covers the three building blocks (all INERT — no restic repo is ever inited, no timer installed):
  * build_backup_set — includes the sealed tree, EXCLUDES the plaintext transcripts + enrollment,
    never backs up data/ wholesale (recon §2).
  * seal_file_for_backup — the seal-before-backup primitive (ruling A): plaintext → enc-id-named
    .age blob; missing source → False; malformed blob → SealError.
  * purge_encounter — the destroy step-3f dependency: rewrite --exclude --forget + prune + the
    assert-empty `restic find <enc>`; fail-closed on missing binary / unconfigured repo / non-zero
    exit / non-empty find; dry-run previews without mutating.

restic is MOCKED throughout (no live repo): _run_restic is stubbed to return canned exits, shutil.which
is patched, and the dedicated-repo env is set via monkeypatch — the pins bind the ORCHESTRATION +
fail-closed contract, not restic itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import structlog

from alfred.scribe import backup
from alfred.scribe import retention as ret
from alfred.scribe.config import load_from_unified

_SALT = "s"
_ENC = "enc-0011223344556677"


class _FakeSealer:
    """Reversible well-formed-blob fake (mirrors test_scribe_retention_seal._FakeSealer) — enough for
    the seal-before-backup pins WITHOUT a crypto dep. Obviously-fake cipher label."""

    cipher = "fake-xor-test"

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        return b"FAKESEAL1" + plaintext

    def verify_wellformed(self, blob: bytes) -> bool:
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        return blob[9:]


class _BadVerifySealer(_FakeSealer):
    def verify_wellformed(self, blob: bytes) -> bool:
        return False


def _cfg(tmp_path, *, retained_dir=None, enrollment=True):
    inbox = tmp_path / "data" / "inbox"
    scribe = {
        "mode": "clinical", "encounter_salt": _SALT, "input_dir": str(inbox),
        "retention": {"mode": "retained"},
    }
    if retained_dir is not None:
        scribe["retention"]["retained_dir"] = str(retained_dir)
    if enrollment:
        scribe["diarize"] = {"enrollment_dir": str(tmp_path / "data" / "enrollment")}
    return load_from_unified({"scribe": scribe})


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["restic"], returncode=returncode, stdout=stdout, stderr=stderr)


# ============================ build_backup_set ============================


def test_backup_set_includes_sealed_tree_excludes_plaintext_and_enrollment(tmp_path):
    cfg = _cfg(tmp_path)
    bs = backup.build_backup_set(cfg)
    retained = Path(cfg.input_dir).parent / "retained"
    # ONE include root: the retained tree (sealed audio + PHI-free sidecars). The sealed-staging dir
    # rides UNDER it (a separate include would be redundant) — assert it is COVERED by the include.
    assert bs.includes == [retained]
    sealed = backup.resolved_sealed_backup_dir(cfg)
    assert sealed.is_relative_to(retained), "sealed staging must ride under the retained include"
    # EXCLUDES: the plaintext transcript ledger (LUKS on-box only) + the biometric enrollment store.
    assert str(retained / "transcripts") in bs.excludes
    assert "**/enrollment" in bs.excludes
    assert str(tmp_path / "data" / "enrollment") in bs.excludes


def test_backup_set_never_includes_data_wholesale_nor_reaches_enrollment(tmp_path):
    """recon §2: the include set must NOT be data/ wholesale, and the biometric enrollment dir must
    be unreachable from any include root (else a backup is a biometric leak + a destruction surface)."""
    cfg = _cfg(tmp_path)
    bs = backup.build_backup_set(cfg)
    data_dir = Path(cfg.input_dir).parent            # <STAYC_DATA>
    enrollment = Path(tmp_path / "data" / "enrollment")
    assert data_dir not in bs.includes               # never the whole data tree
    # enrollment sits under data/ but must not be under any INCLUDE root (retained/ + sealed staging).
    for root in bs.includes:
        assert not (enrollment == root or enrollment.is_relative_to(root)), (
            f"enrollment {enrollment} is reachable from include root {root}"
        )


def test_backup_set_honors_configured_retained_dir(tmp_path):
    cfg = _cfg(tmp_path, retained_dir=tmp_path / "custom_retained")
    bs = backup.build_backup_set(cfg)
    assert (tmp_path / "custom_retained") in bs.includes


# ============================ seal_file_for_backup (ruling A) ============================


def test_sealed_backup_paths_are_enc_id_named(tmp_path):
    cfg = _cfg(tmp_path)
    tpath, npath = backup.sealed_backup_paths(cfg, _ENC)
    d = backup.resolved_sealed_backup_dir(cfg)
    assert tpath == d / f"{_ENC}.transcript.age"
    assert npath == d / f"{_ENC}.note.age"


def test_seal_file_for_backup_writes_verified_blob(tmp_path):
    src = tmp_path / "plain.json"
    src.write_bytes(b'{"transcript":"phi"}')
    dest = tmp_path / "sealed" / f"{_ENC}.transcript.age"
    ok = backup.seal_file_for_backup(src, dest, sealer=_FakeSealer(), recipient_public_key=b"age1x")
    assert ok is True
    assert dest.read_bytes().startswith(b"FAKESEAL1")          # sealed, not plaintext
    assert b"phi" not in dest.read_bytes()[:9]                 # the plaintext is inside the sealed blob


def test_seal_file_for_backup_missing_source_returns_false(tmp_path):
    ok = backup.seal_file_for_backup(
        tmp_path / "nope.json", tmp_path / "out.age", sealer=_FakeSealer(), recipient_public_key=b"age1x")
    assert ok is False
    assert not (tmp_path / "out.age").exists()


def test_seal_file_for_backup_malformed_blob_raises(tmp_path):
    src = tmp_path / "plain.json"
    src.write_bytes(b"data")
    with pytest.raises(ret.SealError):
        backup.seal_file_for_backup(
            src, tmp_path / "out.age", sealer=_BadVerifySealer(), recipient_public_key=b"age1x")
    assert not (tmp_path / "out.age").exists()                 # never write an unverifiable copy


def test_seal_file_for_backup_real_crypto_round_trip(tmp_path):
    """Dep-gated (ruling A guarantee): the seal-before-backup copy is a REAL age blob that opens
    ONLY with the OFFLINE private key — the whole-off-box-archive crypto-shred. Seals a plaintext
    transcript to a real recipient, confirms it round-trips byte-identical with the matching identity,
    and is crypto-shredded (SealError) to any OTHER key."""
    pytest.importorskip("pyrage")
    pub, priv = ret.generate_keypair()
    sealer = ret.make_default_sealer()
    src = tmp_path / "t.json"
    src.write_bytes(b'{"transcript":"real phi content"}')
    dest = tmp_path / "sealed" / f"{_ENC}.transcript.age"
    assert backup.seal_file_for_backup(src, dest, sealer=sealer, recipient_public_key=pub) is True
    blob = dest.read_bytes()
    assert ret._age_blob_wellformed(blob)                       # a genuine age v1 envelope
    assert sealer.unseal(blob, priv) == src.read_bytes()        # off-box-openable with the offline key
    _other_pub, other_priv = ret.generate_keypair()
    with pytest.raises(ret.SealError):
        sealer.unseal(blob, other_priv)                        # crypto-shredded to any other key


# ============================ encounter_backup_globs ============================


def test_encounter_backup_globs_are_all_enc_id_named(tmp_path):
    """The purge invariant: every off-box artifact is enc-id-named (audio, sidecar, sealed
    transcript, sealed note) so `restic find <enc>` is a uniform assert."""
    cfg = _cfg(tmp_path)
    globs = backup.encounter_backup_globs(cfg, _ENC)
    assert len(globs) == 4
    assert all(_ENC in g for g in globs)
    retained = Path(cfg.input_dir).parent / "retained"
    sealed = backup.resolved_sealed_backup_dir(cfg)
    assert str(retained / f"{_ENC}.age") in globs
    assert str(retained / f"{_ENC}.manifest.json") in globs
    assert str(sealed / f"{_ENC}.transcript.age") in globs
    assert str(sealed / f"{_ENC}.note.age") in globs


# ============================ _restic_env ============================


def test_restic_env_none_when_repo_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(backup.ENV_RESTIC_REPO, raising=False)
    assert backup._restic_env() is None


def test_restic_env_prefers_password_file(tmp_path, monkeypatch):
    monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD_FILE, "/etc/stayc.pw")
    monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD, "inline-should-be-dropped")
    env = backup._restic_env()
    assert env["RESTIC_REPOSITORY"] == "sftp:host:/stayc"
    assert env["RESTIC_PASSWORD_FILE"] == "/etc/stayc.pw"
    assert "RESTIC_PASSWORD" not in env                        # the file wins; the inline is dropped


def test_restic_env_falls_back_to_inline_password(tmp_path, monkeypatch):
    monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.delenv(backup.ENV_RESTIC_PASSWORD_FILE, raising=False)
    monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD, "pw")
    env = backup._restic_env()
    assert env["RESTIC_PASSWORD"] == "pw"
    assert "RESTIC_PASSWORD_FILE" not in env


def test_restic_env_none_when_no_password_source(tmp_path, monkeypatch):
    monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.delenv(backup.ENV_RESTIC_PASSWORD_FILE, raising=False)
    monkeypatch.delenv(backup.ENV_RESTIC_PASSWORD, raising=False)
    assert backup._restic_env() is None                        # a repo with no password can't open


def test_restic_env_drops_conflicting_repo_file_and_password_command(tmp_path, monkeypatch):
    """Defense-in-depth for a destruction command: a stale inherited RESTIC_REPOSITORY_FILE (conflicts
    with the RESTIC_REPOSITORY we set) or RESTIC_PASSWORD_COMMAND (shadows the intended password) could
    redirect the purge at a different repo / open with a different key — both are dropped."""
    monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD, "pw")
    monkeypatch.setenv("RESTIC_REPOSITORY_FILE", "/some/other/repo")
    monkeypatch.setenv("RESTIC_PASSWORD_COMMAND", "cat /some/other/pw")
    env = backup._restic_env()
    assert env["RESTIC_REPOSITORY"] == "sftp:host:/stayc"
    assert "RESTIC_REPOSITORY_FILE" not in env
    assert "RESTIC_PASSWORD_COMMAND" not in env


# ============================ purge_encounter (mocked restic) ============================


def _arm_restic(monkeypatch, *, repo=True):
    """Patch shutil.which(restic)→found and the dedicated-repo env present."""
    monkeypatch.setattr(backup.shutil, "which", lambda name: "/usr/bin/restic")
    if repo:
        monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
        monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD, "pw")
    else:
        monkeypatch.delenv(backup.ENV_RESTIC_REPO, raising=False)


def _record_restic(monkeypatch, results):
    """Stub _run_restic to pop canned CompletedProcess per call, recording the arg vectors."""
    calls = []

    def _stub(args, env):
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(backup, "_run_restic", _stub)
    return calls


def test_purge_missing_binary_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    monkeypatch.setenv(backup.ENV_RESTIC_REPO, "sftp:host:/stayc")
    monkeypatch.setenv(backup.ENV_RESTIC_PASSWORD, "pw")
    res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is False and "restic binary" in res.reason


def test_purge_unconfigured_repo_fails_closed(tmp_path, monkeypatch):
    _arm_restic(monkeypatch, repo=False)
    res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is False and backup.ENV_RESTIC_REPO in res.reason


def test_purge_happy_path_complete_when_find_empty(tmp_path, monkeypatch):
    _arm_restic(monkeypatch)
    calls = _record_restic(monkeypatch, [
        _proc(0),                                       # rewrite
        _proc(0),                                       # prune
        _proc(0, stdout="No matching files found\n"),   # find → empty (enc-id absent)
    ])
    with structlog.testing.capture_logs() as captured:
        res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is True and res.reason == ""
    # rewrite carried --forget + an --exclude for ALL 4 enc-id-named artifacts.
    rewrite = calls[0]
    assert rewrite[0] == "rewrite" and "--forget" in rewrite
    assert rewrite.count("--exclude") == 4
    assert calls[1][0] == "prune"
    # The assert-empty find MUST be at least as broad as the repo-wide rewrite — it is DELIBERATELY
    # un-tagged, else a non-stayc-tagged snapshot holding the encounter would slip a false all-clear.
    assert calls[2][0] == "find" and _ENC in calls[2]
    assert "--tag" not in calls[2] and backup.RESTIC_TAG not in calls[2]
    assert any(c.get("event") == "scribe.backup.purge_complete" for c in captured)


def test_purge_incomplete_when_find_still_returns_encounter(tmp_path, monkeypatch):
    _arm_restic(monkeypatch)
    _record_restic(monkeypatch, [
        _proc(0),                                       # rewrite
        _proc(0),                                       # prune
        _proc(0, stdout=f"/stayc/{_ENC}.age\n"),        # find STILL returns it → NOT empty
    ])
    res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is False and "STILL returns" in res.reason


def test_purge_rewrite_failure_skips_prune_and_fails(tmp_path, monkeypatch):
    _arm_restic(monkeypatch)
    calls = _record_restic(monkeypatch, [_proc(1, stderr="lock")])   # rewrite fails
    res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is False and "rewrite" in res.reason
    assert len(calls) == 1                              # prune + find never ran (fail-fast)


def test_purge_prune_failure_fails_closed(tmp_path, monkeypatch):
    _arm_restic(monkeypatch)
    _record_restic(monkeypatch, [_proc(0), _proc(1, stderr="disk")])  # rewrite ok, prune fails
    res = backup.purge_encounter(_cfg(tmp_path), _ENC)
    assert res.complete is False and "prune" in res.reason


def test_purge_dry_run_previews_without_mutating(tmp_path, monkeypatch):
    _arm_restic(monkeypatch)
    calls = _record_restic(monkeypatch, [_proc(0)])     # only the dry-run rewrite
    res = backup.purge_encounter(_cfg(tmp_path), _ENC, dry_run=True)
    assert res.complete is False and res.dry_run is True
    assert len(res.excluded_paths) == 4                 # the preview surfaces the target set
    assert len(calls) == 1                              # NO prune, NO find on a dry-run
    assert "--dry-run" in calls[0]


# ============================ _find_is_empty ============================


def test_find_is_empty_semantics():
    assert backup._find_is_empty(_proc(0, stdout="No matching files found\n"), _ENC) is True
    assert backup._find_is_empty(_proc(0, stdout=f"/x/{_ENC}.age"), _ENC) is False
    assert backup._find_is_empty(_proc(1, stdout="No matching files found"), _ENC) is False  # fail-closed
