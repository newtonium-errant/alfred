"""Checkpoint note-gen trigger tests (scribe P3-b2).

Deterministic — the LLM call (``call_ollama_no_tools``) is mocked with canned
SOAP JSON; the guards + wiring are pure. Covers:

  * FULL-REGEN from the ACCUMULATED transcript (an EARLY-segment cite resolves —
    proves the whole transcript, not just the last chunk, reaches note-gen);
  * the CONTEXT-BUDGET guard firing BEFORE the draft update (over-budget →
    ContextBudgetExceeded, last-good draft intact) — MUTATION-BOUND;
  * CLOBBER-DETECT (a human-edited body → freeze, never clobber) — MUTATION-BOUND;
  * UPDATE-IN-PLACE (checkpoint-2 body_replaces checkpoint-1, no duplicate);
  * ATTESTED draft → refuse (no clobber);
  * ``_CLOSED`` → ready.
"""

from __future__ import annotations

import asyncio
import json
import os

import frontmatter
import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
import alfred.scribe.pipeline as pipeline_mod
import time as _time

from alfred.scribe import (
    ContextBudgetExceeded,
    GROUNDING_UNVERIFIED,
    STATE_BUDGET_CAPPED,
    STATE_DRAFTED,
    STATE_HUMAN_EDITED,
    STATE_INCOMPLETE,
    STATE_POST_ATTEST_AUDIO,
    STATE_READY,
    ScribeState,
    Segment,
    Transcript,
    accumulate_encounter,
    checkpoint_encounter,
    compute_encounter_id,
    ledger_path,
    load_ledger,
    save_ledger,
)
from alfred.scribe.close_manifest import write_close_manifest
from alfred.scribe.config import load_from_unified
from alfred.scribe.notegen import NoteGenError, _NOTEGEN_OLLAMA_OPTIONS
from alfred.vault.ops import vault_read

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(mode="synthetic"):
    return load_from_unified({"scribe": {
        "mode": mode,
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


# A canned SOAP note citing S1 (an EARLY segment). Clean — no numbers/negations,
# so the ONLY way it flags is ungrounded_span (if S1 is absent from the passed
# transcript — i.e. the checkpoint passed only the last chunk instead of the
# full accumulated transcript).
_CANNED_CITES_S1 = json.dumps({
    "subjective": [{"claim": "Reports chest pain", "source_spans": ["S1"]}],
    "objective": [], "assessment": [], "plan": [],
    "assessment_reasoning_stated": False,
})


def _install_fake_ollama(monkeypatch, canned=_CANNED_CITES_S1):
    state = {"prompts": [], "calls": 0}

    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        state["prompts"].append(prompt)
        state["calls"] += 1
        # prompt_eval_count present + in-range (P3-b3 fail-loud requires it).
        return (canned, {"stop_reason": "stop", "prompt_eval_count": 500})

    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)
    return state


def _write_chunk(enc_dir, seq, lines):
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:03d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc_dir / f"{name}.meta.json").write_text(
        json.dumps({"synthetic": True, "seq": seq}), encoding="utf-8"
    )


def _checkpoint(enc_dir, *, config, state, vault):
    """Mirror run_sweep's per-encounter logic: accumulate → checkpoint (threads the
    #57 close-manifest fields exactly as run_sweep does)."""
    r = accumulate_encounter(enc_dir, config=config)
    outcome = None
    if r.folded > 0 or r.closed:
        outcome = asyncio.run(checkpoint_encounter(
            enc_dir, encounter_id=r.encounter_id, config=config,
            state=state, vault_path=vault, did_fold=r.folded > 0, closed=r.closed,
            pending_tail=r.pending_tail,   # Gap-A: block ready-finalize on an unfolded tail
            expected_final_seq=r.expected_final_seq,   # #57 promised bar
            folded_seqs=r.folded_seqs,                 # #57 ledger-truth
            close_ambiguous=r.close_ambiguous,         # #57 strict fail-closed
        ))
    return r, outcome


# ---------------------------------------------------------------------------
# scaffolding pins
# ---------------------------------------------------------------------------

def test_num_predict_is_bounded_and_budget_error_is_notegen():
    assert _NOTEGEN_OLLAMA_OPTIONS["num_predict"] == 2048
    assert issubclass(ContextBudgetExceeded, NoteGenError)


# ---------------------------------------------------------------------------
# 1. FULL-REGEN from the accumulated transcript
# ---------------------------------------------------------------------------

def test_checkpoint_full_regen_resolves_early_segment_cite(tmp_path, monkeypatch):
    fake = _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-A"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    # chunk 1 → S1 "chest pain" (the cited segment), S2.
    _write_chunk(enc, 1, ["Patient reports chest pain.", "History noted."])
    _checkpoint(enc, config=_config(), state=state, vault=vault)
    # chunk 2 → S3. Now regen from the FULL transcript (S1..S3); the mock cites S1.
    _write_chunk(enc, 2, ["Plan discussed."])
    r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    assert outcome == "drafted"
    st = state.get(r.encounter_id)
    note = frontmatter.load(str(vault / st.note_path))
    # the S1 cite RESOLVED — no ungrounded flag (would fire if only chunk-2 passed).
    assert "[S1]" in note.content
    assert GROUNDING_UNVERIFIED not in note.content
    assert not any(f.get("reason", "").startswith("ungrounded")
                   for f in note.get("grounding_flags", []))
    # the FULL accumulated transcript reached note-gen (all 3 segment ids present).
    last_prompt = fake["prompts"][-1]
    assert "S1 " in last_prompt and "S2 " in last_prompt and "S3 " in last_prompt


# ---------------------------------------------------------------------------
# 2. Context-budget guard — fires BEFORE the draft update (MUTATION-BOUND)
# ---------------------------------------------------------------------------

def test_budget_guard_preserves_last_good_draft(tmp_path, monkeypatch):
    fake = _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-B"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    # checkpoint 1 — a normal small draft.
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    eid = r.encounter_id
    good_path = state.get(eid).note_path
    good_body = vault_read(vault, good_path)["body"]
    calls_after_draft = fake["calls"]

    # Now the accumulated transcript grows PAST the context budget. Overwrite the
    # ledger with a huge transcript, then trigger a checkpoint (did_fold=True).
    huge = Transcript(source_id=eid, mode="synthetic", segments=[
        Segment(id=f"S{i+1}", start_s=i * 5.0, end_s=i * 5.0 + 5, text="x" * 500)
        for i in range(300)
    ])
    save_ledger(ledger_path(enc, eid), huge)
    outcome = asyncio.run(checkpoint_encounter(
        enc, encounter_id=eid, config=_config(), state=state, vault_path=vault,
        did_fold=True, closed=False,
    ))

    assert outcome == "budget_capped"
    assert state.get(eid).state == STATE_BUDGET_CAPPED
    # the LAST-GOOD draft is INTACT — never body_replaced with a truncated regen.
    assert vault_read(vault, good_path)["body"] == good_body
    # the guard fired BEFORE the LLM call — no new Ollama call happened.
    assert fake["calls"] == calls_after_draft


def test_budget_guard_log_is_phi_free(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "patient_jane_doe"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    eid = compute_encounter_id("patient_jane_doe", salt=_SALT)
    huge = Transcript(source_id=eid, mode="synthetic", segments=[
        Segment(id=f"S{i+1}", start_s=0.0, end_s=5.0, text="Jane Doe MRN 12345 " * 40)
        for i in range(200)
    ])
    ledger_path(enc, eid).parent.mkdir(parents=True, exist_ok=True)
    save_ledger(ledger_path(enc, eid), huge)
    with structlog.testing.capture_logs() as caps:
        asyncio.run(checkpoint_encounter(
            enc, encounter_id=eid, config=_config(), state=state, vault_path=vault,
            did_fold=True, closed=False,
        ))
    capped = [c for c in caps if c.get("event") == "scribe.pipeline.budget_capped"]
    assert len(capped) == 1 and capped[0]["encounter_id"] == eid
    # PHI-free: no patient label / transcript text anywhere in the logs.
    assert not any("jane" in str(v).lower() or "mrn" in str(v).lower()
                   for c in caps for v in c.values())


def test_context_budget_exceeded_log_fields(tmp_path, monkeypatch):
    # The notegen-layer budget signal (pre-commit #9) — pins the diagnostic
    # fields + that the LLM is never reached.
    from alfred.scribe.notegen import generate_structured
    fake = _install_fake_ollama(monkeypatch)
    huge = Transcript(source_id="enc-budget", mode="synthetic", segments=[
        Segment(id=f"S{i+1}", start_s=0.0, end_s=5.0, text="x" * 500) for i in range(200)
    ])
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(ContextBudgetExceeded):
            asyncio.run(generate_structured(huge, config=_config()))
    ev = [c for c in caps if c.get("event") == "scribe.notegen.context_budget_exceeded"]
    assert len(ev) == 1
    e = ev[0]
    assert e["source_id"] == "enc-budget" and e["segment_count"] == 200
    assert e["num_ctx"] == 8192 and e["est_tokens"] > e["budget"]
    assert fake["calls"] == 0   # the guard fired BEFORE the LLM call


# ---------------------------------------------------------------------------
# 2b. AUTHORITATIVE post-call truncation guard (Ollama's real prompt_eval_count)
# ---------------------------------------------------------------------------

def _fake_ollama_with_pec(canned, prompt_eval_count):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (canned, {"stop_reason": "stop", "prompt_eval_count": prompt_eval_count})
    return _fake


def test_post_call_truncation_raises_context_budget(monkeypatch):
    # A SMALL transcript CLEARS the pre-flight estimate, but Ollama reports a
    # prompt_eval_count at the truncation ceiling → the prompt was TRUNCATED →
    # ContextBudgetExceeded (the AUTHORITATIVE guard, using the model's real
    # tokenizer count, not an estimate).
    from alfred.scribe.notegen import generate_structured, _PROMPT_TRUNCATION_CEILING
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools",
                        _fake_ollama_with_pec(_CANNED_CITES_S1, _PROMPT_TRUNCATION_CEILING))
    t = Transcript(source_id="enc-trunc", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="chest pain")])
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(generate_structured(t, config=_config()))


def test_post_call_within_ceiling_is_accepted(monkeypatch):
    from alfred.scribe.notegen import generate_structured, _PROMPT_TRUNCATION_CEILING
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools",
                        _fake_ollama_with_pec(_CANNED_CITES_S1, _PROMPT_TRUNCATION_CEILING - 1))
    t = Transcript(source_id="enc-ok", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="chest pain")])
    s = asyncio.run(generate_structured(t, config=_config()))   # no raise — fit
    assert s.subjective[0].claim == "Reports chest pain"


def test_post_call_truncation_log_fields(monkeypatch):
    from alfred.scribe.notegen import generate_structured, _PROMPT_TRUNCATION_CEILING
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools",
                        _fake_ollama_with_pec(_CANNED_CITES_S1, 9000))
    t = Transcript(source_id="enc-trunc2", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="chest pain")])
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(ContextBudgetExceeded):
            asyncio.run(generate_structured(t, config=_config()))
    ev = [c for c in caps if c.get("event") == "scribe.notegen.prompt_truncated"]
    assert len(ev) == 1 and ev[0]["prompt_eval_count"] == 9000        # obs pin (#9)
    assert ev[0]["source_id"] == "enc-trunc2"
    assert ev[0]["ceiling"] == _PROMPT_TRUNCATION_CEILING


def test_post_call_truncation_preserves_draft_at_checkpoint(tmp_path, monkeypatch):
    # THE crown post-call MUTATION-BIND at the checkpoint level: checkpoint 1
    # drafts (pec in-range); on checkpoint 2 Ollama reports a TRUNCATED prompt →
    # the note is REFUSED (budget_capped), the last-good draft stays. Remove the
    # post-call check → checkpoint 2 body_replaces with the truncated-prompt note
    # → draft changes + state=drafted → RED.
    from alfred.scribe.notegen import _PROMPT_TRUNCATION_CEILING
    pec = {"v": 1000}

    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (_CANNED_CITES_S1, {"stop_reason": "stop", "prompt_eval_count": pec["v"]})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)

    enc = tmp_path / "inbox" / "enc-T"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    eid = r.encounter_id
    good_path = state.get(eid).note_path
    good_body = vault_read(vault, good_path)["body"]

    # Ollama now reports a TRUNCATED prompt on the regen.
    pec["v"] = _PROMPT_TRUNCATION_CEILING + 5
    _write_chunk(enc, 2, ["Follow-up in one week."])
    r2, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    assert outcome == "budget_capped"
    assert state.get(eid).state == STATE_BUDGET_CAPPED
    assert vault_read(vault, good_path)["body"] == good_body      # last-good draft INTACT


@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_QWEN_IT"),
    reason="real-qwen integration — set ALFRED_SCRIBE_QWEN_IT=1 with a running Ollama + qwen2.5-14b",
)
def test_real_qwen_post_call_truncation_empirical(monkeypatch):
    # EMPIRICAL verification (runs ONLY on the box): with the pre-flight estimate
    # bypassed, a real transcript LARGER than num_ctx reaches qwen; Ollama's REAL
    # prompt_eval_count trips the post-call ceiling → ContextBudgetExceeded. This
    # confirms Ollama's actual over-context behavior + the ceiling threshold.
    import alfred.scribe.notegen as ng
    monkeypatch.setattr(ng, "_estimate_tokens", lambda _t: 0)   # bypass the pre-flight hint
    segs = [Segment(id=f"S{i+1}", start_s=i * 5.0, end_s=i * 5.0 + 5,
                    text=f"BP 120/80 HR 72 temp 37.{i % 10} patient reports symptom {i}")
            for i in range(4000)]   # ~10x num_ctx worth of real tokens
    t = Transcript(source_id="enc-real-oversized", mode="synthetic", segments=segs)
    cfg = load_from_unified({"scribe": {
        "mode": "synthetic", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ng.generate_structured(t, config=cfg))


# ---------------------------------------------------------------------------
# 3. Clobber-detect — human edit freezes auto-evolution (MUTATION-BOUND)
# ---------------------------------------------------------------------------

def test_clobber_detect_freezes_on_human_edit(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-C"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    eid = r.encounter_id
    note_path = state.get(eid).note_path

    # A HUMAN edits the draft body on disk (a clinician correction).
    from alfred.vault.ops import vault_edit
    vault_edit(vault, note_path,
               body_replace="## Subjective\n- CLINICIAN CORRECTION: angina, not GERD.\n",
               scope="stayc_clinical")
    edited_body = vault_read(vault, note_path)["body"]

    # A new chunk folds → the checkpoint MUST NOT clobber the human edit.
    _write_chunk(enc, 2, ["Follow-up in one week."])
    with structlog.testing.capture_logs() as caps:
        r2, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    assert outcome == "human_edited"
    assert state.get(eid).state == STATE_HUMAN_EDITED
    edits = [c for c in caps if c.get("event") == "scribe.pipeline.human_edit_detected"]
    assert len(edits) == 1 and edits[0]["encounter_id"] == eid   # observability pin (#9)
    # the clinician correction is INTACT — the pipeline did not overwrite it.
    assert vault_read(vault, note_path)["body"] == edited_body
    assert "CLINICIAN CORRECTION" in vault_read(vault, note_path)["body"]

    # And it STAYS frozen on subsequent checkpoints (opt-in required to resume).
    _write_chunk(enc, 3, ["Another line."])
    _, outcome3 = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert outcome3 == "human_edited_frozen"
    assert "CLINICIAN CORRECTION" in vault_read(vault, note_path)["body"]


# ---------------------------------------------------------------------------
# 4. Update-in-place — no duplicate note across checkpoints
# ---------------------------------------------------------------------------

def test_checkpoint_updates_in_place_no_duplicate(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-D"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r1, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    path1 = state.get(r1.encounter_id).note_path

    _write_chunk(enc, 2, ["Plan discussed."])
    r2, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)
    path2 = state.get(r2.encounter_id).note_path

    assert outcome == "drafted"
    assert path2 == path1                                       # SAME note
    assert len(list((vault / "clinical_note").glob("*.md"))) == 1  # no duplicate
    # title stable across checkpoints (opaque encounter id).
    assert frontmatter.load(str(vault / path2))["title"] == f"Encounter {r1.encounter_id}"


# ---------------------------------------------------------------------------
# 5. POST-ATTEST AUDIO — a chunk for an already-attested encounter (P3-b3):
# REFUSED + SURFACED (distinct terminal state, NOT FAILED/retry), note untouched.
# ---------------------------------------------------------------------------

def test_post_attest_audio_refuses_and_surfaces(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    from alfred.scribe.attest import attest as scribe_attest
    enc = tmp_path / "inbox" / "enc-E"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    eid = r.encounter_id
    note_path = state.get(eid).note_path
    scribe_attest(vault, note_path, new_status="attested", attester="np_jamie",
                  clinician_ids={"np_jamie"}, audit_path=vault / "audit.jsonl",
                  # #58 — this test attests a DRAFTED (never-closed) note directly; use
                  # the audited override (not about completeness — targets post-attest / draft_original).
                  allow_incomplete=True, override_reason="test — attesting a drafted note")
    attested_body = vault_read(vault, note_path)["body"]

    # NEW audio arrives AFTER attestation (seq 2).
    _write_chunk(enc, 2, ["Follow-up in one week."])
    with structlog.testing.capture_logs() as caps:
        r2, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    # DISTINCT terminal outcome — refused + surfaced, NOT a transient FAILED.
    assert outcome == "post_attest_audio"
    assert state.get(eid).state == STATE_POST_ATTEST_AUDIO
    assert state.get(eid).state != "failed" and state.get(eid).attempts == 0  # no retry-churn
    # surfaced with the opaque id + the NEW chunk's seq (obs pin #9).
    surfaced = [c for c in caps if c.get("event") == "scribe.pipeline.post_attest_audio"]
    assert len(surfaced) == 1
    assert surfaced[0]["encounter_id"] == eid and surfaced[0]["seq"] == 2
    # the signed note is UNTOUCHED.
    post = frontmatter.load(str(vault / note_path))
    assert post["status"] == "attested" and post.content.strip() == attested_body.strip()


def test_post_attest_audio_counted_in_run_sweep(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    from alfred.scribe import run_sweep
    from alfred.scribe.attest import attest as scribe_attest
    input_dir = tmp_path / "inbox"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(input_dir / "enc-P", 1, ["Patient reports chest pain."])
    cfg = _config()
    cfg.input_dir = str(input_dir)
    asyncio.run(run_sweep(cfg, state, vault))
    eid = compute_encounter_id("enc-P", salt=_SALT)
    scribe_attest(vault, state.get(eid).note_path, new_status="attested",
                  attester="np_jamie", clinician_ids={"np_jamie"},
                  audit_path=vault / "audit.jsonl",
                  allow_incomplete=True, override_reason="test — attesting a drafted note")
    _write_chunk(input_dir / "enc-P", 2, ["Post-attest line."])
    counts = asyncio.run(run_sweep(cfg, state, vault))
    assert counts["post_attest_audio"] == 1 and counts["checkpoint_drafted"] == 0


# ---------------------------------------------------------------------------
# 6. _CLOSED → ready
# ---------------------------------------------------------------------------

def test_closed_sentinel_finalizes_to_ready(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-F"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    assert outcome == "ready"
    assert state.get(r.encounter_id).state == STATE_READY
    ready = [c for c in caps if c.get("event") == "scribe.pipeline.encounter_ready"]
    assert len(ready) == 1 and ready[0]["encounter_id"] == r.encounter_id   # obs pin (#9)
    # ledger marked closed; the draft exists and is ready for attestation.
    assert load_ledger(ledger_path(enc, r.encounter_id)).closed is True
    assert state.get(r.encounter_id).note_path


# ---------------------------------------------------------------------------
# Gap-A (medico-legal) — close-before-settle ready-gate
# ---------------------------------------------------------------------------

def _write_unsettled_chunk(enc_dir, seq, lines):
    """A chunk with audio + fake-STT .txt but NO .meta.json marker → HELD."""
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:03d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # NO .meta.json marker → unsettled (held).


def test_closed_with_held_tail_stays_drafted_not_ready(tmp_path, monkeypatch):
    # Gap-A: _CLOSED present but the final chunk is HELD (its .meta.json marker
    # hasn't landed) → the encounter STAYS DRAFTED, NOT finalized to `ready` (which
    # would invite attestation of a note missing its tail — a signed medico-legal
    # record silently incomplete). MUTATION-BIND: drop the pending_tail gate → this
    # finalizes READY prematurely → RED.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-G"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])        # settled → folds + drafts
    _write_unsettled_chunk(enc, 2, ["More history."])            # HELD (no marker)
    (enc / "_CLOSED").write_text("", encoding="utf-8")           # close arrives before the tail settles
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert r.folded == 1 and r.held == 1 and r.closed is True and r.pending_tail is True
    assert outcome != "ready"
    assert state.get(r.encounter_id).state == STATE_DRAFTED      # NOT ready — tail pending
    pend = [c for c in caps if c.get("event") == "scribe.pipeline.close_pending_tail"]
    assert len(pend) == 1 and pend[0]["encounter_id"] == r.encounter_id   # ILB signal


def test_closed_tail_settles_then_finalizes_ready_with_tail(tmp_path, monkeypatch):
    # The e2e (close-then-late-chunk): once the held tail SETTLES (marker lands), the
    # NEXT sweep folds it and THEN finalizes `ready` — WITH the tail included. No
    # attestable note is ever produced missing its tail.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-H"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    _write_unsettled_chunk(enc, 2, ["Denies fever."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")
    r1, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert state.get(r1.encounter_id).state == STATE_DRAFTED     # tail pending → drafted (not ready)
    # the tail's marker lands (settle) → next sweep folds it + finalizes.
    (enc / "chunk_002.meta.json").write_text(
        json.dumps({"synthetic": True, "seq": 2}), encoding="utf-8")
    with structlog.testing.capture_logs() as caps:
        r2, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert r2.folded == 1 and r2.pending_tail is False
    assert outcome == "ready" and state.get(r2.encounter_id).state == STATE_READY
    # the tail IS in the finalized ledger (BOTH chunks folded — the note is complete).
    ledger = load_ledger(ledger_path(enc, r2.encounter_id))
    assert len(ledger.chunk_provenance) == 2
    assert any(c.get("event") == "scribe.pipeline.encounter_ready" for c in caps)


# ---------------------------------------------------------------------------
# #3 — mid-regen seal via ScopeError (sibling of VaultError) → post_attest_audio
# ---------------------------------------------------------------------------

def test_mid_regen_scope_error_seal_is_post_attest_audio(tmp_path, monkeypatch):
    # #3: the mid-regen seal can surface as a ScopeError (the stayc_clinical
    # body_replace gate re-reads frontmatter INSIDE vault_edit and finds status
    # flipped to attested) — a SIBLING of VaultError, not a subclass, never
    # re-wrapped. The broadened seal-catch classifies it post_attest_audio (the
    # signed note is untouched), NOT a transient FAILED. MUTATION-BIND: revert to
    # `except VaultError` only → the ScopeError propagates → FAILED/error → RED.
    from alfred.vault.scope import ScopeError
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-S"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)   # first draft
    assert state.get(r.encounter_id).state == STATE_DRAFTED
    # next checkpoint: the note is sealed mid-regen → the write raises ScopeError.
    def _raise_sealed(*a, **k):
        raise ScopeError("clinical_note content is SEALED once attested")
    monkeypatch.setattr(pipeline_mod, "_create_ai_draft", _raise_sealed)
    _write_chunk(enc, 2, ["Denies shortness of breath."])
    r2, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert outcome == "post_attest_audio"
    assert state.get(r2.encounter_id).state == STATE_POST_ATTEST_AUDIO


# ---------------------------------------------------------------------------
# #4 — grounding flag-count reconcile (grounding.verified under-reports)
# ---------------------------------------------------------------------------

def test_grounding_flag_count_reconciled_after_inferred_dx(tmp_path, monkeypatch):
    # #4: verify_grounding logs flagged=<grounding-only> BEFORE the #48 inferred-dx
    # flags are appended. generate_verified_note now emits a reconciling
    # scribe.grounding.flags_finalized carrying the TRUE total (grounding + inferred),
    # so a downstream flag-counting monitor sees the real count.
    from alfred.scribe import generate_verified_note
    canned = json.dumps({
        "subjective": [], "objective": [],
        # names a lexicon dx (MDD) absent from the cited segment → 1 inferred flag,
        # 0 grounding flags (no number/negation to catch).
        "assessment": [{"claim": "Major depressive disorder", "source_spans": ["S1"]}],
        "plan": [], "assessment_reasoning_stated": False,
    })
    _install_fake_ollama(monkeypatch, canned=canned)
    tx = Transcript(source_id="enc-x", mode="synthetic", segments=[
        Segment(id="S1", start_s=0.0, end_s=1.0, text="Low mood and poor sleep.", speaker=None),
    ])
    with structlog.testing.capture_logs() as caps:
        vnote = asyncio.run(generate_verified_note(tx, config=_config(), title="T"))
    gver = [c for c in caps if c.get("event") == "scribe.grounding.verified"]
    fin = [c for c in caps if c.get("event") == "scribe.grounding.flags_finalized"]
    assert gver and gver[0]["flagged"] == 0                     # grounding-only UNDER-reports
    assert fin and fin[0]["total_flags"] == 1                   # reconciled TRUE total
    assert fin[0]["inferred_diagnosis_flags"] == 1 and fin[0]["grounding_flags"] == 0
    assert vnote.flag_count == 1                                # frontmatter/flag_count already correct


def test_run_sweep_drives_checkpoint_end_to_end(tmp_path, monkeypatch):
    _install_fake_ollama(monkeypatch)
    from alfred.scribe import run_sweep
    input_dir = tmp_path / "inbox"
    _write_chunk(input_dir / "enc-G", 1, ["Patient reports chest pain."])
    cfg = _config()
    cfg.input_dir = str(input_dir)
    state = ScribeState(tmp_path / "state.json")
    with structlog.testing.capture_logs() as caps:
        counts = asyncio.run(run_sweep(cfg, state, tmp_path / "vault"))
    assert counts["chunks_folded"] == 1 and counts["checkpoint_drafted"] == 1
    eid = compute_encounter_id("enc-G", salt=_SALT)
    assert state.get(eid).state == STATE_DRAFTED
    drafted = [c for c in caps if c.get("event") == "scribe.pipeline.checkpoint_drafted"]
    assert len(drafted) == 1 and drafted[0]["encounter_id"] == eid   # obs pin (#9)


# ---------------------------------------------------------------------------
# 7. draft_original (P3-b3 retain-the-diff) — THE load-bearing pin
# ---------------------------------------------------------------------------

def test_draft_original_holds_pipeline_body_not_clinician_edit(tmp_path, monkeypatch):
    # draft_original = the PIPELINE's LAST body, NOT the current body at attest.
    # A clinician edit → clobber-detect FREEZES → draft_original stays = the AI's
    # body → the attest-diff (final body vs draft_original) shows EXACTLY the
    # clinician's change (non-empty). Verifies draft_original is NOT
    # "current-body-at-attest" (which would capture the clinician's edit → empty diff).
    from alfred.scribe.attest import attest as scribe_attest
    from alfred.vault.ops import vault_edit
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-DO"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r, _ = _checkpoint(enc, config=_config(), state=state, vault=vault)
    eid = r.encounter_id
    note_path = state.get(eid).note_path
    ai_body = vault_read(vault, note_path)["body"]                  # the AI's body (B1)
    assert frontmatter.load(str(vault / note_path))["draft_original"].strip() == ai_body.strip()

    # a CLINICIAN edits the body (a correction the AI never wrote).
    clinician_body = "## Subjective\n- CLINICIAN: stable angina, ECG ordered.\n"
    vault_edit(vault, note_path, body_replace=clinician_body, scope="stayc_clinical")

    # a new chunk folds → clobber-detect FREEZES; draft_original stays = the AI body.
    _write_chunk(enc, 2, ["Follow-up in one week."])
    _, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert outcome == "human_edited"

    scribe_attest(vault, note_path, new_status="attested", attester="np_jamie",
                  clinician_ids={"np_jamie"}, audit_path=vault / "audit.jsonl",
                  # #58 — this test attests a DRAFTED (never-closed) note directly; use
                  # the audited override (not about completeness — targets post-attest / draft_original).
                  allow_incomplete=True, override_reason="test — attesting a drafted note")
    sealed = frontmatter.load(str(vault / note_path))

    # draft_original = the AI's un-edited body; the note body = the clinician's edit.
    assert sealed["draft_original"].strip() == ai_body.strip()      # the pipeline's LAST body
    assert sealed.content.strip() == clinician_body.strip()         # body = clinician's edit
    assert sealed["draft_original"].strip() != sealed.content.strip()  # the diff is NON-EMPTY
    assert "CLINICIAN" not in sealed["draft_original"]              # NOT current-body-at-attest
    assert sealed["status"] == "attested"                          # sealed with the note


# ---------------------------------------------------------------------------
# 8. Fail-loud on a MISSING authoritative prompt_eval_count (P3-b3 fold-in)
# ---------------------------------------------------------------------------

def test_missing_prompt_eval_count_fails_loud(monkeypatch):
    # A native response LACKING prompt_eval_count → the AUTHORITATIVE truncation
    # count is unavailable → FAIL-LOUD (refuse), not silent-accept via the
    # pre-flight estimate. MUTATION-BIND: revert to silent-skip → the note is
    # accepted → RED.
    from alfred.scribe.notegen import generate_structured

    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (_CANNED_CITES_S1, {"stop_reason": "stop"})   # NO prompt_eval_count
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake)
    t = Transcript(source_id="enc-nopec", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0.0, end_s=5.0, text="chest pain")])
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(ContextBudgetExceeded):
            asyncio.run(generate_structured(t, config=_config()))
    ev = [c for c in caps if c.get("event") == "scribe.notegen.missing_prompt_eval_count"]
    assert len(ev) == 1 and ev[0]["source_id"] == "enc-nopec"   # obs pin (#9)


# ---------------------------------------------------------------------------
# #57 — close-manifest structural "ready ⇒ complete" invariant
# ---------------------------------------------------------------------------

def test_57_structural_promised_seq_blocks_premature_ready(tmp_path, monkeypatch):
    # THE #57 core (structural mutation-bind): chunk 1 settled + folded, the _CLOSED
    # manifest PROMISES final_seq=3, but chunks 2 & 3 never land → the encounter STAYS
    # DRAFTED (never premature READY) + exactly one close_awaiting_promised_seq
    # (expected_final_seq=3, folded_through=1). MUTATION-BIND: flip `>=`→`<=`, delete
    # the promised_pending conjunct, or use a max>=N shortcut → finalizes READY → RED.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57a"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    write_close_manifest(enc, 3)                                  # promise 3, only 1 on disk
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert r.expected_final_seq == 3 and r.folded_seqs == frozenset({1})
    assert r.promised_seq_pending is True
    assert outcome != "ready"
    assert state.get(r.encounter_id).state == STATE_DRAFTED       # NOT ready — promise unmet
    aw = [c for c in caps if c.get("event") == "scribe.pipeline.close_awaiting_promised_seq"]
    assert len(aw) == 1 and aw[0]["expected_final_seq"] == 3 and aw[0]["folded_through"] == 1


def test_57_strict_checkpoint_empty_manifest_is_ambiguous_never_ready(tmp_path, monkeypatch):
    # STRICT checkpoint: clinical mode + an EMPTY legacy _CLOSED on disk → read with
    # require=True → (None, ambiguous=True) → close_ambiguous → never READY, a
    # close_manifest_ambiguous WARNING. MUTATION-BIND: forcing require=False →
    # premature READY.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57b"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")           # empty legacy close
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=_config(mode="clinical"), state=state, vault=vault)
    assert r.close_ambiguous is True
    assert outcome != "ready" and state.get(r.encounter_id).state == STATE_DRAFTED
    assert any(c.get("event") == "scribe.pipeline.close_manifest_ambiguous" for c in caps)
    # CONTRAST: the SAME empty-_CLOSED content is legacy-tolerant under SYNTHETIC
    # (require=False) → finalizes READY (a fresh encounter so the draft triggers).
    enc2 = tmp_path / "inbox" / "enc-57b2"
    state2 = ScribeState(tmp_path / "state2.json")
    _write_chunk(enc2, 1, ["Patient reports chest pain."])
    (enc2 / "_CLOSED").write_text("", encoding="utf-8")
    r2, outcome2 = _checkpoint(enc2, config=_config(mode="synthetic"), state=state2, vault=vault)
    assert outcome2 == "ready" and state2.get(r2.encounter_id).state == STATE_READY


def test_57_reopen_e2e_promised_tail_lands_then_ready_with_tail(tmp_path, monkeypatch):
    # RE-OPEN e2e (clinical): close promises final_seq=2 with only chunk 1 folded →
    # DRAFTED + awaiting; chunk 2's marker lands → next sweep folds it, folded_seqs
    # {1,2} >= {1,2} → READY, both chunks in the finalized ledger.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57c"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config(mode="clinical")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    write_close_manifest(enc, 2)                                  # promise 2, only 1 folded
    r1, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert state.get(r1.encounter_id).state == STATE_DRAFTED      # awaiting seq 2
    _write_chunk(enc, 2, ["Denies fever."])                       # the promised tail lands
    r2, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert r2.folded_seqs == frozenset({1, 2}) and r2.promised_seq_pending is False
    assert outcome == "ready" and state.get(r2.encounter_id).state == STATE_READY
    assert len(load_ledger(ledger_path(enc, r2.encounter_id)).chunk_provenance) == 2


def test_57_ledger_truth_release_sweep_folds_nothing_still_ready(tmp_path, monkeypatch):
    # LEDGER-TRUTH (binds the pass-delta fix): fold the FULL tail on sweep 1 (no
    # close), then close on sweep 2 which folds NOTHING new → still READY because
    # folded_seqs is read from chunk_provenance, not the pass-delta. MUTATION-BIND:
    # compute folded_seqs from the pass-delta → wedges DRAFTED forever → RED.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57d"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    _write_chunk(enc, 2, ["Denies fever."])
    r1, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)  # folds both, NO close
    assert r1.folded == 2 and state.get(r1.encounter_id).state == STATE_DRAFTED
    write_close_manifest(enc, 2)                                    # close on sweep 2
    r2, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert r2.folded == 0 and r2.folded_seqs == frozenset({1, 2})  # LEDGER-TRUTH, not pass-delta
    assert outcome == "ready" and state.get(r2.encounter_id).state == STATE_READY


def test_57_timeout_marks_incomplete_even_when_never_drafted(tmp_path, monkeypatch):
    # TIMEOUT-WHEN-NEVER-DRAFTED (binds the outside-the-DRAFTED-guard fix): close
    # promises final_seq=2 BEFORE any chunk folds (cur state is None), backdate the
    # _CLOSED mtime beyond incomplete_grace_s → checkpoint sets STATE_INCOMPLETE and
    # emits close_incomplete EVEN THOUGH state was None. MUTATION-BIND: move
    # _maybe_mark_incomplete inside `if cur and cur.state==STATE_DRAFTED` → no
    # transition, no log → RED.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57e"
    enc.mkdir(parents=True, exist_ok=True)
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    cfg.incomplete_grace_s = 1                                    # opt into the terminal
    write_close_manifest(enc, 2)                                  # closed before ANY chunk
    os.utime(enc / "_CLOSED", (_time.time() - 100, _time.time() - 100))  # backdate past the grace
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert state.get(r.encounter_id) is not None                 # a state row was created
    assert state.get(r.encounter_id).state == STATE_INCOMPLETE   # even though cur was None
    assert outcome == "incomplete"
    inc = [c for c in caps if c.get("event") == "scribe.pipeline.close_incomplete"]
    assert len(inc) == 1 and inc[0]["expected_final_seq"] == 2 and inc[0]["folded_through"] == 0


def test_57_malformed_manifest_fail_closed_never_ready(tmp_path, monkeypatch):
    # MALFORMED-MANIFEST fail-closed: a non-empty unparseable _CLOSED → (None,
    # ambiguous=True) regardless of require → never READY + close_manifest_ambiguous.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-57f"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("not-json{", encoding="utf-8")  # garbage
    with structlog.testing.capture_logs() as caps:
        r, outcome = _checkpoint(enc, config=_config(mode="synthetic"), state=state, vault=vault)
    assert r.close_ambiguous is True                             # fail-closed even in synthetic
    assert outcome != "ready" and state.get(r.encounter_id).state == STATE_DRAFTED
    assert any(c.get("event") == "scribe.pipeline.close_manifest_ambiguous" for c in caps)


# ---------------------------------------------------------------------------
# #58 — pipeline stamps/clears/self-heals the completeness marker
# ---------------------------------------------------------------------------

def _marker(vault, note_path):
    from alfred.vault.ops import vault_read as _vr
    return (_vr(vault, note_path)["frontmatter"] or {}).get("encounter_completeness")


def test_58_ready_stamps_complete_marker_note_stays_ai_draft(tmp_path, monkeypatch):
    # PIPELINE STAMP: a checkpoint reaching READY (all promised seqs folded + closed)
    # stamps encounter_completeness.complete=true on the note; the note stays
    # status=='ai_draft' (attest flips it later). Synthetic empty close →
    # expected_final_seq=None.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-58s"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")           # synthetic empty close
    r, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert outcome == "ready" and state.get(r.encounter_id).state == STATE_READY
    note_path = state.get(r.encounter_id).note_path
    m = _marker(vault, note_path)
    assert isinstance(m, dict) and m["complete"] is True and m["expected_final_seq"] is None
    from alfred.vault.ops import vault_read as _vr
    assert _vr(vault, note_path)["frontmatter"]["status"] == "ai_draft"   # NOT attested


def test_58_clinical_ready_stamps_expected_final_seq(tmp_path, monkeypatch):
    # In clinical mode a real manifest → the marker carries expected_final_seq + folded_through.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-58c"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config(mode="clinical")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    write_close_manifest(enc, 1)                                  # promise 1 (folded)
    r, outcome = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert outcome == "ready"
    m = _marker(vault, state.get(r.encounter_id).note_path)
    assert m["complete"] is True and m["expected_final_seq"] == 1 and m["folded_through"] == 1


def test_58_note_first_ordering_stamp_raise_stays_drafted_then_restamps(tmp_path, monkeypatch):
    # NOTE-FIRST: if the stamp vault_edit raises → STATE stays DRAFTED (NOT READY);
    # a second sweep with the stamp restored re-stamps and sets READY.
    _install_fake_ollama(monkeypatch)
    enc = tmp_path / "inbox" / "enc-58n"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")
    # sweep 1: stamp raises → stays DRAFTED, NOT READY.
    monkeypatch.setattr(pipeline_mod, "stamp_complete",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stamp boom")))
    with structlog.testing.capture_logs() as caps:
        r1, outcome1 = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert outcome1 != "ready" and state.get(r1.encounter_id).state == STATE_DRAFTED
    assert any(c.get("event") == "scribe.pipeline.completeness_stamp_failed" for c in caps)
    assert _marker(vault, state.get(r1.encounter_id).note_path) is None   # never stamped
    # sweep 2: stamp restored → re-stamps + READY (proves the re-stamp path).
    monkeypatch.undo()
    _install_fake_ollama(monkeypatch)
    r2, outcome2 = _checkpoint(enc, config=_config(), state=state, vault=vault)
    assert outcome2 == "ready" and state.get(r2.encounter_id).state == STATE_READY
    assert _marker(vault, state.get(r2.encounter_id).note_path)["complete"] is True


def test_58_clear_on_regen_then_attest_refuses(tmp_path, monkeypatch):
    # CLEAR-ON-REGEN: a fold-regen through _update_or_refuse_ai_draft sets
    # encounter_completeness.complete=false ATOMICALLY with the body rewrite; a
    # subsequent attest on that state refuses encounter_incomplete.
    from alfred.scribe.attestation import AttestationError
    _install_fake_ollama(monkeypatch)
    from alfred.scribe.attest import attest as scribe_attest
    enc = tmp_path / "inbox" / "enc-58r"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    # sweep 1: chunk 1 → _create_ai_draft (born markerless).
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    r1, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)
    note_path = state.get(r1.encounter_id).note_path
    # sweep 2: chunk 2 → the UPDATE path (_update_or_refuse_ai_draft body_replace)
    # writes encounter_completeness=regressed(complete:false) ATOMICALLY.
    _write_chunk(enc, 2, ["Denies fever."])
    r2, outcome2 = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert outcome2 == "drafted"
    m = _marker(vault, note_path)
    assert isinstance(m, dict) and m["complete"] is False        # cleared on the regen
    # attest on this (regenerated, not-yet-re-finalized) note REFUSES.
    with pytest.raises(AttestationError) as exc:
        scribe_attest(vault, note_path, new_status="attested", attester="np_jamie",
                      clinician_ids={"np_jamie"}, audit_path=vault / "audit.jsonl")
    assert exc.value.reason == "encounter_incomplete"


def test_58_self_heal_restamps_markerless_ready_note(tmp_path, monkeypatch):
    # SELF-HEAL MIGRATION: a note at STATE_READY but MARKERLESS (pre-#58) is
    # re-stamped on the next closed sweep via the elif branch; idempotent.
    _install_fake_ollama(monkeypatch)
    from alfred.vault.ops import vault_edit as _ve
    enc = tmp_path / "inbox" / "enc-58h"
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    _write_chunk(enc, 1, ["Patient reports chest pain."])
    (enc / "_CLOSED").write_text("", encoding="utf-8")
    r1, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)   # → READY + stamped
    note_path = state.get(r1.encounter_id).note_path
    # simulate a pre-#58 markerless READY note: strip the marker on disk.
    _ve(vault, note_path, set_fields={"encounter_completeness": None}, scope="stayc_clinical")
    assert _marker(vault, note_path) in (None,)                     # markerless now
    # next closed sweep (state already READY) → self-heal re-stamps.
    with structlog.testing.capture_logs() as caps:
        r2, _ = _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert _marker(vault, note_path)["complete"] is True
    assert any(c.get("event") == "scribe.pipeline.completeness_self_healed" for c in caps)
    # idempotent — a SECOND sweep does not re-emit the self-heal.
    with structlog.testing.capture_logs() as caps2:
        _checkpoint(enc, config=cfg, state=state, vault=vault)
    assert not any(c.get("event") == "scribe.pipeline.completeness_self_healed" for c in caps2)
