"""Local multi-speaker diarization for the sovereign scribe (scribe P4-1).

The DIARIZER-WRITER half of P4: it resolves each transcript segment's
``speaker`` to a canonical ROLE ({clinician, patient, other, unknown}) and
latches ``Transcript.diarized``. The attribution-READER half (the mis-attribution
safety net) is P4-2 — NO consumer reads ``speaker`` yet this phase (the plumbing
+ the fake seam + the frozen data shapes ship first, fully CI-covered, before the
heavy engine).

Providers (dispatch on ``config.diarize.provider``) — ALL on the sovereign
barrier-a-sibling allowlist, so no cloud diarization is reachable:

  * ``off`` — the fail-closed default. NO diarization: the chunk is returned
    unchanged (``speaker`` stays ``None``, ``diarized`` stays ``False``). The
    note-gen path is byte-identical to P3.
  * ``fake`` — a DETERMINISTIC CI backend that re-reads the fake-STT ``.txt``
    sidecar, parses an optional leading role tag per line, and writes the
    resolved role onto each segment. NO heavy dep; gives the P4 plumbing
    unconditional coverage.
  * ``pyannote`` — the real on-box engine (P4-4). Raises ``NotImplementedError``
    here; the dependency is the ``[scribe-diarize]`` extra (STAY-C venv only).

FAIL-SAFE-for-safety / FAIL-OPEN-for-availability: a diarize failure degrades to
``speaker=None`` + a loud log and STILL folds the text (un-attributed ≫
mis-attributed). Unlike an STT decode failure it does NOT hold the encounter —
the pipeline wraps ``assign_speakers`` accordingly.

LOCAL-BY-CONSTRUCTION: no ``api_key`` / ``base_url``; the real engine loads its
embedding model offline from the local HF cache (P4-4). The sovereign boundary
(``_check_diarize_local``) independently refuses a non-local provider at load.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    ROLE_UNKNOWN,
    Transcript,
    normalize_role,
)

log = structlog.get_logger(__name__)

# The diarize dispatch set. MUST equal the sovereign barrier-a-sibling allowlist
# (SOVEREIGN_DIARIZE_ALLOWLIST) — pinned in tests. A provider the boundary
# permits is dispatchable here, and nothing else is.
SCRIBE_DIARIZE_PROVIDERS: frozenset[str] = frozenset({"off", "fake", "pyannote"})
# Providers that need a heavy dependency (the [scribe-diarize] extra).
_REAL_ENGINE_PROVIDERS: frozenset[str] = frozenset({"pyannote"})

# The fake-sidecar role-tag syntax → the role WORD ``normalize_role`` folds. An
# unrecognized bracket token (or no tag) is treated as untagged → ``unknown``.
_FAKE_ROLE_TAGS: dict[str, str] = {
    "[CLIN]": "clinician",
    "[PT]": "patient",
    "[OTHER]": "other",
}


class DiarizeError(Exception):
    """Diarization failed — unknown provider, unreadable input, engine failure."""


class MissingDiarizeDependency(Exception):
    """The ``pyannote`` engine is configured but pyannote.audio isn't installed.

    The scribe daemon maps this to exit 78 (missing deps, no-restart) — mirrors
    :class:`~alfred.scribe.stt.MissingSTTDependency`. The ``off`` / ``fake``
    providers never raise this (the daemon boots torch-free).
    """


def _pyannote_available() -> bool:
    """True iff pyannote.audio is importable (the ``[scribe-diarize]`` extra).

    ``find_spec`` on a dotted name imports the parent package, which raises
    ``ModuleNotFoundError`` when pyannote itself is absent — caught here so the
    probe returns a clean ``False`` (never propagates)."""
    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except ImportError:
        return False


def ensure_diarize_backend_available(config: ScribeConfig) -> None:
    """Fail-loud if the configured real-engine provider's dep is missing.

    Called at daemon startup (beside ``stt.ensure_backend_available``). No-op for
    ``off`` / ``fake`` — the daemon boots torch-free. Raises
    :class:`MissingDiarizeDependency` for ``pyannote`` when pyannote.audio isn't
    installed → the runner exits 78.
    """
    provider = (config.diarize.provider or "").strip().lower()
    if provider in _REAL_ENGINE_PROVIDERS and not _pyannote_available():
        raise MissingDiarizeDependency(
            f"scribe diarize provider {provider!r} needs pyannote.audio, which is "
            f"not installed. Install the [scribe-diarize] extra into the STAY-C "
            f"venv (torch from the CPU wheel index). The 'off'/'fake' providers "
            f"need no dependency."
        )


def assign_speakers(
    config: ScribeConfig, audio_path: str | Path, chunk_tx: Transcript,
) -> Transcript:
    """Resolve per-segment speaker roles on ``chunk_tx`` — the pipeline entry.

    Dispatches on ``config.diarize.provider`` (all barrier-a-sibling-allowlisted).
    ``off`` returns the chunk untouched (no diarization); ``fake`` reads the
    sidecar; ``pyannote`` is P4-4. On success the transcript's ``diarized`` gate
    is latched. The pipeline wraps this call so any exception degrades to
    ``speaker=None`` and STILL folds (fail-open-for-availability).
    """
    provider = (config.diarize.provider or "").strip().lower()
    if provider == "off":
        return chunk_tx  # no diarization — speaker stays None, diarized stays False
    if provider == "fake":
        return _fake_diarize(chunk_tx, audio_path)
    if provider == "pyannote":
        raise NotImplementedError(
            "pyannote diarization engine lands in P4-4 (needs HF_TOKEN + on-box "
            "enrollment). P4-1 ships only the plumbing + the fake seam."
        )
    # Defense in depth: the barrier-a sibling already refuses a non-local provider
    # at load; the dispatch fails closed too rather than silently no-op.
    raise DiarizeError(
        f"scribe diarize provider {provider or '(unset)'!r} is not a local "
        f"backend ({', '.join(sorted(SCRIBE_DIARIZE_PROVIDERS))})."
    )


def _split_role_tag(line: str) -> tuple[str | None, str]:
    """Split an optional leading fake role tag from a sidecar line.

    Recognized (case-insensitive): ``[CLIN]`` / ``[PT]`` / ``[OTHER]`` → the role
    WORD (clinician/patient/other), with the tag stripped from the text. Anything
    else — no tag, or an unrecognized bracket token — returns ``(None, line)``;
    the untagged case folds to ``unknown`` via ``normalize_role`` and the text is
    left verbatim.
    """
    stripped = line.lstrip()
    upper = stripped.upper()
    for tag, role in _FAKE_ROLE_TAGS.items():
        if upper.startswith(tag):
            return role, stripped[len(tag):].strip()
    return None, line


def _fake_diarize(chunk_tx: Transcript, audio_path: str | Path) -> Transcript:
    """Deterministic CI backend — re-reads the fake-STT ``.txt`` sidecar, parses a
    role tag per line, writes the resolved role onto each segment.

    The sidecar location mirrors ``stt._fake_transcribe`` (the ``audio_path``
    itself when it is a ``.txt``, else a sibling ``<stem>.txt``). Sidecar lines
    align 1:1 with the STT segments (both are the same non-empty-line sequence).
    A segment with no corresponding line — or an untagged line — resolves to
    ``unknown`` (fail-closed via ``normalize_role``). Latches ``diarized``.
    """
    p = Path(audio_path)
    sidecar = p if p.suffix == ".txt" else p.with_suffix(".txt")
    if not sidecar.is_file():
        raise DiarizeError(
            f"fake diarize backend needs the same text sidecar at {sidecar} as "
            f"the fake STT backend (one role-tagged line per segment). Synthetic "
            f"input only."
        )
    lines = [
        ln.strip()
        for ln in sidecar.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    roles: list[str] = []
    for i, seg in enumerate(chunk_tx.segments):
        if i < len(lines):
            tag, text = _split_role_tag(lines[i])
        else:
            tag, text = None, seg.text  # no line → unknown, text unchanged (fail-closed)
        seg.speaker = normalize_role(tag)
        seg.text = text
        roles.append(seg.speaker)
    chunk_tx.diarized = True
    log.info(
        "scribe.diarize.assigned",
        provider="fake",
        source_id=chunk_tx.source_id,
        segments=len(chunk_tx.segments),
        clinician=roles.count(ROLE_CLINICIAN),
        patient=roles.count(ROLE_PATIENT),
        other=roles.count(ROLE_OTHER),
        unknown=roles.count(ROLE_UNKNOWN),
    )
    return chunk_tx
