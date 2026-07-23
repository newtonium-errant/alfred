"""Extract-not-infer clinical note-gen — CODE + the frozen prompt↔code contract
(scribe P2-c).

This module owns the CONTRACT between the note-gen PROMPT (prompt-tuner's
domain) and the CODE (parse / render / ground). The clinical extract-not-infer
PROMPT is authored by prompt-tuner AGAINST this frozen contract; the placeholder
below is minimal — enough to run the code + a real-qwen integration test.

═══════════════════════════════════════════════════════════════════════════════
THE FROZEN CONTRACT (prompt-tuner authors the prompt to emit exactly this)
═══════════════════════════════════════════════════════════════════════════════

The model returns a SINGLE JSON object (no tool_use — qwen via the sovereign
local Ollama client), shape:

    {
      "subjective": [{"claim": "<text>", "source_spans": ["S1", "S3"]}],
      "objective":  [{"claim": "<text>", "source_spans": ["S2"]}],
      "assessment": [{"claim": "<text>", "source_spans": ["S4"]}],
      "plan":       [{"claim": "<text>", "source_spans": ["S5"]}],
      "assessment_reasoning_stated": true
    }

  * Four SOAP sections, each a LIST of claim objects ``{claim, source_spans}``.
  * ATOMIC CLAIMS — each ``claim`` object states EXACTLY ONE clinical finding.
    ⚠️ LOAD-BEARING: the deterministic NEGATION guard is only SOUND under atomic
    claims. If the model bundles a positive + a negative into one claim string
    ("Reports cough. Denies fever and SOB." cited to "denies fever, cough, SOB")
    the negation set ``{denies}`` matches on both sides and the FLIPPED positive
    passes UNFLAGGED (the ROS-list hole). Under one-finding-per-claim the guard
    over-flags (safe). prompt-tuner MUST instruct qwen to emit atomic claims.
  * ``source_spans`` are transcript SEGMENT IDS — the ``[S#]`` citation format,
    matching ``transcript.make_segment_id`` (``S1``, ``S2``, ...). EVERY claim
    MUST cite the segment(s) it is grounded in; an uncited claim is flagged.
  * ``assessment_reasoning_stated`` (bool): did the transcript VERBALIZE the
    clinical reasoning for the assessment? Extract-not-infer — the model must
    NOT invent reasoning. Absent / false ⇒ the renderer emits the
    REASONING-NOT-STATED literal. DEFAULT FALSE (conservative: flag, never
    fabricate).

THE THREE FROZEN LITERALS the renderer / grounding emit (verbatim):

  * NOT_ADDRESSED             = "Not addressed"
        — an empty SOAP section (intentionally-left-blank; the topic was not
          discussed). NEVER invent content to fill a section.
  * REASONING_NOT_STATED      = "⚠ REASONING NOT STATED — clinician to complete"
        — the assessment has claims but reasoning was not verbalized.
  * GROUNDING_UNVERIFIED      = "⚠ GROUNDING UNVERIFIED — clinician to confirm"
        — a per-claim flag from the DETERMINISTIC grounding-verify (see
          ``scribe.grounding``): ungrounded span, ungrounded assertion, or a
          number/negation that does not match the cited segment.

Extract-not-infer is enforced by (a) the PROMPT (prompt-tuner) and (b) the
deterministic GROUNDING pass. The CODE here renders FAITHFULLY + flags — it
never adds, removes, or "fixes" a claim.

P4-3 (speaker-diarization) adds an INPUT-only signal, NOT an output-contract
change: when ``transcript.diarized`` is True ``build_prompt`` tags each segment
line with an uppercase ``[ROLE]`` ([CLINICIAN]/[PATIENT]/[OTHER]/[UNKNOWN]), and
SYSTEM_PROMPT rule 7 teaches the model to PLACE content by those tags (patient
content → Subjective; a patient-reported home vital → Subjective NOT Objective; a
patient lay self-diagnosis → Subjective NOT Assessment). The output JSON shape is
UNCHANGED — the model emits NO role / attribution field (extract-not-infer holds);
the deterministic ``speaker_attribution`` pass re-derives attribution from the
``[S#]`` citation graph × ``Segment.speaker``.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.transcript import Transcript, normalize_role

if TYPE_CHECKING:  # annotation only — avoids a notegen↔grounding import cycle
    from alfred.scribe.grounding import GroundingResult

log = structlog.get_logger(__name__)

# --- The frozen literals (contract) -----------------------------------------
NOT_ADDRESSED = "Not addressed"
REASONING_NOT_STATED = "⚠ REASONING NOT STATED — clinician to complete"
GROUNDING_UNVERIFIED = "⚠ GROUNDING UNVERIFIED — clinician to confirm"
# #48 — the inferred-diagnosis inline flag. Distinct from GROUNDING_UNVERIFIED so
# a clinician sees WHY (a named diagnosis absent from the cited segments — the
# model likely INFERRED it), dispatched by GroundingResult.flags_for on the flag's
# reason (``inferred_diagnosis``).
INFERRED_DIAGNOSIS = "⚠ INFERRED DIAGNOSIS — not stated by clinician; confirm"

# P4-2 — the speaker-attribution inline flags (the clinical mis-attribution safety
# net). Each is DISTINCT + self-explanatory so a clinician sees WHICH attribution
# failure fired; all dispatched by GroundingResult.flags_for on the flag's reason
# (see grounding._REASON_INLINE_LITERAL + speaker_attribution.py). DERIVED from the
# [S#] citation graph × Segment.speaker — token-subset grounding proves the right
# WORDS; these prove the right SPEAKER.
#   * SPEAKER_MISMATCH (O/A/P, reason ``speaker_mismatch``) — a clinician-section
#     claim (objective/assessment/plan) cites a patient/other turn (even if a
#     clinician turn is co-cited — the co-citation-laundering close). Worded so it
#     stays TRUE in the co-citation case (a co-cited clinician does NOT make ",
#     not the clinician" false and invite dismissal of the very laundering flag).
SPEAKER_MISMATCH = (
    "⚠ SPEAKER MISMATCH — cites a patient/other turn in clinician-authored content; confirm attribution"
)
#   * SPEAKER_UNVERIFIED (all sections, reason ``speaker_unverified``) — a cited
#     turn's speaker could not be confidently identified (unknown / None / a
#     sub-purity demotion).
SPEAKER_UNVERIFIED = (
    "⚠ SPEAKER UNVERIFIED — cited turn's speaker not confidently identified; confirm attribution"
)
#   * COLLATERAL_ATTRIBUTION (Subjective, reason ``collateral_attribution``) — a
#     subjective (patient-report) claim cites a caregiver/other turn, i.e.
#     collateral history. Worded to stay TRUE when the patient is also co-cited
#     (states what the claim cites, not "not the patient").
COLLATERAL_ATTRIBUTION = (
    "⚠ COLLATERAL SOURCE — subjective content cites a caregiver/other turn; confirm source"
)
#   * ATTRIBUTION_UNVERIFIED (NOTE-LEVEL banner, reason ``attribution_unverified``)
#     — diarized but attribution could not be established for the encounter (the
#     composed fail-open close: no clinician voice anywhere ⇒ everything unknown/
#     patient ⇒ per-claim flags alone could still compose into a quiet-looking
#     note; ALSO reused by the pipeline when the pass CRASHES). Rendered as a
#     visible banner line at the top of the note body. Cause-agnostic so it stays
#     TRUE for both the no-clinician case AND the crash case (the specific cause
#     rides the flag's ``detail``, not the inline literal).
ATTRIBUTION_UNVERIFIED = (
    "⚠ ATTRIBUTION UNVERIFIED — speaker attribution could not be established for "
    "this encounter; treat all attribution as unverified"
)

# #14c — the post-note QUALITY-pass inline flags (ADVISORY completeness/style vs the note_profile —
# distinct from the medico-legal grounding/attribution flags above). Each is a NOTE-LEVEL banner
# (section "note", claim_index -1), dispatched by GroundingResult.flags_for on the ``quality_*`` reason
# (see grounding._REASON_INLINE_LITERAL + notegen_quality.py). NEVER gates note-gen; the clinician
# decides. Worded advisory ("consider" / "confirm"), never as a faithfulness assertion.
QUALITY_REQUIRED_SECTION_EMPTY = "⚠ QUALITY — a profile-required section is empty; complete or confirm"
QUALITY_VERBOSE = "⚠ QUALITY — note is over the succinctness target; consider tightening (advisory)"
QUALITY_ASSESSMENT_NO_PLAN = "⚠ QUALITY — assessment has findings but no plan; add a plan or confirm"

# The SOAP sections + their markdown headings (order is the contract).
SOAP_SECTIONS: tuple[str, ...] = ("subjective", "objective", "assessment", "plan")
_SECTION_HEADINGS = {
    "subjective": "## Subjective",
    "objective": "## Objective",
    "assessment": "## Assessment",
    "plan": "## Plan",
}

_DEFAULT_MODEL = "qwen2.5:14b-instruct-q4_K_M"
_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"

# Ollama runtime options for the sovereign note-gen (#46, live-box A/B).
#   * num_ctx=8192: the clinical SYSTEM_PROMPT (~1962 tokens) + a long-encounter
#     transcript EXCEEDS Ollama's OpenAI-compat default context of 2048 →
#     SILENT context truncation (A/B: dtc_lbp 9 flags truncated vs 4 at 8192).
#     Only the NATIVE /api/chat ``options`` block honors num_ctx.
#   * temperature=0: faithfulness-critical extract-not-infer task — remove the
#     nondeterminism the model's default temperature adds.
#   * num_predict=2048: bound the model's OUTPUT explicitly (scribe P3-b2) so
#     the reserved output window is a KNOWN quantity the context-budget guard
#     can subtract — an unbounded num_predict lets a long generation collide
#     with the prompt inside num_ctx.
_NUM_CTX = 8192
_NUM_PREDICT = 2048             # reserved generation window (single source of truth)
_NOTEGEN_OLLAMA_OPTIONS: dict = {
    "num_ctx": _NUM_CTX, "temperature": 0, "num_predict": _NUM_PREDICT,
}

# --- context-budget guard (scribe P3-b2, full-regen fail-loud cap) -----------
# The checkpoint co-pilot re-generates the note from the FULL accumulated
# transcript each checkpoint. A long encounter can grow past what fits in
# num_ctx alongside the system prompt + the reserved output window. A silent
# overflow makes Ollama TRUNCATE the prompt → earlier segments' findings vanish
# from the regen → a note that looks complete but dropped content. FAIL-LOUD
# over silent truncation, via TWO guards:
#
#   (1) PRE-FLIGHT ESTIMATE — a cheap EFFICIENCY HINT that skips the ~35s
#       generation for obviously-over cases. NOT the safety guarantee: no
#       chars/token rate is provably conservative for BOTH digit/header-dense
#       clinical text (vitals/doses ~2.0-2.2 chars/tok, headers ~1.5-2.0) AND
#       prose. So it is tuned CONSERVATIVE — a LOW chars/token AND a fixed
#       per-segment header surcharge (the ``S## [x-y s]:`` header format is
#       fixed; bound it high). Over-firing here is fail-SAFE: it skips a
#       checkpoint update (keeps the last-good draft), never accepts a bad note.
#   (2) AUTHORITATIVE POST-CALL CHECK (in generate_structured) — Ollama's OWN
#       ``prompt_eval_count`` (its real tokenizer's count of the prompt it
#       processed). If it hit the context ceiling the prompt was TRUNCATED →
#       refuse the note. This is the PROVABLY-correct safety net; the estimate
#       above is only a hint.
_CHARS_PER_TOKEN = 2.5          # low (over-estimating) rate — first-line hint only
# TWO rates because the estimate covers two DIFFERENT text kinds:
#   (a) the VARIABLE transcript is digit / timestamp / header-dense (vitals, doses,
#       ``S## [x-y s]:`` prefixes tokenize at ~2.0-2.2 chars/tok) → 2.5 stays
#       deliberately conservative there. But the FIXED English-prose SYSTEM_PROMPT
#       tokenizes at ~4 chars/tok; applying 2.5 to it OVER-counts ~1.6× — and once
#       the P4-3 rule + worked examples grew the prompt, that over-count alone
#       exceeded num_ctx−output−margin → a NEGATIVE budget → EVERY generation
#       false-refused ContextBudgetExceeded (even a 1-segment encounter).
#   (b) UNDER-estimating the FIXED prompt is the SAFE direction: the pre-flight is
#       only a cheap efficiency HINT; the AUTHORITATIVE never-accept-a-truncated-
#       note guard is the post-call ``prompt_eval_count >= _PROMPT_TRUNCATION_CEILING``
#       check (Ollama's real tokenizer), which this does NOT touch. Worst case a
#       looser pre-flight lets a case through that the post-call guard then refuses
#       (an efficiency cost, never a safety one).
#   (c) empirical anchor: the current SYSTEM_PROMPT is ~16687 chars ≈ 4172 real
#       tokens (chars/4.0), well within the window; chars/2.5 mis-estimated it 6675.
_PROSE_CHARS_PER_TOKEN = 4.0    # English-prose rate — for the FIXED SYSTEM_PROMPT only
_HEADER_TOKENS_PER_SEGMENT = 8  # fixed surcharge/seg for the dense ``S## [x-y s]:`` header
_BUDGET_SAFETY_MARGIN = 512     # slack for tokenizer variance vs the char estimate
# A prompt Ollama evaluated at/above this many tokens did NOT safely fit
# alongside the reserved output window → treat as TRUNCATED (fail-closed). Set
# to the LOWER of the two clamp points Ollama might use (num_ctx vs
# num_ctx−num_predict) so it catches EITHER truncation behavior; the exact
# behavior is confirmed/tuned against the real box via the
# ``ALFRED_SCRIBE_QWEN_IT`` integration test (Ollama unreachable in CI).
_PROMPT_TRUNCATION_CEILING = _NUM_CTX - _NUM_PREDICT


def _estimate_tokens(text: str, chars_per_token: float = _CHARS_PER_TOKEN) -> int:
    """Estimate a token count for ``text`` — ``ceil(chars / chars_per_token)``.

    Defaults to the conservative transcript rate (2.5); pass
    :data:`_PROSE_CHARS_PER_TOKEN` (4.0) for the FIXED English-prose SYSTEM_PROMPT
    so its estimate is not over-counted ~1.6× (see the rate constants above). A
    first-line efficiency HINT only; the authoritative guard is the post-call
    ``prompt_eval_count`` check (Ollama's real tokenizer)."""
    return math.ceil(len(text) / chars_per_token)

# The clinical extract-not-infer system prompt (scribe P2-c). Authored to the
# FROZEN CONTRACT above: the model emits EXACTLY the four-SOAP-section JSON object
# of ATOMIC {claim, source_spans} objects. This prompt is the PRIMARY safety
# mechanism — under it a general model (qwen2.5-14b) extracts FAITHFULLY with
# honest gaps instead of fabricating (empirically A/B'd vs a medical fine-tune
# that INVENTED patient names + ALTERED a stated age). It COOPERATES with the
# deterministic grounding-verify (``scribe.grounding``): atomic claims + verbatim
# numbers + verbatim negations + minimal real-segment cites, so clean notes are
# NOT spuriously flagged. Empty section ⇒ empty list ``[]`` (``render_soap``
# supplies the ``NOT_ADDRESSED`` literal; a ``{claim:"Not addressed"}`` object
# with no spans would be flagged ``ungrounded_assertion`` — do NOT emit one).
# Every worked example below was walked against the deterministic checks to render
# ZERO flags — Examples A/B against ``grounding.verify``, and the diarized P4-3
# Examples C/D against the full stack (``verify`` + ``check_inferred_diagnoses`` +
# ``check_speaker_attribution``). See the module docstring for the contract.
SYSTEM_PROMPT = """You are a clinical scribe. Your ONLY job is to EXTRACT what \
was actually said in a recorded clinical encounter into a structured SOAP note. \
You are NOT a diagnostician and you do NOT reason about the case for the \
clinician. The note you produce is a DRAFT the clinician will read, correct, and \
attest — a faithful extraction with honest gaps is ALWAYS better than a fluent \
invention.

You are given a transcript as numbered segments, one per line. Each segment has a
stable id — the "S#" token at the START of the line. You will cite these ids.

When the encounter has been speaker-diarized, each line ALSO carries an uppercase
[ROLE] tag, after the [start-end s] timestamp, naming who spoke:

    S1 [0.0-6.0s] [CLINICIAN]: <what the clinician said>
    S2 [6.0-12.0s] [PATIENT]: <what the patient said>

The roles are [CLINICIAN], [PATIENT], [OTHER] (someone else in the room, e.g. a
caregiver or family member), and [UNKNOWN] (the speaker could not be identified).
When the encounter is NOT diarized the lines have NO [ROLE] tag:

    S1 [0.0-6.0s]: <what was said>
    S2 [6.0-12.0s]: <what was said>

The "S#" id is ALWAYS the first token of the line. Cite the BARE id in
source_spans ("S1") — never the [ROLE] tag, never the timestamp.

=== OUTPUT ===
Return ONE JSON object and NOTHING ELSE — no markdown code fences, no commentary
before or after it. It MUST be valid JSON with EXACTLY this shape:

{"subjective":[{"claim":"<one finding>","source_spans":["S1"]}],"objective":[...],"assessment":[...],"plan":[...],"assessment_reasoning_stated":true}

- Four SOAP sections, each a LIST of claim objects {"claim","source_spans"}:
  * subjective — what the PATIENT reports (symptoms, history, current meds).
  * objective — measured/observed facts stated (vitals, exam findings, results).
  * assessment — the clinician's stated impression/diagnosis (ONLY if verbalized).
  * plan — stated next steps (orders, meds, follow-up, referrals).
- "source_spans" is a list of BARE segment ids the claim came from, e.g. ["S1"]
  or ["S2","S3"]. Write "S1" — NOT "[S1]", NOT the timestamp, NOT the [ROLE] tag.
- "assessment_reasoning_stated" is a bool (see rule 6). Default false.

=== THE RULES (extract, never infer) ===

1. ATOMIC CLAIMS — each "claim" states EXACTLY ONE clinical finding. NEVER put a
   positive finding and a negative finding in the same claim, and never bundle
   two symptoms together. One finding -> one claim object. This is load-bearing:
   the downstream safety check can only catch a flipped positive/negative when
   every claim is atomic; a bundled claim can hide a flipped finding.

2. NUMBERS VERBATIM — copy every number, dose, and measurement EXACTLY as it
   appears in the cited segment, character for character, INCLUDING the decimal
   point and unit. Never round, never convert units, never reformat. "0.5mg" is
   NOT "5mg" is NOT "500mg". If the segment spells it out ("point five
   milligrams"), write it the way the segment rendered it — do not tidy it up.

3. NEGATIONS VERBATIM — copy the EXACT negation word the speaker used ("denies",
   "no", "without", "negative", "none"). Do NOT swap one for another: if the
   segment says "no fever", the claim says "no fever" — NOT "denies fever".
   Preserving the exact negation is what lets the safety check catch a flipped
   pertinent-negative.

4. CITE REAL SEGMENTS, MINIMALLY — every claim's "source_spans" must list the
   id(s) of the segment(s) that ACTUALLY contain that claim's content. Cite the
   FEWEST segments that support the claim — usually exactly one. Never cite a
   segment that does not support the claim, and never invent a segment id that is
   not in the transcript. If you cannot ground a claim in a real segment, DO NOT
   emit that claim at all.

5. NEVER INVENT; EMPTY SECTION -> [] — extract ONLY what the transcript contains.
   Do NOT invent patient names, identifiers, ages, vitals, exam findings, past
   history, or diagnoses — not even plausible ones. Do NOT carry a modifier or
   quantity — a dose, duration, supply length, frequency, or laterality
   (left/right) — from one item to another: attach a detail to an item ONLY if the
   clinician stated that detail FOR THAT item. If a detail was stated for some
   items and an adjacent item was mentioned WITHOUT it, extract that item without
   the detail — never carry it over. If a whole SOAP section has nothing stated in
   the transcript, emit an EMPTY LIST [] for that section — the note renders it as
   "Not addressed" for you. Do NOT put a "Not addressed" claim object in the list,
   and do NOT fill an empty section with anything.

6. IMPRESSION vs REASONING — put a clinical impression/diagnosis in "assessment"
   ONLY if the clinician actually STATED one (extract it as an atomic claim like
   any other; never invent one). NEVER name a diagnosis the clinician did not
   EXPLICITLY say, even when the findings are a textbook fit for one — low mood +
   a high PHQ-9 + an SSRI plan does NOT let you write "major depressive disorder"
   if the clinician never named it; a strongly-implied label is still an inference
   and is forbidden. SEPARATELY, set "assessment_reasoning_stated" to
   true ONLY if the clinician VERBALIZED the clinical REASONING — the WHY, the
   "because" — behind the assessment. A stated conclusion is NOT reasoning: a bare
   impression with no stated why ("This is a viral URI", with no "because ...")
   keeps its claim in "assessment" but sets "assessment_reasoning_stated" FALSE
   (so the note's completeness nudge fires). If the clinician deliberately
   declined to diagnose ("I'm not going to commit to a diagnosis yet") or stated
   no impression at all, leave "assessment" empty ([]) and set the flag false.
   Default false. NEVER fabricate a diagnosis or the reasoning behind one.

7. PLACE CONTENT BY SPEAKER (only when [ROLE] tags are present) — use each cited
   segment's [ROLE] tag to decide WHICH section its content belongs in. This rule
   governs PLACEMENT only; it adds NOTHING to your output.
   * [PATIENT] content — what the patient says about themselves — is the patient's
     report: put it in "subjective", phrased with the patient's provenance
     ("Patient reports ...", "Reports ...").
   * [CLINICIAN] content — the clinician's own observations, exam findings, vitals
     THEY measured, stated impression, and plan — goes in "objective" /
     "assessment" / "plan" as usual.
   * A vital or measurement the PATIENT reports (a home reading — "my blood
     pressure was 150 over 90 this morning") is a PATIENT REPORT: put it in
     "subjective" ("Patient reports a home blood pressure of 150 over 90"), NOT
     "objective". Only a vital the clinician states they measured in the encounter
     is an Objective finding. (A patient-reported reading placed in objective is
     both a mis-attribution and a false in-clinic vital.)
   * A lay self-diagnosis the PATIENT offers ("I think it's my sciatica again") is
     the patient's reported BELIEF: put it in "subjective" as a report ("Patient
     believes the pain is recurrent sciatica"), NOT "assessment". Assessment holds
     ONLY the clinician's own stated impression — rule 6 still governs it (never
     invent or infer a diagnosis).
   CRITICAL: you must NOT emit any speaker, role, "attribution", or [ROLE] tag in
   your JSON — no new fields, and no role text inside a claim beyond the ordinary
   "Patient reports ..." subjective wording already used above. The [ROLE] tags are
   INPUTS that guide placement only; a separate deterministic check re-derives
   attribution from your citations, so your job is just to cite the right segment
   and place it in the right section. If there are NO [ROLE] tags, place content by
   its section meaning as usual (rules 1-6).

8. BE CONCISE — WORDING, NOT CONTENT. Once a claim is atomic (rule 1) and cited,
   say it in as few words as the finding needs. A verbose claim is still a defect
   even when everything in it is true, correctly cited, and correctly atomic.
   * ONE CLAUSE per claim. State the finding directly; do not wrap it in an extra
     descriptive clause. "Patient reports a cough that has been present and
     worsening for approximately the past three days" -> "Reports a cough,
     worsening over approximately three days" (same finding, same citation,
     same stated modifier, fewer words).
   * DO NOT restate the section name or category inside the claim text — the
     clinician already sees the "## Subjective" / "## Objective" heading. Write
     "Reports a cough", not "Subjective: patient reports a cough" or "Reported
     symptom: cough".
   * DO NOT add hedging or narration wrapper words that nobody in the encounter
     said ("it was noted that", "it appears that", "the patient was found to
     have", "the assessment suggests"). If the SPEAKER hedged ("I think it might
     be my sciatica"), extract-not-infer still applies: keep THEIR hedge — this
     rule bans wrapper phrasing you add, never a hedge someone actually said.
   * DO NOT add filler connectives ("in terms of", "with regard to", "it should
     be noted that").
   Concision NEVER outranks rules 1-7: never merge two findings to save words,
   never drop a number/unit/negation/modifier, and never shorten or drop a
   source_spans citation to make a claim look tidier. A faithful, fully-cited
   claim that reads a little long beats a short claim that lost content.

DO NOT (each WRONG below is a real failure the safety check may or may not catch —
so YOU must prevent it):
- DO NOT bundle findings. Given S1 "Patient reports a cough." / S2 "Denies fever."
    WRONG: {"claim":"Reports a cough, denies fever","source_spans":["S1","S2"]}
    RIGHT: {"claim":"Reports a cough","source_spans":["S1"]}
           {"claim":"Denies fever","source_spans":["S2"]}
- DO NOT reformat a dose. Given S1 "Amoxicillin 500mg twice daily."
    WRONG: {"claim":"Amoxicillin 5mg","source_spans":["S1"]}
    RIGHT: {"claim":"Amoxicillin 500mg twice daily","source_spans":["S1"]}
- DO NOT reword a negation. Given S1 "No fever."
    WRONG: {"claim":"Denies fever","source_spans":["S1"]}
    RIGHT: {"claim":"No fever","source_spans":["S1"]}
- DO NOT invent a diagnosis, vital, name, or age the transcript never stated.
- DO NOT carry a shared modifier to an item it was not stated for. Given
    S1 "Refilled levothyroxine 0.5mg and metformin 500mg, each for a 90-day supply."
    S2 "Atorvastatin was also renewed."
    WRONG: {"claim":"Renewed atorvastatin for a 90-day supply","source_spans":["S1","S2"]}
    RIGHT: {"claim":"Renewed atorvastatin","source_spans":["S2"]}
    (the "90-day supply" was stated only for levothyroxine + metformin; because
     "90" appears elsewhere in the cite the grounding check would pass the WRONG
     claim CLEAN — only THIS rule stops it.)
- DO NOT infer an unstated diagnosis, even a textbook-obvious one. Given
    S1 "Low mood and poor sleep for a month; PHQ-9 is 12 today."
    S2 "Start sertraline 50mg and follow up in four weeks." (clinician names NO diagnosis)
    WRONG: {"claim":"Major depressive disorder","source_spans":["S1"]}
    RIGHT: assessment stays [] — the clinician named no diagnosis (the findings +
     the sertraline plan still go in subjective + plan as usual). This WRONG claim
     has no number/negation to check, so the grounding pass would let it through
     CLEAN — only THIS rule stops it.

=== WORKED EXAMPLE A (a complete note) ===
Transcript:
    S1 [0.0-6.0s]: Patient reports a cough for the past three days.
    S2 [6.0-12.0s]: Denies fever and denies shortness of breath.
    S3 [12.0-18.0s]: Currently taking amoxicillin 500mg twice daily.
    S4 [18.0-24.0s]: Temperature 37.2 degrees and blood pressure 128 over 76 on exam.
    S5 [24.0-31.0s]: Given the clear chest, this looks like a viral upper respiratory infection.
    S6 [31.0-37.0s]: Plan is to continue the amoxicillin and review in one week if symptoms persist.
Output:
{"subjective":[{"claim":"Reports a cough for the past three days","source_spans":["S1"]},{"claim":"Denies fever","source_spans":["S2"]},{"claim":"Denies shortness of breath","source_spans":["S2"]},{"claim":"Currently taking amoxicillin 500mg twice daily","source_spans":["S3"]}],"objective":[{"claim":"Temperature 37.2 degrees","source_spans":["S4"]},{"claim":"Blood pressure 128 over 76","source_spans":["S4"]}],"assessment":[{"claim":"Viral upper respiratory infection","source_spans":["S5"]}],"plan":[{"claim":"Continue amoxicillin","source_spans":["S6"]},{"claim":"Review in one week if symptoms persist","source_spans":["S6"]}],"assessment_reasoning_stated":true}

=== WORKED EXAMPLE B (clinician declines to diagnose — do NOT invent one) ===
Transcript:
    S1 [0.0-7.0s]: Patient reports feeling tired for the last two weeks.
    S2 [7.0-13.0s]: Denies fever, weight loss, or night sweats.
    S3 [13.0-20.0s]: I'm not going to commit to a diagnosis yet; we need bloodwork first.
    S4 [20.0-27.0s]: Order a CBC and thyroid panel, and follow up when results are back.
Output:
{"subjective":[{"claim":"Reports feeling tired for the last two weeks","source_spans":["S1"]},{"claim":"Denies fever","source_spans":["S2"]},{"claim":"Denies weight loss","source_spans":["S2"]},{"claim":"Denies night sweats","source_spans":["S2"]}],"objective":[],"assessment":[],"plan":[{"claim":"Order a CBC","source_spans":["S4"]},{"claim":"Order a thyroid panel","source_spans":["S4"]},{"claim":"Follow up when results are back","source_spans":["S4"]}],"assessment_reasoning_stated":false}
In Example B no objective findings and no diagnosis were stated, so those sections
are []; the clinician deliberately declined a diagnosis, so assessment stays empty
and "assessment_reasoning_stated" is false. Inventing a diagnosis here would be a
patient-safety failure.

=== WORKED EXAMPLE C (speaker-diarized — a patient-reported home vital) ===
Transcript:
    S1 [0.0-7.0s] [PATIENT]: I checked my blood pressure at home this morning and it was 150 over 90.
    S2 [7.0-13.0s] [CLINICIAN]: Here in clinic your blood pressure is 128 over 82.
    S3 [13.0-19.0s] [CLINICIAN]: Continue the lisinopril and recheck in two weeks.
Output:
{"subjective":[{"claim":"Patient reports a home blood pressure of 150 over 90","source_spans":["S1"]}],"objective":[{"claim":"Blood pressure 128 over 82","source_spans":["S2"]}],"assessment":[],"plan":[{"claim":"Continue lisinopril","source_spans":["S3"]},{"claim":"Recheck in two weeks","source_spans":["S3"]}],"assessment_reasoning_stated":false}
The home reading (S1, [PATIENT]) goes in subjective as the patient's report — only
the clinician-measured 128 over 82 (S2, [CLINICIAN]) is an Objective vital. If you
placed the S1 home reading in objective, an automated check would add the warning
"⚠ SPEAKER MISMATCH — cites a patient/other turn in clinician-authored content;
confirm attribution" to the note AFTER you finish — you never write that warning
yourself; you avoid it by putting patient-reported content in subjective.

=== WORKED EXAMPLE D (speaker-diarized — clinician-relayed history + a patient self-diagnosis) ===
Transcript:
    S1 [0.0-7.0s] [CLINICIAN]: So you've had lower back pain radiating down the left leg for about a week.
    S2 [7.0-13.0s] [PATIENT]: Yes, and honestly I think my sciatica is flaring up again.
    S3 [13.0-19.0s] [CLINICIAN]: On exam, straight leg raise is positive on the left.
    S4 [19.0-26.0s] [CLINICIAN]: Start naproxen 500mg twice daily and refer to physiotherapy.
Output:
{"subjective":[{"claim":"Lower back pain radiating down the left leg for about a week","source_spans":["S1"]},{"claim":"Patient believes the pain is recurrent sciatica","source_spans":["S2"]}],"objective":[{"claim":"Straight leg raise positive on the left","source_spans":["S3"]}],"assessment":[],"plan":[{"claim":"Start naproxen 500mg twice daily","source_spans":["S4"]},{"claim":"Refer to physiotherapy","source_spans":["S4"]}],"assessment_reasoning_stated":false}
Two placement points. (1) The history in S1 is spoken by the [CLINICIAN] relaying
what the patient told them — it is still the patient's history, so it goes in
subjective cited to S1; a clinician-relayed history in subjective is legitimate and
draws NO warning. (2) The patient's "I think my sciatica is flaring up" (S2,
[PATIENT]) is the patient's own BELIEF — it goes in subjective as a reported belief,
NOT in assessment; the clinician named no diagnosis, so assessment is []. Placing
that self-diagnosis in assessment cited to the [PATIENT] turn S2 would draw the same
"⚠ SPEAKER MISMATCH" warning (assessment is clinician-authored content).

If the transcript has no segments, or a section has nothing to extract, use [].
Return ONLY the JSON object."""


# Diagnostic tail length — the model output is derived from the PHI transcript.
# Kept SHORT (a bad-JSON diagnosis rarely needs more than the trailing fragment).
_DIAG_TAIL_CHARS = 120


def _diag_tail(text: str) -> str:
    """A short diagnostic tail of the model output for a parse-failure message.

    ⚠️ PHI: this fragment is DERIVED FROM THE PHI TRANSCRIPT. It is SAFE ONLY
    because the sovereign scribe slot is LOCAL-ONLY (the P1-a barrier-(d)
    allowlist forbids ``transport`` — no egress path exists). A future transport
    add MUST NOT route ``NoteGenError`` (or any note-gen output) to an
    egressible sink — keep note-gen diagnostics on the local box.
    """
    return text[-_DIAG_TAIL_CHARS:]


class NoteGenError(Exception):
    """Note-gen failed — unparseable model output. Fail-loud, never fabricate.

    The message embeds a SHORT local-only diagnostic tail (see ``_diag_tail`` —
    PHI-derived, safe only on the transport-less sovereign box)."""


class ContextBudgetExceeded(NoteGenError):
    """The rendered prompt would not fit in num_ctx alongside the system prompt +
    the reserved output window (scribe P3-b2). Raised BEFORE the Ollama call so a
    silently-truncated regen never reaches the draft — the LAST GOOD draft stays
    intact. The checkpoint caller treats this as a CAP (skip this checkpoint's
    update, keep folding later chunks), not a failure."""


@dataclass
class Claim:
    """One SOAP claim + its segment citations. Per the frozen contract each
    claim states EXACTLY ONE clinical finding (atomic) — the deterministic
    negation guard is only sound under atomic claims. Grounding flags live in
    the :class:`~alfred.scribe.grounding.GroundingResult`, NOT on the claim."""

    claim: str
    source_spans: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        claim = str(data.get("claim", "")).strip()
        spans_raw = data.get("source_spans")
        spans = [str(s).strip() for s in spans_raw] if isinstance(spans_raw, list) else []
        return cls(claim=claim, source_spans=spans)


@dataclass
class StructuredNote:
    """The parsed structured note (the frozen JSON shape)."""

    subjective: list[Claim] = field(default_factory=list)
    objective: list[Claim] = field(default_factory=list)
    assessment: list[Claim] = field(default_factory=list)
    plan: list[Claim] = field(default_factory=list)
    assessment_reasoning_stated: bool = False

    def section(self, name: str) -> list[Claim]:
        return getattr(self, name)

    def all_claims(self):
        """Yield ``(section, index, claim)`` across all SOAP sections."""
        for sec in SOAP_SECTIONS:
            for i, c in enumerate(self.section(sec)):
                yield sec, i, c

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StructuredNote":
        def _claims(key: str) -> list[Claim]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [Claim.from_dict(x) for x in raw if isinstance(x, dict)]

        return cls(
            subjective=_claims("subjective"),
            objective=_claims("objective"),
            assessment=_claims("assessment"),
            plan=_claims("plan"),
            assessment_reasoning_stated=bool(data.get("assessment_reasoning_stated", False)),
        )


# --- prompt build -----------------------------------------------------------

def build_prompt(transcript: Transcript) -> str:
    """Format the segment-rich transcript for the model — numbered by stable id
    so the model can cite ``[S#]`` in ``source_spans``.

    P4-3: when (and ONLY when) ``transcript.diarized`` is True, each segment line
    also carries an uppercase ``[ROLE]`` tag derived via ``normalize_role`` (a
    ``None`` / ``''`` / raw-cluster speaker → ``[UNKNOWN]``, fail-closed). The tag
    sits AFTER the ``[start-end s]`` timestamp so the stable ``S#`` id stays the
    FIRST token of every line and the ``S## [x-y s]`` header prefix is preserved;
    ``source_spans`` still cite the BARE ``S#`` (never the role). An UN-DIARIZED
    transcript's block is BYTE-IDENTICAL to pre-P4-3 — the tag is OMITTED entirely
    (not rendered as ``[UNKNOWN]``). SYSTEM_PROMPT rule 7 teaches the model to
    PLACE content by the visible tags while emitting NO role in its output (the
    deterministic ``speaker_attribution`` pass re-derives attribution from the
    citation graph)."""
    lines = ["Transcript segments (cite these ids in source_spans):", ""]
    diarized = transcript.diarized
    for seg in transcript.segments:
        if diarized:
            role = normalize_role(seg.speaker).upper()
            lines.append(
                f"{seg.id} [{seg.start_s:.1f}-{seg.end_s:.1f}s] [{role}]: {seg.text}"
            )
        else:
            lines.append(f"{seg.id} [{seg.start_s:.1f}-{seg.end_s:.1f}s]: {seg.text}")
    if not transcript.segments:
        lines.append("(no segments)")
    return "\n".join(lines)


# --- JSON extraction + parse ------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json_object(text: str) -> str:
    """Pull the JSON object out of a model response — strips ```json fences,
    then takes the outermost ``{...}``. Robust to qwen preamble/postamble."""
    m = _FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise NoteGenError(
            f"note-gen output has no JSON object; tail={_diag_tail(text)!r}"
        )
    return candidate[start : end + 1]


def parse_structured_json(text: str) -> StructuredNote:
    """Parse the model's text into a :class:`StructuredNote`. Fail-loud on
    unparseable output (NEVER fabricate a note from a bad response)."""
    raw = _extract_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise NoteGenError(
            f"note-gen returned unparseable JSON: {e}; tail={_diag_tail(text)!r}"
        ) from e
    if not isinstance(data, dict):
        raise NoteGenError(f"note-gen JSON is not an object: {type(data).__name__}")
    return StructuredNote.from_dict(data)


# --- render (faithful; flags inline) ----------------------------------------

def render_soap(
    structured: StructuredNote, *, title: str, grounding: "GroundingResult",
) -> str:
    """Render the structured note to a SOAP markdown body. Emits ``[S#]`` cites,
    the ``Not addressed`` / ``REASONING NOT STATED`` literals, each claim's inline
    flag literals, and (P4-2) the NOTE-LEVEL attribution banner at the top.

    ``grounding`` is REQUIRED — a note can NEVER be rendered without a
    :class:`~alfred.scribe.grounding.GroundingResult`, closing the "render
    without verify ⇒ clean-looking flagged draft" hole. The flags are read from
    the grounding result (``flags_for``), NOT from a mutated claim field — a
    single source of truth. (The AIRTIGHT verify-BEFORE-render — a combined
    generate→verify→render — is enforced structurally in the P2-d pipeline; this
    required param is the cheap structural nudge at this layer.)

    ``flags_for`` returns ALL of a claim's distinct inline literals (P4-2 rename
    of the single-literal ``flag_for``), so a claim carrying both a grounding
    flag and a speaker flag renders BOTH ⚠ inline, joined by a space. The
    NOTE-LEVEL banner (section ``"note"``, claim_index ``-1``) renders as a
    visible line at the top of the body — its section-less identity is a P4-2
    convention (see ``speaker_attribution``).

    Renders FAITHFULLY — never adds, drops, or edits a claim.
    """
    out: list[str] = [f"# {title}", ""]
    # P4-2 NOTE-LEVEL banner(s) — the section-less ("note", -1) flags (e.g. the
    # attribution_unverified banner). Rendered ABOVE the sections so a whole-note
    # caveat is the first thing the clinician reads. Empty when the P4-2 pass
    # contributed no note-level flag (e.g. un-diarized) — the body then differs
    # from pre-P4-2 ONLY by the deliberate flags_for multi-literal render change.
    for banner in grounding.flags_for("note", -1):
        out.append(banner)
        out.append("")
    for sec in SOAP_SECTIONS:
        out.append(_SECTION_HEADINGS[sec])
        claims = structured.section(sec)
        if not claims:
            out.append(NOT_ADDRESSED)
        else:
            for i, c in enumerate(claims):
                cites = f" [{', '.join(c.source_spans)}]" if c.source_spans else ""
                flag_lits = grounding.flags_for(sec, i)
                flag = f" {' '.join(flag_lits)}" if flag_lits else ""
                out.append(f"- {c.claim}{cites}{flag}")
            if sec == "assessment" and not structured.assessment_reasoning_stated:
                out.append(REASONING_NOT_STATED)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --- the sovereign local model call -----------------------------------------

async def generate_structured(
    transcript: Transcript, *, config: ScribeConfig,
) -> StructuredNote:
    """Prompt the SOVEREIGN LOCAL model (qwen via Ollama) and parse the result.

    Routes through ``distiller.backends.ollama.call_ollama_no_tools`` — httpx,
    ALREADY covered by the armed SovereignHttpGuard, loopback endpoint, NO
    tool_use. This module constructs NO http client of its own; the endpoint is
    ``config.llm.base_url`` (barrier-b-validated loopback at config load).
    """
    from alfred.distiller.backends.ollama import call_ollama_no_tools

    prompt = build_prompt(transcript)
    endpoint = (config.llm.base_url or "").strip() or _DEFAULT_ENDPOINT
    model = (config.llm.model or "").strip() or _DEFAULT_MODEL

    # (1) PRE-FLIGHT ESTIMATE (efficiency HINT) — skip the ~35s generation for
    # obviously-over cases. Conservative: LOW chars/token + a fixed per-segment
    # header surcharge (headers are dense). The system prompt is measured
    # DYNAMICALLY (never a hardcoded count that drifts when the prompt is edited).
    # Over-firing here is fail-SAFE (skips this checkpoint, keeps the last-good
    # draft). The AUTHORITATIVE guard is the post-call ``prompt_eval_count`` check.
    num_ctx = int(_NOTEGEN_OLLAMA_OPTIONS.get("num_ctx", _NUM_CTX))
    # The FIXED prose SYSTEM_PROMPT uses the prose rate (chars/4.0); the VARIABLE
    # transcript keeps the conservative digit/header rate (chars/2.5, the default).
    sys_tokens = _estimate_tokens(SYSTEM_PROMPT, _PROSE_CHARS_PER_TOKEN)
    prompt_tokens = (
        _estimate_tokens(prompt)
        + _HEADER_TOKENS_PER_SEGMENT * len(transcript.segments)
    )
    budget = num_ctx - sys_tokens - _NUM_PREDICT - _BUDGET_SAFETY_MARGIN
    if prompt_tokens > budget:
        log.warning(
            "scribe.notegen.context_budget_exceeded",
            source_id=transcript.source_id,          # opaque encounter id (NOTE-4)
            est_tokens=prompt_tokens,
            budget=budget,
            num_ctx=num_ctx,
            segment_count=len(transcript.segments),
        )
        raise ContextBudgetExceeded(
            f"note-gen prompt (~{prompt_tokens} est tok, "
            f"{len(transcript.segments)} segments) exceeds the context budget "
            f"({budget} tok = num_ctx {num_ctx} − system {sys_tokens} − output "
            f"{_NUM_PREDICT} − margin {_BUDGET_SAFETY_MARGIN}) on the pre-flight "
            f"estimate. Refusing to regen — a truncated regen would silently drop "
            f"earlier segments' findings; the last-good draft stays intact."
        )

    text, meta = await call_ollama_no_tools(
        prompt,
        system=SYSTEM_PROMPT,
        model=model,
        endpoint=endpoint,
        # Route via native /api/chat so num_ctx=8192 + temperature=0 are honored
        # (the OpenAI-compat path silently truncates at num_ctx=2048).
        options=_NOTEGEN_OLLAMA_OPTIONS,
    )

    # (2) AUTHORITATIVE POST-CALL TRUNCATION CHECK — Ollama's own
    # ``prompt_eval_count`` is the EXACT number of prompt tokens its real
    # tokenizer processed. If it reached the context ceiling the prompt was
    # TRUNCATED (Ollama drops the oldest tokens → earlier segments' findings
    # vanish), so the note was generated from an INCOMPLETE transcript → REFUSE
    # it (never accept a note from a truncated prompt). Fires the same fail-loud
    # path as the pre-flight (checkpoint → budget_capped, last-good draft kept).
    # Provably conservative: it uses the model's real count, not an estimate.
    prompt_eval = meta.get("prompt_eval_count") if isinstance(meta, dict) else None
    if not isinstance(prompt_eval, int):
        # FAIL-LOUD on a MISSING authoritative count (P3-b3). The native
        # /api/chat path always returns prompt_eval_count; its absence means the
        # authoritative truncation signal is unavailable, so the note CANNOT be
        # verified as generated from a complete prompt. A medico-legal guard must
        # refuse rather than silently degrade to the (not-provably-conservative)
        # pre-flight estimate. Uses the same fail-loud path → checkpoint
        # budget_capped, last-good draft intact.
        log.warning(
            "scribe.notegen.missing_prompt_eval_count",
            source_id=transcript.source_id,          # opaque encounter id (NOTE-4)
            segment_count=len(transcript.segments),
        )
        raise ContextBudgetExceeded(
            "Ollama's /api/chat response lacked prompt_eval_count — the "
            "AUTHORITATIVE truncation count is unavailable, so the note cannot be "
            "verified as generated from a complete (untruncated) prompt. Refusing "
            "FAIL-LOUD rather than accept an unverifiable note (a medico-legal "
            "guard must not silently degrade to the pre-flight estimate)."
        )
    if prompt_eval >= _PROMPT_TRUNCATION_CEILING:
        log.warning(
            "scribe.notegen.prompt_truncated",
            source_id=transcript.source_id,          # opaque encounter id (NOTE-4)
            prompt_eval_count=prompt_eval,
            ceiling=_PROMPT_TRUNCATION_CEILING,
            num_ctx=num_ctx,
            num_predict=_NUM_PREDICT,
            segment_count=len(transcript.segments),
        )
        raise ContextBudgetExceeded(
            f"Ollama evaluated {prompt_eval} prompt tokens (>= the truncation "
            f"ceiling {_PROMPT_TRUNCATION_CEILING} = num_ctx {num_ctx} − "
            f"num_predict {_NUM_PREDICT}) — the prompt was TRUNCATED (earlier "
            f"segments dropped). Refusing the note; the last-good draft stays "
            f"intact. AUTHORITATIVE guard (Ollama's real tokenizer, not an "
            f"estimate)."
        )

    structured = parse_structured_json(text)
    log.info(
        "scribe.notegen.structured",
        source_id=transcript.source_id,
        mode=transcript.mode,
        endpoint=endpoint,
        model=model,
        claims=sum(len(structured.section(s)) for s in SOAP_SECTIONS),
    )
    return structured
