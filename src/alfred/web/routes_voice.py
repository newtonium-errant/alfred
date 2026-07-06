"""Web voice routes — V0 WebRTC echo transport (peer + session gated).

Three routes (contract §2), mounted by ``register_web_routes`` behind the
new ``web.voice`` config block (default-OFF):

* ``POST /voice/offer``  — vanilla-ICE offer → answer (server-minted
  ``voice_session_id``); single round-trip, all candidates embedded.
* ``POST /voice/close``  — idempotent, owner-bound teardown.
* ``GET  /voice/config`` — capability / ICE-server / ``yours``-scoped probe
  (absorbs the old ``/voice/status``; NO global session count — W9).

Media (browser↔server UDP) flows DIRECT, never through the BFF / cloudflared
tunnel. Signaling rides the existing relay chain (browser → Next BFF holding
the ``web`` peer token → this aiohttp transport).

Auth gate order on EVERY route (fail-closed):
  1. peer-pin ``transport_peer == WEB_CHAT_PEER`` — pins OUT the ``web_ingest``
     token (which shares ``allowed_clients: [web]``), closing the escalation
     path per the CLAUDE.md peer-pin requirement. Logs ``web.voice.wrong_peer``.
  2. ``require_web_session`` (``X-Alfred-Session``).
  Both failures render as an indistinguishable 401.

Mount modes (contract §1.6, §1.14):
  * ``voice.enabled`` false/absent → routes NOT mounted (route table
    byte-identical to today).
  * ``web.auth.mode == "relay"`` → NOT mounted (fail-closed, security W5).
  * ``pipeline`` != ``"echo"`` → NOT mounted, loud ``web.voice.disabled
    reason=unknown_pipeline`` (fail-closed-web; the daemon lives).
  * enabled + aiortc MISSING → routes mounted in 503 mode (manager is
    ``None``): ``/voice/offer`` → 503 ``voice_unavailable``, ``/voice/close``
    → always ``not_found``, ``/voice/config`` → ``available:false``.

The daemon must NEVER crash from a voice problem — worst case is voice not
mounted + a loud log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web

from .auth import WEB_CHAT_PEER, require_web_session
from .config import WebConfig
from .keys import KEY_WEB_CONFIG, KEY_WEB_VOICE_MANAGER
from .utils import get_logger
from .voice_session import (
    NegotiationFailed,
    TooManySessions,
    VoiceOfferTimeout,
    VoiceSessionManager,
    aiortc_available,
)

log = get_logger(__name__)


# Defense-in-depth cap on the offer SDP, INSIDE the app's shared 1 MB
# client_max_size envelope (real offers are ~2-20 KB). 128 KB.
MAX_SDP_BYTES = 128 * 1024

# Optional forward-hook: an opaque chat-session correlation key. V0 caps its
# length, logs its presence, and IGNORES it (contract §1.7). Never rejected.
MAX_SESSION_KEY_CHARS = 128


def _base_mime(content_type: str) -> str:
    """``application/json; charset=utf-8`` → ``application/json``."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _require_voice_identity(request: web.Request, web_config: WebConfig):
    """Peer-pin (WEB_CHAT_PEER) then session-verify. ``None`` → the handler
    emits a fail-closed 401 (pin-fail and session-fail indistinguishable)."""
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.voice.wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            reason="wrong_peer",
            detail="voice requires the dedicated 'web' peer token — refusing "
                   "(a web_ingest token shares allowed_clients [web] but must "
                   "not drive voice) — rejecting (401)",
        )
        return None
    return require_web_session(request, web_config)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_voice_offer(request: web.Request) -> web.StreamResponse:
    """POST /voice/offer — negotiate an echo session (contract §2)."""
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = _require_voice_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    manager: VoiceSessionManager | None = request.app.get(KEY_WEB_VOICE_MANAGER)
    if manager is None:
        # aiortc-missing 503 mode — auth ran first (no capability leak).
        log.warning(
            "web.voice.unavailable_at_offer",
            user=identity.user, reason="aiortc_missing",
        )
        return web.json_response(
            {"error": "voice_unavailable", "reason": "aiortc_missing"},
            status=503,
        )

    if _base_mime(request.headers.get("Content-Type", "")) != "application/json":
        return web.json_response({"error": "unsupported_media_type"}, status=415)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON → 400
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad_json"}, status=400)

    sdp = body.get("sdp")
    if not isinstance(sdp, str) or not sdp.strip():
        return web.json_response({"error": "sdp_required"}, status=400)
    if body.get("type") != "offer":
        return web.json_response({"error": "invalid_sdp_type"}, status=400)
    if len(sdp.encode("utf-8")) > MAX_SDP_BYTES:
        return web.json_response(
            {"error": "sdp_too_large", "max_bytes": MAX_SDP_BYTES}, status=413,
        )

    # session_key — length-cap, log presence, IGNORE (forward-hook §1.7).
    raw_key = body.get("session_key")
    session_key_present = isinstance(raw_key, str) and bool(raw_key.strip())
    if session_key_present:
        log.info(
            "web.voice.session_key_ignored",
            user=identity.user,
            length=min(len(raw_key), MAX_SESSION_KEY_CHARS),
            detail="session_key accepted as a V0 forward-hook — capped, "
                   "logged, and ignored (no chat coupling yet)",
        )

    try:
        vid, answer_sdp = await manager.open_session(identity, sdp)
    except TooManySessions as exc:
        return web.json_response(
            {"error": "too_many_sessions", "max_sessions": exc.max_sessions},
            status=429,
        )
    except VoiceOfferTimeout:
        return web.json_response({"error": "voice_offer_timeout"}, status=504)
    except NegotiationFailed:
        return web.json_response({"error": "negotiation_failed"}, status=502)

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=web_config.voice.max_session_seconds)
    ).isoformat()
    return web.json_response({
        "voice_session_id": vid,
        "sdp": answer_sdp,
        "type": "answer",
        "expires_at": expires_at,
    })


async def _handle_voice_close(request: web.Request) -> web.StreamResponse:
    """POST /voice/close — idempotent, owner-bound teardown (contract §2)."""
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = _require_voice_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    if _base_mime(request.headers.get("Content-Type", "")) != "application/json":
        return web.json_response({"error": "unsupported_media_type"}, status=415)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON → 400
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad_json"}, status=400)

    vid = body.get("voice_session_id")
    if not isinstance(vid, str) or not vid.strip():
        return web.json_response(
            {"error": "voice_session_id_required"}, status=400,
        )

    manager: VoiceSessionManager | None = request.app.get(KEY_WEB_VOICE_MANAGER)
    if manager is None:
        # aiortc-missing mode: no registry → always not_found (idempotent).
        return web.json_response({"closed": False, "reason": "not_found"})

    closed = await manager.close_owned(
        vid.strip(), identity.synthetic_chat_id, reason="client_close",
    )
    if closed:
        return web.json_response({"closed": True})
    return web.json_response({"closed": False, "reason": "not_found"})


async def _handle_voice_config(request: web.Request) -> web.StreamResponse:
    """GET /voice/config — capability + ICE + ``yours``-scoped probe (§2)."""
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = _require_voice_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    voice = web_config.voice
    manager: VoiceSessionManager | None = request.app.get(KEY_WEB_VOICE_MANAGER)
    available = manager is not None
    ice_servers = [{"urls": [url]} for url in voice.ice.stun_servers]

    if available:
        yours = [
            {
                "voice_session_id": s.voice_session_id,
                "connection_state": s.connection_state,
                "age_seconds": int(manager.age_seconds(s)),
            }
            for s in manager.sessions_for(identity.synthetic_chat_id)
        ]
    else:
        yours = []

    return web.json_response({
        "available": available,
        "reason": None if available else "aiortc_missing",
        "ice_servers": ice_servers,
        "max_sessions": voice.max_sessions,
        "yours": yours,
    })


# ---------------------------------------------------------------------------
# Registration / wiring
# ---------------------------------------------------------------------------


def register_voice_handlers(
    app: web.Application, *, web_config: WebConfig,
) -> bool:
    """Mount the ``/voice/*`` routes onto ``app`` — IFF voice is enabled.

    Returns ``True`` when routes were mounted (whether in full or 503 mode),
    ``False`` when voice is absent / disabled / relay-mode / mis-piped
    (nothing registered — the route table stays byte-identical). Called by
    ``register_web_routes`` after the STT mount; ``KEY_WEB_CONFIG`` is
    already stashed on the app.

    Never raises — a voice-config problem disables the voice surface loudly
    but leaves the transport / chat / STT surface untouched.
    """
    voice = web_config.voice

    # Gate 1 — master enable.
    if not voice.enabled:
        log.info("web.voice.disabled", reason="not_enabled")
        return False

    # Gate 2 — relay mode never gets voice (V0 pins the session-mode 'web'
    # peer only; fail-closed, security W5).
    mode = getattr(web_config.auth, "mode", "session") or "session"
    if mode == "relay":
        log.info(
            "web.voice.disabled",
            reason="relay_mode",
            detail="voice is session-mode only in V0 — relay instances do "
                   "NOT mount /voice/* (fail-closed)",
        )
        return False

    # Gate 3 — pipeline enum (fail-closed on anything but 'echo').
    if voice.pipeline != "echo":
        log.error(
            "web.voice.disabled",
            reason="unknown_pipeline",
            pipeline=voice.pipeline,
            detail="only 'echo' is valid in V0 — refusing to mount an "
                   "unknown pipeline (fail-closed; daemon lives)",
        )
        return False

    # Reserved ICE knob observability — udp_port_range is accepted but has no
    # aiortc/aioice knob (aiortc#487), so it is NEVER a silent no-op.
    if voice.ice.udp_port_range:
        log.warning(
            "web.voice.ice_option_unapplied",
            option="udp_port_range",
            value=voice.ice.udp_port_range,
            detail="reserved key — aioice has no port-range knob (aiortc#487); "
                   "accepted and logged but NOT applied",
        )

    # Gate 4 — aiortc availability decides full-mount vs 503 mode.
    available, reason = aiortc_available()
    if available:
        manager = VoiceSessionManager(voice)
        app[KEY_WEB_VOICE_MANAGER] = manager

        async def _voice_shutdown(_app: web.Application) -> None:
            mgr: VoiceSessionManager | None = _app.get(KEY_WEB_VOICE_MANAGER)
            if mgr is not None:
                await mgr.close_all(reason="daemon_shutdown")
                mgr.stop_reaper()

        # Fired by run_server's ``runner.cleanup()`` (transport/server.py) —
        # zero daemon.py changes.
        app.on_shutdown.append(_voice_shutdown)
        log.info(
            "web.voice.registered",
            max_sessions=voice.max_sessions,
            stun_servers=len(voice.ice.stun_servers),
            advertised_ip=bool(voice.ice.advertised_ip),
            available=True,
        )
    else:
        # aiortc-missing 503 mode: routes ARE mounted (auth-gated probeable
        # 503 beats an ambiguous 404) but the manager is None.
        app[KEY_WEB_VOICE_MANAGER] = None
        log.error(
            "web.voice.unavailable",
            reason=reason,
            detail="web.voice.enabled=true but aiortc is not installed — "
                   "/voice/* mounted in 503 mode; pip install "
                   "'alfred-vault[webrtc]'",
        )

    app.router.add_post("/voice/offer", _handle_voice_offer)
    app.router.add_post("/voice/close", _handle_voice_close)
    app.router.add_get("/voice/config", _handle_voice_config)
    return True
