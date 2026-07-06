"""WebRTC voice — session manager + media seam (echo + assistant pipelines).

The ``/voice/*`` routes (``routes_voice``) are the wire surface; this module
owns the server side of the WebRTC negotiation and the live-session
registry. V0 ``echo`` = "audio transport up": the mic is echoed straight back.
V1 ``assistant`` = the mic is tapped for streaming STT (a SECOND
``relay.subscribe`` feeding a ``VoiceSttWorker``) while silence is sent
outbound; end-of-utterance text drives ``run_turn_streaming`` via a
``VoiceTurnDriver`` and the reply streams back over the ``voice`` datachannel.
The pipeline is selected purely by whether ``stt_worker_factory`` /
``turn_binding`` are wired — echo stays byte-identical when they are not.

Design constraints (contract §1, §4):

* **ZERO top-level aiortc imports.** ``aiortc`` (which drags in ``av`` /
  ffmpeg, ~100 MB RSS) is imported LAZILY inside :meth:`VoiceSessionManager.
  open_session` on the first offer, so an instance that never enables voice
  — and the whole unconditional test suite — pays no import cost.
  :func:`aiortc_available` probes via ``importlib.util.find_spec`` (no
  import). The pipeline track subclasses aiortc's ``MediaStreamTrack``, so
  its class body is built lazily too (:func:`_voice_pipeline_track`).

* **Injectable seams for aiortc-free unit tests.** ``pc_factory`` (builds the
  RTCPeerConnection), ``description_factory`` (wraps the remote offer SDP —
  the ONLY aiortc-typed construction on the negotiate path), and ``clock``
  (monotonic source, for deterministic reaper tests) all default to the real
  aiortc path but accept fakes. The manager's cap / reservation / replacement
  / reaper / close logic is therefore exercised WITHOUT aiortc installed.

* **The cap counts in-flight negotiations (security W2).** A slot is reserved
  BEFORE the RTCPeerConnection is built and released on failure, so a flood
  of concurrent offers cannot bypass ``max_sessions``. The whole negotiation
  is wrapped in ``asyncio.timeout(offer_timeout_seconds)`` → a wedged
  negotiation frees its slot (504 at the handler).

* **First-audio-track-only echo (security W3).** Exactly the first audio
  track is echoed via ``MediaRelay().subscribe(track)`` → a
  :class:`VoicePipelineTrack` outbound source. The relay + pipeline-track
  seam is deliberate forward-compat: V1 (Deepgram STT) adds a SECOND
  ``relay.subscribe(track)`` consumer feeding the STT tap; V2 (TTS) swaps the
  source feeding ``recv()`` — neither touches the wire contract. Additional
  tracks are logged and NOT echoed.

Never logs SDP bodies (security W6) — only byte-size + m-line count.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebVoiceConfig
    from .identity import WebIdentity

log = get_logger(__name__)


# Bound each ``pc.close()`` so a wedged RTCPeerConnection cannot stall the
# reaper sweep or (via the on_shutdown hook) daemon shutdown.
_PC_CLOSE_TIMEOUT_S = 5.0


# --- Exceptions ------------------------------------------------------------


class TooManySessions(Exception):
    """The ``max_sessions`` cap (incl. in-flight negotiations) is full."""

    def __init__(self, max_sessions: int) -> None:
        super().__init__(f"voice session cap reached ({max_sessions})")
        self.max_sessions = max_sessions


class NegotiationFailed(Exception):
    """The WebRTC offer/answer negotiation raised."""


class VoiceOfferTimeout(Exception):
    """Negotiation exceeded ``offer_timeout_seconds``."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"voice offer negotiation timed out after {timeout_s}s")
        self.timeout_s = timeout_s


# --- Availability probe ----------------------------------------------------


def aiortc_available() -> tuple[bool, str]:
    """Return ``(ok, reason)`` for aiortc availability WITHOUT importing it.

    ``importlib.util.find_spec`` resolves the module spec without running any
    import side effects (no av/ffmpeg load). ``reason`` is ``""`` when
    available, else ``"aiortc_missing"`` — the mount-time gate uses it to
    decide 503-mode vs full-mount.
    """
    try:
        present = importlib.util.find_spec("aiortc") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        present = False
    return (True, "") if present else (False, "aiortc_missing")


# --- advertised_ip SDP rewrite (pure function, aiortc-free) -----------------

# Matches ``a=candidate:<foundation> <component> <transport> <priority>
# <connection-address> <port> typ host ...`` — group 2 is the connection
# address to rewrite. Only ``typ host`` candidates carry the box's local
# address; srflx / relay candidates already carry a reflexive / public
# address and are left untouched. ``re.MULTILINE`` so ``$`` (and thus the
# trailing ``\r`` of an SDP ``\r\n`` line) stays inside the captured tail.
_HOST_CANDIDATE_RE = re.compile(
    r"^(a=candidate:\S+ \d+ \S+ \d+ )(\S+)( \d+ typ host\b.*)$",
    re.MULTILINE,
)


def rewrite_answer_sdp_advertised_ip(sdp: str, advertised_ip: str) -> str:
    """Rewrite host-candidate connection addresses to ``advertised_ip``.

    Pure function over the answer SDP — unit-testable without aiortc. Used
    for 1:1-NAT deploys where the box's on-interface address is private but a
    public IP forwards to it (aiortc / aioice has no ``nat_1to1`` knob).
    A no-op when ``advertised_ip`` is empty. Preserves ``\\r\\n`` line
    endings (the tail group captures the trailing ``\\r``).
    """
    if not advertised_ip:
        return sdp
    return _HOST_CANDIDATE_RE.sub(
        lambda m: f"{m.group(1)}{advertised_ip}{m.group(3)}", sdp,
    )


def _count_mlines(sdp: str) -> int:
    """Count ``m=`` media lines in an SDP (never logs the body itself)."""
    return sum(1 for line in sdp.splitlines() if line.startswith("m="))


# --- Lazy pipeline-track class (subclasses aiortc's MediaStreamTrack) -------

_PIPELINE_TRACK_CLS: Any = None


def _voice_pipeline_track(source: Any) -> Any:
    """Wrap ``source`` as the server's outbound audio track (echo seam).

    Lazily defines + caches a ``MediaStreamTrack`` subclass whose ``recv()``
    pulls frames from a pluggable ``source`` — V0's source is a
    ``MediaRelay().subscribe(inbound_track)`` passthrough (echo). The lazy
    class body keeps this module free of any top-level aiortc import.

    V1 (assistant): the outbound source is :func:`_silence_source` (the mic is
    tapped for STT via a SECOND ``relay.subscribe(track)`` — you MUST relay, a
    raw track has exactly one consumer, aiortc#175). The m-line + RTCRtpSender
    are kept alive precisely so V2 can swap the source with NO renegotiation.

    V2 (TTS) SOURCE-SWAP HAZARD: the TTS source that replaces ``recv()`` MUST
    continue the frame ``pts`` MONOTONICALLY across the swap (or be wrapped in
    a pts-normalizing shim) — a pts discontinuity desyncs the RTP timestamp /
    jitter buffer. Likewise a sample-format/rate CHANGE mid-stream can raise in
    the Opus encoder / any downstream resampler; normalize to the negotiated
    format before the swap. Neither the swap nor V1's STT tap touches the
    offer/answer wire contract.
    """
    global _PIPELINE_TRACK_CLS
    if _PIPELINE_TRACK_CLS is None:
        from aiortc.mediastreams import MediaStreamTrack

        class VoicePipelineTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(self, src: Any) -> None:
                super().__init__()
                self._source = src

            async def recv(self) -> Any:
                return await self._source.recv()

        _PIPELINE_TRACK_CLS = VoicePipelineTrack
    return _PIPELINE_TRACK_CLS(source)


def _silence_source() -> Any:
    """A stock aiortc silence generator (20 ms silent s16 frames, real-time
    paced) — the V1 assistant-pipeline outbound source. Lazy import keeps this
    module aiortc-free at import time. Wrapped in a :func:`_voice_pipeline_track`
    so V2 TTS is a source swap (see that function's swap-hazard note)."""
    from aiortc.mediastreams import AudioStreamTrack

    return AudioStreamTrack()


# --- Session record --------------------------------------------------------


@dataclass
class VoiceSession:
    """One live WebRTC voice session, keyed by ``voice_session_id``.

    ``pc`` is typed ``Any`` so the module imports without aiortc. ``owner``
    (the caller's ``synthetic_chat_id``) is the ownership key for the
    owner-bound close + the ``yours``-scoped config listing.
    """

    voice_session_id: str
    user: str
    owner: int
    pc: Any
    created_mono: float
    last_state_change: float
    connection_state: str = "new"
    connected_once: bool = False
    # Monotonic time of the FIRST "connected" transition — the base for the
    # assistant-pipeline no-speech reaper (§1.6). 0.0 until connected.
    connected_at: float = 0.0
    # Holds strong refs (MediaRelay, outbound track, stt_worker, turn_driver)
    # so they aren't GC'd for the session's lifetime.
    keepalive: dict[str, Any] = field(default_factory=dict)


# --- Manager ---------------------------------------------------------------


class VoiceSessionManager:
    """Registry + lifecycle for V0 WebRTC voice sessions.

    One per app (stashed at ``KEY_WEB_VOICE_MANAGER``). Owns the cap /
    in-flight reservation / same-user replacement / reaper / shutdown-drain.
    """

    def __init__(
        self,
        voice_config: "WebVoiceConfig",
        *,
        pc_factory: Callable[[], Any] | None = None,
        description_factory: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stt_worker_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._config = voice_config
        self.max_sessions = int(voice_config.max_sessions)
        self.offer_timeout = float(voice_config.offer_timeout_seconds)
        self.connect_deadline = float(voice_config.connect_deadline_seconds)
        self.idle_timeout = float(voice_config.idle_timeout_seconds)
        self.max_session_seconds = float(voice_config.max_session_seconds)
        self.no_speech_close_s = float(
            getattr(voice_config, "no_speech_close_s", 600)
        )
        self.reaper_interval = float(voice_config.reaper_interval_seconds)
        self.stun_servers = list(voice_config.ice.stun_servers)
        self.advertised_ip = voice_config.ice.advertised_ip
        # None = V0 echo (byte-identical); set = V1 assistant STT tap.
        self._stt_worker_factory = stt_worker_factory

        self._pc_factory = pc_factory or self._default_pc_factory
        self._description_factory = (
            description_factory or self._default_description_factory
        )
        self._clock = clock

        self._sessions: dict[str, VoiceSession] = {}
        # In-flight negotiation count — reserved BEFORE the pc is built so
        # the cap can't be bypassed by concurrent offers (security W2).
        self._in_flight = 0
        self._reaper_task: asyncio.Task | None = None
        self._reaper_started = False
        self._aiortc_import_logged = False
        # Strong refs to detached connection-state close tasks (GC guard).
        self._bg_tasks: set[asyncio.Task] = set()

    # -- default (real-aiortc) factories ------------------------------------

    def _default_pc_factory(self) -> Any:
        """Build a real RTCPeerConnection honoring the STUN config."""
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        if self.stun_servers:
            config = RTCConfiguration(
                iceServers=[RTCIceServer(urls=list(self.stun_servers))]
            )
            return RTCPeerConnection(configuration=config)
        return RTCPeerConnection()

    def _default_description_factory(self, sdp: str, kind: str) -> Any:
        """Wrap an offer SDP as an ``RTCSessionDescription`` (lazy import)."""
        from aiortc import RTCSessionDescription

        return RTCSessionDescription(sdp=sdp, type=kind)

    # -- introspection ------------------------------------------------------

    def active_count(self) -> int:
        return len(self._sessions)

    def sessions_for(self, owner: int) -> list[VoiceSession]:
        return [s for s in self._sessions.values() if s.owner == owner]

    def age_seconds(self, session: VoiceSession) -> float:
        return self._clock() - session.created_mono

    def reaper_alive(self) -> bool:
        return self._reaper_task is not None and not self._reaper_task.done()

    # -- open ---------------------------------------------------------------

    async def open_session(
        self, identity: "WebIdentity", offer_sdp: str,
        *, turn_binding: Any = None, voice_session_id: str | None = None,
    ) -> tuple[str, str]:
        """Negotiate a new session. Returns ``(voice_session_id, answer_sdp)``.

        ``turn_binding`` (assistant pipeline) is a pre-built ``VoiceTurnDriver``
        stashed BEFORE ``setRemoteDescription`` so the on_track / on_datachannel
        handlers (which fire synchronously during negotiation, before the
        :class:`VoiceSession` exists) can reach it via the keepalive dict.

        Raises :class:`TooManySessions` (→ 429), :class:`VoiceOfferTimeout`
        (→ 504), or :class:`NegotiationFailed` (→ 502).
        """
        # 1. Same-user replacement — a page reload / re-offer closes the
        # caller's previous session first (frees their slot, prevents a
        # self-DoS at the cap). Runs BEFORE the cap check.
        for existing in self.sessions_for(identity.synthetic_chat_id):
            await self.close(existing.voice_session_id, reason="replaced")
            log.info(
                "web.voice.session.replaced",
                voice_session_id=existing.voice_session_id,
                user=identity.user,
                detail="same-user re-offer — closed prior session",
            )

        # 2. Cap (counts in-flight negotiations too — security W2).
        if self.active_count() + self._in_flight >= self.max_sessions:
            log.warning(
                "web.voice.reject",
                reason="too_many_sessions",
                user=identity.user,
                active=self.active_count(),
                in_flight=self._in_flight,
                max_sessions=self.max_sessions,
            )
            raise TooManySessions(self.max_sessions)

        # 3. Reserve the slot, then build + negotiate.
        self._in_flight += 1
        vid = voice_session_id or uuid4().hex
        pc = self._pc_factory()
        keepalive: dict[str, Any] = {}
        if turn_binding is not None:
            keepalive["turn_driver"] = turn_binding
        try:
            self._wire_media(pc, vid, keepalive, turn_driver=turn_binding)
            self._wire_connection_state(pc, vid)
            try:
                async with asyncio.timeout(self.offer_timeout):
                    await pc.setRemoteDescription(
                        self._description_factory(offer_sdp, "offer")
                    )
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                await self._safe_close_pc(pc)
                log.warning(
                    "web.voice.session.fail",
                    reason="offer_timeout",
                    user=identity.user,
                    timeout_s=self.offer_timeout,
                )
                raise VoiceOfferTimeout(self.offer_timeout) from exc

            answer_sdp = pc.localDescription.sdp
            if self.advertised_ip:
                answer_sdp = rewrite_answer_sdp_advertised_ip(
                    answer_sdp, self.advertised_ip,
                )

            now = self._clock()
            session = VoiceSession(
                voice_session_id=vid,
                user=identity.user,
                owner=identity.synthetic_chat_id,
                pc=pc,
                created_mono=now,
                last_state_change=now,
                keepalive=keepalive,
            )
            self._sessions[vid] = session
            self._ensure_reaper()
            log.info(
                "web.voice.session.open",
                voice_session_id=vid,
                user=identity.user,
                sdp_bytes=len(offer_sdp.encode("utf-8")),
                mlines=_count_mlines(offer_sdp),
                active=self.active_count(),
                max_sessions=self.max_sessions,
            )
            return vid, answer_sdp
        except VoiceOfferTimeout:
            raise
        except Exception as exc:  # noqa: BLE001 — any negotiation error → 502
            await self._safe_close_pc(pc)
            log.warning(
                "web.voice.session.fail",
                reason="negotiation_failed",
                user=identity.user,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise NegotiationFailed(str(exc)) from exc
        finally:
            self._in_flight -= 1

    # -- media + state wiring -----------------------------------------------

    def _wire_media(
        self, pc: Any, vid: str, keepalive: dict[str, Any],
        *, turn_driver: Any = None,
    ) -> None:
        """Register the ``on('track')`` handler (fires during
        setRemoteDescription, so the outbound sender lands in the answer).

        Echo (``stt_worker_factory is None``) loops the mic back. Assistant
        taps the mic for STT via a SECOND ``relay.subscribe`` and sends silence
        outbound (the V2 TTS source-swap seam). When ``turn_driver`` is present
        the ``voice`` datachannel is also wired for the reply plane."""

        @pc.on("track")
        def on_track(track: Any) -> None:  # pragma: no cover - needs aiortc media
            log.info("web.voice.track_received", voice_session_id=vid, kind=track.kind)
            if track.kind != "audio":
                log.info(
                    "web.voice.track_ignored",
                    voice_session_id=vid, kind=track.kind, reason="non_audio",
                )
                return
            if keepalive.get("audio_handled"):
                # First-audio-track-only (security W3).
                log.info(
                    "web.voice.track_ignored",
                    voice_session_id=vid, kind=track.kind,
                    reason="additional_audio_track",
                )
                return
            keepalive["audio_handled"] = True

            from aiortc.contrib.media import MediaRelay

            relay = MediaRelay()
            keepalive["relay"] = relay

            if self._stt_worker_factory is not None:
                # Assistant: tap the mic for STT; send silence outbound (keeps
                # the m-line/sender alive for the V2 TTS source swap). The mic
                # is NOT echoed.
                stt_input = relay.subscribe(track)
                outbound = _voice_pipeline_track(_silence_source())
                pc.addTrack(outbound)
                keepalive["outbound"] = outbound
                worker = self._stt_worker_factory(
                    vid, keepalive.get("turn_driver"), self,
                )
                worker.start(stt_input)
                keepalive["stt_worker"] = worker
                log.info("web.voice.assistant_tap", voice_session_id=vid)
            else:
                # Echo (V0): loop the mic back to the browser.
                outbound = _voice_pipeline_track(relay.subscribe(track))
                pc.addTrack(outbound)
                keepalive["outbound"] = outbound

            @track.on("ended")
            def on_ended() -> None:
                log.info("web.voice.track_ended", voice_session_id=vid, kind=track.kind)

        if turn_driver is not None:
            @pc.on("datachannel")
            def on_datachannel(channel: Any) -> None:  # pragma: no cover - needs aiortc
                label = getattr(channel, "label", "")
                if label != "voice" or keepalive.get("dc_attached"):
                    log.info(
                        "web.voice.dc_ignored",
                        voice_session_id=vid, label=label,
                        reason="not_voice_label" if label != "voice"
                        else "additional_channel",
                    )
                    return
                keepalive["dc_attached"] = True
                turn_driver.attach_channel(channel)

                @channel.on("message")
                def on_message(raw: Any) -> None:
                    turn_driver.on_client_message(raw)

    def _wire_connection_state(self, pc: Any, vid: str) -> None:
        """Register ``on('connectionstatechange')`` → state log + auto-close."""

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            state = getattr(pc, "connectionState", "unknown")
            session = self._sessions.get(vid)
            if session is not None:
                session.connection_state = state
                session.last_state_change = self._clock()
                if state == "connected" and not session.connected_once:
                    session.connected_once = True
                    session.connected_at = self._clock()
            log.info("web.voice.session.state", voice_session_id=vid, state=state)
            if state == "connected" and session is not None:
                log.info(
                    "web.voice.session.connected",
                    voice_session_id=vid,
                    elapsed_ms=int(
                        (self._clock() - session.created_mono) * 1000
                    ),
                )
            elif state == "failed":
                self._spawn(self.close(vid, reason="connection_failed"))
            elif state == "closed":
                self._spawn(self.close(vid, reason="peer_closed"))

    def _spawn(self, coro: Any) -> None:
        """Fire-and-retain a detached close task (GC guard, done-discard)."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def schedule_close(self, voice_session_id: str, reason: str) -> None:
        """Detached, fail-honest close (V1 STT fatal → reason=stt_failed). The
        worker's on_fatal wiring calls this; the manager owns the task ref."""
        self._spawn(self.close(voice_session_id, reason=reason))

    # -- close --------------------------------------------------------------

    async def close(self, voice_session_id: str, reason: str) -> bool:
        """Close + deregister a session. Idempotent — a missing id is a
        no-op returning ``False`` (the pc is NOT re-closed)."""
        session = self._sessions.pop(voice_session_id, None)
        if session is None:
            return False
        # Drain the assistant-pipeline STT worker + turn driver BEFORE the pc
        # (bounded + swallowed, mirroring _safe_close_pc) so a wedged worker /
        # driver can't stall teardown. No-ops in echo mode (keys absent).
        await self._drain_pipeline(session, reason)
        await self._safe_close_pc(session.pc)
        log.info(
            "web.voice.session.close",
            voice_session_id=voice_session_id,
            user=session.user,
            reason=reason,
            age_s=int(self._clock() - session.created_mono),
            last_state=session.connection_state,
            active=self.active_count(),
        )
        return True

    async def close_owned(
        self, voice_session_id: str, owner: int, reason: str,
    ) -> bool:
        """Owner-bound close (contract §1.5).

        Returns ``True`` only when the caller owns a LIVE session with that
        id. Unknown id, already-closed, or another user's id all return
        ``False`` — the handler renders them indistinguishably as
        ``{closed:false, reason:not_found}`` (no existence leak). A
        wrong-owner attempt is logged for observability but not revealed.
        """
        session = self._sessions.get(voice_session_id)
        if session is None:
            return False
        if session.owner != owner:
            log.warning(
                "web.voice.close_wrong_user",
                voice_session_id=voice_session_id,
                detail="close request for a session owned by another user — "
                       "returning not_found (no existence leak)",
            )
            return False
        return await self.close(voice_session_id, reason=reason)

    async def close_all(self, reason: str) -> None:
        """Close every live session (the on_shutdown drain)."""
        vids = list(self._sessions.keys())
        if not vids:
            # Intentionally-left-blank: an empty drain is a deliberate state,
            # observably distinct from the hook not firing.
            log.info("web.voice.close_all_empty", reason=reason)
            return
        log.info("web.voice.close_all", reason=reason, count=len(vids))
        await asyncio.gather(
            *(self.close(vid, reason=reason) for vid in vids),
            return_exceptions=True,
        )

    async def _safe_close_pc(self, pc: Any) -> None:
        """``pc.close()`` bounded by a timeout, exceptions swallowed+logged so
        a wedged pc can't stall the reaper or daemon shutdown."""
        try:
            await asyncio.wait_for(pc.close(), timeout=_PC_CLOSE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            log.warning(
                "web.voice.pc_close_error",
                error=str(exc), error_type=type(exc).__name__,
            )

    async def _drain_pipeline(self, session: VoiceSession, reason: str) -> None:
        """Bounded, swallowed teardown of the assistant STT worker + turn
        driver (V1). No-op in echo mode. Converges here because close() is the
        single choke point every teardown path (client_close / reaper /
        replacement / connection-state / shutdown) already funnels through."""
        worker = session.keepalive.get("stt_worker")
        if worker is not None:
            try:
                await asyncio.wait_for(worker.aclose(reason=reason), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                log.warning(
                    "web.voice.stt.close_error",
                    voice_session_id=session.voice_session_id,
                    error_class=type(exc).__name__,
                )
        driver = session.keepalive.get("turn_driver")
        if driver is not None:
            try:
                await asyncio.wait_for(driver.aclose(reason=reason), timeout=10.0)
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                log.warning(
                    "web.voice.driver_close_error",
                    voice_session_id=session.voice_session_id,
                    error_class=type(exc).__name__,
                )

    # -- reaper -------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        """Lazily start the reaper loop on the first opened session."""
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        if self.reaper_interval <= 0:
            # Disabled (test / deliberate) — logged so "no reaper" is a
            # deliberate state, not a silent skip.
            log.info("web.voice.reaper_disabled", interval_s=self.reaper_interval)
            return
        self._reaper_task = asyncio.ensure_future(self._reaper_loop())
        if not self._reaper_started:
            self._reaper_started = True
            log.info("web.voice.reaper_started", interval_s=self.reaper_interval)

    def stop_reaper(self) -> None:
        """Cancel the reaper loop (fire-and-forget cancel; :meth:`aclose`
        awaits it on the shutdown path)."""
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None

    async def aclose(self) -> None:
        """Shutdown drain — cancel + AWAIT the reaper, then await any detached
        connection-state close tasks.

        Called from the on_shutdown hook AFTER ``close_all``. Awaiting the
        cancelled reaper + the ``_bg_tasks`` (spawned by the
        connectionstatechange failed/closed handlers) means the event loop
        never tears down with pending tasks — no "Task was destroyed but it
        is pending" noise at daemon shutdown.
        """
        reaper = self._reaper_task
        self.stop_reaper()
        if reaper is not None:
            try:
                await reaper
            except asyncio.CancelledError:
                pass
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

    async def _reaper_loop(self) -> None:
        """Sweep every ``reaper_interval`` s; a poisoned iteration is logged
        and the loop survives (never dies silently)."""
        while True:
            await asyncio.sleep(self.reaper_interval)
            try:
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad sweep must not kill the loop
                log.warning(
                    "web.voice.reaper_error",
                    error=str(exc), error_type=type(exc).__name__,
                )

    async def _reap_once(self) -> None:
        """Close sessions past their absolute / connect-deadline / idle limit."""
        now = self._clock()
        doomed: list[tuple[str, str]] = []
        for vid, s in list(self._sessions.items()):
            age = now - s.created_mono
            if age >= self.max_session_seconds:
                doomed.append((vid, "absolute_timeout"))
            elif not s.connected_once and age >= self.connect_deadline:
                doomed.append((vid, "connect_deadline"))
            elif (
                s.connected_once
                and s.connection_state != "connected"
                and (now - s.last_state_change) >= self.idle_timeout
            ):
                doomed.append((vid, "idle_timeout"))
            elif self._no_speech_expired(s, now):
                doomed.append((vid, "no_speech"))
        for vid, reason in doomed:
            await self.close(vid, reason=reason)

    def _no_speech_expired(self, s: VoiceSession, now: float) -> bool:
        """Assistant-pipeline no-speech reaper (§1.6): a CONNECTED session that
        has produced ZERO stt finals for ``no_speech_close_s`` closes with
        reason=no_speech — idle_timeout only fires post-disconnect, so without
        this an abandoned connected tab streams billable silence. Echo sessions
        (no ``stt_worker``) are exempt."""
        worker = s.keepalive.get("stt_worker")
        if worker is None or not s.connected_once or s.connection_state != "connected":
            return False
        try:
            finals = worker.stats.get("finals", 0)
        except Exception:  # noqa: BLE001 - defensive
            return False
        return (
            finals == 0
            and bool(s.connected_at)
            and (now - s.connected_at) >= self.no_speech_close_s
        )
