"""STAY-C retention UNSEAL — §6 single-encounter retrieval (slice 13d-2).

Core (retention.unseal_to_dir): decrypt+verify+write is fail-closed on EVERY integrity failure
(truncated blob, tampered/mismatched sidecar, wrong identity) — nothing written, no partial plaintext.
CLI (_retention_unseal): emits retention.unsealed[D]{reason_code, ticket_ref}, routes the free-text
justification → vault_audit.log (NEVER the chain), wipes the temp plaintext on exit; --record-only
emits the attestation WITHOUT a local decrypt and forbids --key/--out.

The core verify pins run WITHOUT pyrage (a reversible fake sealer); one dep-gated real-crypto round-trip
proves the age guarantee + wrong-identity rejection.
"""
from __future__ import annotations

import argparse
import json

import pytest
import structlog
import yaml

from alfred import cli
from alfred.scribe import retention as ret
from alfred.scribe.events import CLINICAL, ScribeEvents

_ENC = "enc-aaaa1111bbbb2222"
_FAKE_PUB = b"age1fakerecipientforunsealtests"


@pytest.fixture(autouse=True)
def _capture_structlog():
    with structlog.testing.capture_logs():
        yield


class _FakeSealer:
    """Reversible well-formed-blob fake (mirrors the seal tests) — the fake unseal IGNORES the identity
    (so identity-independent round-trips work without pyrage). cipher is obviously-fake."""

    cipher = "fake-xor-test"

    def seal(self, plaintext: bytes, recipient_public_key: bytes) -> bytes:
        return b"FAKESEAL1" + bytes([len(recipient_public_key)]) + recipient_public_key + plaintext

    def verify_wellformed(self, blob: bytes) -> bool:
        return blob.startswith(b"FAKESEAL1")

    def unseal(self, blob: bytes, private_key: bytes) -> bytes:
        if not blob.startswith(b"FAKESEAL1"):
            raise ret.SealError("fake: not a FAKESEAL1 blob")
        n = blob[9]
        return blob[10 + n:]


def _events(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": "s", "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"))


def _sealed(tmp_path, ev, sealer, *, enc=_ENC, pub=_FAKE_PUB, retained=None):
    """Seal a 2-chunk encounter via the real 13a seal path; returns (retained_dir, enc, manifest_sha256)."""
    enc_dir = tmp_path / "inbox" / "jane-doe-2026-07-19"
    enc_dir.mkdir(parents=True, exist_ok=True)
    for seq in (1, 2):
        (enc_dir / f"chunk_{seq}.webm").write_bytes(f"audio-{seq}".encode())
        (enc_dir / f"chunk_{seq}.meta.json").write_text("{}", encoding="utf-8")
    (enc_dir / "_CLOSED").write_text("{}", encoding="utf-8")
    (enc_dir / f"{enc}.transcript.json").write_text("{}", encoding="utf-8")
    retained = retained or (tmp_path / "retained")
    out = ret.seal_encounter(
        enc_dir, enc, events=ev, sealer=sealer, recipient_public_key=pub, retained_dir=retained)
    assert out.status == ret.SEAL_STATUS_SEALED, out.status
    return retained, enc, out.manifest_sha256


# ============================ unseal_to_dir core ============================


def test_unseal_to_dir_round_trip_fake(tmp_path):
    ev = _events(tmp_path)
    sealer = _FakeSealer()
    retained, enc, msha = _sealed(tmp_path, ev, sealer)
    out_dir = tmp_path / "out"
    res = ret.unseal_to_dir(
        retained, enc, identity=b"anykey", sealer=sealer, out_dir=out_dir, expected_manifest_sha256=msha)
    assert res.chunk_count == 2
    assert (out_dir / "chunk_1.webm").read_bytes() == b"audio-1"
    assert (out_dir / "chunk_2.webm").read_bytes() == b"audio-2"


def test_unseal_missing_blob_fails_closed(tmp_path):
    with pytest.raises(ret.SealError):
        ret.unseal_to_dir(tmp_path / "retained", _ENC, identity=b"k", sealer=_FakeSealer(),
                          out_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unseal_truncated_blob_fails_closed_no_output(tmp_path):
    ev = _events(tmp_path)
    sealer = _FakeSealer()
    retained, enc, msha = _sealed(tmp_path, ev, sealer)
    blob = retained / f"{enc}.age"
    blob.write_bytes(blob.read_bytes()[:-4])            # truncate → sha != sidecar blob_sha256
    out_dir = tmp_path / "out"
    with pytest.raises(ret.SealError):
        ret.unseal_to_dir(retained, enc, identity=b"k", sealer=sealer, out_dir=out_dir,
                          expected_manifest_sha256=msha)
    assert not out_dir.exists() or not list(out_dir.iterdir())   # NOTHING written


def test_unseal_expected_manifest_mismatch_fails_closed(tmp_path):
    ev = _events(tmp_path)
    sealer = _FakeSealer()
    retained, enc, _msha = _sealed(tmp_path, ev, sealer)
    with pytest.raises(ret.SealError):
        ret.unseal_to_dir(retained, enc, identity=b"k", sealer=sealer, out_dir=tmp_path / "out",
                          expected_manifest_sha256="deadbeefdeadbeef")   # not the chain's sha


def test_unseal_tampered_sidecar_manifest_fails_closed(tmp_path):
    ev = _events(tmp_path)
    sealer = _FakeSealer()
    retained, enc, msha = _sealed(tmp_path, ev, sealer)
    sidecar = retained / f"{enc}.manifest.json"
    data = json.loads(sidecar.read_text())
    data["manifest"][0]["sha256"] = "0" * 64            # corrupt a chunk sha in the sidecar
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    # The sidecar's manifest digest now diverges from BOTH the chain sha and the decrypted tar manifest.
    with pytest.raises(ret.SealError):
        ret.unseal_to_dir(retained, enc, identity=b"k", sealer=sealer, out_dir=tmp_path / "out",
                          expected_manifest_sha256=msha)


def test_unseal_to_dir_real_crypto_round_trip_and_wrong_key(tmp_path):
    pytest.importorskip("pyrage")
    ev = _events(tmp_path)
    pub, priv = ret.generate_keypair()
    sealer = ret.make_default_sealer()
    retained, enc, msha = _sealed(tmp_path, ev, sealer, pub=pub)
    res = ret.unseal_to_dir(
        retained, enc, identity=priv, sealer=sealer, out_dir=tmp_path / "out", expected_manifest_sha256=msha)
    assert res.chunk_count == 2
    assert (tmp_path / "out" / "chunk_1.webm").read_bytes() == b"audio-1"
    # A DIFFERENT offline key cannot open it (crypto-shredded) — SealError, no output.
    _other_pub, other_priv = ret.generate_keypair()
    with pytest.raises(ret.SealError):
        ret.unseal_to_dir(retained, enc, identity=other_priv, sealer=sealer, out_dir=tmp_path / "out2",
                          expected_manifest_sha256=msha)
    assert not (tmp_path / "out2").exists() or not list((tmp_path / "out2").iterdir())


# ============================ wipe_plaintext_dir ============================


def test_wipe_plaintext_dir_overwrites_and_removes_created(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    c1 = out / "chunk_1.webm"
    c1.write_bytes(b"phi-audio")
    ret.wipe_plaintext_dir(out, (c1,), created=True)
    assert not c1.exists() and not out.exists()          # wiped + dir removed


def test_wipe_plaintext_dir_never_raises_on_missing(tmp_path):
    ret.wipe_plaintext_dir(tmp_path / "gone", None, created=True)   # no such dir → no raise


# ============================ CLI (_retention_unseal) ============================


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


def _unseal_ns(config, encounter, *, key=None, out=None, reason="dispute", ticket="T-1",
               justification=None, record_only=False):
    return argparse.Namespace(
        config=config, scribe_cmd="retention", retention_cmd="unseal", encounter=encounter,
        key=key, out=out, reason=reason, ticket=ticket, justification=justification,
        record_only=record_only)


def test_unseal_cli_emits_routes_justification_and_wipes(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    sealer = _FakeSealer()
    _retained, enc, _msha = _sealed(tmp_path, ev, sealer, retained=tmp_path / "retained")
    monkeypatch.setattr(ret, "make_default_sealer", lambda: _FakeSealer())
    keyfile = tmp_path / "id.txt"
    keyfile.write_text("AGE-SECRET-KEY-FAKEIDENTITY", encoding="utf-8")
    out_dir = tmp_path / "unseal_out"
    cli._cmd_scribe_retention(_unseal_ns(
        cfg_path, enc, key=str(keyfile), out=str(out_dir), reason="dispute", ticket="TCK-9",
        justification="opened for the Jane Doe dispute review"))
    summary = json.loads(capsys.readouterr().out)
    assert summary["unsealed"] is True and summary["chunk_count"] == 2
    # retention.unsealed row on the chain — PHI-FREE payload (exactly reason_code + ticket_ref).
    rows = ev.query(CLINICAL, family="retention", kind="retention.unsealed", subject_id=enc)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"reason_code": "dispute", "ticket_ref": "TCK-9"}
    # The free-text justification is in vault_audit.log, NEVER the chain.
    audit = (tmp_path / "data" / "vault_audit.log").read_text(encoding="utf-8")
    assert "opened for the Jane Doe dispute review" in audit
    assert "Jane Doe" not in json.dumps(rows)
    # The decrypted plaintext is WIPED on exit (non-tty → no hold; the finally wiped it).
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_unseal_cli_manifest_mismatch_fails_closed_no_event(tmp_path, capsys, monkeypatch):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    sealer = _FakeSealer()
    _retained, enc, _msha = _sealed(tmp_path, ev, sealer, retained=tmp_path / "retained")
    # Corrupt the sidecar so verification fails inside unseal_to_dir.
    sidecar = tmp_path / "retained" / f"{enc}.manifest.json"
    d = json.loads(sidecar.read_text())
    d["blob_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(ret, "make_default_sealer", lambda: _FakeSealer())
    keyfile = tmp_path / "id.txt"
    keyfile.write_text("AGE-SECRET-KEY-X", encoding="utf-8")
    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(cfg_path, enc, key=str(keyfile), out=str(out_dir)))
    # FAIL-CLOSED: no unsealed event, no lingering plaintext.
    assert ev.query(CLINICAL, family="retention", kind="retention.unsealed", subject_id=enc) == []
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_unseal_record_only_emits_without_decrypt(tmp_path, capsys):
    cfg_path, ev = _cfg_and_ev(tmp_path)
    # The encounter WAS sealed on-box (chain row exists); the operator opened it off-box → record-only.
    _r, enc, _m = _sealed(tmp_path, ev, _FakeSealer(), retained=tmp_path / "retained")
    cli._cmd_scribe_retention(_unseal_ns(
        cfg_path, enc, reason="audit", ticket="TCK-2", record_only=True,
        justification="opened off-box on trusted-laptop-07"))
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"unsealed": True, "record_only": True, "encounter_id": enc,
                       "reason_code": "audit"}
    rows = ev.query(CLINICAL, family="retention", kind="retention.unsealed", subject_id=enc)
    assert len(rows) == 1 and rows[0]["payload"] == {"reason_code": "audit", "ticket_ref": "TCK-2"}
    audit = (tmp_path / "data" / "vault_audit.log").read_text(encoding="utf-8")
    assert "off-box on trusted-laptop-07" in audit and "RECORD-ONLY" in audit


def test_unseal_refuses_blob_without_chain_row(tmp_path, capsys, monkeypatch):
    """WARN-1: a blob + sidecar exist on disk but NO retention.sealed chain row → fail-closed, NO
    decrypt, NO event. Serving PHI the chain does not attest violates chain-is-source-of-truth (#11)."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    retained = tmp_path / "retained"
    retained.mkdir(parents=True, exist_ok=True)
    enc = "enc-planted-no-row"
    (retained / f"{enc}.age").write_bytes(b"FAKESEAL1planted")          # a blob with NO chain row
    (retained / f"{enc}.manifest.json").write_text(
        json.dumps({"manifest": [], "blob_sha256": "x"}), encoding="utf-8")
    monkeypatch.setattr(ret, "make_default_sealer", lambda: _FakeSealer())
    keyfile = tmp_path / "id.txt"
    keyfile.write_text("AGE-SECRET-KEY-X", encoding="utf-8")
    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(cfg_path, enc, key=str(keyfile), out=str(out_dir)))
    err = json.loads(capsys.readouterr().out)
    assert "no retention.sealed row" in err["error"]
    assert ev.query(CLINICAL, family="retention", kind="retention.unsealed", subject_id=enc) == []
    assert not out_dir.exists()                                         # NO decrypt happened


def test_record_only_refuses_enc_without_chain_row(tmp_path, capsys):
    """WARN-1 (record-only leg): attesting an unseal of a never-sealed enc is equally wrong."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(
            cfg_path, "enc-never-sealed", reason="audit", ticket="T", record_only=True))
    err = json.loads(capsys.readouterr().out)
    assert "no retention.sealed row" in err["error"]
    assert ev.query(CLINICAL, family="retention", kind="retention.unsealed",
                    subject_id="enc-never-sealed") == []


def test_unseal_failed_decrypt_preserves_pre_existing_operator_chunk(tmp_path, capsys, monkeypatch):
    """WARN-2: a reused --out with an operator's own chunk_9.webm is NOT collateral-wiped when the
    unseal fails (verify-then-write wrote nothing, so only the pre-existing file is present — the wipe
    must protect it)."""
    cfg_path, ev = _cfg_and_ev(tmp_path)
    sealer = _FakeSealer()
    _r, enc, _m = _sealed(tmp_path, ev, sealer, retained=tmp_path / "retained")
    # Corrupt the sidecar so unseal_to_dir fails AFTER out_dir is (pre)populated by the operator.
    sidecar = tmp_path / "retained" / f"{enc}.manifest.json"
    d = json.loads(sidecar.read_text())
    d["blob_sha256"] = "0" * 64
    sidecar.write_text(json.dumps(d), encoding="utf-8")
    out_dir = tmp_path / "reused_out"
    out_dir.mkdir()
    operator_file = out_dir / "chunk_9.webm"
    operator_file.write_bytes(b"operator-own-audio")               # pre-existing, NOT from this unseal
    monkeypatch.setattr(ret, "make_default_sealer", lambda: _FakeSealer())
    keyfile = tmp_path / "id.txt"
    keyfile.write_text("AGE-SECRET-KEY-X", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(cfg_path, enc, key=str(keyfile), out=str(out_dir)))
    assert operator_file.exists()                                  # NEVER collateral-wiped
    assert operator_file.read_bytes() == b"operator-own-audio"


def test_unseal_record_only_forbids_key_and_out(tmp_path, capsys):
    cfg_path, _ev = _cfg_and_ev(tmp_path)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(
            cfg_path, "enc-1", reason="audit", ticket="T", record_only=True, key="/some/key"))
    assert "record-only" in json.dumps(json.loads(capsys.readouterr().out)).lower()


def test_unseal_requires_key_and_out_when_not_record_only(tmp_path, capsys):
    cfg_path, _ev = _cfg_and_ev(tmp_path)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_unseal_ns(cfg_path, "enc-1", reason="dispute", ticket="T"))
    assert "requires --key" in json.dumps(json.loads(capsys.readouterr().out))
