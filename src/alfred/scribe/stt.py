"""Local segment-rich STT for the sovereign scribe (scribe P2-b).

Deliberately NOT the flat Telegram ``SttResult`` path — the scribe is a
different consumer: it needs SEGMENTS (stable ids + timestamps) for the
``[S#]`` grounding contract (P2-c) + the delta/diarization-ready
:class:`~alfred.scribe.transcript.Transcript` shape (P3/P4). This module is
standalone; the Telegram ``build_chain`` ``local-whisper`` slot is deliberately
NOT wired (a different, flatter consumer).

Providers (dispatch on ``config.stt.provider``) — ALL barrier-a-allowlisted,
so no cloud STT is reachable (the sovereign boundary refuses a cloud provider
at load, and the armed HTTP guard blocks any stray egress at runtime):

  * ``faster-whisper`` / ``local-whisper`` — the real local model
    (distil-large-v3, CPU int8, VAD, word timestamps). Lazy-imports
    faster-whisper so the daemon / CI without the ``[scribe]`` extra still
    boots (fake provider) — the runner exits 78 when a real-model provider is
    configured but the extra is missing (see :func:`ensure_backend_available`).
  * ``fake`` — a DETERMINISTIC CI backend that reads a text sidecar and mints a
    fixed segment-rich transcript. NO heavy dep; gives the pipeline core
    unconditional coverage.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.transcript import Segment, Transcript, make_segment_id

log = structlog.get_logger(__name__)

# The scribe STT dispatch set. MUST equal the sovereign barrier-a allowlist
# (SOVEREIGN_STT_ALLOWLIST) — pinned in tests. A provider the boundary permits
# is dispatchable here, and nothing else is.
SCRIBE_STT_PROVIDERS: frozenset[str] = frozenset(
    {"faster-whisper", "local-whisper", "fake"}
)
_REAL_MODEL_PROVIDERS: frozenset[str] = frozenset({"faster-whisper", "local-whisper"})

_DEFAULT_MODEL = "distil-large-v3"
# Deterministic synthetic segment cadence for the fake backend (5s/segment).
_FAKE_SEGMENT_SECONDS = 5.0


class STTError(Exception):
    """STT failed — unknown provider, unreadable input, model failure."""


class MissingSTTDependency(Exception):
    """A real-model provider is configured but faster-whisper isn't installed.

    The scribe daemon maps this to exit 78 (missing deps, no-restart) — mirrors
    surveyor's ML-deps handling. The ``fake`` provider never raises this.
    """


def _faster_whisper_available() -> bool:
    """True iff faster-whisper is importable (the ``[scribe]`` extra)."""
    return importlib.util.find_spec("faster_whisper") is not None


def ensure_backend_available(config: ScribeConfig) -> None:
    """Fail-loud if the configured real-model provider's dep is missing.

    Called at daemon startup. No-op for ``fake``. Raises
    :class:`MissingSTTDependency` for ``faster-whisper`` / ``local-whisper``
    when faster-whisper isn't installed → the runner exits 78.
    """
    provider = (config.stt.provider or "").strip().lower()
    if provider in _REAL_MODEL_PROVIDERS and not _faster_whisper_available():
        raise MissingSTTDependency(
            f"scribe STT provider {provider!r} needs faster-whisper, which is "
            f"not installed. Install the [scribe] extra: "
            f"pip install 'alfred-vault[scribe]'. (The 'fake' provider needs "
            f"no dependency.)"
        )


def transcribe(
    config: ScribeConfig,
    audio_path: str | Path,
    *,
    source_id: str,
) -> Transcript:
    """Transcribe ``audio_path`` locally into the segment-rich Transcript.

    Dispatches on ``config.stt.provider`` (all barrier-a-allowlisted). Cloud
    providers can never reach here — the boundary refuses them at load; the
    dispatch fails closed on anything outside :data:`SCRIBE_STT_PROVIDERS`.
    """
    provider = (config.stt.provider or "").strip().lower()
    mode = config.mode
    if provider == "fake":
        return _fake_transcribe(audio_path, source_id=source_id, mode=mode)
    if provider in _REAL_MODEL_PROVIDERS:
        return _faster_whisper_transcribe(
            config, audio_path, source_id=source_id, mode=mode,
        )
    # Defense in depth: barrier-a already refuses a non-local provider at load;
    # the STT dispatch fails closed too rather than silently reaching a cloud
    # engine.
    raise STTError(
        f"scribe STT provider {provider or '(unset)'!r} is not a local backend "
        f"({', '.join(sorted(SCRIBE_STT_PROVIDERS))}). Cloud STT is refused on "
        f"a sovereign instance."
    )


def _fake_transcribe(
    audio_path: str | Path, *, source_id: str, mode: str,
) -> Transcript:
    """Deterministic CI backend — reads a text sidecar, mints fixed segments.

    The sidecar is the ``audio_path`` itself when it is a ``.txt``, else a
    sibling ``<stem>.txt``. Each non-empty line becomes one segment with stable
    id ``S1``.. and synthetic timestamps (5s/segment). No heavy dependency.
    """
    p = Path(audio_path)
    sidecar = p if p.suffix == ".txt" else p.with_suffix(".txt")
    if not sidecar.is_file():
        raise STTError(
            f"fake STT backend needs a text sidecar at {sidecar} (one line per "
            f"segment). Synthetic input only."
        )
    lines = [ln.strip() for ln in sidecar.read_text(encoding="utf-8").splitlines() if ln.strip()]
    segments: list[Segment] = []
    for i, line in enumerate(lines):
        start = i * _FAKE_SEGMENT_SECONDS
        segments.append(
            Segment(
                id=make_segment_id(i),
                start_s=start,
                end_s=start + _FAKE_SEGMENT_SECONDS,
                text=line,
                speaker=None,
            )
        )
    log.info(
        "scribe.stt.transcribed",
        provider="fake",
        source_id=source_id,
        mode=mode,
        segments=len(segments),
    )
    return Transcript(source_id=source_id, mode=mode, segments=segments)


def _faster_whisper_transcribe(
    config: ScribeConfig, audio_path: str | Path, *, source_id: str, mode: str,
) -> Transcript:
    """Real local model — faster-whisper distil-large-v3, CPU int8, VAD, word
    timestamps → the segment-rich Transcript. Lazy-imports faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover — guarded upstream by ensure_backend_available
        raise MissingSTTDependency(
            "faster-whisper is not installed — install the [scribe] extra."
        ) from e

    model_name = (config.stt.model or "").strip() or _DEFAULT_MODEL
    # SOVEREIGN: cache-only load — NO HuggingFace revision-check egress.
    #
    # A bare-name ``WhisperModel(model_name)`` triggers a HF ``repo_info`` /
    # revision-check HTTP GET (Systran/faster-distil-whisper-large-v3) EVEN when
    # the model is fully cached locally — which the armed SovereignHttpGuard
    # (correctly) blocks → SovereignBoundaryError → every encounter fails.
    #
    # The env-var path is DEAD (both verified on the box): (a) HF_HUB_OFFLINE=1
    # / TRANSFORMERS_OFFLINE=1 are UNHONORED by huggingface_hub 1.23 /
    # faster-whisper 1.2 (still calls ``repo_info``); AND (b) the ``alfred up``
    # daemonize re-exec STRIPS launch-env vars before the daemon process. So the
    # fix MUST be an in-process WhisperModel PARAMETER, not an env var.
    #
    # ``local_files_only=True`` is the ONLY reliable mechanism: load from the
    # local HF cache, never check the remote revision. The model MUST be
    # pre-staged in the HF cache offline before use.
    try:
        model = WhisperModel(
            model_name, device="cpu", compute_type="int8", cpu_threads=8,
            local_files_only=True,
        )
    except Exception as e:  # noqa: BLE001 — obscure HF cache-miss → actionable ops error
        raise STTError(
            f"STT model {model_name!r} is not pre-staged in the local model "
            f"cache; a sovereign box cannot download at runtime (egress is "
            f"blocked by design). Pre-stage the model offline before use."
        ) from e
    seg_iter, _info = model.transcribe(
        str(audio_path), vad_filter=True, word_timestamps=True,
    )
    segments: list[Segment] = []
    for i, seg in enumerate(seg_iter):
        segments.append(
            Segment(
                id=make_segment_id(i),
                start_s=float(seg.start),
                end_s=float(seg.end),
                text=(seg.text or "").strip(),
                speaker=None,
            )
        )
    log.info(
        "scribe.stt.transcribed",
        provider="faster-whisper",
        source_id=source_id,
        mode=mode,
        model=model_name,
        segments=len(segments),
    )
    return Transcript(source_id=source_id, mode=mode, segments=segments)
