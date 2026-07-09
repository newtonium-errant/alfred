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
     This is the SINGLE load-bearing pin (unconditional, both modes); it pins
     ONLY ``web`` — NEVER ``rrts_relay`` (the text bug-report lane never drives
     voice).
  2. identity resolution, mode-aware:
       * ``session`` → ``require_web_session`` (``X-Alfred-Session`` token).
       * ``relay``   → ``_resolve_voice_relay_identity`` (asserted
         ``X-Alfred-User`` re-resolved against THIS instance's own
         ``web.users``; NO ``rrts_relay`` branch — voice is web-peer-only).
  All failures render as an indistinguishable 401.

Mount modes (contract §1.6, §1.14):
  * ``voice.enabled`` false/absent → routes NOT mounted (route table
    byte-identical to today).
  * ``web.auth.mode == "relay"`` → MOUNTED (multi-instance voice): relay-mode
    voice re-resolves the asserted identity against this instance's own
    ``web.users``, guarded by the unconditional ``web``-only peer-pin. (The
    original V0 fail-closed-on-relay W5 gate was relaxed once the peer-pin was
    proven the correct fail-closed guard — see ``_require_voice_identity``.)
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
from typing import Any
from uuid import uuid4

from aiohttp import web

from .auth import USER_HEADER, WEB_CHAT_PEER, require_web_session
from .config import WebConfig, _is_unresolved
from .identity import resolve_identity_from_name
from .keys import (
    KEY_WEB_ANTHROPIC,
    KEY_WEB_CONFIG,
    KEY_WEB_INFLIGHT,
    KEY_WEB_STATE_MGR,
    KEY_WEB_SYSTEM_PROVIDER,
    KEY_WEB_TALKER_CONFIG,
    KEY_WEB_VAULT_CTX,
    KEY_WEB_VOICE_MANAGER,
)
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

# session_key cap. In echo (V0) it's a logged-and-ignored forward-hook; in
# assistant (V1) it BINDS the voice session to the caller's chat session.
MAX_SESSION_KEY_CHARS = 128

# Pipeline enum. Anything outside this set fails closed (no mount).
_KNOWN_PIPELINES = frozenset({"echo", "assistant"})
# Assistant STT providers the code knows how to drive (fail-closed otherwise).
_KNOWN_STT_PROVIDERS = frozenset({"deepgram", "fake"})
# V2 TTS providers the code knows how to drive (fail-closed → text-only voice).
_KNOWN_TTS_PROVIDERS = frozenset({"elevenlabs", "fake"})
# App-key for the mount-normalized V3 barge settings (None = disabled).
_KEY_WEB_BARGE = "web.barge_settings"


def _base_mime(content_type: str) -> str:
    """``application/json; charset=utf-8`` → ``application/json``."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _require_voice_identity(request: web.Request, web_config: WebConfig):
    """Peer-pin (WEB_CHAT_PEER) then mode-aware identity resolution.

    ``None`` → the handler emits a fail-closed 401 (pin-fail and
    identity-fail render as an indistinguishable 401).

    The ``transport_peer == WEB_CHAT_PEER`` pin is the SINGLE load-bearing
    guard, applied UNCONDITIONALLY before the mode branch. It pins ONLY
    ``web`` (never ``rrts_relay``), so the shared-``allowed_clients: [web]``
    ``web_ingest`` token — and the vouched ``rrts_relay`` token — are both
    refused here regardless of auth mode. The negative-check regression test
    proves this: deleting this pin lets a ``web_ingest`` / ``rrts_relay``
    request + ``X-Alfred-User`` escalate to a full voice session.

    After the pin, identity resolution branches on ``web.auth.mode``:

    * ``session`` (default) — instance-signed ``X-Alfred-Session`` token
      (``require_web_session``); UNCHANGED (session-mode is byte-identical).
    * ``relay`` — asserted ``X-Alfred-User`` re-resolved against THIS
      instance's own ``web.users`` (``_resolve_voice_relay_identity``). No
      ``rrts_relay`` vouched branch — voice is web-peer-only.
    """
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.voice.wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            reason="wrong_peer",
            detail="voice requires the dedicated 'web' peer token — refusing "
                   "(a web_ingest token shares allowed_clients [web], and the "
                   "rrts_relay token is a text bug-report lane — neither may "
                   "drive voice) — rejecting (401)",
        )
        return None
    mode = getattr(web_config.auth, "mode", "session") or "session"
    if mode == "relay":
        return _resolve_voice_relay_identity(request, web_config)
    return require_web_session(request, web_config)


def _resolve_voice_relay_identity(request: web.Request, web_config: WebConfig):
    """Relay-mode voice identity — FIXED-ROSTER ``web`` peer ONLY.

    Mirrors the fixed-roster half of ``auth.py:_resolve_relay_identity`` but
    deliberately DROPS its ``rrts_relay`` vouched branch: voice is web-peer-
    only (§4.1 — the RRTS text bug-report lane must be *structurally* unable
    to reach voice, not merely pin-gated). The unconditional peer-pin in
    :func:`_require_voice_identity` has ALREADY confirmed
    ``transport_peer == WEB_CHAT_PEER`` before this runs — so this resolver
    does NOT re-check the peer. That is intentional: the line-96 pin stays the
    single load-bearing guard, keeping the negative-check meaningful (a
    redundant pin here would mask a line-96 regression).

    Re-resolves the asserted ``X-Alfred-User`` name against THIS instance's
    own ``web.users`` roster (never trusting an asserted role) → owner / role
    / synthetic-id derived locally. Fail-closed (→ 401), each with an ILB log
    so a mis-wired BFF (peer token present, user absent/unknown) is observably
    distinct from a healthy idle route:

    * missing / empty ``X-Alfred-User`` → ``web.voice.relay_user_missing``;
    * name not in this instance's ``web.users`` → ``web.voice.relay_user_unknown``.
    """
    name = request.headers.get(USER_HEADER, "")
    if not name or not name.strip():
        log.warning(
            "web.voice.relay_user_missing",
            detail="relay-mode voice but X-Alfred-User absent — rejecting (401)",
        )
        return None
    identity = resolve_identity_from_name(web_config, name)
    if identity is None:
        log.warning(
            "web.voice.relay_user_unknown",
            name=name.strip()[:64],
            detail="X-Alfred-User not in this instance's web.users — "
                   "rejecting (401)",
        )
        return None
    return identity


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_voice_offer(request: web.Request) -> web.StreamResponse:
    """POST /voice/offer — negotiate a voice session (contract §2).

    Echo pipeline (V0) negotiates the hear-yourself session directly; the
    assistant pipeline (V1) dispatches to :func:`_offer_assistant` after the
    shared auth + SDP validation below."""
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

    # Assistant pipeline (V1): bind to the caller's chat session + drive the
    # reply plane over a datachannel. Echo (V0) keeps session_key as a
    # logged-and-ignored forward-hook (byte-identical).
    if web_config.voice.pipeline == "assistant":
        return await _offer_assistant(request, identity, body, sdp, manager, web_config)

    raw_key = body.get("session_key")
    if isinstance(raw_key, str) and raw_key.strip():
        log.info(
            "web.voice.session_key_ignored",
            user=identity.user,
            length=min(len(raw_key), MAX_SESSION_KEY_CHARS),
            detail="echo pipeline — session_key accepted as a forward-hook, "
                   "capped, logged, and ignored (no chat coupling)",
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


async def _offer_assistant(
    request: web.Request, identity: Any, body: dict, sdp: str,
    manager: VoiceSessionManager, web_config: WebConfig,
) -> web.StreamResponse:
    """Assistant-pipeline offer: resolve the chat binding, build the turn
    driver, negotiate, and return the answer + additive ``chat_session_key``."""
    chat_session_key, err = _resolve_chat_binding(request, identity, body)
    if err is not None:
        return err

    from .voice_turns import TurnDeps, VoiceTurnDriver

    vid = uuid4().hex
    deps = TurnDeps(
        client=request.app[KEY_WEB_ANTHROPIC],
        state_mgr=request.app[KEY_WEB_STATE_MGR],
        talker_config=request.app[KEY_WEB_TALKER_CONFIG],
        web_config=web_config,
        system_prompt_provider=request.app[KEY_WEB_SYSTEM_PROVIDER],
        vault_context_str=request.app[KEY_WEB_VAULT_CTX],
        in_flight=request.app[KEY_WEB_INFLIGHT],
        identity=identity,
        chat_session_key=chat_session_key,
        reply_guidance=web_config.voice.reply_guidance,
    )
    driver = VoiceTurnDriver(
        deps, voice_session_id=vid, barge=request.app.get(_KEY_WEB_BARGE),
    )
    try:
        vid, answer_sdp = await manager.open_session(
            identity, sdp, turn_binding=driver, voice_session_id=vid,
        )
    except TooManySessions as exc:
        await driver.aclose(reason="offer_rejected")  # tear down the loop task
        return web.json_response(
            {"error": "too_many_sessions", "max_sessions": exc.max_sessions},
            status=429,
        )
    except VoiceOfferTimeout:
        await driver.aclose(reason="offer_rejected")
        return web.json_response({"error": "voice_offer_timeout"}, status=504)
    except NegotiationFailed:
        await driver.aclose(reason="offer_rejected")
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
        "chat_session_key": chat_session_key,
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
        "pipeline": voice.pipeline,   # echo | assistant (FE hard-fail-vs-benign)
        "tts": bool(available and getattr(manager, "tts_enabled", False)),
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
    ``False`` when voice is absent / disabled / mis-piped (nothing registered
    — the route table stays byte-identical). Relay mode no longer blocks the
    mount (multi-instance voice): both session and relay mount, guarded at
    request time by the ``WEB_CHAT_PEER`` pin. Called by ``register_web_routes``
    after the STT mount; ``KEY_WEB_CONFIG`` is already stashed on the app.

    Never raises — a voice-config problem disables the voice surface loudly
    but leaves the transport / chat / STT surface untouched.
    """
    voice = web_config.voice

    # Gate 1 — master enable.
    if not voice.enabled:
        log.info("web.voice.disabled", reason="not_enabled")
        return False

    # Gate 2 — auth-mode observability. Relay-mode voice is now PERMITTED
    # (multi-instance voice): both session-mode (Salem, home) and relay-mode
    # (Hypatia / KAL-LE switch-targets) mount /voice/*. The unconditional
    # WEB_CHAT_PEER pin in _require_voice_identity is the fail-closed guard the
    # original V0 relay-mode no-mount (W5) used to provide; relay-mode voice
    # re-resolves the asserted identity against THIS instance's own web.users.
    # Log the mode so mount-mode stays observable (ILB — relay-mount is
    # log-distinguishable from session-mount).
    mode = getattr(web_config.auth, "mode", "session") or "session"
    log.info("web.voice.auth_mode", mode=mode)

    # Gate 3 — pipeline enum (fail-closed on anything unknown).
    if voice.pipeline not in _KNOWN_PIPELINES:
        log.error(
            "web.voice.disabled",
            reason="unknown_pipeline",
            pipeline=voice.pipeline,
            detail="pipeline must be 'echo' or 'assistant' — refusing to "
                   "mount an unknown pipeline (fail-closed; daemon lives)",
        )
        return False

    # Gate 3b — the assistant pipeline needs a usable STT provider config;
    # fail closed (no mount, loud log) if it's absent / unknown / keyless.
    stt_worker_factory = None
    if voice.pipeline == "assistant":
        stt_worker_factory = _build_assistant_stt(
            voice, app.get(KEY_WEB_TALKER_CONFIG))
        if stt_worker_factory is None:
            return False  # _build_assistant_stt logged the specific reason

    # Gate 3c — V2 TTS talk-back (contract §1.13): an OPTIONAL enhancement on
    # the assistant pipeline. Absent / disabled / misconfigured tts DEGRADES to
    # text-only voice (returns None + a loud log) — it NEVER unmounts /voice/*
    # (unlike STT, which IS the product). Voice mounts regardless.
    tts_worker_factory = None
    if voice.pipeline == "assistant":
        tts_worker_factory = _build_assistant_tts(voice)

    # Gate 3d — V3 barge-in (§1.3): requires tts.enabled. Build the mount-
    # normalized settings (or None = disabled → V2 discard byte-identical, §1.12)
    # and stash for the per-request driver ctor. Mount-time ILB both ways.
    if voice.pipeline == "assistant":
        app[_KEY_WEB_BARGE] = _build_barge(
            voice, app.get(KEY_WEB_TALKER_CONFIG),
            tts_mounted=tts_worker_factory is not None,
        )

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
        manager = VoiceSessionManager(
            voice, stt_worker_factory=stt_worker_factory,
            tts_worker_factory=tts_worker_factory,
        )
        app[KEY_WEB_VOICE_MANAGER] = manager

        async def _voice_shutdown(_app: web.Application) -> None:
            mgr: VoiceSessionManager | None = _app.get(KEY_WEB_VOICE_MANAGER)
            if mgr is not None:
                await mgr.close_all(reason="daemon_shutdown")
                # aclose cancels + AWAITS the reaper and drains the detached
                # connection-state close tasks, so shutdown never leaves a
                # pending task ("Task was destroyed but it is pending").
                await mgr.aclose()

        # Fired by run_server's ``runner.cleanup()`` (transport/server.py) —
        # zero daemon.py changes.
        app.on_shutdown.append(_voice_shutdown)
        log.info(
            "web.voice.registered",
            max_sessions=voice.max_sessions,
            pipeline=voice.pipeline,
            stt_provider=voice.stt.provider if voice.pipeline == "assistant" else "",
            tts_provider=voice.tts.provider if tts_worker_factory is not None else "",
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


# ---------------------------------------------------------------------------
# Assistant pipeline (V1) — STT worker factory + chat-session binding
# ---------------------------------------------------------------------------


def _build_assistant_stt(voice: Any, talker_config: Any = None):
    """Validate the assistant STT config and return a worker factory, or
    ``None`` after logging the fail-closed no-mount reason.

    Fail-closed matrix (contract §2 / design §4): empty provider →
    ``stt_unconfigured``; unknown provider → ``unknown_stt_provider``;
    ``deepgram`` with an unresolved key → ``stt_key_missing``. ``fake`` always
    mounts (the keyless dev / test path). Clamps are logged as
    ``web.voice.stt.config_clamped``.

    ``talker_config`` is threaded through so shadow-capture (default-OFF STT
    test-series measurement) can resolve its Groq backend from the served
    ``talker_config.stt`` chain — the SAME creds/model/vocab the batch path
    uses. Shadow build failure NEVER unmounts STT (it degrades to no-shadow)."""
    stt = voice.stt
    provider = stt.provider
    if not provider:
        log.error(
            "web.voice.disabled", reason="stt_unconfigured",
            detail="pipeline: assistant requires a web.voice.stt block with a "
                   "provider (deepgram | fake)",
        )
        return None
    if provider not in _KNOWN_STT_PROVIDERS:
        log.error(
            "web.voice.disabled", reason="unknown_stt_provider",
            provider=provider,
            detail="unknown web.voice.stt.provider — fail-closed no-mount",
        )
        return None
    if provider == "deepgram" and _is_unresolved(stt.api_key):
        log.error(
            "web.voice.disabled", reason="stt_key_missing",
            detail="provider: deepgram but api_key (${DEEPGRAM_API_KEY}) is "
                   "empty/unresolved — fail-closed no-mount",
        )
        return None

    from .stt_stream import normalize_stt_settings

    stt_norm, warnings = normalize_stt_settings(stt)
    for warning in warnings:
        log.warning("web.voice.stt.config_clamped", detail=warning)
    # Bias the LIVE Deepgram stream with the SAME per-instance vocab the
    # batch/shadow STT path uses (clinic-capture Piece 2a). The live stream
    # previously ignored vocab entirely — yet it is the path the operator
    # actually dictates into; seeding vocab that never reaches it is theatre.
    # build_deepgram_url maps these to keyterm (nova-3) / keywords (nova-2).
    stt_norm.vocab_terms = list(
        getattr(getattr(talker_config, "stt", None), "vocab_terms", []) or []
    )
    # Intentionally-left-blank: 0 = no biasing (distinguishable from broken).
    log.info(
        "web.voice.stt.vocab_wired",
        terms=len(stt_norm.vocab_terms),
        keyword_param="keyterm" if str(stt_norm.model).startswith("nova-3")
        else "keywords",
    )
    shadow_factory = _build_stt_shadow(voice, talker_config, stt_norm)

    # Adaptive endpointing (default-OFF). Mount-clamp mirrors normalize_stt.
    from .endpoint_hold import normalize_endpoint_hold_settings

    endpoint_settings, ep_warnings = normalize_endpoint_hold_settings(
        getattr(stt, "endpoint_hold", None) or object())
    for warning in ep_warnings:
        log.warning("web.voice.stt.config_clamped", detail=warning)
    instance_name = getattr(
        getattr(talker_config, "instance", None), "name", "") or ""
    telemetry_dir = ""
    if endpoint_settings.enabled:
        telemetry_dir = getattr(
            getattr(stt, "endpoint_hold", None), "telemetry_dir",
            "./data/voice_calibration") or "./data/voice_calibration"
        log.info(
            "web.voice.stt.endpoint_hold_enabled",
            base_extend_ms=endpoint_settings.base_extend_ms,
            max_total_hold_ms=endpoint_settings.max_total_hold_ms,
            telemetry_dir=telemetry_dir, instance=instance_name,
        )
    else:
        # Intentionally-left-blank: idle distinguishable from broken.
        log.info("web.voice.stt.endpoint_hold_disabled", reason="not_enabled")

    # Clinical-safety denylist (Piece 2b): the SAME per-instance extra terms the
    # Telegram voice seam uses (talker.stt.hallucination_denylist), unioned onto
    # the universal default inside the worker's filter.
    stt_denylist = list(
        getattr(getattr(talker_config, "stt", None), "hallucination_denylist", [])
        or []
    )
    return _make_stt_worker_factory(
        stt_norm, shadow_factory,
        endpoint_settings=endpoint_settings,
        endpoint_telemetry_dir=telemetry_dir,
        instance_name=instance_name,
        stt_denylist=stt_denylist,
    )


def _driver_web_user(driver: Any) -> str:
    """The per-session ``identity.user`` (the endpoint-telemetry per-user key),
    read defensively off the driver's deps. Empty when absent (a public
    accessor on VoiceTurnDriver would be a clean follow-up)."""
    deps = getattr(driver, "_deps", None)
    ident = getattr(deps, "identity", None)
    return getattr(ident, "user", "") or ""


def _build_stt_shadow(voice: Any, talker_config: Any, stt_norm: Any):
    """Return a ``shadow_factory(vid) -> VoiceSttShadow`` or ``None``.

    DEFAULT-OFF: no ``shadow_capture`` block / ``enabled: false`` → ``None`` +
    an ILB ``shadow_disabled reason=not_enabled`` (idle distinguishable from
    broken). When enabled, resolve the Groq backend from ``talker_config.stt``
    (reuses the served creds/model/vocab). FAIL-CLOSED to no-shadow (never a
    shadow that 100%-errors) if there is no talker STT, no Groq engine, or no
    resolvable Groq key — always a loud ``shadow_disabled`` with the reason.
    Shadow is fully isolated, so a no-shadow degrade never affects the turn."""
    sc = getattr(voice.stt, "shadow_capture", None)
    if sc is None or not getattr(sc, "enabled", False):
        log.info("web.voice.stt.shadow_disabled", reason="not_enabled")
        return None

    tstt = getattr(talker_config, "stt", None)
    if tstt is None:
        log.error(
            "web.voice.stt.shadow_disabled", reason="no_talker_stt",
            detail="shadow_capture.enabled but no talker_config.stt to resolve "
                   "the Groq backend — degrade to no-shadow",
        )
        return None

    from alfred.telegram.stt_backends import build_chain

    groq = None
    try:
        for engine in build_chain(tstt):
            if getattr(engine, "backend_id", "") == "groq-whisper":
                groq = engine
                break
    except Exception as exc:  # noqa: BLE001 — never unmount over a shadow build
        log.error("web.voice.stt.shadow_disabled", reason="groq_build_failed",
                  error=str(exc)[:200])
        return None

    if groq is None or not getattr(groq, "api_key", "") \
            or _is_unresolved(groq.api_key):
        log.error(
            "web.voice.stt.shadow_disabled", reason="groq_key_missing",
            detail="shadow_capture.enabled but talker_config.stt has no "
                   "resolvable Groq key — degrade to no-shadow",
        )
        return None

    vocab = list(getattr(tstt, "vocab_terms", []) or [])
    instance_name = getattr(
        getattr(talker_config, "instance", None), "name", "") or ""
    log.info(
        "web.voice.stt.shadow_enabled",
        corpus_dir=sc.dir, groq_model=getattr(groq, "model", ""),
        vocab_terms=len(vocab), instance=instance_name,
    )

    def shadow_factory(vid: str):
        from .voice_stt_shadow import VoiceSttShadow

        return VoiceSttShadow(
            groq_backend=groq, vocab=vocab, corpus_dir=sc.dir,
            instance_name=instance_name, voice_session_id=vid,
            sample_rate=stt_norm.sample_rate,
        )

    return shadow_factory


def _make_stt_worker_factory(
    stt_norm: Any, shadow_factory: Any = None, *,
    endpoint_settings: Any = None, endpoint_telemetry_dir: str = "",
    instance_name: str = "", stt_denylist: "list[str] | None" = None,
):
    """Return ``factory(vid, driver, manager) -> VoiceSttWorker`` closing over
    the normalized STT config. The worker's callbacks bridge to the per-session
    turn driver (partials / utterances) and, fail-honest, close the session on
    a fatal STT error (contract §1.15). ``shadow_factory`` (or ``None``) mints a
    per-session shadow whose ``capture`` is wired as the worker's fire-and-forget
    hook — ``None`` ⇒ the live path is byte-identical (no tee / snapshot / call).
    ``endpoint_settings`` drives the adaptive turn-end hold (default-OFF ⇒ the
    seam is byte-identical); per-session endpoint telemetry is wired only when
    the feature is enabled."""

    def factory(vid: str, driver: Any, manager: Any):
        from .voice_stt import VoiceSttWorker

        provider = _make_stt_provider(stt_norm, vid)
        on_partial = driver.emit_stt_partial if driver is not None else None

        async def _drop_utterance(_text: str) -> None:
            # Defensive: assistant mode always wires a driver; if somehow
            # absent, an utterance has nowhere to go — do NOT drive a turn.
            return None

        on_utterance = driver.submit_utterance if driver is not None else _drop_utterance

        async def on_fatal(ev: Any) -> None:
            if driver is not None:
                driver.emit_stt_unavailable(ev.reason)
            manager.schedule_close(vid, reason="stt_failed")

        # Per-session shadow (default-OFF → shadow_factory is None → no hook).
        shadow = shadow_factory(vid) if shadow_factory is not None else None

        # Per-session endpoint telemetry (features-only). Wired ONLY when the
        # adaptive-endpoint feature is enabled (default-OFF → no hook → collect
        # nothing). web_user is the per-user calibration key.
        endpoint_telemetry = None
        if (endpoint_settings is not None and getattr(endpoint_settings, "enabled", False)
                and endpoint_telemetry_dir):
            from .voice_endpoint_telemetry import VoiceEndpointTelemetry

            endpoint_telemetry = VoiceEndpointTelemetry(
                corpus_dir=endpoint_telemetry_dir,
                web_user=_driver_web_user(driver),
                voice_session_id=vid,
                instance_name=instance_name,
            ).emit

        worker = VoiceSttWorker(
            provider=provider,
            voice_session_id=vid,
            on_utterance=on_utterance,
            on_partial=on_partial,
            on_fatal=on_fatal,
            min_utterance_chars=stt_norm.min_utterance_chars,
            sample_rate=stt_norm.sample_rate,
            hello_gate=True,  # §17b: connect/feed the provider only on DC hello
            shadow_capture=shadow.capture if shadow is not None else None,
            endpoint_settings=endpoint_settings,
            endpoint_telemetry=endpoint_telemetry,
            stt_denylist=stt_denylist,
        )
        # Release the hello-gate when the client's DC hello arrives. With no
        # driver (no feedback channel) open it immediately — a DC-less session
        # can never send hello, and blocking forever would just leak the worker
        # (the fatal-STT close path still applies).
        if driver is not None:
            driver.add_hello_callback(worker.allow_feed)
        else:
            worker.allow_feed()
        return worker

    return factory


def _make_stt_provider(stt_norm: Any, vid: str):
    if stt_norm.provider == "fake":
        from .stt_stream import FakeStreamProvider

        return FakeStreamProvider(voice_session_id=vid)
    from .stt_deepgram import DeepgramStreamProvider

    return DeepgramStreamProvider(stt_norm, voice_session_id=vid)


# ---------------------------------------------------------------------------
# Assistant pipeline (V2) — TTS worker factory (degrade-not-no-mount)
# ---------------------------------------------------------------------------


def _build_assistant_tts(voice: Any):
    """Validate the V2 TTS config and return a ``(playout, worker)`` factory,
    or ``None`` (voice STILL mounts — TTS is an enhancement, contract §1.13).

    Fail-open matrix — every miss returns None + a loud log, voice mounts
    text-only: disabled → ``not_enabled`` (SW6 mount-time signal); empty
    provider → ``tts_unconfigured``; unknown provider → ``unknown_tts_provider``
    (raw typo preserved); ``elevenlabs`` + unresolved key → ``tts_key_missing``.
    ``fake`` always mounts (keyless dev / test)."""
    tts = voice.tts
    if not tts.enabled:
        # SW6: log disabled-by-config AT MOUNT so off-by-config is
        # log-distinguishable from dead-by-error (latched_off) and healthy.
        log.info("web.voice.disabled_tts", reason="not_enabled")
        return None
    provider = tts.provider
    if not provider:
        log.error(
            "web.voice.disabled_tts", reason="tts_unconfigured",
            detail="web.voice.tts.enabled=true but no provider (elevenlabs | "
                   "fake) — voice mounts text-only",
        )
        return None
    if provider not in _KNOWN_TTS_PROVIDERS:
        log.error(
            "web.voice.disabled_tts", reason="unknown_tts_provider",
            provider=provider,
            detail="unknown web.voice.tts.provider — voice mounts text-only",
        )
        return None
    if provider == "elevenlabs" and _is_unresolved(tts.api_key):
        log.error(
            "web.voice.disabled_tts", reason="tts_key_missing",
            detail="provider: elevenlabs but api_key (${ELEVENLABS_API_KEY}) is "
                   "empty/unresolved — voice mounts text-only",
        )
        return None

    from .tts_stream import normalize_tts_settings

    tts_norm, warnings = normalize_tts_settings(tts)
    for warning in warnings:
        log.warning("web.voice.tts.config_clamped", detail=warning)
    return _make_tts_worker_factory(tts_norm)


def _make_tts_worker_factory(tts_norm: Any):
    """Return ``factory(vid, driver) -> (playout, worker)`` closing over the
    normalized TTS config. Binds the worker's speaking / fatal callbacks to the
    driver (NO ``schedule_close`` — TTS fatal degrades to text-only, §1.4)."""

    def factory(vid: str, driver: Any):
        from .voice_tts import TTSPlayoutSource, VoiceTtsWorker

        provider = _make_tts_provider(tts_norm, vid)
        playout = TTSPlayoutSource(
            source_rate=provider.output_rate,
            voice_session_id=vid,
            max_buffer_seconds=tts_norm.max_buffer_seconds,
        )
        worker = VoiceTtsWorker(
            provider=provider,
            playout=playout,
            voice_session_id=vid,
            on_speaking_started=driver.on_speaking_started if driver else None,
            on_speaking_done=driver.on_speaking_done if driver else None,
            on_fatal=driver.on_tts_fatal if driver else None,
            max_chars_per_turn=tts_norm.max_tts_chars_per_turn,
        )
        worker.start()
        return playout, worker

    return factory


def _make_tts_provider(tts_norm: Any, vid: str):
    if tts_norm.provider == "fake":
        from .tts_stream import FakeTTSProvider

        return FakeTTSProvider(voice_session_id=vid)
    from .tts_elevenlabs import ElevenLabsStreamProvider

    return ElevenLabsStreamProvider(tts_norm, voice_session_id=vid)


# ---------------------------------------------------------------------------
# V3 barge-in — mount gate + settings (§1.3)
# ---------------------------------------------------------------------------


def _build_barge(voice: Any, talker_config: Any, *, tts_mounted: bool):
    """Return the mount-normalized ``BargeSettings`` (or ``None`` = disabled →
    V2 discard byte-identical). Requires ``tts.enabled``; a barge-enabled but
    tts-unusable mount disables barge with a loud log. Mount-time ILB both ways
    (contract §1.3). Config clamps + list-cap drops log ``config_clamped``."""
    from .barge_in import normalize_barge_settings

    bcfg = voice.tts.barge_in
    if not bcfg.enabled:
        log.info("web.voice.barge.disabled", reason="not_enabled")
        return None
    if not tts_mounted:
        log.error(
            "web.voice.barge.disabled", reason="tts_unavailable",
            detail="barge_in requires a usable web.voice.tts block — disabled",
        )
        return None
    instance_name = getattr(getattr(talker_config, "instance", None), "name", "") or ""
    settings, warnings = normalize_barge_settings(bcfg, instance_name=instance_name)
    for warning in warnings:
        log.warning("web.voice.barge.config_clamped", detail=warning)
    log.info(
        "web.voice.barge.enabled",
        too_early_ms=settings.too_early_ms,
        echo_threshold=settings.echo_threshold,
        interrupt_phrases=len(settings.interrupt_phrases),
    )
    return settings


def _resolve_chat_binding(request: web.Request, identity: Any, body: dict):
    """Resolve the voice session's chat binding (assistant pipeline, §1.14).

    Returns ``(chat_session_key, None)`` on success or ``(None, response)``
    with a fail-closed 400. Explicit ``session_key`` must equal the caller's
    active chat session (else ``bad_session_key`` — single body for
    missing-vs-mismatch, no existence leak); absent → reuse the active session,
    else auto-open one (NEVER close-then-open — that would 404 the chat tab)."""
    state_mgr = request.app[KEY_WEB_STATE_MGR]
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]
    owner = identity.synthetic_chat_id

    raw_key = body.get("session_key")
    if isinstance(raw_key, str) and raw_key.strip():
        active = state_mgr.get_active(owner)
        if active is None or active.get("session_id") != raw_key:
            log.info("web.voice.bad_session_key", user=identity.user)
            return None, web.json_response({"error": "bad_session_key"}, status=400)
        bind_mode = "explicit"
        key = raw_key
    else:
        active = state_mgr.get_active(owner)
        if active is not None and active.get("session_id"):
            key = active["session_id"]
            bind_mode = "reused"
        else:
            from alfred.telegram.session import open_session

            session_obj = open_session(
                state_mgr, owner, model=talker_config.anthropic.model,
            )
            key = session_obj.session_id
            bind_mode = "opened"
    log.info(
        "web.voice.session_bound",
        user=identity.user, bind_mode=bind_mode, session_key_prefix=key[:8],
    )
    return key, None
