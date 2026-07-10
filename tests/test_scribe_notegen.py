"""Tests for extract-not-infer note-gen + deterministic grounding-verify (P2-c).

All synthetic. The CORE (parse / render / ground) gets UNCONDITIONAL coverage
via CANNED qwen JSON — no real qwen in unit tests. The real-qwen end-to-end is
an integration test gated on a running Ollama (skip-gated env var).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from alfred.scribe import load_from_unified
from alfred.scribe.grounding import GroundingResult, verify as verify_grounding
from alfred.scribe.notegen import (
    GROUNDING_UNVERIFIED,
    NOT_ADDRESSED,
    REASONING_NOT_STATED,
    ContextBudgetExceeded,
    NoteGenError,
    StructuredNote,
    generate_structured,
    parse_structured_json,
    render_soap,
)
from alfred.scribe.transcript import Segment, Transcript


def _transcript(*texts):
    segs = [
        Segment(id=f"S{i+1}", start_s=float(i * 5), end_s=float(i * 5 + 5), text=t)
        for i, t in enumerate(texts)
    ]
    return Transcript(source_id="synth-1", mode="synthetic", segments=segs)


def _structured(**sections):
    return StructuredNote.from_dict(sections)


# ---------------------------------------------------------------------------
# parse + render (canned JSON)
# ---------------------------------------------------------------------------

def test_parse_and_render_soap_with_cites_and_literals():
    canned = json.dumps({
        "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
        "objective": [],  # empty → "Not addressed"
        "assessment": [{"claim": "Likely musculoskeletal", "source_spans": ["S1"]}],
        "plan": [{"claim": "Ibuprofen 400mg", "source_spans": ["S2"]}],
        "assessment_reasoning_stated": False,  # → REASONING NOT STATED
    })
    s = parse_structured_json(canned)
    body = render_soap(s, title="Encounter 1", grounding=GroundingResult())
    assert "# Encounter 1" in body
    assert "- Chest pain for 2 days [S1]" in body
    # empty objective section → the ILB literal
    assert f"## Objective\n{NOT_ADDRESSED}" in body
    # assessment with claims but reasoning not stated → the literal appended
    assert REASONING_NOT_STATED in body
    assert "- Ibuprofen 400mg [S2]" in body


def test_render_reasoning_stated_omits_literal():
    s = _structured(
        assessment=[{"claim": "Stable angina", "source_spans": ["S1"]}],
        assessment_reasoning_stated=True,
    )
    body = render_soap(s, title="E", grounding=GroundingResult())
    assert REASONING_NOT_STATED not in body


def test_render_requires_grounding_result_structural_guard():
    # NOTE-2 cheap structural guard (P2-c): render_soap REQUIRES a
    # GroundingResult — a note can never be rendered without a grounding pass.
    # (Airtight verify-before-render is enforced in the P2-d pipeline.)
    s = _structured(subjective=[{"claim": "x", "source_spans": ["S1"]}])
    with pytest.raises(TypeError):
        render_soap(s, title="E")  # missing required grounding= → TypeError


def test_parse_unparseable_fails_loud():
    with pytest.raises(NoteGenError):
        parse_structured_json("qwen said hello but no json here")


def test_parse_strips_markdown_fence():
    fenced = "Sure:\n```json\n{\"subjective\": [], \"objective\": [], \"assessment\": [], \"plan\": [], \"assessment_reasoning_stated\": true}\n```\n"
    s = parse_structured_json(fenced)
    assert s.subjective == [] and s.assessment_reasoning_stated is True


def test_frozen_contract_carries_atomic_claim_requirement():
    # The atomic-claim requirement is LOAD-BEARING for the negation guard and is
    # part of the FROZEN CONTRACT handed to prompt-tuner — pin it so it can't be
    # silently dropped from the contract the prompt is authored against.
    import alfred.scribe.notegen as ng
    src = open(ng.__file__, encoding="utf-8").read()
    assert "EXACTLY ONE clinical finding" in src        # the contract line
    assert "atomic" in ng.SYSTEM_PROMPT.lower()  # the prompt instructs it


# ---------------------------------------------------------------------------
# grounding-verify — the anti-hallucination pins
# ---------------------------------------------------------------------------

def test_grounding_clean_note_zero_flags():
    t = _transcript(
        "Patient reports chest pain for 2 days.",
        "Amoxicillin 500mg three times daily.",
        "Patient denies shortness of breath.",
    )
    s = _structured(
        subjective=[{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
        plan=[{"claim": "Amoxicillin 500mg", "source_spans": ["S2"]}],
        objective=[{"claim": "Denies shortness of breath", "source_spans": ["S3"]}],
    )
    r = verify_grounding(s, t)
    assert r.clean is True
    assert r.metadata == []
    assert all(r.flag_for(sec, i) is None for sec, i, _ in s.all_claims())


def test_grounding_catches_dose_flip_500mg_5mg():
    # THE 500mg→5mg pin.
    t = _transcript("Amoxicillin 500mg three times daily.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean
    assert r.flags[0].reason == "number_mismatch"
    assert r.flag_for("plan", 0) == GROUNDING_UNVERIFIED


@pytest.mark.parametrize(
    "truth, note_dose",
    [
        ("Digoxin 0.5mg daily.", "Digoxin 5mg"),      # 10x — \b would pass CLEAN
        ("Colchicine 2.5mg.", "Colchicine 25mg"),      # decimal-boundary
        ("Bumetanide 12.5mg.", "Bumetanide 12mg"),     # truncated decimal
        ("Bumetanide 12mg.", "Bumetanide 12.5mg"),     # the reverse truncation
    ],
)
def test_grounding_catches_decimal_dose_flip(truth, note_dose):
    # THE decimal-boundary regression (mutation: revert _token_in to \b →
    # 5mg matches inside 0.5mg → passes clean → this fails). No decimal test
    # existed before this review — why the 10x hole shipped.
    t = _transcript(truth)
    s = _structured(plan=[{"claim": note_dose, "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean, f"{note_dose!r} vs {truth!r} must FLAG"
    assert r.flags[0].reason == "number_mismatch"


@pytest.mark.parametrize("dose", ["Digoxin 0.5mg", "Colchicine 2.5mg", "Bumetanide 12.5mg"])
def test_grounding_decimal_self_match_is_clean(dose):
    # A correct decimal dose must NOT false-flag.
    t = _transcript(f"{dose} daily.")
    s = _structured(plan=[{"claim": dose, "source_spans": ["S1"]}])
    assert verify_grounding(s, t).clean is True


@pytest.mark.parametrize(
    "claim, segment",
    [
        ("BP is 120", "bp is 120."),          # sentence-final INTEGER
        ("Temp is 98.6", "temp is 98.6."),    # sentence-final DECIMAL
        ("Heart rate over 80", "...over 80."),  # sentence-final, leading ellipsis
    ],
)
def test_grounding_sentence_final_number_not_false_flagged(claim, segment):
    # FALSE-POSITIVE FIX (P2-d): a number ending a sentence (period is NOT a
    # decimal point) must be CLEAN — not a spurious ⚠ (alarm fatigue erodes the
    # net). Mutation-bind: revert _token_in to (?<![\d.])...(?![\d.]) → THIS pin
    # goes red while the decimal-flip pins stay green (fix closes the FP without
    # reopening the 0.5mg BLOCK).
    t = _transcript(segment)
    s = _structured(objective=[{"claim": claim, "source_spans": ["S1"]}])
    assert verify_grounding(s, t).clean is True, (
        f"{claim!r} vs {segment!r} must NOT flag — trailing '.' is a sentence "
        f"period, not a decimal"
    )


def test_grounding_catches_negation_flip_denies_reports():
    # THE denies→reports pin (dropped negation — the dangerous flip).
    t = _transcript("Patient denies shortness of breath.")
    s = _structured(objective=[{"claim": "Reports shortness of breath", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean
    assert r.flags[0].reason == "negation_mismatch"


def test_grounding_catches_fabricated_negation():
    t = _transcript("Patient reports chest pain.")
    s = _structured(subjective=[{"claim": "Denies chest pain", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert r.flags[0].reason == "negation_mismatch"


# ---------------------------------------------------------------------------
# Negation redesign (P2-e, 66%→~0 FP) — invented + targeted-flip, subset clean.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "claim, segment",
    [
        # (A) SUBSET IS CLEAN — atomic claim's negation is a subset of the
        # multi-finding segment's negations; the claim's finding is not one the
        # segment flips. MUTATION-BIND: revert to set-equality → THESE go RED.
        ("Denies fever", "denies fever, no ear pain"),
        ("Reports cough", "denies fever, productive cough"),
        # (D) "non-" must NOT register as a negation — a positive claim citing a
        # segment with "non-productive cough" must be CLEAN.
        ("Reports nasal congestion", "non-productive cough, nasal congestion"),
        ("Reports nonspecific findings", "nonspecific ST changes noted"),
    ],
)
def test_grounding_negation_subset_and_non_are_clean(claim, segment):
    t = _transcript(segment)
    s = _structured(objective=[{"claim": claim, "source_spans": ["S1"]}])
    assert verify_grounding(s, t).clean is True, f"{claim!r} vs {segment!r} must NOT flag"


def test_grounding_negation_invented_flags():
    # (B) the claim asserts a negation NOT present in the cited segment.
    t = _transcript("Patient reports headache.")
    s = _structured(objective=[{"claim": "Denies chest pain", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"
    assert "invented" in r.flags[0].detail


def test_grounding_negation_targeted_flip_flags():
    # (C) the cited segment NEGATES a finding the claim asserts POSITIVELY.
    t = _transcript("Patient denies shortness of breath.")
    s = _structured(objective=[{"claim": "Reports shortness of breath", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"
    assert "positively" in r.flags[0].detail


def test_grounding_non_prefix_not_in_negation_lexicon():
    # (D) the curated lexicon must not register the bare "non-" prefix.
    from alfred.scribe.grounding import _negation_set
    assert _negation_set("non-productive cough") == set()
    assert _negation_set("nonspecific changes") == set()
    # but real negations still register
    assert "denies" in _negation_set("patient denies fever")
    assert "no" in _negation_set("no ear pain")


def test_grounding_lexicon_lacks_neither_nor_are_word_bounded_safe():
    # Re-added negations (review BLOCK): word-bounded-safe — must NOT register
    # inside longer words, but DO register as standalone negations.
    from alfred.scribe.grounding import _negation_set
    for benign in ["black", "lackadaisical", "lacerate", "norepinephrine",
                   "north", "minor", "normal"]:
        assert _negation_set(benign) == set(), f"{benign!r} false-registered"
    assert "lacks" in _negation_set("abdomen lacks bowel sounds")
    assert {"neither", "nor"} <= _negation_set("neither fever nor chills")


def test_grounding_lacks_flip_flags():
    # (C) FLIP via "lacks" (mutation-bind: drop lacks? from the lexicon → RED,
    # while the non-productive/pain-free clean pins stay GREEN).
    t = _transcript("Abdomen lacks bowel sounds.")
    s = _structured(objective=[{"claim": "Bowel sounds present", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"


def test_grounding_lacks_invented_flags():
    # (B) INVENTED via "lacks" — cited segment never negates insight.
    t = _transcript("Patient is oriented.")
    s = _structured(objective=[{"claim": "Lacks insight", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"


def test_grounding_neither_nor_flip_flags():
    # (C) FLIP via "neither/nor" coordinator — the " nor " phrase boundary makes
    # "Neither fever nor chills" negate "fever" (not "fever nor chills").
    t = _transcript("Neither fever nor chills.")
    s = _structured(objective=[{"claim": "Reports fever", "source_spans": ["S1"]}])
    assert not verify_grounding(s, t).clean


@pytest.mark.parametrize("claim, segment", [
    ("Reports nasal congestion", "non-productive cough, nasal congestion"),
    ("Reports pain", "left ankle is pain-free"),
    ("Reports carbohydrate intake", "diet is carbohydrate-free"),
])
def test_grounding_non_free_prefixes_stay_clean_no_new_fp(claim, segment):
    # non-/free stay OUT of the lexicon → no new FP. Mutation-bind partner: when
    # lacks? is dropped these stay GREEN, proving the flip fix is targeted.
    t = _transcript(segment)
    s = _structured(objective=[{"claim": claim, "source_spans": ["S1"]}])
    assert verify_grounding(s, t).clean is True


def test_grounding_catches_ungrounded_span_S99():
    t = _transcript("Patient reports chest pain.")
    s = _structured(subjective=[{"claim": "Chest pain", "source_spans": ["S99"]}])
    r = verify_grounding(s, t)
    assert r.flags[0].reason == "ungrounded_span"


def test_grounding_catches_no_source_spans():
    t = _transcript("Patient reports chest pain.")
    s = _structured(assessment=[{"claim": "Anxiety", "source_spans": []}])
    r = verify_grounding(s, t)
    assert r.flags[0].reason == "ungrounded_assertion"


def test_grounding_catches_fabricated_claim():
    # A fabricated vital not in the cited segment → number_mismatch flag.
    t = _transcript("Patient reports chest pain for 2 days.")
    s = _structured(objective=[{"claim": "BP 200/110 mmHg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean
    assert r.flags[0].reason == "number_mismatch"


def test_grounding_flags_recorded_in_metadata_auditable():
    t = _transcript("Amoxicillin 500mg.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    md = r.metadata
    assert len(md) == 1
    entry = md[0]
    assert entry["section"] == "plan"
    assert entry["claim_index"] == 0
    assert entry["reason"] == "number_mismatch"
    assert entry["claim"] == "Amoxicillin 5mg"
    assert entry["source_spans"] == ["S1"]
    assert "detail" in entry


def test_grounding_flag_rendered_inline_unmissable():
    t = _transcript("Amoxicillin 500mg.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    body = render_soap(s, title="E", grounding=r)
    assert f"- Amoxicillin 5mg [S1] {GROUNDING_UNVERIFIED}" in body


def test_render_without_verify_shows_no_flags_documents_p2d_hole():
    # The cheap guard requires a GroundingResult but does NOT force it to be
    # VERIFIED — passing an EMPTY (unverified) result renders a would-be-flagged
    # note CLEAN. This is the exact hole P2-d's combined generate→verify→render
    # closes structurally; pinned here so the gap is explicit, not silent.
    t = _transcript("Amoxicillin 500mg.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    body = render_soap(s, title="E", grounding=GroundingResult())  # unverified
    assert GROUNDING_UNVERIFIED not in body  # renders clean — the P2-d hole
    # ... whereas verifying first flags it:
    assert not verify_grounding(s, t).clean


# ---------------------------------------------------------------------------
# the sovereign local call routes through call_ollama_no_tools (loopback)
# ---------------------------------------------------------------------------

def test_generate_routes_through_loopback_ollama(monkeypatch):
    captured = {}

    async def _fake_ollama(prompt, system=None, model="", endpoint="", **kw):
        captured["endpoint"] = endpoint
        captured["model"] = model
        captured["system"] = system
        captured["options"] = kw.get("options")
        return (
            json.dumps({
                "subjective": [{"claim": "Chest pain", "source_spans": ["S1"]}],
                "objective": [], "assessment": [], "plan": [],
                "assessment_reasoning_stated": False,
            }),
            {"stop_reason": "stop"},
        )

    import alfred.distiller.backends.ollama as ollama_mod
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama)

    cfg = load_from_unified({"scribe": {
        "mode": "synthetic",
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})
    t = _transcript("Patient reports chest pain.")
    s = asyncio.run(generate_structured(t, config=cfg))
    assert s.subjective[0].claim == "Chest pain"
    # routed through the loopback endpoint (barrier-b-validated), not a cloud host
    assert captured["endpoint"] == "http://127.0.0.1:11434"
    assert captured["model"] == "qwen2.5:14b-instruct-q4_K_M"
    assert captured["system"] is not None  # the placeholder system prompt is passed
    # #46: the note-gen forces num_ctx + temperature=0 (mutation-bind: drop the
    # options pass-through at the call site → this goes RED).
    assert captured["options"] is not None
    assert captured["options"]["num_ctx"] >= 8192
    assert captured["options"]["temperature"] == 0


def test_notegen_constructs_no_http_client():
    # Pin: note-gen delegates to call_ollama_no_tools (httpx, guard-covered) and
    # IMPORTS / CONSTRUCTS no http client of its own → no non-loopback egress
    # path. (Matches import/construction patterns, not the docstring mention.)
    import alfred.scribe.notegen as notegen_mod
    src = open(notegen_mod.__file__, encoding="utf-8").read()
    assert "import httpx" not in src
    assert "httpx.AsyncClient" not in src
    assert "httpx.Client" not in src
    assert "import aiohttp" not in src
    assert "aiohttp.ClientSession" not in src


# ---------------------------------------------------------------------------
# real-qwen END-TO-END — integration test (skip-gated on a running Ollama)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_QWEN_IT"),
    reason="real-qwen integration test — set ALFRED_SCRIBE_QWEN_IT=1 with a running Ollama + qwen2.5-14b",
)
def test_real_qwen_end_to_end():
    cfg = load_from_unified({"scribe": {
        "mode": "synthetic",
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})
    t = _transcript(
        "Patient reports chest pain for two days.",
        "Denies shortness of breath.",
        "Plan is to order an ECG.",
    )
    s = asyncio.run(generate_structured(t, config=cfg))
    r = verify_grounding(s, t)
    body = render_soap(s, title="Integration encounter", grounding=r)
    assert "## Subjective" in body and "## Plan" in body
    assert isinstance(r.metadata, list)


# ---------------------------------------------------------------------------
# #46 — generate_structured drives the NATIVE /api/chat endpoint with the
# num_ctx options block (httpx-level; the OpenAI-compat path silently
# truncates at num_ctx=2048).
# ---------------------------------------------------------------------------

def test_generate_structured_hits_native_api_chat_with_num_ctx(monkeypatch):
    import httpx
    import alfred.distiller.backends.ollama as ollama_mod

    captured = {}

    def _dispatch(req):
        captured["path"] = req.url.path
        captured["payload"] = json.loads(req.content)
        return httpx.Response(200, json={
            "model": "qwen2.5:14b-instruct-q4_K_M",
            "message": {"role": "assistant", "content": json.dumps({
                "subjective": [{"claim": "Chest pain", "source_spans": ["S1"]}],
                "objective": [], "assessment": [], "plan": [],
                "assessment_reasoning_stated": False,
            })},
            "done_reason": "stop", "done": True,
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        ollama_mod.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(_dispatch), timeout=k.get("timeout")),
    )

    cfg = load_from_unified({"scribe": {
        "mode": "synthetic",
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})
    s = asyncio.run(generate_structured(_transcript("Patient reports chest pain."), config=cfg))
    assert s.subjective[0].claim == "Chest pain"          # parsed via native message.content
    assert captured["path"] == "/api/chat"                 # NATIVE endpoint (honors num_ctx)
    assert captured["payload"]["options"]["num_ctx"] >= 8192
    assert captured["payload"]["options"]["temperature"] == 0


# ---------------------------------------------------------------------------
# P3-b2 — the native /api/chat path surfaces Ollama's real prompt_eval_count,
# and the POST-CALL truncation guard fires end-to-end (REAL response-parse path,
# not a mock of call_ollama_no_tools).
# ---------------------------------------------------------------------------

def _native_chat_transport(monkeypatch, *, prompt_eval_count):
    import httpx
    import alfred.distiller.backends.ollama as ollama_mod

    def _dispatch(req):
        return httpx.Response(200, json={
            "model": "qwen2.5:14b-instruct-q4_K_M",
            "message": {"role": "assistant", "content": json.dumps({
                "subjective": [{"claim": "Chest pain", "source_spans": ["S1"]}],
                "objective": [], "assessment": [], "plan": [],
                "assessment_reasoning_stated": False,
            })},
            "done_reason": "stop", "done": True,
            "prompt_eval_count": prompt_eval_count,
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        ollama_mod.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(_dispatch), timeout=k.get("timeout")),
    )


def _cfg():
    return load_from_unified({"scribe": {
        "mode": "synthetic",
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:14b-instruct-q4_K_M"},
    }})


def test_native_chat_prompt_eval_under_ceiling_accepted(monkeypatch):
    from alfred.scribe.notegen import _PROMPT_TRUNCATION_CEILING
    _native_chat_transport(monkeypatch, prompt_eval_count=_PROMPT_TRUNCATION_CEILING - 1)
    s = asyncio.run(generate_structured(_transcript("Patient reports chest pain."), config=_cfg()))
    assert s.subjective[0].claim == "Chest pain"          # fit → accepted


def test_native_chat_prompt_eval_at_ceiling_refused(monkeypatch):
    # The REAL native-parse path surfaces prompt_eval_count into metadata, and the
    # post-call guard refuses a truncated-prompt note (proves the ollama.py
    # metadata surfacing works end-to-end, not just a mocked call).
    from alfred.scribe.notegen import _PROMPT_TRUNCATION_CEILING
    _native_chat_transport(monkeypatch, prompt_eval_count=_PROMPT_TRUNCATION_CEILING)
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(generate_structured(_transcript("Patient reports chest pain."), config=_cfg()))
