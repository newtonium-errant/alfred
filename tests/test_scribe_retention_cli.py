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


def test_publish_write_failure_after_pin_reports_dangling(tmp_path, capsys, monkeypatch):
    # C2: the durable-first ordering makes a post-pin write failure (read-only seal dir / disk-full)
    # possible — it must surface a JSON error naming the dangling-pin state + the re-publish remedy,
    # NOT a raw traceback. The [D] pin DID land (that's the dangling state).
    from alfred.scribe import retention as ret_mod
    cfg, sched_path = _write_cfg(tmp_path)
    src = _write_schedule_file(tmp_path)
    monkeypatch.setattr(ret_mod, "_atomic_write_bytes",
                        lambda path, data: (_ for _ in ()).throw(OSError("read-only seal dir")))
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_ns(cfg, "publish", file=src))
    out = _stdout_json(capsys)
    assert out.get("dangling_pin") is True and "re-run" in out["error"].lower()
    assert not sched_path.exists()                           # the artifact write failed
    row = _events(tmp_path).latest(CLINICAL, family="retention", kind="retention.schedule_published")
    assert row is not None                                    # ...but the [D] pin landed (the dangling state)


def test_show_toctou_file_vanishes_between_load_and_reread(tmp_path, capsys, monkeypatch):
    # C9: a TOCTOU — the file vanishes (operator rm / re-publish rename) between load_schedule and the
    # drift re-read must fall to the fail-closed empty branch, never a raw FileNotFoundError traceback.
    from alfred.scribe import schedule as sched_mod
    cfg, sched_path = _write_cfg(tmp_path)
    monkeypatch.setattr(sched_mod, "load_schedule", lambda p: sched_mod.default_schedule_v1())
    assert not sched_path.exists()                           # load 'succeeds' but the file is absent
    cli._cmd_scribe_retention(_ns(cfg, "show"))              # must NOT raise
    out = capsys.readouterr()
    assert json.loads(out.out)["schedule_present"] is False
    assert "vanished" in out.err                             # ILB


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


# ============================ keygen (13d-1) ============================
#
# The offline-key custody ceremony (design §3.1). CONTRACT: write ONLY the public
# recipient on-box; stream the PRIVATE identity to STDERR once (never a file, never
# the chain, never a log); JSON summary carries the PUBLIC fingerprint only. The
# structural pins (public-only-written, private-stays-off-stdout-and-off-logs,
# fail-closed guards) run UNCONDITIONALLY with a fake keypair; one dep-gated test
# asserts the real age recipient validity.


_FAKE_PUB = b"age1qfakepublicrecipientdonotusexxxxxxxxxxxxxxxxxxxxxxxxxxx"
_FAKE_PRIV = "AGE-SECRET-KEY-1FAKEIDENTITYDONOTUSEXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"


def _keygen_cfg(tmp_path, *, mode="clinical", set_pub_path=True):
    pub_path = tmp_path / "seal" / "seal_pub.age"
    retention = {"seal_public_key_path": str(pub_path)} if set_pub_path else {}
    body = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "clinicians": ["np_jamie"], "encounter_salt": "s", "mode": mode,
            "retention": retention,
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p), pub_path


def _keygen_ns(config, *, force=False):
    return argparse.Namespace(
        config=config, scribe_cmd="retention", retention_cmd="keygen", force=force)


def _patch_keypair(monkeypatch, pub=_FAKE_PUB, priv=_FAKE_PRIV):
    from alfred.scribe import retention as ret_mod
    monkeypatch.setattr(ret_mod, "generate_keypair", lambda: (pub, priv.encode("utf-8")))


def test_keygen_writes_public_only_streams_private_to_stderr(tmp_path, capsys, monkeypatch):
    """Public key written on-box; private identity ONLY on stderr — never stdout,
    never the pubkey file, never a captured log event."""
    from alfred.scribe import retention as ret_mod
    _patch_keypair(monkeypatch)
    cfg, pub_path = _keygen_cfg(tmp_path)
    with structlog.testing.capture_logs() as captured:
        cli._cmd_scribe_retention(_keygen_ns(cfg))
    cap = capsys.readouterr()
    # Public key file holds EXACTLY the recipient (a trailing newline is fine).
    assert pub_path.read_text(encoding="utf-8").strip() == _FAKE_PUB.decode()
    # JSON summary (stdout) carries the PUBLIC fingerprint + rotated flag — no private key.
    out = json.loads(cap.out)
    assert out == {
        "keygen": True, "seal_public_key_path": str(pub_path),
        "sealed_to_key_fp": ret_mod.key_fingerprint(_FAKE_PUB), "rotated": False,
    }
    assert "AGE-SECRET-KEY" not in cap.out
    # The private identity IS delivered on stderr (the one-time custody block).
    assert _FAKE_PRIV in cap.err
    # And it appears in NO log event (no structlog line ever carries the secret).
    assert not any("AGE-SECRET-KEY" in repr(rec) for rec in captured)


def test_keygen_unset_path_errors(tmp_path, capsys, monkeypatch):
    """seal_public_key_path unset ⇒ fail-closed error, exit 1, no keypair minted."""
    _patch_keypair(monkeypatch)  # would succeed if reached — proves we fail BEFORE it
    cfg, _ = _keygen_cfg(tmp_path, set_pub_path=False)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_keygen_ns(cfg))
    assert "unset" in json.dumps(_stdout_json(capsys))


def test_keygen_refuses_overwrite_without_force(tmp_path, capsys, monkeypatch):
    """An existing pubkey is NOT overwritten without --force; the file is untouched."""
    _patch_keypair(monkeypatch)
    cfg, pub_path = _keygen_cfg(tmp_path)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_text("age1existingkeyxxxxxxxxxxxxxx\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_keygen_ns(cfg, force=False))
    assert "--force" in json.dumps(_stdout_json(capsys))
    # UNCHANGED — never clobbered a live key without an explicit rotation.
    assert pub_path.read_text(encoding="utf-8") == "age1existingkeyxxxxxxxxxxxxxx\n"


def test_keygen_force_rotates_new_key(tmp_path, capsys, monkeypatch):
    """--force rotates: a NEW key is written + rotated:true (additive rotation)."""
    cfg, pub_path = _keygen_cfg(tmp_path)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.write_text("age1oldkeyxxxxxxxxxxxxxxxxxxxx\n", encoding="utf-8")
    _patch_keypair(monkeypatch, pub=b"age1rotatednewkeyxxxxxxxxxxxxx")
    cli._cmd_scribe_retention(_keygen_ns(cfg, force=True))
    out = _stdout_json(capsys)
    assert out["rotated"] is True
    assert pub_path.read_text(encoding="utf-8").strip() == "age1rotatednewkeyxxxxxxxxxxxxx"


def test_keygen_sealer_unavailable_fails_closed(tmp_path, capsys, monkeypatch):
    """pyrage absent ⇒ SealerUnavailable ⇒ fail-closed error, exit 1, no pubkey written."""
    from alfred.scribe import retention as ret_mod

    def _boom():
        raise ret_mod.SealerUnavailable("no pyrage")

    monkeypatch.setattr(ret_mod, "generate_keypair", _boom)
    cfg, pub_path = _keygen_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cli._cmd_scribe_retention(_keygen_ns(cfg))
    assert "pyrage" in json.dumps(_stdout_json(capsys))
    assert not pub_path.exists()


def test_keygen_real_crypto_writes_valid_recipient(tmp_path, capsys):
    """Dep-gated: with pyrage present, keygen writes a CANONICAL age recipient (the
    sweep's is_valid_age_recipient accepts it) and the fp matches key_fingerprint."""
    from alfred.scribe import retention as ret_mod
    pytest.importorskip("pyrage")
    cfg, pub_path = _keygen_cfg(tmp_path)
    cli._cmd_scribe_retention(_keygen_ns(cfg))
    out = _stdout_json(capsys)
    written = pub_path.read_text(encoding="utf-8").strip()
    assert ret_mod.is_valid_age_recipient(written)
    assert out["sealed_to_key_fp"] == ret_mod.key_fingerprint(written.encode("utf-8"))
