"""#12 slice 12d — the deterministic, LLM-free consent line (design §7.2/§7.3).

Pins the auto-inserted consent attestation line:

  * each state (confirmed / declined / withdrawn / none) renders the EXACT deterministic string,
    with the date/time read from the durable consent event's ``ts`` (an injected clock);
  * the pipeline PREPENDS it under the note title (alongside the P4-2 banner convention);
  * composition-only — the line is built AFTER render_soap and never enters the LLM prompt;
  * regen-safety (§7.3) — re-composing after a mid-encounter withdrawal flips the line.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import alfred.distiller.backends.ollama as ollama_mod
import alfred.scribe.pipeline as pipeline_mod
from alfred.scribe.config import load_from_unified
from alfred.scribe.events import ScribeEvents
from alfred.scribe.pipeline import generate_verified_note
from alfred.scribe.transcript import Segment, Transcript

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(mode="synthetic"):
    return load_from_unified({"scribe": {
        "mode": mode, "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _facade(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"))


_CANNED = (
    '{"subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],'
    ' "objective": [], "assessment": [], "plan": [], "assessment_reasoning_stated": false}'
)


def _fake_ollama(capture=None):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        if capture is not None:
            capture["prompt"] = prompt
            capture["system"] = system or ""
        return (_CANNED, {"stop_reason": "stop", "prompt_eval_count": 500})
    return _fake


# ── facade consent_line — the exact deterministic strings (§7.2) ─────────────

def test_consent_line_confirmed(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-c", captured_by="jdoe", now="2026-07-18T10:15:00+00:00")
    assert ev.consent_line("enc-c") == (
        "> Consent: patient verbally consented on 2026-07-18 at 10:15, using STAY-C.")


def test_consent_line_confirmed_custom_tool(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-c", captured_by="jdoe", now="2026-07-18T10:15:00+00:00")
    assert "using MyScribe." in ev.consent_line("enc-c", tool="MyScribe")


def test_consent_line_declined(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_declined(subject_id="enc-d", captured_by="jdoe", now="2026-07-18T08:00:00+00:00")
    assert ev.consent_line("enc-d") == (
        "> Consent: patient DECLINED AI recording on 2026-07-18 at 08:00. No recording captured.")


def test_consent_line_withdrawn_reads_both_event_times(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-w", captured_by="jdoe", now="2026-07-18T09:00:00+00:00")
    ev.consent_withdrawn(subject_id="enc-w", at_seq=7, actor="jdoe", now="2026-07-18T14:30:00+00:00")
    assert ev.consent_line("enc-w") == (
        "> Consent: patient verbally consented on 2026-07-18 at 09:00; consent WITHDRAWN at "
        "2026-07-18 at 14:30 (audio boundary seq 7). Recording stopped.")


def test_consent_line_none_is_explicit_ilb(tmp_path):
    ev = _facade(tmp_path)
    # no consent recorded → an EXPLICIT line, never a blank (ILB).
    assert ev.consent_line("enc-never") == "> Consent: not recorded (synthetic/test encounter)."


def test_consent_line_inactive_store_returns_empty(tmp_path):
    ev = _facade(tmp_path)
    ev._active = False
    assert ev.consent_line("enc-x") == ""       # caller prepends nothing when the store is inactive


def test_consent_line_malformed_ts_degrades_not_crashes(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-m", captured_by="jdoe", now="not-a-timestamp")
    assert ev.consent_line("enc-m") == (
        "> Consent: patient verbally consented on unknown at unknown, using STAY-C.")


# ── regen-safety (§7.3) — re-composition flips on a mid-encounter withdrawal ──

def test_consent_line_regen_flips_confirmed_to_withdrawn(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-r", captured_by="jdoe", now="2026-07-18T09:00:00+00:00")
    first = ev.consent_line("enc-r")
    assert first.startswith("> Consent: patient verbally consented on 2026-07-18 at 09:00, using")
    # a withdrawal lands between checkpoints — the NEXT compose flips the line (no stored drift).
    ev.consent_withdrawn(subject_id="enc-r", at_seq=3, actor="jdoe", now="2026-07-18T09:20:00+00:00")
    second = ev.consent_line("enc-r")
    assert "WITHDRAWN at 2026-07-18 at 09:20 (audio boundary seq 3)" in second


# ── _prepend_consent_line — placement under the title, above the sections ────

def test_prepend_places_consent_under_title_above_sections():
    body = "# Encounter E\n\n## Subjective\n- foo\n"
    out = pipeline_mod._prepend_consent_line(body, "> Consent: X.")
    assert out.index("# Encounter E") < out.index("> Consent: X.") < out.index("## Subjective")


def test_prepend_keeps_body_above_a_p4_banner():
    # a P4-2 note-level banner (rendered after the title) stays BELOW the consent line.
    body = "# Encounter E\n\n> attribution unverified\n\n## Subjective\n- foo\n"
    out = pipeline_mod._prepend_consent_line(body, "> Consent: X.")
    assert out.index("> Consent: X.") < out.index("> attribution unverified")


def test_prepend_empty_consent_is_noop():
    body = "# Encounter E\n\n## Subjective\n- foo\n"
    assert pipeline_mod._prepend_consent_line(body, "") == body


# ── pipeline integration — the note carries the line; the LLM never sees it ──

def test_pipeline_prepends_consent_line(tmp_path, monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama(cap))
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="s", captured_by="jdoe", now="2026-07-18T11:00:00+00:00")
    t = Transcript(source_id="s", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0, end_s=5, text="chest pain for 2 days")])
    vnote = asyncio.run(generate_verified_note(t, config=_config(), title="E", events=ev))
    line = "> Consent: patient verbally consented on 2026-07-18 at 11:00, using STAY-C."
    assert line in vnote.body
    assert vnote.body.index("# E") < vnote.body.index(line) < vnote.body.index("## ")
    # COMPOSITION-ONLY — the consent line never entered the model prompt (§7.2 un-hallucinatable).
    assert "> Consent:" not in cap["prompt"] and "> Consent:" not in cap["system"]


def test_pipeline_no_events_no_consent_line(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama())
    t = Transcript(source_id="s", mode="synthetic",
                   segments=[Segment(id="S1", start_s=0, end_s=5, text="chest pain")])
    vnote = asyncio.run(generate_verified_note(t, config=_config(), title="E"))  # events=None
    assert "> Consent:" not in vnote.body       # no facade threaded in → no line


def test_consent_line_composed_after_render_and_llm(tmp_path):
    # structural pin: in the choke, the consent line is composed AFTER both generate_structured
    # (the LLM call) and render_soap — it can never be part of what the model is asked to produce.
    choke = inspect.getsource(pipeline_mod.generate_verified_note)
    assert "consent_line(" in choke
    assert choke.index("generate_structured(") < choke.index("consent_line(")
    assert choke.index("render_soap(") < choke.index("consent_line(")
