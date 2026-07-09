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

import re
import time
from collections import OrderedDict
from typing import Callable

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

# --- Idempotency (retry-safe dedup) ----------------------------------------
# /stt/transcribe is stateless — every POST re-transcribes. On flaky LTE a
# long note can transcribe successfully but drop the RESPONSE, so the operator
# retries and the server re-does the whole STT (double work + double charge +
# a possibly-different transcript). The BFF/FE computes a SHA-256 hex of the
# audio blob and sends it in this header (content-addressed — same audio →
# same key, so a retry dedups naturally with no key-retention). A retry that
# hits a cached SUCCESS returns it WITHOUT re-running the chain. Frozen wire
# contract (the voice-frontend builds to this header).
STT_IDEMPOTENCY_HEADER = "X-Alfred-Stt-Idempotency-Key"

# A SHA-256 hex digest is exactly 64 lowercase hex chars. A present-but-
# malformed header is IGNORED (treated as no-key = transcribe fresh), never an
# error.
_STT_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

# Bounded in-process store: a retry happens within seconds/minutes, so a small
# LRU + short TTL is enough. No persistence — a process restart losing the
# cache just means a re-transcribe (acceptable).
_STT_DEDUP_MAX_ENTRIES = 32
_STT_DEDUP_TTL_S = 15 * 60


class _SttDedupCache:
    """Bounded in-process LRU + TTL cache for STT idempotency (retry-safe).

    Keys are ``(user, audio_sha256_hex)`` — namespaced by the authenticated
    user so a content-hash can never bleed a transcript across users if
    ``web.users`` ever grows beyond the single owner. Values are
    response-payload dicts (copies in, copies out, so a caller mutating the
    returned dict can't corrupt the store). NOT persisted. In-process + a
    single event-loop thread + await-free get/put → no lock needed.

    Only SUCCESS is cached (an SttResult with a real transcript); a retry
    after any failure/empty MUST re-attempt (the caller enforces that — the
    cache never sees a failure). In-flight coalescing (a future-per-key for
    concurrent same-key requests) is a deliberate follow-up, NOT built here:
    the incident is sequential (first completes → response drops → retry hits
    the completed cache); a concurrent same-key retry is rare and just
    re-transcribes once (bounded).
    """

    def __init__(
        self, *, max_entries: int = _STT_DEDUP_MAX_ENTRIES,
        ttl_s: float = _STT_DEDUP_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store: "OrderedDict[tuple[str, str], tuple[float, dict]]" = (
            OrderedDict()
        )
        self._max = max_entries
        self._ttl = ttl_s
        self._clock = clock

    def _evict_expired(self, now: float) -> None:
        expired = [
            k for k, (ts, _) in self._store.items() if now - ts > self._ttl
        ]
        for k in expired:
            del self._store[k]

    def get(self, key: "tuple[str, str]") -> "dict | None":
        now = self._clock()
        self._evict_expired(now)
        item = self._store.get(key)
        if item is None:
            return None
        self._store.move_to_end(key)  # LRU touch
        return dict(item[1])

    def put(self, key: "tuple[str, str]", payload: dict) -> None:
        now = self._clock()
        self._evict_expired(now)
        self._store[key] = (now, dict(payload))
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # evict oldest (LRU)

    def clear(self) -> None:
        self._store.clear()


# Module-level singleton (the hot-path store). Tests monkeypatch this with a
# fresh instance (optionally a fake clock) per test to avoid cross-test bleed.
_STT_DEDUP = _SttDedupCache()


def _read_idempotency_key(request: web.Request) -> str:
    """Return a validated lowercase SHA-256-hex idempotency key, or ``""``.

    Empty header → ``""`` (the normal non-idempotent path, no log). A present
    but malformed value (not 64 hex chars) → ``""`` + ONE info log so a
    mis-wired BFF is observable — never an error, and NEVER the value itself.
    """
    raw = request.headers.get(STT_IDEMPOTENCY_HEADER, "")
    if not raw:
        return ""
    norm = raw.strip().lower()
    if not _STT_KEY_RE.match(norm):
        log.info(
            "web.stt.idempotency_key_ignored",
            reason="malformed",
            key_len=len(raw),
        )
        return ""
    return norm

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

    # Idempotency: a content-addressed retry (same audio hash) returns the
    # cached transcript WITHOUT re-running the STT chain. The GET happens
    # POST-body (the body is fully drained above — clean keep-alive) but
    # BEFORE transcribe_with_fallback, so the load-bearing win (no double STT
    # call / charge) holds. Key is namespaced per authenticated user. A
    # malformed / absent header → no key → transcribe fresh (byte-identical
    # to the pre-idempotency behaviour; no ``deduped`` field in the response).
    idem_key = _read_idempotency_key(request)
    cache_key = (identity.user, idem_key) if idem_key else None
    if cache_key is not None:
        cached = _STT_DEDUP.get(cache_key)
        if cached is not None:
            log.info(
                "web.stt.deduped",
                user=identity.user,
                key_prefix=idem_key[:8],
                chars=len(cached.get("transcript", "")),
            )
            return web.json_response({**cached, "deduped": True})

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
    payload = {
        "transcript": result.text,
        "backend_used": result.backend_id,
        "fell_back": fell_back,
        "tier": result.tier,
        "low_confidence": low_confidence,
    }
    # CACHE SUCCESS ONLY: this is the sole path with a real transcript (every
    # 502 / empty / degraded outcome returned above, so the store never sees a
    # failure). Store the clean payload; the ``deduped`` flag is applied at the
    # response boundary, not persisted.
    if cache_key is not None:
        _STT_DEDUP.put(cache_key, payload)
    log.info(
        "web.stt.transcribed",
        user=identity.user,
        backend_used=result.backend_id,
        fell_back=fell_back,
        tier=result.tier,
        low_confidence=low_confidence,
        chars=len(result.text),
        stored=cache_key is not None,
    )
    # ``deduped`` appears ONLY in the keyed flow — a no-key request stays
    # byte-identical to the pre-idempotency response.
    if cache_key is not None:
        return web.json_response({**payload, "deduped": False})
    return web.json_response(payload)


def register_stt_handlers(app: web.Application) -> None:
    """Mount the /stt route. Called by ``register_web_routes`` (deps already
    stashed on ``app`` — KEY_WEB_CONFIG + KEY_WEB_TALKER_CONFIG)."""
    app.router.add_post("/stt/transcribe", _handle_stt_transcribe)
