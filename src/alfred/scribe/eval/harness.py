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

import atexit
import json
import shutil
import tempfile
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


def _eval_scratch_dir() -> str:
    """A private throwaway data dir for the fixture-eval config.

    ``mkdtemp`` (0700, unguessable name) rather than a fixed ``/tmp`` path: this
    box is PHI-adjacent, and a predictable shared spool location is a smell even
    when — as here — only synthetic fixture data reaches it. Created once per
    process, removed at exit.
    """
    global _EVAL_SCRATCH_DIR
    if _EVAL_SCRATCH_DIR is None:
        _EVAL_SCRATCH_DIR = tempfile.mkdtemp(prefix="alfred-scribe-eval-")
        atexit.register(shutil.rmtree, _EVAL_SCRATCH_DIR, True)
    return _EVAL_SCRATCH_DIR


_EVAL_SCRATCH_DIR: str | None = None


def _default_config() -> ScribeConfig:
    """A minimal ScribeConfig for the deterministic composition.

    ``render_verified_note`` reads ``config.diarize.purity_threshold`` — and,
    since #26, ALSO ``resolve_candidates_dir(config)``: it spools negation
    paraphrase CANDIDATES at render time. So this config is not inert, and where
    it points matters.

    It points at a throwaway dir, which is a correctness requirement and not
    just suite hygiene. The candidate spool is the Tier-1 input to a
    self-correcting loop — morning-review approves rows out of it into a learned
    suppression glossary. Scoring SYNTHETIC eval fixtures through a config that
    resolves to an operator's real spool would seed that glossary with
    eval-fixture vocabulary. ``alfred scribe eval`` (without ``--real``) reaches
    exactly this default, so on the box it would have written into the live
    ``<data>/scribe/scribe/`` spool; #74 caught it as suite debris.

    A caller with a real config passes it (``run_suite(config=...)``) and this
    is never consulted.
    """
    return load_from_unified({
        "logging": {"dir": _eval_scratch_dir()},
        "scribe": {"mode": "synthetic", "stt": {"provider": "fake"}},
    })


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
