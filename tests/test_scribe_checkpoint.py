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
from alfred.scribe import (
    ContextBudgetExceeded,
    GROUNDING_UNVERIFIED,
    STATE_BUDGET_CAPPED,
    STATE_DRAFTED,
    STATE_HUMAN_EDITED,
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
        return (canned, {"stop_reason": "stop"})

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
    """Mirror run_sweep's per-encounter logic: accumulate → checkpoint."""
    r = accumulate_encounter(enc_dir, config=config)
    outcome = None
    if r.folded > 0 or r.closed:
        outcome = asyncio.run(checkpoint_encounter(
            enc_dir, encounter_id=r.encounter_id, config=config,
            state=state, vault_path=vault, did_fold=r.folded > 0, closed=r.closed,
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
# 5. Attested draft → refuse (no clobber)
# ---------------------------------------------------------------------------

def test_checkpoint_refuses_attested_draft(tmp_path, monkeypatch):
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
                  clinician_ids={"np_jamie"}, audit_path=vault / "audit.jsonl")
    attested_body = vault_read(vault, note_path)["body"]

    _write_chunk(enc, 2, ["Follow-up in one week."])
    with structlog.testing.capture_logs() as caps:
        r2, outcome = _checkpoint(enc, config=_config(), state=state, vault=vault)

    assert outcome == "attested_refused"
    refused = [c for c in caps if c.get("event") == "scribe.pipeline.checkpoint_refused_sealed"]
    assert len(refused) == 1 and refused[0]["encounter_id"] == eid   # observability pin (#9)
    # the sealed note is untouched.
    post = frontmatter.load(str(vault / note_path))
    assert post["status"] == "attested" and post.content.strip() == attested_body.strip()


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
