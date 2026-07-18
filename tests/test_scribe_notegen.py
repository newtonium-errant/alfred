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
    SYSTEM_PROMPT,
    ContextBudgetExceeded,
    NoteGenError,
    StructuredNote,
    build_prompt,
    generate_structured,
    parse_structured_json,
    render_soap,
)
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    Segment,
    Transcript,
)


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
    assert all(r.flags_for(sec, i) == [] for sec, i, _ in s.all_claims())


def test_grounding_catches_dose_flip_500mg_5mg():
    # THE 500mg→5mg pin.
    t = _transcript("Amoxicillin 500mg three times daily.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean
    assert r.flags[0].reason == "number_mismatch"
    assert r.flags_for("plan", 0) == [GROUNDING_UNVERIFIED]


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


# ---------------------------------------------------------------------------
# NEGATION-PRECISION redesign (P2-f, task #24) — negated-CONCEPT grounding
# replaces the P2-e marker SET-DIFFERENCE. A faithful paraphrase realizing the
# same pertinent-negative with a DIFFERENT negation surface form (or a
# contraction the base lexicon misses) must NOT false-flag as invented, WHILE a
# genuinely invented / flipped negation still flags (recall preserved).
# These 3 PRECISION cases are the exact false positives the #16 eval corpus's
# first run surfaced — reproduced inline (claim + cited segment verbatim) from
# the eval fixtures so the pin is self-contained. Mutation-bind: revert (B) to
# `_negation_set(claim) - _negation_set(cited)` → cases 1 & 3 go RED.
# ---------------------------------------------------------------------------

def test_grounding_negation_precision_no_neck_swelling_clean():
    # #24 case 1 — fab_fatigue_nodx_ambient: "no X" grounded by "haven't noticed
    # any X" (a contraction the base lexicon doesn't even see). Faithful → CLEAN.
    t = _transcript(
        "I've just been wiped out for about a month. And my hair seems a bit "
        "thinner. But my weight's the same and I haven't noticed any neck swelling."
    )
    s = _structured(subjective=[
        {"claim": "Weight unchanged, no neck swelling", "source_spans": ["S1"]},
    ])
    assert verify_grounding(s, t).clean is True


def test_grounding_negation_precision_denies_plan_clean():
    # #24 case 3 — mh_passive_si: "denies a plan" grounded by "Not like a plan",
    # "would not wake up" grounded by "wouldn't wake up" (contraction). CLEAN.
    t = _transcript(
        "The back's the same. Honestly, though, I've been really low lately. Some "
        "mornings I just lie there and wonder what the point of it all is.",
        "Not like a plan or anything. Just... sometimes I wish I wouldn't wake up. "
        "I wouldn't do anything.",
    )
    s = _structured(subjective=[{
        "claim": (
            "Expresses passive suicidal ideation — wonders what the point is "
            "and at times wishes she would not wake up; denies a plan or intent to act"
        ),
        "source_spans": ["S1", "S2"],
    }])
    assert verify_grounding(s, t).clean is True


def test_grounding_negation_precision_lexically_disjoint_paraphrase_STILL_FLAGS():
    # #24 case 2 — drug_switch_empagliflozin: "not adequately controlled" ≈
    # "haven't come down as hoped" is a faithful paraphrase BUT the negated
    # concepts are lexically DISJOINT (only "metformin" overlaps). v1 keeps this
    # FLAGGED — loosening to catch it would drop genuine invented negations (the
    # false-NEGATIVE that matters most on a medico-legal detector). This pin
    # FLIPS to clean when the #26 learned-suppression loop lands; until then it
    # tracks the residual as a deliberate, documented FLAG.
    t = _transcript("Your sugars haven't come down the way I'd hoped on the metformin.")
    s = _structured(objective=[
        {"claim": "Blood sugars not adequately controlled on metformin",
         "source_spans": ["S1"]},
    ])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"


@pytest.mark.parametrize("claim, segment", [
    # RECALL guard (the false-NEGATIVE risk of concept-grounding): a claim
    # negation whose concept is NOT negated in the cite must still FLAG even when
    # the cite carries an UNRELATED negation that shares an incidental word.
    # "chest pain" ⊄ "chest tube" — "pain" absent → the shared "chest" does NOT
    # ground it. Mutation-bind: a single-word-overlap suppression → this goes RED.
    ("Denies chest pain", "No chest tube placed; chest tube is absent."),
    # A shared DRUG NAME does not ground a differently negated concept (this is
    # exactly why case 2 above stays flagged, stated as a recall guard).
    ("Denies taking metformin", "Sugars haven't come down on the metformin."),
])
def test_grounding_negation_precision_incidental_overlap_still_flags(claim, segment):
    t = _transcript(segment)
    s = _structured(subjective=[{"claim": claim, "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"


def test_grounding_negation_precision_wrong_symptom_now_caught():
    # BONUS recall gain: concept-grounding CLOSES the documented "(b) wrong-
    # symptom" gap the old marker-identity (B) missed. "Denies SOB" cited to
    # "denies chest pain" — the marker "denies" matched on both sides so P2-e
    # passed it CLEAN; P2-f sees SOB is negated NOWHERE in the cite → FLAG.
    t = _transcript("Patient denies chest pain.")
    s = _structured(subjective=[{"claim": "Denies SOB", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean and r.flags[0].reason == "negation_mismatch"


def test_grounding_negation_precision_emits_verified_log_paraphrase_clean():
    # LOG-EMISSION pin (standing discipline): the negated-concept-grounding path
    # MUST drive the production `scribe.grounding.verified` emission, and a
    # faithful paraphrase must report flagged=0 (not silently degrade). Pins the
    # key `flagged` field so a future refactor that drops the log OR mis-counts
    # goes red, not just green-because-untested.
    import structlog

    t = _transcript(
        "But my weight's the same and I haven't noticed any neck swelling."
    )
    s = _structured(subjective=[
        {"claim": "No neck swelling", "source_spans": ["S1"]},
    ])
    with structlog.testing.capture_logs() as cap:
        r = verify_grounding(s, t)
    assert r.clean is True
    verified = [e for e in cap if e.get("event") == "scribe.grounding.verified"]
    assert len(verified) == 1
    assert verified[0]["flagged"] == 0
    assert verified[0]["total_claims"] == 1


def test_grounding_negation_precision_emits_verified_log_flag_counted():
    # Partner to the clean-path log pin: a genuinely invented negation increments
    # the emitted `flagged` count (the observability the operator greps on).
    import structlog

    t = _transcript("Patient reports headache.")
    s = _structured(objective=[
        {"claim": "Denies chest pain", "source_spans": ["S1"]},
    ])
    with structlog.testing.capture_logs() as cap:
        r = verify_grounding(s, t)
    assert not r.clean
    verified = [e for e in cap if e.get("event") == "scribe.grounding.verified"]
    assert len(verified) == 1
    assert verified[0]["flagged"] == 1


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
            {"stop_reason": "stop", "prompt_eval_count": 500},  # P3-b3: pec required
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
            "prompt_eval_count": 500,   # P3-b3: pec required (native path always returns it)
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


# ---------------------------------------------------------------------------
# P4-3 — speaker-aware note-gen prompt: [ROLE] tags, rule 7, worked examples.
# The flag SEMANTICS of the worked examples (that they walk CLEAN and the
# documented mis-placements flag) are pinned in test_scribe_speaker_attribution;
# THESE pins cover the prompt ASSEMBLY surface (build_prompt + SYSTEM_PROMPT text).
# ---------------------------------------------------------------------------

def _diarized_tx(*segs, diarized=True):
    """``segs`` are ``(text, speaker)`` tuples; ``speaker`` is a resolved role or
    None. Segment ids/timestamps are minted the same way as ``_transcript``."""
    segments = [
        Segment(id=f"S{i+1}", start_s=float(i * 5), end_s=float(i * 5 + 5),
                text=t, speaker=sp)
        for i, (t, sp) in enumerate(segs)
    ]
    return Transcript(source_id="synth-d", mode="synthetic", segments=segments,
                      diarized=diarized)


def test_p43_diarized_prompt_carries_role_tags_with_s_id_first_token():
    tx = _diarized_tx(
        ("I have had a cough for three days.", ROLE_PATIENT),
        ("Blood pressure is 120 over 80.", ROLE_CLINICIAN),
        ("He has not been eating well.", ROLE_OTHER),
    )
    lines = [ln for ln in build_prompt(tx).splitlines() if ln.startswith("S")]
    # S# stays the FIRST token of every line (the citation anchor).
    assert lines[0].startswith("S1 ") and lines[1].startswith("S2 ") and lines[2].startswith("S3 ")
    # the role tag sits AFTER the timestamp (S## [x-y s] [ROLE]:), uppercase.
    assert lines[0] == "S1 [0.0-5.0s] [PATIENT]: I have had a cough for three days."
    assert "[CLINICIAN]:" in lines[1]
    assert "[OTHER]:" in lines[2]


def test_p43_none_speaker_renders_unknown_tag():
    # A None speaker on a diarized transcript folds to [UNKNOWN] (fail-closed,
    # normalize_role) — never omitted, never a raw value.
    line = [ln for ln in build_prompt(_diarized_tx(("Some content.", None))).splitlines()
            if ln.startswith("S1")][0]
    assert line == "S1 [0.0-5.0s] [UNKNOWN]: Some content."


def test_p43_raw_cluster_speaker_renders_unknown_tag():
    # A raw pyannote cluster id (never a canonical role) folds to [UNKNOWN].
    line = [ln for ln in build_prompt(_diarized_tx(("Some content.", "SPEAKER_00"))).splitlines()
            if ln.startswith("S1")][0]
    assert "[UNKNOWN]:" in line


def test_p43_undiarized_prompt_byte_identical_to_pre_p43():
    # THE byte-identical pin: an un-diarized transcript's block carries NO [ROLE]
    # tag (not even [UNKNOWN]), even if a segment carries a stale speaker — the
    # exact pre-P4-3 "S## [x-y s]: text" line format. Reconstruct it literally.
    segs = [
        Segment(id="S1", start_s=0.0, end_s=5.0, text="Patient reports a cough.",
                speaker=ROLE_PATIENT),   # speaker present but transcript un-diarized
        Segment(id="S2", start_s=5.0, end_s=10.0, text="Amoxicillin 500mg."),
    ]
    tx = Transcript(source_id="synth-u", mode="synthetic", segments=segs, diarized=False)
    expected = "\n".join([
        "Transcript segments (cite these ids in source_spans):",
        "",
        "S1 [0.0-5.0s]: Patient reports a cough.",
        "S2 [5.0-10.0s]: Amoxicillin 500mg.",
    ])
    assert build_prompt(tx) == expected
    body = build_prompt(tx)
    assert "[PATIENT]" not in body and "[UNKNOWN]" not in body and "[ROLE]" not in body


def test_p43_empty_transcript_keeps_no_segments_marker_both_modes():
    for diar in (True, False):
        tx = Transcript(source_id="e", mode="synthetic", segments=[], diarized=diar)
        assert build_prompt(tx).endswith("(no segments)")


def test_p43_rule7_present_in_system_prompt():
    # rule 7 attribution-placement — anchor on distinctive phrases so a silent drop
    # of any of the four placement rules is caught.
    assert "PLACE CONTENT BY SPEAKER" in SYSTEM_PROMPT
    assert "home blood pressure" in SYSTEM_PROMPT           # patient home-vital → Subjective
    assert "self-diagnosis" in SYSTEM_PROMPT                # patient lay self-dx → Subjective
    # the extract-not-infer invariant: the model emits NO role/attribution field.
    assert "must NOT emit any speaker, role" in SYSTEM_PROMPT


def test_p43_worked_examples_c_and_d_present():
    assert "WORKED EXAMPLE C" in SYSTEM_PROMPT and "WORKED EXAMPLE D" in SYSTEM_PROMPT
    # the diarized worked-example lines use the [ROLE] format build_prompt emits.
    assert "[PATIENT]:" in SYSTEM_PROMPT and "[CLINICIAN]:" in SYSTEM_PROMPT


def test_p43_worked_example_line_matches_build_prompt_format():
    # The worked-example transcript lines in SYSTEM_PROMPT must be BYTE-IDENTICAL
    # to what build_prompt actually emits — so a format change (e.g. moving [ROLE]
    # before the timestamp) that forgets the examples goes RED here.
    seg = Segment(
        id="S1", start_s=0.0, end_s=7.0, speaker=ROLE_PATIENT,
        text="I checked my blood pressure at home this morning and it was 150 over 90.",
    )
    tx = Transcript(source_id="c", mode="synthetic", segments=[seg], diarized=True)
    line = [ln for ln in build_prompt(tx).splitlines() if ln.startswith("S1")][0]
    assert line in SYSTEM_PROMPT
