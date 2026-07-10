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
from alfred.scribe.grounding import verify as verify_grounding
from alfred.scribe.notegen import (
    GROUNDING_UNVERIFIED,
    NOT_ADDRESSED,
    REASONING_NOT_STATED,
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
    body = render_soap(s, title="Encounter 1")
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
    body = render_soap(s, title="E")
    assert REASONING_NOT_STATED not in body


def test_parse_unparseable_fails_loud():
    with pytest.raises(NoteGenError):
        parse_structured_json("qwen said hello but no json here")


def test_parse_strips_markdown_fence():
    fenced = "Sure:\n```json\n{\"subjective\": [], \"objective\": [], \"assessment\": [], \"plan\": [], \"assessment_reasoning_stated\": true}\n```\n"
    s = parse_structured_json(fenced)
    assert s.subjective == [] and s.assessment_reasoning_stated is True


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
    assert all(c.grounding_flag is None for _, _, c in s.all_claims())


def test_grounding_catches_dose_flip_500mg_5mg():
    # THE 500mg→5mg pin.
    t = _transcript("Amoxicillin 500mg three times daily.")
    s = _structured(plan=[{"claim": "Amoxicillin 5mg", "source_spans": ["S1"]}])
    r = verify_grounding(s, t)
    assert not r.clean
    assert r.flags[0].reason == "number_mismatch"
    assert s.plan[0].grounding_flag == GROUNDING_UNVERIFIED


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
    verify_grounding(s, t)
    body = render_soap(s, title="E")
    assert f"- Amoxicillin 5mg [S1] {GROUNDING_UNVERIFIED}" in body


# ---------------------------------------------------------------------------
# the sovereign local call routes through call_ollama_no_tools (loopback)
# ---------------------------------------------------------------------------

def test_generate_routes_through_loopback_ollama(monkeypatch):
    captured = {}

    async def _fake_ollama(prompt, system=None, model="", endpoint="", **kw):
        captured["endpoint"] = endpoint
        captured["model"] = model
        captured["system"] = system
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
    body = render_soap(s, title="Integration encounter")
    assert "## Subjective" in body and "## Plan" in body
    assert isinstance(r.metadata, list)
