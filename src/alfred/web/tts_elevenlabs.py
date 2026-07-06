"""ElevenLabs streaming-TTS provider (aiohttp WebSocket, per-turn connection).

The TTS mirror of :mod:`stt_deepgram` with the feed direction inverted (feed =
reply text, events = PCM audio). Implements
:class:`~alfred.web.tts_stream.TTSStreamProvider` over ElevenLabs'
``/v1/text-to-speech/{voice_id}/stream-input`` live endpoint:

* pure :func:`build_elevenlabs_url` (query params; key NEVER in the URL) +
  :func:`parse_elevenlabs_message` (message → PCM / isFinal / error) so the wire
  mapping is unit-testable without a socket;
* ONE WebSocket PER TURN — lazily opened when :meth:`begin_turn` pre-warms it
  (so the ~150-400 ms connect hides inside the LLM's turn_started→first-sentence
  gap), closed at :meth:`end_of_reply`. NO mid-turn reconnect (partial-text
  replay is unsound; the DC text reply is the honest fallback);
* a single-space keepalive watchdog (15 s of send-silence → ``{"text":" "}``)
  covering >20 s tool-call gaps between sentence yields (idle-close is 1008);
* error classification reusing the batch ``TTS_ERR_*`` taxonomy — AUTH /
  BAD_REQUEST are fatal (session-latch upstream), NETWORK / RATE_LIMIT are
  per-turn transient (the worker escalates to a latch after 3 consecutive).

**Key + text hygiene (contract §1.4 / security SW4):** the ElevenLabs key rides
the ``xi-api-key`` header ONLY; ws handshake/connect exceptions are logged as
exception CLASS + HTTP/close status ONLY — never ``str(exc)`` and never the fed
reply text or a provider-sent error string (which can echo input). We do NOT
copy ``telegram/tts.py``'s ``resp.text[:200]`` body-tail logging.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

import aiohttp

from .tts_stream import (
    EVENT_AUDIO,
    EVENT_ERROR,
    EVENT_TURN_DONE,
    TTS_ERR_AUTH,
    TTS_ERR_BAD_REQUEST,
    TTS_ERR_NETWORK,
    TTS_ERR_RATE_LIMIT,
    TTSEvent,
    TTSStreamProvider,
    rate_from_output_format,
)
from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebVoiceTtsConfig

log = get_logger(__name__)

# Bounded wait for the per-turn flush drain (final audio after CloseConnection).
_END_DRAIN_S = 30.0
# ElevenLabs default voice_settings — reuse the batch /brief defaults verbatim.
_VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}


# --- pure URL + message parsing --------------------------------------------


def build_elevenlabs_url(
    *,
    voice_id: str,
    model_id: str,
    output_format: str,
    auto_mode: bool,
    inactivity_timeout_s: int,
    zero_retention: bool,
    base_url: str = "wss://api.elevenlabs.io",
) -> str:
    """Build the ElevenLabs ``stream-input`` wss URL. Pure.

    The API key is NEVER in the URL (``xi-api-key`` header only, §1.4).
    ``enable_logging=false`` is appended only when ``zero_retention`` is set
    (plan-gated at ElevenLabs; default off = provider default).
    """
    params = [
        ("model_id", model_id),
        ("output_format", output_format),
        ("auto_mode", "true" if auto_mode else "false"),
        ("inactivity_timeout", str(inactivity_timeout_s)),
    ]
    if zero_retention:
        params.append(("enable_logging", "false"))
    base = base_url.rstrip("/")
    return f"{base}/v1/text-to-speech/{voice_id}/stream-input?" + urlencode(params)


@dataclass
class ParsedTtsMessage:
    """One parsed ElevenLabs ws message. ``error`` is the raw provider string
    (presence-only; the provider NEVER logs it — SW4). ``code`` is an optional
    numeric status the provider MAY surface as a classified head."""

    pcm: bytes = b""
    is_final: bool = False
    error: str = ""
    code: int | None = None


def parse_elevenlabs_message(raw: str) -> ParsedTtsMessage:
    """Map one ElevenLabs ws message to a :class:`ParsedTtsMessage`. Pure.

    ``audio`` (base64 PCM) → ``pcm``; ``isFinal`` → ``is_final``; ``error`` /
    ``message`` → ``error`` (raw, presence-only). ``alignment`` /
    ``normalizedAlignment`` are IGNORED (sync_alignment stays off in V2 —
    payload cost; V3 truncation may enable it). Unknown keys tolerated;
    malformed → empty.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return ParsedTtsMessage()
    if not isinstance(msg, dict):
        return ParsedTtsMessage()
    err = msg.get("error") or msg.get("message") or ""
    if err:
        code = msg.get("code")
        return ParsedTtsMessage(
            error=str(err), code=code if isinstance(code, int) else None,
        )
    audio_b64 = msg.get("audio")
    pcm = b""
    if isinstance(audio_b64, str) and audio_b64:
        try:
            pcm = base64.b64decode(audio_b64)
        except (ValueError, TypeError):
            pcm = b""
    return ParsedTtsMessage(pcm=pcm, is_final=bool(msg.get("isFinal")))


def _classify_handshake_status(status: int | None) -> str:
    """Handshake HTTP status → TTS_ERR_* class (status only, never body)."""
    if status in (401, 403):
        return TTS_ERR_AUTH
    if status == 429:
        return TTS_ERR_RATE_LIMIT
    if status is not None and 400 <= status < 500:
        return TTS_ERR_BAD_REQUEST
    return TTS_ERR_NETWORK


def _is_fatal_class(reason: str) -> bool:
    """AUTH / BAD_REQUEST are deterministic (config/tier/account) → fatal =
    session-latch. NETWORK / RATE_LIMIT are transient → per-turn (the worker
    escalates to a latch after 3 consecutive failures, contract §1.4)."""
    return reason in (TTS_ERR_AUTH, TTS_ERR_BAD_REQUEST)


class _HandshakeFailed(Exception):
    """Raised when the per-turn ws handshake fails; carries the class."""

    def __init__(self, reason: str, status: int | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# --- provider --------------------------------------------------------------


class ElevenLabsStreamProvider(TTSStreamProvider):
    """ElevenLabs live TTS over a per-turn aiohttp WebSocket."""

    provider_id = "elevenlabs"

    def __init__(
        self,
        cfg: "WebVoiceTtsConfig",
        *,
        base_url: str = "wss://api.elevenlabs.io",
        keepalive_interval_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        voice_session_id: str = "",
    ) -> None:
        from alfred.telegram.tts import resolve_voice_id

        self._cfg = cfg
        self._base_url = base_url.rstrip("/")
        self._voice_id = resolve_voice_id(cfg.voice)
        self.output_rate = rate_from_output_format(cfg.output_format)
        self._keepalive_interval = keepalive_interval_s
        self._clock = clock
        self._vid = voice_session_id

        # session-level event stream (spans every turn; ends on close/fatal)
        self._events: asyncio.Queue[TTSEvent | None] = asyncio.Queue()
        self._closed = False

        # per-turn state
        self._turn_id = ""
        self._cancelled = False
        self._turn_failed = False
        self._connect_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_send = 0.0
        self._sent_any = False

    def _url(self) -> str:
        return build_elevenlabs_url(
            voice_id=self._voice_id,
            model_id=self._cfg.model,
            output_format=self._cfg.output_format,
            auto_mode=self._cfg.auto_mode,
            inactivity_timeout_s=self._cfg.inactivity_timeout_s,
            zero_retention=self._cfg.zero_retention,
            base_url=self._base_url,
        )

    # -- per-turn lifecycle -------------------------------------------------

    async def begin_turn(self, turn_id: str) -> None:
        """Reset per-turn state + PRE-WARM the ws (connect hides in the LLM
        gap). No text is sent until the first :meth:`feed_text`."""
        self._turn_id = turn_id
        self._cancelled = False
        self._turn_failed = False
        self._sent_any = False
        self._ws = None
        self._session = None
        self._recv_task = None
        self._keepalive_task = None
        self._connect_task = asyncio.ensure_future(self._open_turn_ws(turn_id))
        # Retrieve the prewarm task's exception even if the turn is cancelled /
        # closed before any feed_text awaits it (no "exception never retrieved").
        self._connect_task.add_done_callback(
            lambda t: not t.cancelled() and t.exception()
        )

    async def _open_turn_ws(self, turn_id: str) -> None:
        session = aiohttp.ClientSession()
        headers = {"xi-api-key": self._cfg.api_key}
        try:
            ws = await session.ws_connect(self._url(), headers=headers)
        except aiohttp.WSServerHandshakeError as exc:
            status = getattr(exc, "status", None)
            reason = _classify_handshake_status(status)
            await session.close()
            # §1.4: class + status ONLY — never str(exc) (can embed headers).
            log.warning(
                "web.voice.tts.error", voice_session_id=self._vid,
                error_class="WSServerHandshakeError", status=status,
                reason=reason, fatal=_is_fatal_class(reason),
            )
            raise _HandshakeFailed(reason, status) from None
        except Exception as exc:  # noqa: BLE001 — connect errors → network
            await session.close()
            log.warning(
                "web.voice.tts.error", voice_session_id=self._vid,
                error_class=type(exc).__name__, status=None,
                reason=TTS_ERR_NETWORK, fatal=False,
            )
            raise _HandshakeFailed(TTS_ERR_NETWORK, None) from None
        self._session = session
        self._ws = ws
        # InitializeConnection: voice_settings ride the first message.
        await ws.send_str(json.dumps({
            "text": " ", "voice_settings": _VOICE_SETTINGS,
        }))
        self._last_send = self._clock()
        self._recv_task = asyncio.ensure_future(self._receive_loop(turn_id))
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        log.info(
            "web.voice.tts.connected", voice_session_id=self._vid,
            provider=self.provider_id,
        )

    async def feed_text(self, chunk: str) -> None:
        if self._closed or self._cancelled or self._turn_failed:
            return
        try:
            if self._connect_task is not None:
                await self._connect_task
        except _HandshakeFailed as exc:
            self._fail_turn(exc.reason, f"handshake={exc.status}")
            return
        ws = self._ws
        if ws is None or ws.closed:
            self._fail_turn(TTS_ERR_NETWORK, "ws_closed")
            return
        try:
            await ws.send_str(json.dumps({"text": chunk + " "}))
            self._last_send = self._clock()
            self._sent_any = True
        except (ConnectionError, aiohttp.ClientError):
            self._fail_turn(TTS_ERR_NETWORK, "send_failed")

    async def end_of_reply(self) -> None:
        """Flush (``{"text":""}``) + drain the receiver's isFinal, then close
        the per-turn ws. A zero-feed turn emits ``turn_done`` with no ws."""
        if self._closed or self._cancelled:
            return
        try:
            if self._connect_task is not None:
                await self._connect_task
        except _HandshakeFailed as exc:
            self._fail_turn(exc.reason, f"handshake={exc.status}")
            return
        if self._turn_failed:
            return  # the transient error already terminated the turn
        ws = self._ws
        if ws is None or ws.closed or not self._sent_any:
            # Zero-feed turn (or a ws that never opened): no audio, no error —
            # still emit turn_done so the worker completes its bookkeeping.
            self._events.put_nowait(TTSEvent(type=EVENT_TURN_DONE, turn_id=self._turn_id))
            await self._teardown_turn_ws()
            return
        try:
            await ws.send_str(json.dumps({"text": ""}))
        except Exception:  # noqa: BLE001 — best-effort flush
            pass
        if self._recv_task is not None:
            try:
                await asyncio.wait_for(self._recv_task, _END_DRAIN_S)
            except (asyncio.TimeoutError, TimeoutError):
                self._recv_task.cancel()
                self._events.put_nowait(TTSEvent(
                    type=EVENT_ERROR, turn_id=self._turn_id,
                    reason=TTS_ERR_NETWORK, detail="drain_timeout", fatal=False,
                ))
            except Exception:  # noqa: BLE001
                pass
        await self._teardown_turn_ws()

    async def cancel_turn(self) -> None:
        """Abort synthesis ASAP; suppress the turn's remaining events."""
        self._cancelled = True
        await self._teardown_turn_ws()

    # -- receive loop + keepalive -------------------------------------------

    async def _receive_loop(self, turn_id: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for msg in ws:
                if self._cancelled:
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    parsed = parse_elevenlabs_message(msg.data)
                    if parsed.error:
                        # BAD_REQUEST (deterministic account/tier/voice) →
                        # fatal; never log the raw provider string (SW4).
                        head = f"code={parsed.code}" if parsed.code else "ws_error"
                        self._emit_error(TTS_ERR_BAD_REQUEST, head, fatal=True)
                        return
                    if parsed.pcm:
                        self._events.put_nowait(TTSEvent(
                            type=EVENT_AUDIO, turn_id=turn_id, pcm=parsed.pcm,
                        ))
                    if parsed.is_final:
                        self._events.put_nowait(TTSEvent(
                            type=EVENT_TURN_DONE, turn_id=turn_id,
                        ))
                        return
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception:  # noqa: BLE001 — connection error → transient turn error
            if not self._cancelled and not self._turn_failed:
                self._emit_error(TTS_ERR_NETWORK, "ws_recv_drop", fatal=False)
            return
        # ws ended without isFinal → transient drop (non-fatal, retry next turn).
        if not self._cancelled and not self._turn_failed:
            self._emit_error(TTS_ERR_NETWORK, "ws_closed_no_final", fatal=False)

    async def _keepalive_loop(self) -> None:
        while not self._closed and not self._cancelled:
            await asyncio.sleep(self._keepalive_interval)
            ws = self._ws
            if ws is None or ws.closed or self._cancelled:
                return
            if self._clock() - self._last_send >= self._keepalive_interval:
                try:
                    await ws.send_str(json.dumps({"text": " "}))
                    self._last_send = self._clock()
                except Exception:  # noqa: BLE001 — best-effort
                    return

    # -- helpers ------------------------------------------------------------

    def _fail_turn(self, reason: str, detail: str) -> None:
        """Feed/flush-side turn failure → one classified error event."""
        if self._turn_failed:
            return
        self._emit_error(reason, detail, fatal=_is_fatal_class(reason))

    def _emit_error(self, reason: str, detail: str, *, fatal: bool) -> None:
        if self._turn_failed:
            return
        self._turn_failed = True
        log.warning(
            "web.voice.tts.error", voice_session_id=self._vid,
            reason=reason, detail=detail, fatal=fatal,
        )
        self._events.put_nowait(TTSEvent(
            type=EVENT_ERROR, turn_id=self._turn_id, reason=reason,
            detail=detail, fatal=fatal,
        ))

    async def _teardown_turn_ws(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    # -- close --------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancelled = True
        await self._teardown_turn_ws()
        self._events.put_nowait(None)  # sentinel — ends events()

    async def events(self) -> AsyncIterator[TTSEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev
            if ev.type == EVENT_ERROR and ev.fatal:
                return  # fatal is the last event
