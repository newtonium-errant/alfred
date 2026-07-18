"""The eval runner + the pluggable note-gen seam (task #16).

The seam is the FAKE/REAL split (mirrors the diarize ``fake``/``pyannote`` seam):

  * :class:`FixtureNoteGenSeam` — loads a committed StructuredNote JSON per case
    (``eval/fixtures/<case_id>.json``). LLM-free ⇒ CI runs the whole suite with
    NO torch, NO Ollama, NO network. This is the repeatable-regression spine.
  * :class:`RealNoteGenSeam` — calls the sovereign local model
    (``generate_structured`` → box Ollama qwen2.5-14b). The on-box run that
    produces the LIVE quality numbers.

BOTH seams then run the EXACT production composition —
:func:`alfred.scribe.pipeline.render_verified_note` (grounding-verify + #48
inferred-dx + P4-2 speaker-attribution + render) — so the scorecard measures the
same pipeline that ships (no eval-vs-prod drift).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from alfred.scribe.config import ScribeConfig, load_from_unified
from alfred.scribe.eval.corpus import EvalCase, all_cases, build_transcript
from alfred.scribe.eval.scorecard import Scorecard, aggregate
from alfred.scribe.eval.scoring import score_case
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.pipeline import render_verified_note
from alfred.scribe.transcript import Transcript

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FixtureMissing(Exception):
    """A case has no committed note fixture. FAIL-LOUD — a missing fixture must
    NOT silently score an empty (trivially-clean) note."""


def _default_config() -> ScribeConfig:
    """A minimal ScribeConfig sufficient for the deterministic composition
    (``render_verified_note`` only reads ``config.diarize.purity_threshold``)."""
    return load_from_unified({"scribe": {"mode": "synthetic", "stt": {"provider": "fake"}}})


@dataclass
class FixtureNoteGenSeam:
    """LLM-free seam: loads the committed StructuredNote fixtures."""

    mode: str = "fixture"
    model: str = "fixture (committed reference notes)"
    fixtures_dir: Path = FIXTURES_DIR

    async def note_for(self, case: EvalCase, transcript: Transcript) -> StructuredNote:
        path = self.fixtures_dir / f"{case.case_id}.json"
        if not path.is_file():
            raise FixtureMissing(
                f"no note fixture for case {case.case_id!r} at {path} — author it "
                f"(or capture it from the box) before scoring; refusing to score an "
                f"empty note."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return StructuredNote.from_dict(data)


@dataclass
class RealNoteGenSeam:
    """On-box seam: the live sovereign local model (Ollama qwen2.5-14b)."""

    config: ScribeConfig
    mode: str = "real"

    @property
    def model(self) -> str:
        # Mirror generate_structured's own fallback (the canonical note-gen default)
        # so the scorecard's model label never drifts from what actually runs.
        from alfred.scribe.notegen import _DEFAULT_MODEL
        return (self.config.llm.model or "").strip() or _DEFAULT_MODEL

    async def note_for(self, case: EvalCase, transcript: Transcript) -> StructuredNote:
        # Imported lazily so the fixture path never imports the Ollama backend.
        from alfred.scribe.notegen import generate_structured
        return await generate_structured(transcript, config=self.config)


async def run_suite(seam, *, config: ScribeConfig | None = None) -> Scorecard:
    """Run every corpus case through the seam + the production composition, score
    each, and aggregate into a :class:`Scorecard`.

    ``config`` drives the deterministic composition (purity threshold etc.); the
    real seam carries its OWN config (the loopback LLM endpoint). Defaults to a
    minimal synthetic config."""
    cfg = config or _default_config()
    scores = []
    for case in all_cases():
        transcript = build_transcript(case)
        structured = await seam.note_for(case, transcript)
        # Render with a NEUTRAL fixed title — the descriptive ``case.title`` AND the
        # ``case_id`` (e.g. ``fab_noplan_therapy``) name the bait, which would
        # otherwise leak into the scored body. The scorer also strips the H1 title
        # line defensively; a bait-free title keeps rendered notes clean too.
        note = render_verified_note(
            structured, transcript, config=cfg, title="Clinical Note (STAY-C eval)",
        )
        scores.append(score_case(case, note))
    return aggregate(scores, mode=seam.mode, model=seam.model)


def fixture_path(case_id: str) -> Path:
    return FIXTURES_DIR / f"{case_id}.json"
