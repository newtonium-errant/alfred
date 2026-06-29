"""Web STT route — ``POST /stt/transcribe`` (peer + session gated).

Accepts an uploaded audio blob and returns a transcript by REUSING the
live STT fallback chain in :mod:`alfred.telegram.stt_backends`
(``build_chain`` + ``transcribe_with_fallback`` over
``talker_config.stt``) — the SAME path ``bot.py``'s ``on_voice`` uses, NOT
a rebuild. The browser records via MediaRecorder, the BFF relays the blob
(holding the peer token server-side), and the operator edits the returned
transcript before it feeds the chat Composer (and a future ingest body) —
the editable field is the human-in-the-loop correction surface.

Auth layering (reused unchanged from the chat handlers):
  * Layer 1 — the transport ``auth_middleware`` peer-gates every
    non-``/health`` route, so this route is peer-token-gated the moment it
    mounts on the shared app.
  * Layer 2 — :func:`alfred.web.auth.require_web_session` resolves the
    verified named user from the ``X-Alfred-Session`` token, fail-closed
    401.

The one load-bearing aiohttp detail (verified, aiohttp 3.13.x): the shared
transport app is built with the DEFAULT ``client_max_size`` of 1 MB
(``build_app`` passes no override), and ``request.read()`` /
``request.post()`` / ``request.multipart()`` all enforce it — they would
413 every voice note. So this handler STREAMS ``request.content`` with its
OWN byte cap (:data:`MAX_AUDIO_BYTES`), leaving the 1 MB guard on the peer
JSON routes untouched. Do NOT "fix" this by raising ``client_max_size`` on
the shared app — that weakens DoS protection on every peer route.

Per the intentionally-left-blank + self-correcting standards, the response
surfaces ``low_confidence`` / ``empty`` / ``degraded`` / ``fell_back`` so
silence-vs-failure is distinguishable, and the empty / degraded cases
return an explicit 200 SIGNAL (not a silent blank). NEVER a cross-engine
numeric confidence (Whisper logprob vs Deepgram probability are not
comparable) — ``fell_back`` / ``tier`` drive ``low_confidence``.

Opt-in: mounted by :func:`alfred.web.routes_chat.register_web_routes`, so
``/stt/transcribe`` exists only when the web surface is enabled (M1 =
Salem only).
"""

from __future__ import annotations

from aiohttp import web

from .auth import require_web_session
from .config import WebConfig
from .keys import KEY_WEB_CONFIG, KEY_WEB_TALKER_CONFIG
from .utils import get_logger

log = get_logger(__name__)


# 25 MB — Groq Whisper's upload ceiling (CONTRACT §5 ``MAX_AUDIO_BYTES``).
MAX_AUDIO_BYTES = 25 * 1024 * 1024

# Stream chunk size for the bounded body read.
_CHUNK_BYTES = 64 * 1024

# Accepted audio MIME types (CONTRACT §5 ``AUDIO_MIME_ALLOWLIST``). Matched
# against the base mime (parameters like ``;codecs=opus`` stripped).
# ``application/octet-stream`` is the lenient fallback some clients send.
AUDIO_MIME_ALLOWLIST: frozenset[str] = frozenset({
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/x-m4a",
    "audio/mp4a-latm",
    "audio/flac",
    "application/octet-stream",
})


def _base_mime(content_type: str) -> str:
    """Strip parameters + normalise: ``audio/webm;codecs=opus`` → ``audio/webm``."""
    return (content_type or "").split(";", 1)[0].strip().lower()


async def _handle_stt_transcribe(request: web.Request) -> web.StreamResponse:
    """POST /stt/transcribe — transcribe an uploaded audio blob.

    Error taxonomy (CONTRACT §4, JSON ``{"error": <code>}``):
        invalid_session (401), unsupported_media_type (415),
        audio_too_large (413), no_audio (400), stt_failed (502).
    Empty / degraded transcripts are a 200 SIGNAL, not an error.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]

    identity = require_web_session(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    mime = _base_mime(request.headers.get("Content-Type", ""))
    if mime not in AUDIO_MIME_ALLOWLIST:
        log.warning(
            "web.stt.unsupported_media_type",
            user=identity.user,
            content_type=request.headers.get("Content-Type", ""),
        )
        return web.json_response(
            {"error": "unsupported_media_type"}, status=415,
        )

    # Stream the body with our OWN cap — NOT request.read()/post()/
    # multipart() (those enforce the app's 1 MB client_max_size). See the
    # module docstring for the aiohttp rationale.
    audio = bytearray()
    async for chunk in request.content.iter_chunked(_CHUNK_BYTES):
        audio.extend(chunk)
        if len(audio) > MAX_AUDIO_BYTES:
            log.warning(
                "web.stt.audio_too_large",
                user=identity.user,
                bytes_seen=len(audio),
                max_bytes=MAX_AUDIO_BYTES,
            )
            return web.json_response(
                {"error": "audio_too_large", "max_bytes": MAX_AUDIO_BYTES},
                status=413,
            )

    if not audio:
        log.warning("web.stt.no_audio", user=identity.user)
        return web.json_response({"error": "no_audio"}, status=400)

    talker_config = request.app[KEY_WEB_TALKER_CONFIG]
    # Lazy import — reuse the LIVE fallback chain (NOT the legacy
    # single-Groq transcribe.py). Byte-identical to bot.py's on_voice
    # call site.
    from alfred.telegram import stt_backends

    try:
        chain = stt_backends.build_chain(talker_config.stt)
        result = await stt_backends.transcribe_with_fallback(
            bytes(audio),
            mime,
            chain,
            talker_config.stt.vocab_terms,
            talker_config.stt.total_budget_s,
        )
    except Exception as exc:  # noqa: BLE001 — surface engine errors as 502
        log.warning(
            "web.stt.failed",
            user=identity.user,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return web.json_response(
            {"error": "stt_failed", "detail": str(exc)}, status=502,
        )

    # --- outcome mapping (CONTRACT §4 + spec) ---------------------------
    if isinstance(result, stt_backends.NoTranscript):
        if result.reason == "all_failed":
            log.warning(
                "web.stt.failed",
                user=identity.user,
                reason="all_failed",
                audio_bytes=len(audio),
            )
            return web.json_response({"error": "stt_failed"}, status=502)
        # degraded (chain-end empty, not an error) → 200 explicit signal.
        log.info(
            "web.stt.empty",
            user=identity.user,
            reason=result.reason,
            audio_bytes=len(audio),
        )
        return web.json_response(
            {"transcript": "", "degraded": True, "low_confidence": True}
        )

    # An SttResult — but a served-empty/whitespace transcript must NOT be
    # forwarded as a blank; return the explicit empty signal instead
    # (mirrors on_voice's served-empty handling, but keeps the FE editable
    # field open rather than reprompting).
    if not result.text.strip():
        log.info(
            "web.stt.empty",
            user=identity.user,
            reason="served_empty",
            backend_used=result.backend_id,
            audio_bytes=len(audio),
        )
        return web.json_response(
            {"transcript": "", "empty": True, "low_confidence": True}
        )

    # Non-empty served transcript. ``fell_back`` is derived (the router
    # computes it internally but does not store it on the result): the
    # served backend is not the first link in the chain. ``low_confidence``
    # is driven by fell_back OR a degraded tier — NEVER a cross-engine
    # numeric confidence.
    fell_back = bool(chain) and result.backend_id != chain[0].backend_id
    low_confidence = fell_back or result.tier == "degraded"
    log.info(
        "web.stt.transcribed",
        user=identity.user,
        backend_used=result.backend_id,
        fell_back=fell_back,
        tier=result.tier,
        low_confidence=low_confidence,
        chars=len(result.text),
    )
    return web.json_response({
        "transcript": result.text,
        "backend_used": result.backend_id,
        "fell_back": fell_back,
        "tier": result.tier,
        "low_confidence": low_confidence,
    })


def register_stt_handlers(app: web.Application) -> None:
    """Mount the /stt route. Called by ``register_web_routes`` (deps already
    stashed on ``app`` — KEY_WEB_CONFIG + KEY_WEB_TALKER_CONFIG)."""
    app.router.add_post("/stt/transcribe", _handle_stt_transcribe)
