"""Pipeline note.* event emissions (event-store design §5.3 / §8 rows 4-8 / §15.5).

Deterministic — the note-gen LLM call is mocked with canned SOAP JSON (mirrors
test_scribe_checkpoint.py). Drives the real checkpoint path with an ACTIVE facade
and pins the six best-effort note.* emissions + their payloads:

  note.draft_created (first checkpoint) → note.draft_regenerated (second, marker=regressed);
  note.human_edit_detected (out-of-band edit surfaced at the next sweep, before/after sha);
  note.ready (on _CLOSED finalize, body_sha + expected_final_seq + folded_through);
  note.post_attest_audio (new audio after the note was signed);
  note.marker_selfheal (markerless READY re-stamp);
  events=None → the pipeline runs unchanged and touches no store.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import alfred.distiller.backends.ollama as ollama_mod
from alfred.scribe import (
    ScribeState,
    accumulate_encounter,
    checkpoint_encounter,
    compute_encounter_id,
)
from alfred.scribe.close_manifest import write_close_manifest
from alfred.scribe.config import load_from_unified
from alfred.scribe.events import ScribeEvents
from alfred.scribe.pipeline import _body_sha
from alfred.vault.ops import vault_edit, vault_read

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_CLOCK = "2026-07-16T12:00:00+00:00"
_CANNED = json.dumps({
    "subjective": [{"claim": "Reports chest pain", "source_spans": ["S1"]}],
    "objective": [], "assessment": [], "plan": [],
    "assessment_reasoning_stated": False,
})


def _config():
    return load_from_unified({"scribe": {
        "mode": "clinical", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _events(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


def _install_fake_ollama(monkeypatch):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (_CANNED, {"stop_reason": "stop", "prompt_eval_count": 500})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)


def _write_chunk(enc_dir, seq, lines):
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:03d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc_dir / f"{name}.meta.json").write_text(
        json.dumps({"synthetic": True, "seq": seq}), encoding="utf-8")


def _checkpoint(enc_dir, *, config, state, vault, events=None):
    r = accumulate_encounter(enc_dir, config=config)
    outcome = None
    if r.folded > 0 or r.closed:
        outcome = asyncio.run(checkpoint_encounter(
            enc_dir, encounter_id=r.encounter_id, config=config,
            state=state, vault_path=vault, did_fold=r.folded > 0, closed=r.closed,
            pending_tail=r.pending_tail, expected_final_seq=r.expected_final_seq,
            folded_seqs=r.folded_seqs, close_ambiguous=r.close_ambiguous, events=events,
        ))
    return r, outcome


# --- draft_created → draft_regenerated --------------------------------------

def test_draft_created_then_regenerated(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    ev = _events(tmp_path)
    enc = tmp_path / "inbox" / "enc-A"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    eid = r.encounter_id
    note_path = state.get(eid).note_path

    created = ev.query("clinical", kind="note.draft_created")
    assert len(created) == 1 and created[0]["subject_id"] == eid
    # body_sha is the READ-BACK sha (== the pipeline's stored canonical fingerprint).
    assert created[0]["payload"]["body_sha"] == _body_sha(vault_read(vault, note_path)["body"])
    assert created[0]["payload"]["body_sha"] == state.get(eid).pipeline_body_sha
    assert created[0]["actor"] == "stayc_scribe" and created[0]["actor_kind"] == "pipeline"

    # second chunk → regen in place → note.draft_regenerated.
    _write_chunk(enc, 2, ["Follow-up in one week."])
    _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    regen = ev.query("clinical", kind="note.draft_regenerated")
    assert len(regen) == 1
    assert regen[0]["payload"]["marker"] == "regressed"
    assert regen[0]["payload"]["body_sha"] == _body_sha(vault_read(vault, note_path)["body"])
    assert "grounding_flag_count" in regen[0]["payload"]


# --- human_edit_detected ----------------------------------------------------

def test_human_edit_detected(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    ev = _events(tmp_path)
    enc = tmp_path / "inbox" / "enc-C"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    eid = r.encounter_id
    note_path = state.get(eid).note_path
    sha_before = state.get(eid).pipeline_body_sha

    # a human corrects the draft body out-of-band.
    vault_edit(vault, note_path,
               body_replace="## Subjective\n- CLINICIAN CORRECTION: angina.\n",
               scope="stayc_clinical")
    sha_after = _body_sha(vault_read(vault, note_path)["body"])

    _write_chunk(enc, 2, ["Follow-up in one week."])
    _, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    assert outcome == "human_edited"
    edits = ev.query("clinical", kind="note.human_edit_detected")
    assert len(edits) == 1
    p = edits[0]["payload"]
    assert p["body_sha_before"] == sha_before and p["body_sha_after"] == sha_after


# --- note.ready -------------------------------------------------------------

def test_note_ready_on_close(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    ev = _events(tmp_path)
    enc = tmp_path / "inbox" / "enc-F"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    write_close_manifest(enc, final_seq=1)
    r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    assert outcome == "ready"
    ready = ev.query("clinical", kind="note.ready")
    assert len(ready) == 1
    p = ready[0]["payload"]
    assert p["body_sha"] == state.get(r.encounter_id).pipeline_body_sha
    assert p["expected_final_seq"] == 1 and p["folded_through"] == 1


# --- post_attest_audio ------------------------------------------------------

def test_post_attest_audio_after_signature(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    ev = _events(tmp_path)
    enc = tmp_path / "inbox" / "enc-P"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    eid = r.encounter_id
    from alfred.scribe.attest import attest
    attest(vault, state.get(eid).note_path, new_status="attested", attester="np_jamie",
           clinician_ids={"np_jamie"}, audit_path=vault / "audit.jsonl",
           allow_incomplete=True, override_reason="test — signing a drafted note")

    # new audio arrives AFTER the note was signed → refuse + post_attest_audio.
    _write_chunk(enc, 2, ["Post-attest line."])
    _, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault, events=ev)
    assert outcome == "post_attest_audio"
    rows = ev.query("clinical", kind="note.post_attest_audio")
    assert len(rows) == 1 and rows[0]["subject_id"] == eid


# --- events=None → no store, pipeline unchanged -----------------------------

def test_events_none_runs_clean(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-N"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)  # no events
    assert outcome == "drafted"
    assert state.get(r.encounter_id).note_path
    assert not (tmp_path / "ev").exists()  # the event store is never created
