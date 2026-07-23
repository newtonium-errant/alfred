"""P4-2 — deterministic speaker-aware grounding + the mis-attribution safety net.

UNCONDITIONAL CI — no torch, no pyannote, no ``importorskip``. Pure deterministic
string/graph ops over hand-built diarized transcripts. Covers:

  * the asymmetric per-section rules — speaker_mismatch in each of O/A/P, the
    co-citation-laundering close, speaker_unverified in every section, Subjective
    collateral, Subjective clinician-only NO-flag, the clean baselines;
  * the note-level attribution_unverified banner on/off + its body render +
    frontmatter carry;
  * the cr-p41 MIXED accumulation (chunk1 diarized, chunk2 fail-opened → a
    diarized=True transcript WITH speaker=None segments → unverified, no crash),
    both as a direct transcript and produced by the real accumulator fail-open;
  * sub-purity conf demotion (config-driven threshold) + the conf-None-stands case;
  * the un-diarized BYTE-IDENTICAL no-op pin (rendered body + frontmatter);
  * the flags_for multi-literal render + dedupe pins;
  * the reason→literal lockstep pin + the flags_finalized / fail-open log seams.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import frontmatter
import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
import alfred.scribe.diarize as diarize_mod
import alfred.scribe.pipeline as pipeline_mod
from alfred.scribe import (
    SCRIBE_DRAFTER_IDENTITY,
    accumulate_encounter,
    generate_verified_note,
    ledger_path,
    load_from_unified,
    load_ledger,
)
from alfred.scribe.attest import attest
from alfred.vault.ops import vault_create
from alfred.scribe.grounding import GroundingFlag, GroundingResult, verify
from alfred.scribe.inferred_dx import check_inferred_diagnoses
from alfred.scribe.notegen import (
    ATTRIBUTION_UNVERIFIED,
    COLLATERAL_ATTRIBUTION,
    GROUNDING_UNVERIFIED,
    SPEAKER_MISMATCH,
    SPEAKER_UNVERIFIED,
    Claim,
    StructuredNote,
    render_soap,
)
from alfred.scribe.speaker_attribution import (
    ATTRIBUTION_UNVERIFIED_REASON,
    COLLATERAL_ATTRIBUTION_REASON,
    SPEAKER_MISMATCH_REASON,
    SPEAKER_UNVERIFIED_REASON,
    check_speaker_attribution,
)
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    Segment,
    Transcript,
)

# Obviously-fake test salt (NOT a real-provider-shaped secret) — the sovereign
# scribe fail-louds without one (P3-b1), so every fixture config carries it.
_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(provider="fake", purity=0.80):
    return load_from_unified({"scribe": {
        "mode": "synthetic",
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "diarize": {"provider": provider, "purity_threshold": purity},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _seg(i, text, *, speaker=None, conf=None):
    return Segment(
        id=f"S{i}", start_s=float(i), end_s=float(i) + 1, text=text,
        speaker=speaker, speaker_conf=conf,
    )


def _tx(*segs, diarized=True):
    return Transcript(
        source_id="enc-test", mode="synthetic", segments=list(segs), diarized=diarized,
    )


def _note(**sections):
    return StructuredNote(
        subjective=sections.get("subjective", []),
        objective=sections.get("objective", []),
        assessment=sections.get("assessment", []),
        plan=sections.get("plan", []),
        assessment_reasoning_stated=sections.get("assessment_reasoning_stated", True),
    )


def _run(note, tx, config=None):
    return check_speaker_attribution(note, tx, config or _config())


def _reasons(flags):
    return [f.reason for f in flags]


def _install_fake_ollama(monkeypatch, canned):
    async def _f(prompt, system=None, model="", endpoint="", **kw):
        return (canned, {"stop_reason": "stop", "prompt_eval_count": 500})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _f)


def _write_chunk(enc_dir, seq, lines, *, synthetic=True, pad=3):
    """``chunk_NNN.wav`` + role-tagged ``.txt`` sidecar + ``.meta.json`` marker
    (mirrors test_scribe_diarize). Distinct bytes per seq so hashes differ."""
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:0{pad}d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-bytes-seq-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc_dir / f"{name}.meta.json").write_text(
        json.dumps({"synthetic": synthetic, "seq": seq}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# O/A/P — patient/other cited ⇒ speaker_mismatch (each section, each role)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", ["objective", "assessment", "plan"])
@pytest.mark.parametrize("role", [ROLE_PATIENT, ROLE_OTHER])
def test_oap_patient_or_other_cited_flags_speaker_mismatch(section, role):
    # A clinician-authored section (O/A/P) claim citing a patient/other turn is a
    # mismatch. A second clinician segment keeps the note-level banner OFF so the
    # assertion isolates the per-claim rule.
    tx = _tx(
        _seg(1, "Some finding.", speaker=role),
        _seg(2, "Clinician turn.", speaker=ROLE_CLINICIAN),
    )
    note = _note(**{section: [Claim(claim="Some finding", source_spans=["S1"])]})
    flags = _run(note, tx)
    assert _reasons(flags) == [SPEAKER_MISMATCH_REASON]
    assert flags[0].section == section and flags[0].claim_index == 0
    assert flags[0].source_spans == ["S1"]


def test_oap_cocited_clinician_does_not_clear_mismatch():
    # THE co-citation-laundering close: an Objective claim co-citing a clinician
    # turn AND a patient turn STILL flags (do NOT let "any clinician clears it").
    tx = _tx(
        _seg(1, "My blood pressure was high.", speaker=ROLE_PATIENT),
        _seg(2, "BP measured 150 over 95.", speaker=ROLE_CLINICIAN),
    )
    note = _note(objective=[Claim(claim="BP 150 over 95", source_spans=["S1", "S2"])])
    flags = _run(note, tx)
    assert _reasons(flags) == [SPEAKER_MISMATCH_REASON]
    assert flags[0].source_spans == ["S1", "S2"]


def test_oap_clinician_cited_clean():
    # Clinician-authored content cited to the clinician → no flag (expected).
    tx = _tx(_seg(1, "BP measured 120 over 80.", speaker=ROLE_CLINICIAN))
    note = _note(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    assert _run(note, tx) == []


# ---------------------------------------------------------------------------
# ALL sections — an unknown cited role ⇒ speaker_unverified
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section", ["subjective", "objective", "assessment", "plan"])
def test_unknown_cited_flags_speaker_unverified_every_section(section):
    tx = _tx(
        _seg(1, "Some content.", speaker=None),              # None → unknown
        _seg(2, "Clinician turn.", speaker=ROLE_CLINICIAN),  # banner OFF
    )
    note = _note(**{section: [Claim(claim="Some content", source_spans=["S1"])]})
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


def test_raw_cluster_speaker_resolves_unknown():
    # A raw pyannote cluster id (never a canonical role) folds to unknown.
    tx = _tx(
        _seg(1, "Some content.", speaker="SPEAKER_00"),
        _seg(2, "Clinician turn.", speaker=ROLE_CLINICIAN),
    )
    note = _note(objective=[Claim(claim="Some content", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


# ---------------------------------------------------------------------------
# Subjective — asymmetric: other ⇒ collateral; clinician-only ⇒ NO flag
# ---------------------------------------------------------------------------

def test_subjective_other_cited_flags_collateral():
    tx = _tx(
        _seg(1, "He hasn't been eating well.", speaker=ROLE_OTHER),  # caregiver
        _seg(2, "How long has that been going on?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Not eating well", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [COLLATERAL_ATTRIBUTION_REASON]


def test_subjective_clinician_only_no_flag():
    # Clinician-relayed HPI is legit — a Subjective claim citing ONLY a clinician
    # turn gets NO flag (the alarm-fatigue guard; pinned explicitly per spec).
    tx = _tx(_seg(1, "Patient reports three days of cough.", speaker=ROLE_CLINICIAN))
    note = _note(subjective=[Claim(claim="Cough for three days", source_spans=["S1"])])
    assert _run(note, tx) == []


def test_subjective_patient_cited_clean():
    # The patient's own report cited to the patient → expected, no flag.
    tx = _tx(
        _seg(1, "I've had a cough for three days.", speaker=ROLE_PATIENT),
        _seg(2, "Any fever?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Cough for three days", source_spans=["S1"])])
    assert _run(note, tx) == []


def test_subjective_patient_in_oap_is_not_collateral_but_mismatch():
    # A patient turn in Objective is a mismatch (NOT collateral — collateral is
    # Subjective-only + other-only). Guards the two asymmetric rules don't bleed.
    tx = _tx(
        _seg(1, "My BP was high at home.", speaker=ROLE_PATIENT),
        _seg(2, "Noted.", speaker=ROLE_CLINICIAN),
    )
    note = _note(objective=[Claim(claim="BP high", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_MISMATCH_REASON]


# ---------------------------------------------------------------------------
# A claim may carry MULTIPLE distinct reasons; one MAX per reason
# ---------------------------------------------------------------------------

def test_claim_carries_multiple_distinct_reasons():
    # An Objective claim citing BOTH a patient turn AND an unknown turn carries
    # speaker_mismatch AND speaker_unverified — two distinct reasons, one each.
    tx = _tx(
        _seg(1, "My BP was high.", speaker=ROLE_PATIENT),
        _seg(2, "Some content.", speaker=None),
        _seg(3, "Clinician turn.", speaker=ROLE_CLINICIAN),  # banner OFF
    )
    note = _note(objective=[Claim(claim="BP high", source_spans=["S1", "S2"])])
    flags = _run(note, tx)
    assert set(_reasons(flags)) == {SPEAKER_MISMATCH_REASON, SPEAKER_UNVERIFIED_REASON}
    assert len(flags) == 2                                    # one per reason, no dupes


def test_two_patient_citations_yield_one_mismatch():
    # One flag MAX per claim per reason — two patient turns → a single mismatch.
    tx = _tx(
        _seg(1, "My BP was high.", speaker=ROLE_PATIENT),
        _seg(2, "And my pulse raced.", speaker=ROLE_PATIENT),
        _seg(3, "Clinician turn.", speaker=ROLE_CLINICIAN),
    )
    note = _note(assessment=[Claim(claim="BP high with tachycardia", source_spans=["S1", "S2"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_MISMATCH_REASON]


# ---------------------------------------------------------------------------
# NOTE-LEVEL banner — on when no clinician anywhere, off with one clinician turn
# ---------------------------------------------------------------------------

def test_banner_fires_when_no_clinician_anywhere():
    tx = _tx(
        _seg(1, "I feel unwell.", speaker=ROLE_PATIENT),
        _seg(2, "He looks tired.", speaker=ROLE_OTHER),
    )
    note = _note(subjective=[Claim(claim="Feels unwell", source_spans=["S1"])])
    note_flags = [f for f in _run(note, tx) if f.section == "note"]
    assert len(note_flags) == 1
    assert note_flags[0].reason == ATTRIBUTION_UNVERIFIED_REASON
    assert note_flags[0].claim_index == -1
    assert note_flags[0].source_spans == []


def test_banner_off_when_one_clinician_segment_present():
    tx = _tx(
        _seg(1, "I feel unwell.", speaker=ROLE_PATIENT),
        _seg(2, "Let me take a look.", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Feels unwell", source_spans=["S1"])])
    assert [f for f in _run(note, tx) if f.section == "note"] == []


def test_banner_uses_purity_resolution_sub_purity_clinician_does_not_clear():
    # The banner's "any clinician anywhere" check uses the SAME sub-purity
    # resolution — a sub-purity clinician turn does NOT clear the banner.
    tx = _tx(_seg(1, "BP 120 over 80.", speaker=ROLE_CLINICIAN, conf=0.5))  # < 0.80
    note = _note(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    note_flags = [f for f in _run(note, tx) if f.section == "note"]
    assert len(note_flags) == 1 and note_flags[0].reason == ATTRIBUTION_UNVERIFIED_REASON


# ---------------------------------------------------------------------------
# cr-p41 MIXED accumulation — diarized=True WITH speaker=None segments
# ---------------------------------------------------------------------------

def test_mixed_accumulation_none_speaker_resolves_unverified_no_crash():
    # diarized LATCHES True once any chunk diarizes; a mixed accumulation leaves
    # diarized=True WITH speaker=None segments (chunk2 fail-opened). Those resolve
    # to unknown → a claim citing them gets speaker_unverified, and the pass does
    # NOT crash on the None-speaker segment.
    tx = _tx(
        _seg(1, "BP measured 120 over 80.", speaker=ROLE_CLINICIAN),  # chunk1 diarized
        _seg(2, "Follow-up next week.", speaker=None),                # chunk2 fail-opened
        diarized=True,
    )
    note = _note(plan=[Claim(claim="Follow-up next week", source_spans=["S2"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


def test_accumulator_produces_mixed_diarized_transcript(tmp_path, monkeypatch):
    # Prove the mixed transcript is PRODUCED by the real fail-open accumulator:
    # chunk1 diarizes (roles set, latches diarized), chunk2's diarize raises →
    # folds un-attributed (speaker=None) WITHOUT holding the encounter.
    real_assign = diarize_mod.assign_speakers
    calls = {"n": 0}

    def _assign(config, audio_path, chunk_tx, *, resolved=None, match_sink=None):  # P4-5 kwargs
        calls["n"] += 1
        if calls["n"] == 1:
            return real_assign(config, audio_path, chunk_tx, resolved=resolved,
                               match_sink=match_sink)  # chunk1 diarizes
        raise RuntimeError("diarizer exploded on chunk2")      # chunk2 fail-opens

    monkeypatch.setattr(diarize_mod, "assign_speakers", _assign)

    enc = tmp_path / "encMix"
    _write_chunk(enc, 1, ["[CLIN] BP measured 120 over 80."])
    _write_chunk(enc, 2, ["Follow-up next week."])
    cfg = _config(provider="fake")

    r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 2
    led = load_ledger(ledger_path(enc, r.encounter_id))
    assert led.diarized is True                          # latched by chunk1
    assert led.segments[0].speaker == ROLE_CLINICIAN     # chunk1 role
    assert led.segments[1].speaker is None               # chunk2 fail-opened

    note = _note(plan=[Claim(claim="Follow-up next week", source_spans=["S2"])])
    flags = check_speaker_attribution(note, led, cfg)    # must NOT crash
    assert SPEAKER_UNVERIFIED_REASON in _reasons(flags)


# ---------------------------------------------------------------------------
# Sub-purity conf demotion (config-driven threshold) + conf-None-stands
# ---------------------------------------------------------------------------

def test_sub_purity_patient_in_subjective_demotes_to_unverified():
    # conf 0.5 < purity 0.80 → patient demotes to unknown → speaker_unverified
    # (NOT collateral, NOT clean).
    tx = _tx(
        _seg(1, "I feel dizzy.", speaker=ROLE_PATIENT, conf=0.5),
        _seg(2, "Since when?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Feels dizzy", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


def test_sub_purity_clinician_in_objective_demotes_to_unverified():
    # Sub-purity demotes even a clinician: an Objective claim citing a
    # sub-purity clinician turn resolves unknown → speaker_unverified (not clean).
    tx = _tx(
        _seg(1, "BP 120 over 80.", speaker=ROLE_CLINICIAN, conf=0.5),
        _seg(2, "Noted.", speaker=ROLE_CLINICIAN, conf=None),  # high-conf → banner OFF
    )
    note = _note(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


def test_conf_none_known_role_stands():
    # conf None + a known role → the role STANDS (a None conf is a confident
    # resolution, not a missing one). A patient in Subjective stays CLEAN.
    tx = _tx(
        _seg(1, "I feel dizzy.", speaker=ROLE_PATIENT, conf=None),
        _seg(2, "Since when?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Feels dizzy", source_spans=["S1"])])
    assert _run(note, tx) == []


def test_conf_at_threshold_not_demoted():
    # Demotion is strict ``< threshold``; conf == purity_threshold is NOT sub-purity.
    tx = _tx(
        _seg(1, "I feel dizzy.", speaker=ROLE_PATIENT, conf=0.80),
        _seg(2, "Since when?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Feels dizzy", source_spans=["S1"])])
    assert _run(note, tx) == []


def test_purity_threshold_comes_from_config():
    # A conf sub-purity under a HIGH threshold but OK under a LOW one — proves the
    # threshold is read from config.diarize.purity_threshold, not hardcoded.
    tx = _tx(
        _seg(1, "I feel dizzy.", speaker=ROLE_PATIENT, conf=0.70),
        _seg(2, "Since when?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Feels dizzy", source_spans=["S1"])])
    assert _reasons(_run(note, tx, _config(purity=0.80))) == [SPEAKER_UNVERIFIED_REASON]
    assert _run(note, tx, _config(purity=0.60)) == []


# ---------------------------------------------------------------------------
# Un-diarized BYTE-IDENTICAL no-op (frontmatter + rendered body)
# ---------------------------------------------------------------------------

def test_undiarized_pass_returns_empty():
    tx = _tx(_seg(1, "BP 120 over 80.", speaker=ROLE_PATIENT), diarized=False)
    note = _note(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    assert check_speaker_attribution(note, tx, _config()) == []


def test_undiarized_note_byte_identical_frontmatter_and_body():
    # An un-diarized encounter's rendered note + grounding_flags frontmatter is
    # BYTE-IDENTICAL whether or not the P4-2 pass runs (it no-ops). Holds the
    # flags_for rename constant (both sides use the new render) so only the pass's
    # no-op is under test.
    tx = _tx(
        _seg(1, "Patient reports chest pain for 2 days.", speaker=ROLE_PATIENT),
        _seg(2, "Amoxicillin 500mg.", speaker=ROLE_CLINICIAN),
        diarized=False,
    )
    note = _note(
        subjective=[Claim(claim="Chest pain for 2 days", source_spans=["S1"])],
        plan=[Claim(claim="Amoxicillin 500mg", source_spans=["S2"])],
    )
    cfg = _config()

    g_without = verify(note, tx)
    g_without.flags.extend(check_inferred_diagnoses(note, tx))
    body_without = render_soap(note, title="E", grounding=g_without)
    md_without = g_without.metadata

    g_with = verify(note, tx)
    g_with.flags.extend(check_inferred_diagnoses(note, tx))
    g_with.flags.extend(check_speaker_attribution(note, tx, cfg))
    body_with = render_soap(note, title="E", grounding=g_with)
    md_with = g_with.metadata

    assert body_with == body_without                          # byte-identical body
    assert md_with == md_without                              # byte-identical frontmatter
    for lit in (SPEAKER_MISMATCH, SPEAKER_UNVERIFIED, COLLATERAL_ATTRIBUTION, ATTRIBUTION_UNVERIFIED):
        assert lit not in body_with                           # no speaker/banner leak


# ---------------------------------------------------------------------------
# flags_for multi-literal render + dedupe (the flag_for → flags_for rename)
# ---------------------------------------------------------------------------

def test_flags_for_renders_both_grounding_and_speaker_literals_inline():
    # A claim carrying BOTH a grounding flag and a speaker flag renders BOTH ⚠
    # inline (space-joined) on the same claim line.
    g = GroundingResult(flags=[
        GroundingFlag("objective", 0, "number_mismatch", "d", "BP 5", ["S1"]),
        GroundingFlag("objective", 0, "speaker_mismatch", "d", "BP 5", ["S1"]),
    ])
    assert g.flags_for("objective", 0) == [GROUNDING_UNVERIFIED, SPEAKER_MISMATCH]
    note = _note(objective=[Claim(claim="BP 5", source_spans=["S1"])])
    body = render_soap(note, title="E", grounding=g)
    line = [ln for ln in body.splitlines() if ln.startswith("- BP 5")][0]
    assert GROUNDING_UNVERIFIED in line and SPEAKER_MISMATCH in line


def test_flags_for_dedupes_same_literal():
    # Two flags mapping to the SAME literal (two grounding reasons → GROUNDING_
    # UNVERIFIED) render ONE literal (dedupe by literal).
    g = GroundingResult(flags=[
        GroundingFlag("plan", 0, "number_mismatch", "d", "c", ["S1"]),
        GroundingFlag("plan", 0, "negation_mismatch", "d", "c", ["S1"]),
    ])
    assert g.flags_for("plan", 0) == [GROUNDING_UNVERIFIED]


# ---------------------------------------------------------------------------
# Banner render — visible body line + rides frontmatter metadata
# ---------------------------------------------------------------------------

def test_banner_renders_in_body_above_sections_and_rides_frontmatter():
    tx = _tx(_seg(1, "I feel unwell.", speaker=ROLE_PATIENT), diarized=True)  # no clinician
    note = _note(subjective=[Claim(claim="Feels unwell", source_spans=["S1"])])
    g = verify(note, tx)
    g.flags.extend(check_speaker_attribution(note, tx, _config()))
    body = render_soap(note, title="Enc", grounding=g)

    assert ATTRIBUTION_UNVERIFIED in body
    assert body.index(ATTRIBUTION_UNVERIFIED) < body.index("## Subjective")  # ABOVE sections
    note_meta = [m for m in g.metadata if m["section"] == "note"]
    assert len(note_meta) == 1
    assert note_meta[0]["reason"] == "attribution_unverified"
    assert note_meta[0]["claim_index"] == -1


# ---------------------------------------------------------------------------
# reason → literal lockstep (the cross-module duplication guard)
# ---------------------------------------------------------------------------

def test_reason_constants_are_mapped_in_grounding_dispatch():
    # The reason STRINGS are duplicated (speaker_attribution defines the constants;
    # grounding._REASON_INLINE_LITERAL keys them). Pin both sides in lockstep so a
    # rename on one side cannot silently un-map a reason (→ wrong ⚠).
    from alfred.scribe.grounding import _REASON_INLINE_LITERAL
    assert _REASON_INLINE_LITERAL[SPEAKER_MISMATCH_REASON] == SPEAKER_MISMATCH
    assert _REASON_INLINE_LITERAL[SPEAKER_UNVERIFIED_REASON] == SPEAKER_UNVERIFIED
    assert _REASON_INLINE_LITERAL[COLLATERAL_ATTRIBUTION_REASON] == COLLATERAL_ATTRIBUTION
    assert _REASON_INLINE_LITERAL[ATTRIBUTION_UNVERIFIED_REASON] == ATTRIBUTION_UNVERIFIED


# ---------------------------------------------------------------------------
# Pipeline seams — flags_finalized breakdown + fail-open (feedback rule #9)
# ---------------------------------------------------------------------------

def test_flags_finalized_log_carries_speaker_attribution_count(monkeypatch):
    # feedback_log_emission_test_pattern: the flags_finalized observability seam
    # must carry speaker_attribution_flags so a downstream monitor sees the true
    # breakdown, driven through the real generate_verified_note path.
    canned = json.dumps({
        "subjective": [], "objective": [{"claim": "BP 120 over 80", "source_spans": ["S1"]}],
        "assessment": [], "plan": [], "assessment_reasoning_stated": True,
    })
    _install_fake_ollama(monkeypatch, canned)
    tx = _tx(
        _seg(1, "BP 120 over 80.", speaker=ROLE_PATIENT),    # objective cites patient → mismatch
        _seg(2, "Let me check.", speaker=ROLE_CLINICIAN),    # banner OFF
        diarized=True,
    )
    with structlog.testing.capture_logs() as caps:
        vnote = asyncio.run(generate_verified_note(tx, config=_config(), title="T"))
    fin = [c for c in caps if c.get("event") == "scribe.grounding.flags_finalized"]
    assert len(fin) == 1
    assert fin[0]["speaker_attribution_flags"] == 1
    assert fin[0]["grounding_flags"] == 0 and fin[0]["inferred_diagnosis_flags"] == 0
    # #14c — the empty required S/A/P sections raise 3 note-level quality flags; the flags_finalized
    # seam now carries them too. total = 1 speaker + 3 quality.
    assert fin[0]["quality_flags"] == 3
    assert fin[0]["total_flags"] == 4
    assert vnote.flag_count == 4
    assert SPEAKER_MISMATCH in vnote.body                    # rendered inline


def test_speaker_attribution_crash_synthesizes_banner_and_logs_loudly(monkeypatch):
    # Fail-open but VISIBLE (F1): a crash in the safety net must NOT lose the note
    # (un-attributed ≫ lost) AND must surface — the note is drafted WITH a
    # synthesized note-level attribution_unverified banner (in body + frontmatter),
    # plus a loud PHI-free log.
    canned = json.dumps({
        "subjective": [], "objective": [{"claim": "BP 120 over 80", "source_spans": ["S1"]}],
        "assessment": [], "plan": [], "assessment_reasoning_stated": True,
    })
    _install_fake_ollama(monkeypatch, canned)

    def _boom(structured, transcript, config):
        raise RuntimeError("attribution exploded")

    monkeypatch.setattr(pipeline_mod, "check_speaker_attribution", _boom)
    tx = _tx(_seg(1, "BP 120 over 80.", speaker=ROLE_CLINICIAN), diarized=True)
    with structlog.testing.capture_logs() as caps:
        vnote = asyncio.run(generate_verified_note(tx, config=_config(), title="T"))

    assert vnote.body                                        # note STILL produced (not lost)
    # banner synthesized: renders in the body AND rides grounding_flags frontmatter
    assert ATTRIBUTION_UNVERIFIED in vnote.body
    note_meta = [m for m in vnote.grounding_flags if m["section"] == "note"]
    assert len(note_meta) == 1 and note_meta[0]["reason"] == "attribution_unverified"
    assert note_meta[0]["claim_index"] == -1
    # loud PHI-free log fires, carrying only the error CLASS — never the message (F5)
    warn = [c for c in caps if c.get("event") == "scribe.speaker_attribution.failed"]
    assert len(warn) == 1 and warn[0]["error_class"] == "RuntimeError"
    assert "attribution exploded" not in json.dumps(warn[0])   # raw message never logged (NOTE-4)
    # the finalized breakdown counts the synthesized banner
    fin = [c for c in caps if c.get("event") == "scribe.grounding.flags_finalized"]
    assert fin[0]["speaker_attribution_flags"] == 1


# ---------------------------------------------------------------------------
# QA round hardenings (F3/F4/F6/F8/F9) — mutation-proof pins
# ---------------------------------------------------------------------------

def test_subjective_other_cocited_clinician_still_collateral():
    # F3 — mirror of the O/A/P laundering pin: a Subjective claim co-citing an
    # OTHER turn AND a clinician turn STILL flags collateral (a co-cited clinician
    # does not launder the collateral source clean). Mutant that must fail:
    # `ROLE_OTHER in cited_roles and ROLE_CLINICIAN not in cited_roles`.
    tx = _tx(
        _seg(1, "He hasn't been eating well.", speaker=ROLE_OTHER),
        _seg(2, "How long has that been going on?", speaker=ROLE_CLINICIAN),
    )
    note = _note(subjective=[Claim(claim="Not eating well", source_spans=["S1", "S2"])])
    assert _reasons(_run(note, tx)) == [COLLATERAL_ATTRIBUTION_REASON]


def test_dangling_span_no_crash_other_claims_keep_flags():
    # F4 — a claim citing a nonexistent span id (S99) on a diarized transcript must
    # NOT crash; the dangling span contributes no role; OTHER claims keep their
    # speaker flags. Mutant that must fail: dropping the `if s in seg_by_id` guard
    # (→ KeyError on seg_by_id["S99"] → the whole pass raises).
    tx = _tx(
        _seg(1, "BP high.", speaker=ROLE_PATIENT),
        _seg(2, "Noted.", speaker=ROLE_CLINICIAN),          # banner OFF
    )
    note = _note(objective=[
        Claim(claim="Dangling", source_spans=["S99"]),      # nonexistent span
        Claim(claim="BP high", source_spans=["S1"]),        # real patient span
    ])
    flags = _run(note, tx)                                   # must NOT crash
    assert not any(f.section == "objective" and f.claim_index == 0 for f in flags)
    assert any(
        f.section == "objective" and f.claim_index == 1 and f.reason == SPEAKER_MISMATCH_REASON
        for f in flags
    )


@pytest.mark.parametrize("bad_conf", [float("nan"), float("inf")])
def test_nonfinite_conf_clinician_does_not_clear_banner(bad_conf):
    # F6 — a non-finite conf (NaN/±inf) is NOT a trustworthy high-purity value → the
    # clinician demotes to unknown → the banner still fires (no clinician anywhere).
    tx = _tx(_seg(1, "BP 120 over 80.", speaker=ROLE_CLINICIAN, conf=bad_conf))
    note = _note(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    note_flags = [f for f in _run(note, tx) if f.section == "note"]
    assert len(note_flags) == 1 and note_flags[0].reason == ATTRIBUTION_UNVERIFIED_REASON


@pytest.mark.parametrize("bad_conf", [float("nan"), float("inf")])
def test_nonfinite_conf_patient_in_subjective_demotes_to_unverified(bad_conf):
    # F6 — a non-finite conf patient in Subjective → unknown → speaker_unverified
    # (NOT collateral, NOT clean). Mutant that must fail: the pre-fix
    # `conf < purity_threshold` alone (NaN < x is False → would NOT demote).
    tx = _tx(
        _seg(1, "I feel dizzy.", speaker=ROLE_PATIENT, conf=bad_conf),
        _seg(2, "Since when?", speaker=ROLE_CLINICIAN),      # banner OFF
    )
    note = _note(subjective=[Claim(claim="Feels dizzy", source_spans=["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_UNVERIFIED_REASON]


def test_oap_sections_lockstep_with_soap_sections():
    # F9 — _OAP_SECTIONS must stay == SOAP_SECTIONS - {subjective} so a section
    # rename/add in notegen can't silently strip the mismatch/collateral rules.
    from alfred.scribe.notegen import SOAP_SECTIONS
    from alfred.scribe.speaker_attribution import _OAP_SECTIONS
    assert _OAP_SECTIONS == frozenset(SOAP_SECTIONS) - {"subjective"}


def test_attest_tolerates_mixed_flags_and_speaker_flags_carry_spans(tmp_path):
    # F8 — drive attest over a note whose grounding_flags carry inferred +
    # per-claim speaker + the note-level banner TOGETHER. Attest must tolerate the
    # shapes (the inferred-dx capture at attest is fail-silent, so a shape
    # regression is invisible without this). ALSO assert per-claim speaker flags
    # carry their source_spans (P4-5's correction loop re-derives cited roles from
    # spans × the ledger transcript — confirm the payload supports it).
    clinicians = {"np_jamie", "dr_synthetic"}
    now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
    grounding_flags = [
        {"section": "assessment", "claim_index": 0, "reason": "inferred_diagnosis",
         "detail": "inferred_diagnosis: major depressive disorder named in the claim "
                   "but absent from the cited segment(s)",
         "claim": "Major depressive disorder", "source_spans": ["S1"]},
        {"section": "objective", "claim_index": 0, "reason": "speaker_mismatch",
         "detail": "speaker_mismatch: this objective claim cites a patient/other turn",
         "claim": "BP 150 over 95", "source_spans": ["S1", "S2"]},
        {"section": "note", "claim_index": -1, "reason": "attribution_unverified",
         "detail": "attribution_unverified: no clinician voice identified",
         "claim": "", "source_spans": []},
    ]
    rel = vault_create(
        tmp_path, "clinical_note", "Synthetic mixed-flags encounter",
        set_fields={
            "ai_draft": True, "synthetic": True, "status": "ai_draft",
            "source_id": "enc-abc0123456789d", "drafted_by": SCRIBE_DRAFTER_IDENTITY,
            "grounding_flags": grounding_flags,
            "draft_original": "## Assessment\n- Major depressive disorder [S1]\n",
            "encounter_completeness": {"protocol": 1, "complete": True},  # complete → attest gate passes
        },
        body="## Assessment\n- Major depressive disorder [S1]\n",
        scope="stayc_clinical",
    )["path"]

    result = attest(
        tmp_path, rel, new_status="attested", attester="np_jamie",
        clinician_ids=clinicians, audit_path=tmp_path / "attest_audit.jsonl", now=now,
    )
    assert result["path"] == rel

    fm = frontmatter.load(tmp_path / rel)
    assert fm["status"] == "attested" and fm["attested_by"] == "np_jamie"   # attest tolerated the shapes
    # the per-claim speaker flag survives in frontmatter WITH its source_spans (P4-5 payload)
    speaker = [f for f in fm["grounding_flags"] if f["reason"] == "speaker_mismatch"]
    assert len(speaker) == 1 and speaker[0]["source_spans"] == ["S1", "S2"]
    # the note-level banner also round-trips
    assert any(
        f["reason"] == "attribution_unverified" and f["claim_index"] == -1
        for f in fm["grounding_flags"]
    )
    # audit appended (attest fully completed)
    audit = (tmp_path / "attest_audit.jsonl").read_text().strip().splitlines()
    assert len(audit) == 1 and json.loads(audit[0])["to_status"] == "attested"


# ---------------------------------------------------------------------------
# P4-3 worked-example ACCURACY (feedback_worked_example_accuracy) — the two
# SYSTEM_PROMPT worked examples (C, D) must walk CLEAN through the FULL stack
# (verify + inferred-dx + speaker-attribution), and the mis-placements the
# examples warn against must flag speaker_mismatch. These pin the examples to the
# code's actual behavior so a rule/engine change that breaks them is caught.
# ---------------------------------------------------------------------------

def _full_stack_flags(note, tx, config=None):
    c = config or _config()
    g = verify(note, tx)
    g.flags.extend(check_inferred_diagnoses(note, tx))
    g.flags.extend(check_speaker_attribution(note, tx, c))
    return g.flags


def test_p43_example_c_home_vital_in_subjective_is_clean():
    # WORKED EXAMPLE C output — the patient home vital placed in Subjective, the
    # clinician-measured vital in Objective: the whole note is CLEAN.
    tx = _tx(
        _seg(1, "I checked my blood pressure at home this morning and it was 150 over 90.", speaker=ROLE_PATIENT),
        _seg(2, "Here in clinic your blood pressure is 128 over 82.", speaker=ROLE_CLINICIAN),
        _seg(3, "Continue the lisinopril and recheck in two weeks.", speaker=ROLE_CLINICIAN),
    )
    note = _note(
        subjective=[Claim("Patient reports a home blood pressure of 150 over 90", ["S1"])],
        objective=[Claim("Blood pressure 128 over 82", ["S2"])],
        plan=[Claim("Continue lisinopril", ["S3"]), Claim("Recheck in two weeks", ["S3"])],
    )
    assert _full_stack_flags(note, tx) == []


def test_p43_example_c_home_vital_in_objective_flags_mismatch():
    # The documented counterfactual: the S1 home reading placed in OBJECTIVE
    # (citing the patient turn) draws speaker_mismatch — what rule 7 avoids.
    tx = _tx(
        _seg(1, "I checked my blood pressure at home this morning and it was 150 over 90.", speaker=ROLE_PATIENT),
        _seg(2, "Here in clinic your blood pressure is 128 over 82.", speaker=ROLE_CLINICIAN),
    )
    note = _note(objective=[Claim("Blood pressure 150 over 90", ["S1"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_MISMATCH_REASON]


def test_p43_example_d_relayed_hpi_and_self_dx_in_subjective_is_clean():
    # WORKED EXAMPLE D output — a clinician-RELAYED history in Subjective (legit,
    # no collateral/mismatch flag) AND the patient's lay self-diagnosis in
    # Subjective (not Assessment): the whole note is CLEAN, incl. inferred-dx.
    # inferred-dx mechanism (verified against the lexicon): diagnoses_named_in(
    # "...recurrent sciatica") returns [] — "sciatica" is NOT in the diagnosis
    # lexicon — so check_inferred_diagnoses SKIPS the claim at its `if not named:
    # continue` guard and never reaches the grounding-clear path. (Were sciatica
    # later added to the lexicon the claim would STILL clear: the term is verbatim
    # in the cited patient turn and "I think…" is not a hedge, so _stated_current
    # would clear it — but today it is the lexicon-skip that makes this clean.)
    tx = _tx(
        _seg(1, "So you've had lower back pain radiating down the left leg for about a week.", speaker=ROLE_CLINICIAN),
        _seg(2, "Yes, and honestly I think my sciatica is flaring up again.", speaker=ROLE_PATIENT),
        _seg(3, "On exam, straight leg raise is positive on the left.", speaker=ROLE_CLINICIAN),
        _seg(4, "Start naproxen 500mg twice daily and refer to physiotherapy.", speaker=ROLE_CLINICIAN),
    )
    note = _note(
        subjective=[
            Claim("Lower back pain radiating down the left leg for about a week", ["S1"]),
            Claim("Patient believes the pain is recurrent sciatica", ["S2"]),
        ],
        objective=[Claim("Straight leg raise positive on the left", ["S3"])],
        plan=[Claim("Start naproxen 500mg twice daily", ["S4"]), Claim("Refer to physiotherapy", ["S4"])],
    )
    assert _full_stack_flags(note, tx) == []


def test_p43_example_d_self_dx_in_assessment_flags_mismatch():
    # The documented counterfactual: the patient's self-diagnosis placed in
    # ASSESSMENT (citing the patient turn S2) draws speaker_mismatch (assessment is
    # clinician-authored content).
    tx = _tx(
        _seg(1, "So you've had lower back pain for a week.", speaker=ROLE_CLINICIAN),
        _seg(2, "Yes, and honestly I think my sciatica is flaring up again.", speaker=ROLE_PATIENT),
    )
    note = _note(assessment=[Claim("Recurrent sciatica", ["S2"])])
    assert _reasons(_run(note, tx)) == [SPEAKER_MISMATCH_REASON]
