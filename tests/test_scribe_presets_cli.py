"""P4-5 `alfred scribe presets {list|audit|delete}` — CLI contract tests.

Local file ops over the voice-enrollment store: list (+ classification + orphaned
biometrics), audit (names joined at DISPLAY time; the audit.log itself is preset_id-only),
delete (revoke + tombstone). Drives the real parser + handler.
"""

from __future__ import annotations

import yaml

from alfred.cli import build_parser, cmd_scribe
from alfred.scribe import embed_voice, enroll_learning
from alfred.scribe import enrollment as en
from alfred.scribe.config import load_from_unified

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _cfg_obj(enroll_dir):
    return load_from_unified({"scribe": {
        "encounter_salt": _SALT, "stt": {"provider": "fake"},
        "diarize": {"provider": "fake", "enrollment_dir": str(enroll_dir)},
    }})


def _write_config(tmp_path, enroll_dir, clinicians):
    cfg = {"scribe": {
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
        "diarize": {"provider": "fake", "enrollment_dir": str(enroll_dir)},
        "clinicians": list(clinicians),
    }}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def _enroll(enroll_dir, user, *, name="Room A"):
    cfg = _cfg_obj(enroll_dir)
    centroid = en.spherical_mean_centroid(embed_voice.embed_windows(cfg, [b"voice-window"]))
    now = en._iso_now()
    p = en.Preset(
        preset_id=en.mint_preset_id(), user=user, name=name, status=en.STATUS_ACTIVE,
        centroids=[centroid], embedding_dim=len(centroid),
        centroid_digest=en.centroid_digest([centroid]), centroid_version=1,
        centroid_source=en.CENTROID_SOURCE_RECORDED, enrolled_at=now, created_at=now,
        updated_at=now, engine=embed_voice.engine_fingerprint(cfg),
        sample_stats={"n_windows": 1, "duration_s": 30.0, "net_speech_s": 30.0,
                      "snr_db_est": 20.0, "spread": 0.0},
        quality={"verdict": "ok", "advisory": {}}, device_hint={},
    )
    en.write_preset(str(enroll_dir), p, is_new=True)
    return p


def _run(config, *argv):
    args = build_parser().parse_args(["--config", config, "scribe", "presets", *argv])
    cmd_scribe(args)


# --- list -------------------------------------------------------------------

def test_list_shows_preset_with_classification(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    p = _enroll(enroll, "np_jamie", name="Clinic Room A")
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    _run(config, "list", "--user", "np_jamie")
    out = capsys.readouterr().out
    assert p.preset_id in out
    assert "usable" in out and "Clinic Room A" in out
    assert "ORPHANED" not in out                       # np_jamie IS a clinician


def test_list_flags_orphaned_biometrics(tmp_path, capsys):
    # a preset for a user NO LONGER in scribe.clinicians → flagged ORPHANED.
    enroll = tmp_path / "enroll"
    _enroll(enroll, "ex_locum")
    config = _write_config(tmp_path, enroll, ["np_jamie"])   # ex_locum not a clinician
    _run(config, "list")                                     # no --user → enumerate all
    out = capsys.readouterr().out
    assert "ex_locum" in out and "ORPHANED" in out


def test_list_empty_is_explicit(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    enroll.mkdir()
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    _run(config, "list")
    assert "No enrolled users." in capsys.readouterr().out   # intentionally-left-blank


# --- audit ------------------------------------------------------------------

def test_audit_joins_names_at_display_time(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    p = _enroll(enroll, "np_jamie", name="Clinic Room A")
    enroll_learning.audit(str(enroll), "preset_created", preset_id=p.preset_id, user="np_jamie")
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    _run(config, "audit")
    out = capsys.readouterr().out
    assert "preset_created" in out and p.preset_id in out
    assert "Clinic Room A" in out                       # NAME joined from the preset file
    assert "No orphaned biometrics" in out


def test_audit_flags_orphans(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    p = _enroll(enroll, "ex_locum")
    enroll_learning.audit(str(enroll), "preset_created", preset_id=p.preset_id, user="ex_locum")
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    _run(config, "audit")
    out = capsys.readouterr().out
    assert "ORPHANED" in out and "ex_locum" in out


# --- delete -----------------------------------------------------------------

def test_delete_revokes_and_tombstones(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    p = _enroll(enroll, "np_jamie")
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    _run(config, "delete", "--user", "np_jamie", "--preset", p.preset_id)
    out = capsys.readouterr().out
    assert "Deleted" in out and p.preset_id in out
    # the preset file is now a tombstone: revoked, centroids dropped, id retained.
    preset, _ = en.load_preset(en.preset_path(str(enroll), "np_jamie", p.preset_id))
    assert preset.status == en.STATUS_REVOKED and preset.centroids == []
    # ...and a preset_deleted audit event landed.
    audit = (enroll / enroll_learning.AUDIT_NAME).read_text(encoding="utf-8")
    assert "preset_deleted" in audit and p.preset_id in audit


def test_delete_unknown_preset_fails_cleanly(tmp_path, capsys):
    enroll = tmp_path / "enroll"
    enroll.mkdir()
    config = _write_config(tmp_path, enroll, ["np_jamie"])
    import pytest
    with pytest.raises(SystemExit):
        _run(config, "delete", "--user", "np_jamie", "--preset", "pst-1720000000000-0123456789abcdef")
    assert "Delete failed" in capsys.readouterr().out
