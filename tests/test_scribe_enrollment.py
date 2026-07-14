"""P4-5a enrollment store / registry / binding / resolve (torch-free, fake embed).

Covers the frozen DATA MODEL: id grammar, user/clinicians gate, centroid math +
canonical digest, atomic 0600 writes, preset round-trip, the load contract WITH
TEETH + the classification set, cap 32, revoke tombstone, binding write-once, and
resolve_for_encounter (every typed refusal + the digest pin + fail-open + log).
"""

from __future__ import annotations

import json
import os
import stat

import pytest
import structlog

from alfred.scribe import enrollment as en
from alfred.scribe.embed_voice import _fake_embed, engine_fingerprint
from alfred.scribe.config import load_from_unified

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _fp():
    return engine_fingerprint(load_from_unified(
        {"scribe": {"encounter_salt": _SALT, "stt": {"provider": "fake"},
                    "diarize": {"provider": "fake"}}}))


def _centroid(seed=b"jamie-voice"):
    vecs = [_fake_embed(seed + str(i).encode()) for i in range(6)]
    return en.spherical_mean_centroid(vecs)


def _make_preset(user="np_jamie", *, name="Clinic room A / iPhone", version=1,
                 engine=None, centroid=None):
    c = centroid if centroid is not None else _centroid()
    now = en._iso_now()
    return en.Preset(
        preset_id=en.mint_preset_id(), user=user, name=name, status=en.STATUS_ACTIVE,
        centroids=[c], embedding_dim=len(c), centroid_digest=en.centroid_digest([c]),
        centroid_version=version, centroid_source=en.CENTROID_SOURCE_RECORDED,
        enrolled_at=now, created_at=now, updated_at=now,
        engine=engine if engine is not None else _fp(),
        sample_stats={"n_windows": 6, "net_speech_s": 30.0},
        quality={"verdict": "ok", "advisory": {}}, device_hint={"mic_label": "iPhone"},
    )


# --- id grammar + identity ---------------------------------------------------

def test_id_grammar_fullmatch():
    assert en.PRESET_ID_RE.fullmatch(en.mint_preset_id())
    assert en.SESSION_ID_RE.fullmatch(en.mint_session_id())
    assert not en.PRESET_ID_RE.fullmatch("pst-123-abc")           # too short
    assert not en.PRESET_ID_RE.fullmatch("enr-" + "0" * 13 + "-" + "a" * 16)  # wrong prefix


@pytest.mark.parametrize("user,ok", [
    ("np_jamie", True), ("dr.smith", True), ("a", True), ("A_bad", False),
    ("", False), ("has space", False), ("x" * 65, False), ("_leading", False),
])
def test_user_regex(user, ok):
    assert en.valid_user(user) is ok


def test_validate_user_for_enroll_fail_closed():
    en.validate_user_for_enroll("np_jamie", ["np_jamie", "dr_x"])   # ok
    with pytest.raises(en.EnrollmentError):
        en.validate_user_for_enroll("np_jamie", ["dr_x"])           # not a clinician
    with pytest.raises(en.EnrollmentError):
        en.validate_user_for_enroll("Bad Name", ["Bad Name"])       # bad shape (even if listed)


# --- centroid math + digest --------------------------------------------------

def test_spherical_mean_is_unit_and_deterministic():
    c1 = _centroid(b"seed")
    c2 = _centroid(b"seed")
    assert c1 == c2
    assert abs(en.l2_norm(c1) - 1.0) < 1e-9


def test_centroid_digest_reproducible_and_sensitive():
    c = _centroid()
    assert en.centroid_digest([c]) == en.centroid_digest([c])
    perturbed = list(c); perturbed[0] += 0.01
    assert en.centroid_digest([c]) != en.centroid_digest([perturbed])


def test_cosine_self_is_one():
    c = _centroid()
    assert abs(en.cosine(c, c) - 1.0) < 1e-9


# --- atomic 0600 store -------------------------------------------------------

def test_write_preset_is_0600_and_under_enrollment_dir_only(tmp_path):
    d = tmp_path / "enroll"
    p = _make_preset()
    path = en.write_preset(d, p, is_new=True)
    assert path.parent == d / "np_jamie"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    # ONLY the preset file exists (no raw-audio / tmp residue)
    assert [x.name for x in (d / "np_jamie").iterdir()] == [f"{p.preset_id}.json"]


def test_preset_round_trips():
    p = _make_preset()
    p2 = en.Preset.from_dict(p.to_dict())
    assert p2.to_dict() == p.to_dict()


# --- load contract teeth + classification ------------------------------------

def test_load_usable(tmp_path):
    d = tmp_path / "enroll"
    p = _make_preset()
    en.write_preset(d, p, is_new=True)
    entries = en.list_user_presets(d, "np_jamie", _fp())
    assert len(entries) == 1 and entries[0].classification == en.CLASS_USABLE


def test_load_unsupported_schema(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    raw = p.to_dict(); raw["schema_version"] = 99
    _write_raw(d, p, raw)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_UNSUPPORTED_SCHEMA


def test_load_corrupt_bad_digest(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    raw = p.to_dict(); raw["centroid_digest"] = "deadbeef"      # digest ≠ centroids
    _write_raw(d, p, raw)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_CORRUPT


def test_load_corrupt_dim_mismatch(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    raw = p.to_dict(); raw["embedding_dim"] = len(raw["centroids"][0]) + 1
    _write_raw(d, p, raw)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_CORRUPT


def test_load_corrupt_non_unit_centroid(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    raw = p.to_dict(); raw["centroids"] = [[2.0] + [0.0] * (raw["embedding_dim"] - 1)]
    raw["centroid_digest"] = en.centroid_digest(raw["centroids"])   # digest ok, but not unit
    _write_raw(d, p, raw)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_CORRUPT


def test_load_corrupt_id_path_disagreement(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    raw = p.to_dict()
    ud = d / "np_jamie"; ud.mkdir(parents=True)
    (ud / f"{p.preset_id}.json").write_text(json.dumps({**raw, "preset_id": en.mint_preset_id()}))
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_CORRUPT


def test_classify_incompatible_model(tmp_path):
    d = tmp_path / "enroll"
    p = _make_preset(engine={"embedding_model": "OTHER", "embedding_revision": "r", "engine_version": "v"})
    en.write_preset(d, p, is_new=True)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_INCOMPATIBLE_MODEL


def test_classify_incompatible_engine(tmp_path):
    d = tmp_path / "enroll"
    fp = _fp()
    p = _make_preset(engine={**fp, "engine_version": "OLD"})
    en.write_preset(d, p, is_new=True)
    assert en.list_user_presets(d, "np_jamie", _fp())[0].classification == en.CLASS_INCOMPATIBLE_ENGINE


# --- cap + write guards ------------------------------------------------------

def test_write_preset_cap(tmp_path, monkeypatch):
    d = tmp_path / "enroll"
    monkeypatch.setattr(en, "MAX_PRESETS_PER_USER", 2)
    en.write_preset(d, _make_preset(), is_new=True)
    en.write_preset(d, _make_preset(), is_new=True)
    with pytest.raises(en.EnrollmentError, match="preset_cap"):
        en.write_preset(d, _make_preset(), is_new=True)


def test_write_preset_rejects_bad_digest(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    p.centroid_digest = "wrong"
    with pytest.raises(en.EnrollmentError):
        en.write_preset(d, p, is_new=True)


# --- revoke tombstone --------------------------------------------------------

def test_revoke_tombstones_and_blocks_reuse(tmp_path):
    d = tmp_path / "enroll"; p = _make_preset()
    en.write_preset(d, p, is_new=True)
    en.revoke_preset(d, "np_jamie", p.preset_id, reason="mic changed")
    entries = en.list_user_presets(d, "np_jamie", _fp())
    assert len(entries) == 1 and entries[0].classification == en.CLASS_REVOKED
    assert entries[0].preset.centroids == []                    # centroids dropped
    assert entries[0].preset.revoked["reason"] == "mic changed"
    assert en.count_active_presets(d, "np_jamie") == 1          # id still blocks reuse


# --- binding sidecar ---------------------------------------------------------

def test_binding_write_once(tmp_path):
    enc = tmp_path / "enc"
    p = _make_preset()
    en.write_binding(enc, p)
    b = en.read_binding(enc)
    assert b["preset_id"] == p.preset_id and b["centroid_digest"] == p.centroid_digest
    assert "centroids" not in b                                 # ids only, no biometric
    with pytest.raises(en.EnrollmentError):
        en.write_binding(enc, p)                                # write-once


# --- resolve_for_encounter (refusals + digest pin + fail-open) ---------------

def _bind_and_resolve(tmp_path, *, mutate_binding=None, mutate_preset=None, write_preset=True):
    d = tmp_path / "enroll"; enc = tmp_path / "enc"
    p = _make_preset()
    if mutate_preset:
        mutate_preset(p)
    if write_preset:
        en.write_preset(d, p, is_new=True)
    en.write_binding(enc, p)
    if mutate_binding:
        b = en.read_binding(enc)
        mutate_binding(b)
        (enc / en.BINDING_NAME).write_text(json.dumps(b))
    return en.resolve_for_encounter(enc, d, _fp()), p


def test_resolve_usable(tmp_path):
    r, p = _bind_and_resolve(tmp_path)
    assert isinstance(r, en.ResolvedEnrollment)
    assert r.preset_id == p.preset_id and r.centroids == p.centroids


def test_resolve_no_binding(tmp_path):
    assert en.resolve_for_encounter(tmp_path / "enc", tmp_path / "enroll", _fp()) == en.REFUSAL_NO_BINDING


def test_resolve_unknown_preset(tmp_path):
    r, _ = _bind_and_resolve(tmp_path, write_preset=False)      # bound but no preset file
    assert r == en.REFUSAL_UNKNOWN_PRESET


def test_resolve_digest_mismatch_fails_open(tmp_path):
    # THE laundering close: a binding whose digest no longer matches the preset →
    # digest_mismatch → fail-open (never a silent re-anchor).
    def _bad_digest(b):
        b["centroid_digest"] = "0" * 64
    with structlog.testing.capture_logs() as caps:
        r, _ = _bind_and_resolve(tmp_path, mutate_binding=_bad_digest)
    assert r == en.REFUSAL_DIGEST_MISMATCH
    unusable = [c for c in caps if c.get("event") == "scribe.enrollment.unusable"]
    assert len(unusable) == 1 and unusable[0]["reason"] == en.REFUSAL_DIGEST_MISMATCH


def test_resolve_revoked(tmp_path):
    d = tmp_path / "enroll"; enc = tmp_path / "enc"
    p = _make_preset()
    en.write_preset(d, p, is_new=True)
    en.write_binding(enc, p)
    en.revoke_preset(d, "np_jamie", p.preset_id, reason="x")
    assert en.resolve_for_encounter(enc, d, _fp()) == en.REFUSAL_REVOKED


def test_resolve_incompatible_engine(tmp_path):
    d = tmp_path / "enroll"; enc = tmp_path / "enc"
    p = _make_preset(engine={**_fp(), "engine_version": "OLD"})
    en.write_preset(d, p, is_new=True)
    en.write_binding(enc, p)
    assert en.resolve_for_encounter(enc, d, _fp()) == en.REFUSAL_INCOMPATIBLE_ENGINE


def test_every_refusal_logs_unusable(tmp_path):
    with structlog.testing.capture_logs() as caps:
        en.resolve_for_encounter(tmp_path / "enc", tmp_path / "enroll", _fp())
    assert any(c.get("event") == "scribe.enrollment.unusable"
               and c.get("reason") == en.REFUSAL_NO_BINDING for c in caps)


def _write_raw(d, preset, raw):
    ud = d / preset.user
    ud.mkdir(parents=True, exist_ok=True)
    (ud / f"{preset.preset_id}.json").write_text(json.dumps(raw), encoding="utf-8")
