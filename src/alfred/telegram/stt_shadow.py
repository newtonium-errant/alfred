"""STT shadow-capture — R1-baseline corpus builder for the STT test series.

See ``algernon-stt-test-series-2026-06-27.md`` / ``project_stt_test_series``.

The M1 fallback chain (``stt_backends``) only runs the backup engine when the
primary FAILS. Shadow-capture is the R1 enabler: when ``enabled`` it runs
BOTH engines on EVERY voice note (Groq still serves the user via the normal
chain; the second engine is the only extra call) and writes the audio + both
transcripts + their word-level divergence to a replayable corpus. Because the
audio is retained, later test-series rounds re-run both engines over the same
inputs with different settings — a controlled A/B on identical audio.

THE CRITICAL CORRECTNESS PROPERTY — isolation. This path is FULLY isolated
from the user-facing turn. It is invoked fire-and-forget (``asyncio.
create_task``) from ``on_voice`` AFTER the served result is in hand, so the
extra engine call, the disk writes, and any timeout/error here can NEVER
block, delay, or break the served turn. As a second guard, the whole of
:func:`capture` is wrapped in a top-level catch-all that logs and returns —
it never raises out.

The corpus contract (load-bearing — matches the replay harness
``stt_replay.py`` exactly): a corpus DIR with audio files + a ``corpus.jsonl``
whose lines are RESULTS-SHAPED so the SAME file is both the replay input
(needs ``audio_file``) AND directly consumable by the harness's
``divergences`` / ``score`` modes (need ``groq`` / ``deepgram`` /
``divergence``). The :func:`divergence` metric is replicated byte-identically
from the harness (it lives OUTSIDE this package, so it is copied not imported)
so the live-captured numbers are directly comparable with replay-computed ones.

DEFAULT-OFF (``stt.shadow_capture.enabled``). When disabled :func:`capture`
returns immediately — no extra call, no files, no log line (event-driven; per
``feedback_intentionally_left_blank`` an idle tick is unnecessary for a path
that only acts on inbound voice).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .stt_backends import SttError, SttResult, build_chain
from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Divergence metric — replicated byte-identically from the replay harness
# (aftermath-honeydew-review/teams/honeydew-review/stt_replay.py:46-84).
#
# The harness lives OUTSIDE the alfred package, so it cannot be imported;
# these three functions are copied verbatim so the divergence written by the
# live capture is the SAME number the harness would compute on replay. Keep
# them in lockstep with the harness if either side changes.
#
# KEEP IN SYNC with stt_replay.py:divergence (vendored copy — no shared source
# until the harness is repo-vendored). Sibling vendored copy also lives in
# web/voice_stt_shadow.py; update all three together.
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
                prev[j] + 1,               # deletion
                cur[j - 1] + 1,            # insertion
                prev[j - 1] + (ca != cb),  # substitution
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
# Vendor → corpus-key mapping
# ---------------------------------------------------------------------------

# The harness's ``divergences`` / ``score`` modes read records keyed by the
# LITERAL strings "groq" and "deepgram". Map each backend's ``backend_id``
# (build_chain assigns "groq-whisper" / "deepgram") to its corpus key.
_VENDOR_KEYS = ("groq", "deepgram")


def _corpus_key(backend_id: str) -> str | None:
    """Map a backend_id to its corpus vendor key, or None if not a vendor we
    bucket (e.g. an M4 local-whisper backstop — not part of the A/B)."""
    bid = (backend_id or "").lower()
    if bid in ("groq-whisper", "groq"):
        return "groq"
    if bid == "deepgram":
        return "deepgram"
    return None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def capture(
    audio_bytes: bytes,
    mime: str,
    served_result: SttResult | None,
    stt_config: Any,
    *,
    instance_name: str,
    chat_id: int | None,
    duration: int,
) -> None:
    """Shadow-capture one voice note into the R1 corpus (no-op when disabled).

    ``served_result`` is the SttResult the M1 chain already served the user
    (or ``None`` when the chain degraded / failed — NoTranscript). For each
    engine whose ``backend_id`` matches ``served_result`` we REUSE that result
    rather than re-spending the call that already served; the other engine(s)
    run fresh. Net cost in the served case: exactly one extra engine call.

    NEVER raises: the whole body is wrapped so a disk error, a backend
    exception, or a timeout is logged (``stt.shadow_capture_failed``) and
    swallowed. Isolation from the served turn is the #1 property (see module
    docstring) — this runs fire-and-forget, so nothing here touches the user.
    """
    shadow = getattr(stt_config, "shadow_capture", None)
    if shadow is None or not getattr(shadow, "enabled", False):
        return  # default-OFF: behaves exactly as before shadow-capture existed
    try:
        await _capture_inner(
            audio_bytes, mime, served_result, stt_config, shadow,
            instance_name=instance_name, chat_id=chat_id, duration=duration,
        )
    except asyncio.CancelledError:
        raise  # cooperative cancellation (shutdown) must propagate
    except Exception as exc:  # noqa: BLE001 — isolation backstop
        log.warning(
            "stt.shadow_capture_failed",
            error=str(exc)[:300],
            error_type=type(exc).__name__,
            instance=instance_name,
        )
        return


async def _capture_inner(
    audio_bytes: bytes,
    mime: str,
    served_result: SttResult | None,
    stt_config: Any,
    shadow: Any,
    *,
    instance_name: str,
    chat_id: int | None,
    duration: int,
) -> None:
    vocab = list(getattr(stt_config, "vocab_terms", []) or [])
    engines = build_chain(stt_config)

    records: dict[str, dict] = {}
    for engine in engines:
        key = _corpus_key(getattr(engine, "backend_id", ""))
        if key is None or key in records:
            # Unknown vendor (skip) or already filled (primary wins on a
            # duplicate vendor — shouldn't happen with a sane chain).
            continue
        records[key] = await _run_engine(
            engine, served_result, audio_bytes, mime, vocab,
        )
    # The harness's groq/deepgram contract needs both slots present even if
    # the chain only carried one vendor — mark the absent one explicitly so a
    # mis-configured 1-engine chain is grep-able rather than silently missing.
    for key in _VENDOR_KEYS:
        records.setdefault(
            key, {"text": "", "latency_ms": 0, "error": "not_in_chain"},
        )

    div = round(divergence(records["groq"]["text"], records["deepgram"]["text"]), 3)

    now = datetime.now(timezone.utc)
    short_hash = hashlib.sha256(audio_bytes).hexdigest()[:12]
    audio_file = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{short_hash}.ogg"

    corpus_dir = Path(getattr(shadow, "dir", "./data/stt_corpus"))
    corpus_dir.mkdir(parents=True, exist_ok=True)
    audio_path = corpus_dir / audio_file
    # Filename is timestamp + content-hash, so the skip-if-exists is only a
    # WITHIN-SECOND idempotency guard (identical bytes captured in the same
    # second collide on the name and re-write is skipped). Across different
    # seconds the same audio yields two files — that's fine; one capture event
    # = one audio file + one corpus line.
    if not audio_path.exists():
        audio_path.write_bytes(audio_bytes)

    record = {
        "audio_file": audio_file,        # harness replay input key (relative)
        "ts": now.isoformat(),
        "instance": instance_name,
        "chat_id": chat_id,
        "duration": duration,
        "groq": records["groq"],         # literal key — harness contract
        "deepgram": records["deepgram"],  # literal key — harness contract
        "divergence": div,
    }
    corpus_jsonl = corpus_dir / "corpus.jsonl"
    with corpus_jsonl.open("a", encoding="utf-8") as fout:
        fout.write(json.dumps(record) + "\n")

    log.info(
        "stt.shadow_captured",
        instance=instance_name,
        audio_file=audio_file,
        divergence=div,
        groq_error=records["groq"]["error"],
        deepgram_error=records["deepgram"]["error"],
    )


async def _run_engine(
    engine: Any,
    served_result: SttResult | None,
    audio_bytes: bytes,
    mime: str,
    vocab: list[str],
) -> dict:
    """Transcribe one engine into a results-shaped dict, reusing the served
    result when this engine is the one that already served (no re-spend).

    Each engine is wrapped in its OWN try/except so one engine failing does
    not abort the capture: an ``SttError`` records its ``error_class``; any
    other exception records ``unknown:<...>``. A per-engine ``asyncio.wait_for``
    backstops a hung backend (the backend already times out via httpx; this
    bounds the non-HTTP failure modes too). Result shape mirrors the harness's
    ``one()`` helper: ``{"text", "latency_ms", "error"}``.
    """
    backend_id = getattr(engine, "backend_id", None)
    if (
        isinstance(served_result, SttResult)
        and backend_id is not None
        and served_result.backend_id == backend_id
    ):
        # Reuse the call that already served the user — avoid double-spend.
        return {
            "text": served_result.text,
            "latency_ms": served_result.latency_ms,
            "error": None,
        }

    # Backstop the per-engine call. The backend already enforces ``timeout_s``
    # via its httpx client; this generous wait_for bounds any non-HTTP hang.
    timeout_s = float(getattr(engine, "timeout_s", 10.0) or 10.0) + 5.0
    try:
        result = await asyncio.wait_for(
            engine.transcribe(audio_bytes, mime, vocab), timeout=timeout_s,
        )
        return {
            "text": result.text,
            "latency_ms": result.latency_ms,
            "error": None,
        }
    except asyncio.CancelledError:
        raise
    except SttError as exc:
        return {"text": "", "latency_ms": 0, "error": exc.error_class}
    except asyncio.TimeoutError:
        return {"text": "", "latency_ms": 0, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001 — per-engine isolation
        return {
            "text": "",
            "latency_ms": 0,
            "error": f"unknown:{type(exc).__name__}: {exc}"[:200],
        }


__all__ = ["capture", "divergence"]
