"""Web voice STT shadow-capture — Increment 1 of the Groq-Whisper-hybrid arc.

Mirrors the proven Telegram ``telegram/stt_shadow.py`` but for the WebRTC voice
path, and is SIMPLER on the Deepgram side: the Deepgram STREAMING final is
already in hand (the served turn's text), so there is exactly ONE extra call —
Groq batch on the fed PCM — per captured utterance. The audio + both transcripts
+ their word-level divergence + a noise tag are appended to a replayable corpus.

THE #1 CORRECTNESS PROPERTY — isolation from the served turn. :meth:`capture` is
invoked FIRE-AND-FORGET from the STT worker's pump AFTER the unchanged live
``on_utterance`` has fired, so the Groq call, the disk writes, and any
timeout/error here can NEVER block, delay, or break the served voice turn. As a
second guard the whole capture body sits under a top-level catch-all that logs
and returns. And per ``project_stt_test_series``: a bare ``create_task`` is held
only by a weak ref and can be GC'd mid-flight — every task is retained in the
module-level ``_SHADOW_TASKS`` set with a discard-in-done-callback.

Corpus contract (matches ``stt_replay.py`` + ``telegram/stt_shadow`` records):
``corpus.jsonl`` lines carry ``audio_file`` (replay input), the literal ``groq``
/ ``deepgram`` result slots, and ``divergence`` (computed via the byte-identical
:func:`divergence` copied from the harness). Web-specific additions:
``voice_session_id`` and a ``noise`` block (per-utterance RMS + a rolling
inter-utterance noise-floor → ``noisy: bool``) for the noisy-subset cut.

DEFAULT-OFF, Salem-only. When disabled the worker never wires this in, so the
tee / snapshot / hook are all skipped and the live path is byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.telegram.stt_backends import SttError

from .utils import get_logger, pcm_rms
from .voice_stt import pcm16_to_wav

log = get_logger(__name__)

# Noise tagging. utt/floor RMS are stored RAW so the operator recalibrates this
# threshold post-hoc from the corpus (scope §6.6) rather than trusting a
# hardcoded line; this is a defensible first guess for a quiet-room-vs-ambient cut.
_NOISE_FLOOR_RMS = 150.0
_NOISE_FLOOR_ALPHA = 0.35     # rolling EMA weight on the newest utterance's floor
_NOISE_WINDOW_MS = 100        # per-window RMS granularity for peak / floor
_NOISE_FLOOR_PCTL = 0.20      # low-percentile window RMS ≈ the ambient in over-capture

# MODULE-LEVEL (not per-session): a bare create_task is only weak-ref'd by the
# loop and can be GC'd mid-flight (project_stt_test_series gotcha). A module set
# keeps the task — and, via the running coroutine, the VoiceSttShadow it belongs
# to — strongly reachable until done, even if the session's worker is dropped
# first. discard-in-done-callback prevents unbounded growth.
_SHADOW_TASKS: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Divergence metric — copied byte-identically from the replay harness
# (aftermath-honeydew-review/teams/honeydew-review/stt_replay.py) via
# telegram/stt_shadow.py, so the live-captured number == the replay number.
#
# KEEP IN SYNC with stt_replay.py:divergence (vendored copy — no shared source
# until the harness is repo-vendored). If the harness formula changes, update
# _norm/_edit_distance/divergence here AND in telegram/stt_shadow.py, else the
# live divergence silently stops matching the replay-computed one.
# ---------------------------------------------------------------------------


def _norm(text: str) -> list[str]:
    """Lowercase, strip punctuation, collapse whitespace -> word tokens."""
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return t.split()


def _edit_distance(a: list, b: list) -> int:
    """Word-level Levenshtein distance."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


def divergence(a: str, b: str) -> float:
    """Normalized divergence between two transcripts (0=identical, ~1=disjoint)."""
    na, nb = _norm(a), _norm(b)
    if not na and not nb:
        return 0.0
    return _edit_distance(na, nb) / max(len(na), len(nb), 1)


# ---------------------------------------------------------------------------
# Noise metrics — pure
# ---------------------------------------------------------------------------


def noise_metrics(pcm: bytes, sample_rate: int) -> tuple[float, float, float]:
    """``(peak_rms, avg_rms, floor_rms)`` for s16 mono ``pcm``. Pure.

    ``peak`` = loudest 100 ms window (speech energy); ``avg`` = whole-buffer RMS;
    ``floor`` = a low-percentile window RMS ≈ the ambient noise floor captured in
    the leading/trailing silence (Deepgram runs without VAD, so the buffer
    over-captures inter-utterance silence — that silence IS the noise sample)."""
    if not pcm:
        return 0.0, 0.0, 0.0
    win_bytes = max(2, (sample_rate * _NOISE_WINDOW_MS // 1000) * 2)
    windows = [
        pcm_rms(pcm[i:i + win_bytes])
        for i in range(0, len(pcm), win_bytes)
        if len(pcm[i:i + win_bytes]) >= 2
    ]
    if not windows:
        return 0.0, 0.0, 0.0
    windows_sorted = sorted(windows)
    idx = min(len(windows_sorted) - 1, int(_NOISE_FLOOR_PCTL * len(windows_sorted)))
    return max(windows), pcm_rms(pcm), windows_sorted[idx]


# ---------------------------------------------------------------------------
# Per-session shadow
# ---------------------------------------------------------------------------


class VoiceSttShadow:
    """One per voice session. Holds the rolling inter-utterance noise floor.
    ``capture`` is the fire-and-forget hook the STT worker calls after the
    served turn; it is sync and returns immediately (the task lives in the
    module-level ``_SHADOW_TASKS`` set, not on the instance)."""

    def __init__(
        self,
        *,
        groq_backend: Any,
        vocab: list[str],
        corpus_dir: str,
        instance_name: str,
        voice_session_id: str,
        sample_rate: int,
    ) -> None:
        self._groq = groq_backend
        self._vocab = list(vocab or [])
        self._dir = Path(corpus_dir)
        self._instance = instance_name
        self._vid = voice_session_id
        self._rate = sample_rate
        self._noise_floor_ema: float | None = None

    def capture(self, pcm: bytes, deepgram_text: str, duration_s: float) -> None:
        """Fire-and-forget. NEVER blocks or raises into the caller (the served
        voice turn). Snapshots the PCM into a fresh bytes so the worker may keep
        reusing its buffer. The task is retained in the module-level
        ``_SHADOW_TASKS`` (GC-safe, survives this shadow object being dropped)."""
        try:
            task = asyncio.ensure_future(
                self._capture(bytes(pcm), deepgram_text, duration_s))
            _SHADOW_TASKS.add(task)
            task.add_done_callback(_SHADOW_TASKS.discard)
        except Exception:  # noqa: BLE001 — scheduling must never touch the turn
            log.warning("web.voice.stt.shadow_schedule_failed",
                        voice_session_id=self._vid)

    async def _capture(self, pcm: bytes, deepgram_text: str, duration_s: float) -> None:
        try:
            await self._capture_inner(pcm, deepgram_text, duration_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolation backstop
            log.warning(
                "web.voice.stt.shadow_capture_failed",
                voice_session_id=self._vid,
                error=str(exc)[:300], error_type=type(exc).__name__,
            )

    async def _capture_inner(
        self, pcm: bytes, deepgram_text: str, duration_s: float,
    ) -> None:
        # Noise tag (rolling inter-utterance floor).
        peak, avg, floor = noise_metrics(pcm, self._rate)
        self._noise_floor_ema = (
            floor if self._noise_floor_ema is None
            else _NOISE_FLOOR_ALPHA * floor
            + (1 - _NOISE_FLOOR_ALPHA) * self._noise_floor_ema
        )
        noisy = self._noise_floor_ema >= _NOISE_FLOOR_RMS

        # The ONE extra call: Groq batch on the WAV-wrapped fed PCM.
        wav = pcm16_to_wav(pcm, self._rate)
        groq_rec = await self._run_groq(wav)

        deepgram_rec = {"text": deepgram_text, "latency_ms": 0, "error": None}
        div = round(divergence(groq_rec["text"], deepgram_text), 3)

        now = datetime.now(timezone.utc)
        short_hash = hashlib.sha256(wav).hexdigest()[:12]
        audio_file = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{short_hash}.wav"

        self._dir.mkdir(parents=True, exist_ok=True)
        audio_path = self._dir / audio_file
        if not audio_path.exists():
            audio_path.write_bytes(wav)

        record = {
            "audio_file": audio_file,          # harness replay input key
            "ts": now.isoformat(),
            "instance": self._instance,
            "voice_session_id": self._vid,
            "duration": round(duration_s, 3),
            "groq": groq_rec,                  # literal key — harness contract
            "deepgram": deepgram_rec,          # literal key — streaming final
            "divergence": div,
            "noise": {
                "utt_peak_rms": round(peak, 1),
                "utt_avg_rms": round(avg, 1),
                "noise_floor": round(floor, 1),
                "noise_floor_ema": round(self._noise_floor_ema, 1),
                "noisy": bool(noisy),
            },
        }
        with (self._dir / "corpus.jsonl").open("a", encoding="utf-8") as fout:
            fout.write(json.dumps(record) + "\n")

        log.info(
            "web.voice.stt.shadow_captured",
            voice_session_id=self._vid, instance=self._instance,
            audio_file=audio_file, divergence=div, noisy=bool(noisy),
            groq_error=groq_rec["error"], groq_latency_ms=groq_rec["latency_ms"],
        )

    async def _run_groq(self, wav: bytes) -> dict:
        """Groq batch → results-shaped dict. Own try/except (isolation) + a
        wait_for backstop over the backend's own httpx timeout."""
        timeout_s = float(getattr(self._groq, "timeout_s", 10.0) or 10.0) + 5.0
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._groq.transcribe(wav, "audio/wav", self._vocab),
                timeout=timeout_s,
            )
            return {"text": result.text, "latency_ms": result.latency_ms,
                    "error": None}
        except asyncio.CancelledError:
            raise
        except SttError as exc:
            return {"text": "", "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": exc.error_class}
        except asyncio.TimeoutError:
            return {"text": "", "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": "timeout"}
        except Exception as exc:  # noqa: BLE001 — per-call isolation
            return {"text": "", "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": f"unknown:{type(exc).__name__}"[:200]}


__all__ = ["VoiceSttShadow", "divergence", "noise_metrics"]
