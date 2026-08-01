"""Brief-audio route — the interruptible player's on-demand TTS (Phase C3a).

    GET /web/brief/audio[?speed=<0.7-1.2>] → audio/mpeg  (or an ILB state)

Renders the day's spooled narration to speech ON DEMAND (the first open per
brief+speed), caches it, and streams the mp3. Replay/scrub cost ZERO credits
(cache hit → no synth). Mounted on the transport app by ``register_web_routes``,
so Layer-1 peer-token auth already gates it; the handler mirrors #30
(``routes_brief.py``) EXACTLY:

1. **Peer-pin FIRST** — ``transport_peer`` must be the dedicated chat ``web``
   peer (:data:`alfred.web.auth.WEB_CHAT_PEER`), BEFORE identity or any read.
   Fail-closed 401 + a logged ``web.brief_audio.wrong_peer`` (an ingest token
   shares ``allowed_clients:[web]`` and must not reach the render). NOT a new
   asserted-identity path — reuses the established peer + session identity.
2. **Identity** — :func:`resolve_web_identity`, fail-closed 401.
3. **Read** the spooled narration (the brief daemon spools it, no synth); absent
   / empty → the intentionally-left-blank ``200 {state:"no_brief"}`` (never a
   404 — the FE offers the deck instead).
4. **Cache** per (brief_date, narration-content-hash, speed); HIT streams bytes
   with NO synth (the credit guard).
5. **Synth** on miss via ``telegram.tts.synthesize`` (the shared batch primitive
   — ratified reuse) using the talker's TtsConfig; no key → ``200
   {state:"tts_not_configured"}`` (honest disabled-audio, FE renders text-along);
   a transient synth error → 502 ``tts_synthesis_failed`` (retryable).
"""

from __future__ import annotations

import json

from aiohttp import web

from .auth import WEB_CHAT_PEER, resolve_web_identity
from .brief_audio_cache import (
    audio_cache_key,
    narration_content_hash,
    read_cached_audio,
    write_cached_audio,
)
from .config import WebConfig
from .keys import KEY_WEB_CONFIG, KEY_WEB_DATA_DIR, KEY_WEB_TALKER_CONFIG
from .outbound_store import read_latest
from .utils import get_logger

log = get_logger(__name__)

# The outbound_store spool kind the brief daemon writes the narration JSON under
# (in the markdown slot — outbound_store is payload-agnostic). MUST match
# ``brief.daemon._spool_brief_narration``.
NARRATION_KIND = "brief_narration"

# ElevenLabs speed range (matches telegram/tts.py's contract); out-of-range /
# missing → None (provider default).
_SPEED_MIN, _SPEED_MAX = 0.7, 1.2


def _resolve_speed(raw: str | None) -> float | None:
    """Parse + clamp the ``speed`` query param to the valid TTS range, or None
    (provider default) when absent/unparseable. The FE passes the operator's
    /speed pref; we clamp defensively rather than 400 on a stray value."""
    if not raw:
        return None
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        return None
    return max(_SPEED_MIN, min(_SPEED_MAX, speed))


def _read_spooled_narration_dict(data_dir) -> dict | None:
    """Return the parsed narration payload dict from the spool, or None when
    nothing is spooled / the payload is corrupt. The dict is the
    ``BriefNarration.to_dict`` shape ({brief_date, segments, total_words,
    empty})."""
    latest = read_latest(data_dir, NARRATION_KIND)
    if latest is None:
        return None
    try:
        payload = json.loads(latest.get("markdown") or "")
    except (ValueError, TypeError, AttributeError):
        log.warning("web.brief_audio.corrupt_narration")
        return None
    if not isinstance(payload, dict):
        return None
    # Backfill brief_date from the spool sidecar if the payload omitted it.
    if not payload.get("brief_date"):
        payload["brief_date"] = str(latest.get("date") or "")
    return payload


def _absent_narration_state(data_dir) -> str:
    """Distinguish the two ILB absences (house rule: idle vs broken must not
    collapse):
      * ``no_brief`` — no brief spooled today (the #30 ``brief`` spool is absent
        too) → FE offers the deck.
      * ``narration_unavailable`` — the brief EXISTS but its narration spool is
        missing / failed / empty (spool crashed, or narration had nothing
        speakable) → FE says "brief exists, audio unavailable" + offers the
        brief page. Checked against the #30 brief spool's presence.
    """
    brief_present = read_latest(data_dir, "brief") is not None
    return "narration_unavailable" if brief_present else "no_brief"


def _spooled_full_text(payload: dict) -> tuple[str, str] | None:
    """From a narration payload dict, return ``(brief_date, full_text)`` (the
    single-shot synth input) or None when empty / dateless."""
    segments = payload.get("segments") or []
    full_text = "\n\n".join(
        str(s.get("text") or "") for s in segments if isinstance(s, dict) and s.get("text")
    ).strip()
    brief_date = str(payload.get("brief_date") or "")
    if not full_text or not brief_date:
        return None
    return brief_date, full_text


async def _handle_brief_audio(request: web.Request) -> web.StreamResponse:
    """GET /web/brief/audio — render/serve the day's briefing audio."""
    # (1) Peer-pin — BEFORE identity or any read (CLAUDE.md peer-pin rule).
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.brief_audio.wrong_peer",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            detail="brief-audio requires the dedicated chat 'web' peer token — "
                   "refusing another peer (e.g. web_ingest) — 401",
        )
        return web.json_response({"error": "wrong_peer"}, status=401)

    # (2) Identity.
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    # (3) Read the spooled narration.
    data_dir = request.app.get(KEY_WEB_DATA_DIR)
    payload = _read_spooled_narration_dict(data_dir)
    loaded = _spooled_full_text(payload) if payload is not None else None
    if loaded is None:
        state = _absent_narration_state(data_dir)  # no_brief vs narration_unavailable
        log.info("web.brief_audio.absent", user=identity.user, state=state)
        return web.json_response({"state": state})  # ILB 200
    brief_date, full_text = loaded

    speed = _resolve_speed(request.query.get("speed"))

    # (4) Cache — a HIT is the credit guard (no synth).
    content_hash = narration_content_hash(full_text)
    key = audio_cache_key(brief_date, content_hash, speed)
    cached = read_cached_audio(data_dir, key)
    if cached is not None:
        return web.Response(
            body=cached, content_type="audio/mpeg",
            headers={"X-Brief-Audio-Cache": "hit"},
        )

    # (5) Synth on miss — reuse the shared batch primitive (ratified).
    talker_config = request.app[KEY_WEB_TALKER_CONFIG]
    tts_cfg = getattr(talker_config, "tts", None)
    if tts_cfg is None or not getattr(tts_cfg, "api_key", ""):
        log.info("web.brief_audio.tts_not_configured", brief_date=brief_date)
        return web.json_response({"state": "tts_not_configured"})  # ILB 200

    from alfred.telegram.tts import TtsError, TtsNotConfigured, synthesize

    try:
        audio = await synthesize(full_text, tts_cfg, speed=speed)
    except TtsNotConfigured:
        log.info("web.brief_audio.tts_not_configured", brief_date=brief_date)
        return web.json_response({"state": "tts_not_configured"})
    except TtsError as exc:
        log.warning("web.brief_audio.synth_failed", brief_date=brief_date, error=str(exc))
        return web.json_response({"error": "tts_synthesis_failed"}, status=502)

    write_cached_audio(data_dir, key, audio)
    log.info(
        "web.brief_audio.rendered",
        brief_date=brief_date, bytes=len(audio),
        speed=speed if speed is not None else "default",
    )
    return web.Response(
        body=audio, content_type="audio/mpeg",
        headers={"X-Brief-Audio-Cache": "miss"},
    )


async def _handle_brief_narration(request: web.Request) -> web.StreamResponse:
    """GET /web/brief/narration — the sectioned narration JSON the player renders
    slides from (no synth, no cache — a cheap read of the daemon-spooled model).

    Same peer-pin + identity as the audio route. Returns the ``BriefNarration``
    dict ({brief_date, segments, total_words, empty}); an absent / corrupt spool
    is the intentionally-left-blank ``200 {state:"no_brief"}`` (never a 404)."""
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.brief_narration.wrong_peer", reason="wrong_peer",
            peer=peer or "(none)", expected=WEB_CHAT_PEER,
        )
        return web.json_response({"error": "wrong_peer"}, status=401)

    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    data_dir = request.app.get(KEY_WEB_DATA_DIR)
    payload = _read_spooled_narration_dict(data_dir)
    if payload is None:
        state = _absent_narration_state(data_dir)  # no_brief vs narration_unavailable
        log.info("web.brief_narration.absent", user=identity.user, state=state)
        return web.json_response({"state": state})  # ILB 200
    return web.json_response(payload)


def register_brief_audio_routes(app: web.Application) -> None:
    """Mount ``GET /web/brief/audio`` + ``GET /web/brief/narration`` (called by
    ``register_web_routes``; web/talker config + data_dir already stashed)."""
    app.router.add_get("/web/brief/audio", _handle_brief_audio)
    app.router.add_get("/web/brief/narration", _handle_brief_narration)


__all__ = ["NARRATION_KIND", "register_brief_audio_routes"]
