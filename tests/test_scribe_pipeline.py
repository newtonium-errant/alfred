"""Tests for the sovereign scribe pipeline state machine (scribe P2-d).

All synthetic; the note-gen model call is monkeypatched with canned qwen JSON
(no real qwen). Covers: the verify-before-render choke, the state machine +
idempotent resume, the fail-closed retriable path, the ILB idle-tick, NOTE-2
(ai_draft only), NOTE-3 (local-only, no claude -p), NOTE-4 (PHI-free ids), and
the "no unverified note reaches vault_create" structural pin.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import frontmatter
import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
import alfred.scribe.pipeline as pipeline_mod
from alfred.scribe import (
    STATE_DRAFTED,
    STATE_FAILED,
    STATE_REFUSED,
    STATE_STRUCTURING,
    GROUNDING_UNVERIFIED,
    ScribeState,
    generate_verified_note,
    load_from_unified,
    process_source,
    run_sweep,
)
from alfred.scribe.transcript import Segment, Transcript


def _config(mode="synthetic"):
    return load_from_unified({"scribe": {
        "mode": mode,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})


# A canned qwen response with a DOSE FLIP (5mg cited to a 500mg segment) so the
# grounding pass has something to flag → proves verify ran before render.
_CANNED_FLIPPED = json.dumps({
    "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
    "objective": [],
    "assessment": [],
    "plan": [{"claim": "Amoxicillin 5mg", "source_spans": ["S2"]}],
    "assessment_reasoning_stated": False,
})
_CANNED_CLEAN = json.dumps({
    "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
    "objective": [], "assessment": [], "plan": [],
    "assessment_reasoning_stated": False,
})


def _fake_ollama_returning(canned):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (canned, {"stop_reason": "stop"})
    return _fake


def _drop_input(input_dir, stem="enc1", *, synthetic=True, transcript_lines=None):
    """Write a synthetic input: audio placeholder + .txt (fake STT) + .meta.json."""
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / f"{stem}.wav").write_bytes(b"")
    lines = transcript_lines or [
        "Patient reports chest pain for 2 days.",
        "Amoxicillin 500mg three times daily.",
    ]
    (input_dir / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta = {"synthetic": synthetic} if synthetic is not None else {}
    (input_dir / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return input_dir / f"{stem}.wav"


# ---------------------------------------------------------------------------
# The verify-before-render CHOKE (the HARD P2-c commitment)
# ---------------------------------------------------------------------------

def test_choke_verifies_the_same_object_it_renders(monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_FLIPPED))
    seen = {}

    import alfred.scribe.pipeline as pl
    orig_verify = pl.verify_grounding
    orig_render = pl.render_soap

    def _verify_spy(structured, transcript):
        seen["verified"] = id(structured)
        return orig_verify(structured, transcript)

    def _render_spy(structured, *, title, grounding):
        seen["rendered"] = id(structured)
        seen["grounding_present"] = grounding is not None
        return orig_render(structured, title=title, grounding=grounding)

    monkeypatch.setattr(pl, "verify_grounding", _verify_spy)
    monkeypatch.setattr(pl, "render_soap", _render_spy)

    t = Transcript(source_id="s", mode="synthetic", segments=[
        Segment(id="S1", start_s=0, end_s=5, text="chest pain for 2 days"),
        Segment(id="S2", start_s=5, end_s=10, text="Amoxicillin 500mg"),
    ])
    vnote = asyncio.run(generate_verified_note(t, config=_config(), title="E"))
    # verify + render ran on the SAME structured object, verify BEFORE render.
    assert seen["verified"] == seen["rendered"]
    assert seen["grounding_present"] is True
    # the dose flip was flagged → grounding actually ran on this note.
    assert vnote.flag_count == 1
    assert GROUNDING_UNVERIFIED in vnote.body


def test_pipeline_render_only_inside_the_choke():
    # Structural pin: render_soap is called EXACTLY once in the pipeline module,
    # inside generate_verified_note (right after verify_grounding). No other code
    # path renders a note → nothing reaches vault_create unverified.
    src = inspect.getsource(pipeline_mod)
    assert src.count("render_soap(") == 1
    choke = inspect.getsource(pipeline_mod.generate_verified_note)
    assert "verify_grounding(" in choke and "render_soap(" in choke
    assert choke.index("verify_grounding(") < choke.index("render_soap(")


# ---------------------------------------------------------------------------
# End-to-end: a synthetic drop → clinical_note ai_draft (the milestone)
# ---------------------------------------------------------------------------

def test_process_source_drafts_ai_draft_with_grounding_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_FLIPPED))
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir)
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    outcome = asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault))
    assert outcome == "drafted"

    # the note landed as ai_draft (NOTE-2) with source-id provenance (NOTE-4)
    st = state.get("enc1.wav")
    assert st.state == STATE_DRAFTED
    note = frontmatter.load(str(vault / st.note_path))
    assert note["status"] == "ai_draft"
    assert note["ai_draft"] is True
    assert note["synthetic"] is True
    assert note["source_id"] == "enc1.wav"           # synthetic → filename
    assert note["drafted_by"] == "stayc_scribe"
    assert "attested_by" not in note.metadata        # NOTE-2: never attested
    # the grounding pass ran → the dose flip is in the frontmatter + inline body
    assert len(note["grounding_flags"]) == 1
    assert note["grounding_flags"][0]["reason"] == "number_mismatch"
    assert GROUNDING_UNVERIFIED in note.content


# ---------------------------------------------------------------------------
# Idempotency + resume
# ---------------------------------------------------------------------------

def test_replaying_a_drafted_source_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir)
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    assert asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault)) == "drafted"
    # a second run of the SAME source → skipped (never reprocessed)
    assert asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault)) == "skipped"


def test_resume_after_reload_skips_drafted(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir)
    vault = tmp_path / "vault"
    sp = tmp_path / "state.json"

    s1 = ScribeState(sp)
    assert asyncio.run(process_source(audio, config=_config(), state=s1, vault_path=vault)) == "drafted"
    # simulate a restart: fresh state object, load from disk
    s2 = ScribeState(sp)
    s2.load()
    assert s2.get("enc1.wav").state == STATE_DRAFTED
    assert asyncio.run(process_source(audio, config=_config(), state=s2, vault_path=vault)) == "skipped"


# ---------------------------------------------------------------------------
# Fail-closed for PHI
# ---------------------------------------------------------------------------

def test_non_synthetic_input_refused_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    input_dir = tmp_path / "inbox"
    # meta with synthetic:false → refused in synthetic mode
    audio = _drop_input(input_dir, synthetic=False)
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    outcome = asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault))
    assert outcome == "refused"
    assert state.get("enc1.wav").state == STATE_REFUSED
    # NO note written
    assert not (vault / "clinical_note").exists()


def test_missing_provenance_refused_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir, synthetic=None)  # no synthetic tag at all
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    assert asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault)) == "refused"


def test_exception_leaves_source_retriable_no_partial_note(tmp_path, monkeypatch):
    # STT raises → fail-closed: source at FAILED (retriable), no note, error CLASS
    # only in state (no PHI).
    import alfred.scribe.stt as stt
    def _boom(*a, **k):
        raise stt.STTError("synthetic boom")
    monkeypatch.setattr(pipeline_mod.stt_mod, "transcribe", _boom)
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir)
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    with structlog.testing.capture_logs() as caps:
        outcome = asyncio.run(process_source(audio, config=_config(), state=state, vault_path=vault))
    assert outcome == "failed"
    st = state.get("enc1.wav")
    assert st.state == STATE_FAILED and st.attempts == 1
    assert st.last_error_class == "STTError"     # class only, NO PHI
    assert not (vault / "clinical_note").exists()  # no partial note
    # the failure log carries source_id + state + error_class (no PHI)
    fail = [c for c in caps if c.get("event") == "scribe.pipeline.failed"]
    assert len(fail) == 1
    assert fail[0]["source_id"] == "enc1.wav" and fail[0]["error_class"] == "STTError"
    assert "synthetic boom" not in json.dumps(fail[0])  # the message never logged


# ---------------------------------------------------------------------------
# run_sweep + the ILB idle-tick
# ---------------------------------------------------------------------------

def test_sweep_drafts_new_sources_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    input_dir = tmp_path / "inbox"
    _drop_input(input_dir, stem="a")
    _drop_input(input_dir, stem="b")
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")
    cfg = _config()
    cfg.input_dir = str(input_dir)  # point the sweep at the drop dir
    counts = asyncio.run(run_sweep(cfg, state, vault))
    assert counts["scanned"] == 2 and counts["drafted"] == 2
    # a second sweep → all skipped (idempotent) → ILB idle
    with structlog.testing.capture_logs() as caps:
        counts2 = asyncio.run(run_sweep(cfg, state, vault))
    assert counts2["drafted"] == 0 and counts2["skipped"] == 2
    idle = [c for c in caps if c.get("event") == "scribe.pipeline.idle"]
    assert len(idle) == 1 and "nothing to do" in idle[0]["detail"]


def test_sweep_empty_input_dir_emits_idle_tick(tmp_path):
    cfg = _config()
    cfg.input_dir = str(tmp_path / "nope")  # non-existent input dir
    state = ScribeState(tmp_path / "state.json")
    with structlog.testing.capture_logs() as caps:
        counts = asyncio.run(run_sweep(cfg, state, tmp_path / "vault"))
    assert counts["scanned"] == 0
    idle = [c for c in caps if c.get("event") == "scribe.pipeline.idle"]
    assert len(idle) == 1 and "nothing to do" in idle[0]["detail"]


# ---------------------------------------------------------------------------
# NOTE-3 — the pipeline is LOCAL-PYTHON, no claude -p / subprocess egress
# ---------------------------------------------------------------------------

def test_note3_pipeline_has_no_subprocess_or_claude_p():
    # Strip the module docstring (which legitimately EXPLAINS why claude -p is
    # forbidden) and assert the CODE body has no subprocess / claude egress
    # vector — the note path must stay local-python.
    src = inspect.getsource(pipeline_mod)
    code = src.replace(pipeline_mod.__doc__ or "", "")
    assert "import subprocess" not in code
    assert "subprocess." not in code
    assert "claude_subprocess_env" not in code
    assert "claude" not in code  # no claude anything in the code body


# ---------------------------------------------------------------------------
# _create_ai_draft — the crash-window already-exists recovery branch
# (functionally correct but was untested + STRING-PARSE-COUPLED to the
# VaultError message; pin the current behavior so a message-format change
# can't silently break the idempotent resume).
# ---------------------------------------------------------------------------

def test_create_ai_draft_recovers_on_already_exists(tmp_path, monkeypatch):
    # Crash-window: a prior run created the note but crashed before persisting
    # DRAFTED → the resume hits VaultError("File already exists: <path>"),
    # recovers the path, and treats it as drafted (no duplicate note).
    from alfred.vault.ops import VaultError

    def _raise_exists(*a, **k):
        raise VaultError("File already exists: clinical_note/Encounter x.md")

    monkeypatch.setattr(pipeline_mod, "vault_create", _raise_exists)
    vnote = pipeline_mod.VerifiedNote(body="b", grounding_flags=[], flag_count=0)
    path = pipeline_mod._create_ai_draft(
        tmp_path / "vault", "Encounter x", "x", _config(), vnote,
    )
    # the rel_path is recovered from the message (STRING-PARSE-COUPLED — pinned)
    assert path == "clinical_note/Encounter x.md"


def test_create_ai_draft_reraises_other_vaulterror(tmp_path, monkeypatch):
    # A VaultError that is NOT the already-exists case must PROPAGATE (fail-
    # closed) — never silently swallowed as a spurious "drafted".
    from alfred.vault.ops import VaultError

    def _raise_other(*a, **k):
        raise VaultError("some other validation error")

    monkeypatch.setattr(pipeline_mod, "vault_create", _raise_other)
    vnote = pipeline_mod.VerifiedNote(body="b", grounding_flags=[], flag_count=0)
    with pytest.raises(VaultError):
        pipeline_mod._create_ai_draft(tmp_path / "vault", "t", "x", _config(), vnote)


def test_process_source_recovers_drafted_on_crash_window(tmp_path, monkeypatch):
    # End-to-end crash-window: process_source that hits already-exists still
    # reaches DRAFTED (idempotent resume — no re-raise, no partial/failed state).
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED_CLEAN))
    from alfred.vault.ops import VaultError

    def _raise_exists(*a, **k):
        raise VaultError("File already exists: clinical_note/Encounter enc1.wav.md")

    monkeypatch.setattr(pipeline_mod, "vault_create", _raise_exists)
    input_dir = tmp_path / "inbox"
    audio = _drop_input(input_dir)
    state = ScribeState(tmp_path / "state.json")
    outcome = asyncio.run(process_source(audio, config=_config(), state=state, vault_path=tmp_path / "vault"))
    assert outcome == "drafted"
    assert state.get("enc1.wav").state == STATE_DRAFTED
