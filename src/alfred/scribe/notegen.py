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
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

# --- The frozen literals (contract) -----------------------------------------
NOT_ADDRESSED = "Not addressed"
REASONING_NOT_STATED = "⚠ REASONING NOT STATED — clinician to complete"
GROUNDING_UNVERIFIED = "⚠ GROUNDING UNVERIFIED — clinician to confirm"

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

# PLACEHOLDER PROMPT — prompt-tuner authors the real clinical extract-not-infer
# prompt to the frozen contract above. This is minimal: enough to make the code
# + the real-qwen integration test runnable. Do NOT treat this as the clinical
# prompt.
SYSTEM_PROMPT_PLACEHOLDER = (
    "PLACEHOLDER PROMPT — prompt-tuner authors the clinical prompt.\n"
    "You are a clinical scribe. From the numbered transcript segments, EXTRACT "
    "(do NOT infer) what was stated into a SOAP note. Return ONE JSON object:\n"
    '{"subjective":[{"claim":"...","source_spans":["S1"]}],'
    '"objective":[...],"assessment":[...],"plan":[...],'
    '"assessment_reasoning_stated":false}\n'
    "Every claim MUST cite the segment id(s) it came from in source_spans "
    "(e.g. [\"S1\",\"S3\"]). If a section was not discussed, return an empty "
    "list for it — NEVER invent content. Copy numbers, doses, and negations "
    "VERBATIM from the segments. Set assessment_reasoning_stated true ONLY if "
    "the clinician verbalized their reasoning; otherwise false. Output ONLY the "
    "JSON object."
)


class NoteGenError(Exception):
    """Note-gen failed — unparseable model output. Fail-loud, never fabricate."""


@dataclass
class Claim:
    """One SOAP claim + its segment citations. ``grounding_flag`` is set by the
    deterministic grounding pass (``scribe.grounding``), never by the model."""

    claim: str
    source_spans: list[str] = field(default_factory=list)
    grounding_flag: str | None = None

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
    so the model can cite ``[S#]`` in ``source_spans``."""
    lines = ["Transcript segments (cite these ids in source_spans):", ""]
    for seg in transcript.segments:
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
            f"note-gen output has no JSON object; tail={text[-200:]!r}"
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
            f"note-gen returned unparseable JSON: {e}; tail={text[-200:]!r}"
        ) from e
    if not isinstance(data, dict):
        raise NoteGenError(f"note-gen JSON is not an object: {type(data).__name__}")
    return StructuredNote.from_dict(data)


# --- render (faithful; flags inline) ----------------------------------------

def render_soap(structured: StructuredNote, *, title: str) -> str:
    """Render the structured note to a SOAP markdown body. Emits ``[S#]`` cites,
    the ``Not addressed`` / ``REASONING NOT STATED`` literals, and any per-claim
    ``grounding_flag`` inline (set by ``scribe.grounding.verify`` beforehand).

    Renders FAITHFULLY — never adds, drops, or edits a claim.
    """
    out: list[str] = [f"# {title}", ""]
    for sec in SOAP_SECTIONS:
        out.append(_SECTION_HEADINGS[sec])
        claims = structured.section(sec)
        if not claims:
            out.append(NOT_ADDRESSED)
        else:
            for c in claims:
                cites = f" [{', '.join(c.source_spans)}]" if c.source_spans else ""
                flag = f" {c.grounding_flag}" if c.grounding_flag else ""
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

    text, _meta = await call_ollama_no_tools(
        prompt,
        system=SYSTEM_PROMPT_PLACEHOLDER,
        model=model,
        endpoint=endpoint,
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
