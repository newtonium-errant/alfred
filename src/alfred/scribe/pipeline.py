"""The sovereign scribe pipeline state machine (scribe P2-d).

Wires the audio→transcript→note→ai_draft pipeline, ALL LOCAL-PYTHON (NOTE-3 —
NO ``claude -p`` / agent backend: claude -p egresses via cached OAuth even with
keys stripped, so the note path MUST stay local-loopback). The flow, per source:

  watch input_dir → guard_ingest (synthetic gate, fail-closed) → STT (local) →
  delta transcript → generate_verified_note (loopback Ollama) → grounding →
  render → vault_create clinical_note status=ai_draft via the stayc_clinical
  scope.

VERIFY-BEFORE-RENDER (HARD, the P2-c deferred commitment): the combined
``generate → verify → render`` is ONE choke function :func:`generate_verified_note`
— render_soap is called with the GroundingResult produced by verifying THE SAME
structured object. A note can NEVER be rendered without a grounding pass on its
own claims; nothing else in the pipeline calls ``render_soap``. So no code path
reaches ``vault_create`` with an unverified note.

NOTE-2 (type-change guard): the pipeline creates clinical_note status=ai_draft
ONLY — never attested_by / status=attested (that is scribe/attest.py's exclusive
path; the create-bypass scope guard refuses a born-attested note anyway).

NOTE-4 (PHI): source ids are opaque hashes in clinical mode (source_id_for);
logs / state / audit carry ids + counts + state-name ONLY — never title /
transcript / note text.

FAIL-CLOSED (PHI): any exception leaves the source in a retriable state (never
advanced past its real phase), logs the failure with source_id + state +
error-class (no PHI), and emits NO partial/unverified note to the vault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from alfred.scribe import stt as stt_mod
from alfred.scribe.attest import source_id_for
from alfred.scribe.attestation import SCRIBE_DRAFTER_IDENTITY
from alfred.scribe.config import ScribeConfig
from alfred.scribe.grounding import verify as verify_grounding
from alfred.scribe.ingest import ScribeIngestRefused, guard_ingest
from alfred.scribe.notegen import (
    StructuredNote,
    generate_structured,
    render_soap,
)
from alfred.scribe.state import (
    STATE_DRAFTED,
    STATE_FAILED,
    STATE_REFUSED,
    STATE_STRUCTURING,
    STATE_TRANSCRIBING,
    ScribeState,
)
from alfred.scribe.transcript import Transcript
from alfred.vault.ops import VaultError, vault_create

log = structlog.get_logger(__name__)

# Audio files the sweep treats as pipeline inputs. Sidecars (``.meta.json``
# provenance, ``.txt`` fake-STT transcript) are NOT primary inputs.
_AUDIO_EXTENSIONS = frozenset({".wav", ".ogg", ".mp3", ".m4a", ".flac", ".webm"})


@dataclass
class VerifiedNote:
    """The output of the verify-before-render choke — a note that has PROVABLY
    been grounding-verified (the grounding ran on THIS structured object)."""

    body: str
    grounding_flags: list[dict[str, Any]] = field(default_factory=list)
    flag_count: int = 0
    structured: StructuredNote | None = None


async def generate_verified_note(
    transcript: Transcript, *, config: ScribeConfig, title: str,
) -> VerifiedNote:
    """THE choke — generate → verify → render on the SAME object.

    Closes the P2-c residual hole (an empty GroundingResult renders clean): here
    the GroundingResult is produced by verifying the exact structured object
    that is then rendered. This is the ONLY producer of a rendered clinical-note
    body in the pipeline.
    """
    structured = await generate_structured(transcript, config=config)
    grounding = verify_grounding(structured, transcript)      # verify THE SAME object
    body = render_soap(structured, title=title, grounding=grounding)  # render with THAT grounding
    return VerifiedNote(
        body=body,
        grounding_flags=grounding.metadata,
        flag_count=len(grounding.flags),
        structured=structured,
    )


def _read_provenance(audio_path: Path) -> dict[str, Any]:
    """Read the input's provenance sidecar ``<stem>.meta.json``. Missing /
    malformed → ``{}`` (fail-closed: guard_ingest refuses it in synthetic mode)."""
    meta = audio_path.with_suffix(".meta.json")
    if not meta.is_file():
        return {}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _source_id_for_input(audio_path: Path, config: ScribeConfig) -> str:
    """Opaque source id (NOTE-4). Clinical mode hashes the audio bytes; synthetic
    mode uses the filename (synthetic, no real PHI)."""
    audio_bytes = audio_path.read_bytes() if config.is_clinical else None
    return source_id_for(
        mode=config.mode, filename=audio_path.name, audio_bytes=audio_bytes,
    )


async def process_source(
    audio_path: Path, *, config: ScribeConfig, state: ScribeState, vault_path: Path,
) -> str:
    """Process one input to a clinical_note ai_draft. Idempotent + fail-closed.

    Returns an outcome tag: ``skipped`` (already done) / ``refused`` /
    ``drafted`` / ``failed``.
    """
    source_id = _source_id_for_input(audio_path, config)

    # Idempotency gate — never reprocess a done source (or a failed one that
    # exhausted its retry budget). Replaying a drafted source is a no-op.
    if state.is_done(source_id):
        return "skipped"

    # Mode gate (fail-closed) — refuse non-synthetic input in synthetic mode.
    provenance = _read_provenance(audio_path)
    try:
        guard_ingest(config, provenance=provenance, source_id=source_id)
    except ScribeIngestRefused:
        state.set(source_id, state=STATE_REFUSED)
        log.info("scribe.pipeline.refused", source_id=source_id, state=STATE_REFUSED)
        return "refused"

    try:
        # recorded → transcribing (STT, local).
        state.set(source_id, state=STATE_TRANSCRIBING)
        transcript = stt_mod.transcribe(config, audio_path, source_id=source_id)

        # transcribing → structuring (delta → note-gen → verify → render).
        state.set(source_id, state=STATE_STRUCTURING)
        title = f"Encounter {source_id}"  # source_id-based → PHI-free (NOTE-4)
        vnote = await generate_verified_note(
            transcript.delta(), config=config, title=title,
        )

        # structuring → drafted (vault_create clinical_note ai_draft ONLY).
        note_path = _create_ai_draft(
            vault_path, title, source_id, config, vnote,
        )
        state.set(source_id, state=STATE_DRAFTED, note_path=note_path)
        log.info(
            "scribe.pipeline.drafted",
            source_id=source_id,
            state=STATE_DRAFTED,
            grounding_flags=vnote.flag_count,
        )
        return "drafted"
    except Exception as e:  # noqa: BLE001 — fail-closed: retriable, no partial note
        prior = state.get(source_id)
        attempts = (prior.attempts if prior else 0) + 1
        state.set(
            source_id, state=STATE_FAILED, attempts=attempts,
            last_error_class=type(e).__name__,
        )
        log.warning(
            "scribe.pipeline.failed",
            source_id=source_id,
            state=STATE_FAILED,
            error_class=type(e).__name__,   # class only — NO PHI
            attempts=attempts,
        )
        return "failed"


def _create_ai_draft(
    vault_path: Path, title: str, source_id: str, config: ScribeConfig,
    vnote: VerifiedNote,
) -> str:
    """Create the clinical_note ai_draft (NOTE-2: ai_draft ONLY). Idempotent on
    the crash-window where a prior run created the file but did not persist the
    drafted state → the already-exists case is treated as drafted."""
    try:
        result = vault_create(
            vault_path,
            "clinical_note",
            title,
            set_fields={
                "ai_draft": True,
                "synthetic": config.mode != "clinical",
                "status": "ai_draft",
                "source_id": source_id,
                "drafted_by": SCRIBE_DRAFTER_IDENTITY,
                "grounding_flags": vnote.grounding_flags,
            },
            body=vnote.body,
            scope="stayc_clinical",
        )
        return result["path"]
    except VaultError as e:
        if "already exists" in str(e):
            # Prior run drafted it but crashed pre-state-save → idempotent.
            tail = str(e).split("already exists:")
            return tail[-1].strip() if len(tail) > 1 else ""
        raise


async def run_sweep(
    config: ScribeConfig, state: ScribeState, vault_path: Path,
) -> dict[str, int]:
    """Scan input_dir once, process each source. Returns per-outcome counts.

    Intentionally-left-blank: emits ``scribe.pipeline.idle`` (ran, nothing to
    do) when the sweep produced no new work — so idle is distinguishable from
    broken — and ``scribe.pipeline.swept`` with counts when it did.
    """
    input_dir = Path(config.input_dir)
    counts = {"scanned": 0, "drafted": 0, "refused": 0, "failed": 0, "skipped": 0}

    if not input_dir.is_dir():
        log.info(
            "scribe.pipeline.idle",
            input_dir=str(input_dir),
            scanned=0,
            detail="ran, nothing to do — input_dir does not exist yet",
        )
        return counts

    audio_files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTENSIONS
    )
    counts["scanned"] = len(audio_files)

    for audio in audio_files:
        outcome = await process_source(
            audio, config=config, state=state, vault_path=vault_path,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    new_work = counts["drafted"] + counts["refused"] + counts["failed"]
    if new_work == 0:
        log.info(
            "scribe.pipeline.idle",
            input_dir=str(input_dir),
            scanned=counts["scanned"],
            detail="ran, nothing to do — no new sources to process",
        )
    else:
        log.info("scribe.pipeline.swept", **counts)
    return counts
