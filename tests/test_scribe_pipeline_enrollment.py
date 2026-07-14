"""P4-5 pipeline INTEGRATION — resolve → provenance → diarize_stats → fail-open.

Contract-first (memo-derived): the pipeline resolves the encounter's bound voice preset
(fail-open to all-unknown on ANY typed refusal), stamps WHICH preset attributed the note
(ledger ``diarize_preset`` → frontmatter ``diarize_provenance``), and lands a per-chunk
``diarize_stats`` capture row for EVERY folded chunk (no-preset rows still land). Uses the
fake STT + fake diarize (role tags) + fake embed seams — torch-free.
"""

from __future__ import annotations

import asyncio
import json

import frontmatter
import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
from alfred.scribe import embed_voice
from alfred.scribe import enroll_learning
from alfred.scribe import enrollment as en
from alfred.scribe.config import load_from_unified
from alfred.scribe.pipeline import accumulate_encounter, checkpoint_encounter
from alfred.scribe.state import ScribeState
from alfred.scribe.ledger import ledger_path, load_ledger

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_USER = "np_jamie"


def _config(tmp_path, *, enrollment_dir="__set__"):
    diarize = {"provider": "fake"}
    if enrollment_dir == "__set__":
        diarize["enrollment_dir"] = str(tmp_path / "enroll")
    elif enrollment_dir is not None:
        diarize["enrollment_dir"] = str(enrollment_dir)
    return load_from_unified({"scribe": {
        "mode": "synthetic", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
        "diarize": diarize,
    }})


def _make_preset(cfg, user, *, name="Room A", version=1, window=b"clinician-voice-window"):
    """Build a USABLE preset stamped with the runtime (fake) engine fingerprint, so it
    resolves + classifies usable. A distinct ``window`` yields a distinct centroid/digest."""
    fp = embed_voice.engine_fingerprint(cfg)
    centroid = en.spherical_mean_centroid(embed_voice.embed_windows(cfg, [window]))
    now = en._iso_now()
    return en.Preset(
        preset_id=en.mint_preset_id(), user=user, name=name, status=en.STATUS_ACTIVE,
        centroids=[centroid], embedding_dim=len(centroid),
        centroid_digest=en.centroid_digest([centroid]), centroid_version=version,
        centroid_source=en.CENTROID_SOURCE_RECORDED, enrolled_at=now, created_at=now,
        updated_at=now, engine=fp,
        sample_stats={"n_windows": 1, "duration_s": 30.0, "net_speech_s": 30.0,
                      "snr_db_est": 20.0, "spread": 0.0},
        quality={"verdict": "ok", "advisory": {}}, device_hint={},
    )


def _enroll_and_bind(cfg, enc_dir, user, *, name="Room A"):
    """Enroll a preset + bind it to the encounter. Returns the Preset."""
    p = _make_preset(cfg, user, name=name)
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    enc_dir.mkdir(parents=True, exist_ok=True)
    en.write_binding(enc_dir, p)
    return p


def _write_chunk(enc_dir, seq, lines):
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:03d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc_dir / f"{name}.meta.json").write_text(
        json.dumps({"synthetic": True, "seq": seq}), encoding="utf-8")


_TAGGED = ["[CLIN] Doctor asks about symptoms.", "[PT] Patient reports chest pain."]


def _capture_rows(cfg):
    path = enroll_learning._capture_path(cfg.diarize.enrollment_dir)
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _diarize_rows(cfg):
    return [r for r in _capture_rows(cfg) if r.get("kind") == "diarize_stats"]


# --- resolve → ledger provenance + diarize_stats ----------------------------

def test_resolved_preset_stamps_ledger_provenance_and_diarize_stats(tmp_path):
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-A"
    preset = _enroll_and_bind(cfg, enc, _USER)
    _write_chunk(enc, 1, _TAGGED)

    r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 1

    # ledger carries WHICH preset anchored the encounter.
    led = load_ledger(ledger_path(enc, r.encounter_id))
    prov = led.diarize_preset
    assert prov["user"] == _USER
    assert prov["preset_id"] == preset.preset_id
    assert prov["centroid_version"] == 1
    assert prov["engine_fingerprint"] == embed_voice.engine_fingerprint(cfg)

    # a diarize_stats row landed, carrying the preset provenance + the role counts
    # (fake diarize assigned clinician + patient from the [CLIN]/[PT] tags).
    rows = _diarize_rows(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row["preset_id"] == preset.preset_id and row["user"] == _USER
    assert row["centroid_version"] == 1 and row["chunk_seq"] == 1
    assert row["role_counts"]["clinician"] == 1 and row["role_counts"]["patient"] == 1


def test_no_binding_lands_null_preset_row_no_unusable_log(tmp_path):
    # enrollment configured but NO preset bound → a first-class choice: resolved None,
    # NO scribe.enrollment.unusable spam, but the diarize_stats row STILL lands (null preset).
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-B"
    _write_chunk(enc, 1, _TAGGED)
    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 1
    led = load_ledger(ledger_path(enc, r.encounter_id))
    assert led.diarize_preset is None                     # un-anchored
    rows = _diarize_rows(cfg)
    assert len(rows) == 1 and rows[0]["preset_id"] is None and rows[0]["user"] is None
    assert not [c for c in cap if c.get("event") == "scribe.enrollment.unusable"]
    assert not [c for c in cap if c.get("event") == "scribe.enrollment.new_preset_first_use"]


def test_digest_mismatch_fails_open_with_unusable_log(tmp_path):
    # bind preset v1, then RE-RECORD in place (v2, new centroid/digest). The old binding's
    # pinned digest no longer matches → digest_mismatch → resolved None (fail-open) + a
    # loud reason-coded log. The encounter STILL folds, un-anchored.
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-C"
    p1 = _enroll_and_bind(cfg, enc, _USER)                # binding pins v1 digest
    p2 = _make_preset(cfg, _USER, version=2, window=b"a-totally-different-voice!!")
    p2.preset_id, p2.created_at = p1.preset_id, p1.created_at
    en.write_preset(cfg.diarize.enrollment_dir, p2, is_new=False)   # in-place re-record
    _write_chunk(enc, 1, _TAGGED)

    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 1
    led = load_ledger(ledger_path(enc, r.encounter_id))
    assert led.diarize_preset is None                     # fail-open: un-anchored
    unusable = [c for c in cap if c.get("event") == "scribe.enrollment.unusable"]
    assert len(unusable) == 1
    assert unusable[0]["reason"] == en.REFUSAL_DIGEST_MISMATCH
    assert unusable[0]["artifact"] == "binding"
    # the diarize_stats row lands with a NULL preset (resolution refused → no anchor).
    assert _diarize_rows(cfg)[0]["preset_id"] is None


def test_new_preset_first_use_fires_once(tmp_path):
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-D"
    _enroll_and_bind(cfg, enc, _USER)
    _write_chunk(enc, 1, _TAGGED)
    with structlog.testing.capture_logs() as cap1:
        accumulate_encounter(enc, config=cfg)
    first = [c for c in cap1 if c.get("event") == "scribe.enrollment.new_preset_first_use"]
    assert len(first) == 1                                # fires on the first use

    _write_chunk(enc, 2, ["[PT] Still hurts."])
    with structlog.testing.capture_logs() as cap2:
        accumulate_encounter(enc, config=cfg)
    # prior diarize_stats rows now exist for (preset_id, v1) → NOT first use → silent.
    assert not [c for c in cap2 if c.get("event") == "scribe.enrollment.new_preset_first_use"]


def test_diarize_stats_is_phi_free(tmp_path):
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-E"
    secret = "MRS-SMITH-ROOM-3B"
    _enroll_and_bind(cfg, enc, _USER, name=secret)
    _write_chunk(enc, 1, _TAGGED)
    accumulate_encounter(enc, config=cfg)
    raw = enroll_learning._capture_path(cfg.diarize.enrollment_dir).read_text(encoding="utf-8")
    assert secret not in raw                              # the preset NAME never enters the sink


def test_no_capture_when_enrollment_dormant(tmp_path):
    # enrollment_dir unset (dormant) → NO capture sink is created (nothing to write to),
    # and diarization still runs (fake) — the encounter folds normally.
    cfg = _config(tmp_path, enrollment_dir=None)
    enc = tmp_path / "inbox" / "enc-F"
    _write_chunk(enc, 1, _TAGGED)
    r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 1
    assert not (tmp_path / "enroll").exists()             # no sink dir materialized


# --- provenance round-trips to the note frontmatter (full draft flow) -------

_CANNED = json.dumps({
    "subjective": [{"claim": "Reports chest pain", "source_spans": ["S2"]}],
    "objective": [], "assessment": [], "plan": [], "assessment_reasoning_stated": False,
})


def _install_fake_ollama(monkeypatch):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (_CANNED, {"stop_reason": "stop", "prompt_eval_count": 500})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)


def test_provenance_round_trips_to_note_frontmatter(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    cfg = _config(tmp_path)
    enc = tmp_path / "inbox" / "enc-G"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    preset = _enroll_and_bind(cfg, enc, _USER)
    _write_chunk(enc, 1, _TAGGED)

    r = accumulate_encounter(enc, config=cfg)
    outcome = asyncio.run(checkpoint_encounter(
        enc, encounter_id=r.encounter_id, config=cfg, state=state,
        vault_path=vault, did_fold=r.folded > 0, closed=r.closed,
    ))
    assert outcome == "drafted"

    note = frontmatter.load(str(vault / state.get(r.encounter_id).note_path))
    prov = note.get("diarize_provenance")
    assert prov is not None
    assert prov["preset_id"] == preset.preset_id
    assert prov["user"] == _USER
    assert prov["centroid_version"] == 1
    assert prov["engine_fingerprint"] == embed_voice.engine_fingerprint(cfg)
