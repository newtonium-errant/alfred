"""Deepgram streaming-STT provider (aiohttp WebSocket).

The FIRST client-side aiohttp WebSocket use in the codebase (all other HTTP
client work is httpx; aiohttp was server-side only). Implements
:class:`~alfred.web.stt_stream.STTStreamProvider` over Deepgram's
``/v1/listen`` live endpoint:

* pure :func:`build_deepgram_url` (query params incl. ``smart_format`` from
  config, contract §1.8) + :func:`parse_deepgram_message` (message → normalized
  events) so the wire mapping is unit-testable without a socket;
* a KeepAlive watchdog (3 s of silence → ``{"type":"KeepAlive"}``, beating
  Deepgram's 10 s NET-0001 idle close);
* reconnect-ONCE, re-armed after ``reconnect_rearm_s`` of healthy connection
  (a long session survives sparse blips; a flap fails fast → fatal);
* ``speech_final`` vs ``UtteranceEnd`` dedup (Deepgram's documented pattern);
* error classification reusing the batch ``STT_ERR_*`` taxonomy.

**Key hygiene (contract §1.4):** the Deepgram key rides the ``Authorization``
header only, and ws handshake/connect exceptions are logged as exception
CLASS + HTTP/close status ONLY — never ``str(exc)`` (aiohttp handshake
exception strings can embed request headers).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

import aiohttp

from .stt_stream import (
    EVENT_ERROR,
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STT_ERR_AUTH,
    STT_ERR_BAD_REQUEST,
    STT_ERR_NETWORK,
    STT_ERR_RATE_LIMIT,
    STTEvent,
    STTStreamProvider,
)
from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebVoiceSttConfig

log = get_logger(__name__)

# Bounded feed-side wait for an in-progress reconnect before dropping a chunk.
_FEED_RECONNECT_WAIT_S = 5.0
# Graceful-close drain: wait this long for a trailing final after CloseStream.
_CLOSE_DRAIN_S = 2.0


# --- pure URL + message parsing --------------------------------------------


def build_deepgram_url(cfg: "WebVoiceSttConfig") -> str:
    """Build the Deepgram ``/v1/listen`` wss URL from config. Pure.

    ``interim_results=true`` is HARDWIRED (required for live partials +
    UtteranceEnd). ``smart_format`` comes from config (§1.8). ``utterance_end_ms``
    is omitted when 0 (fallback disabled). ``vad_events`` is deliberately
    omitted (its only payload is SpeechStarted, a V3 barge-in concern).
    """
    params = [
        ("encoding", "linear16"),
        ("sample_rate", str(cfg.sample_rate)),
        ("channels", "1"),
        ("model", cfg.model),
        ("language", cfg.language),
        ("interim_results", "true"),
        ("smart_format", "true" if cfg.smart_format else "false"),
        ("endpointing", str(cfg.endpointing_ms)),
    ]
    if cfg.utterance_end_ms > 0:
        params.append(("utterance_end_ms", str(cfg.utterance_end_ms)))
    return "wss://api.deepgram.com/v1/listen?" + urlencode(params)


def parse_deepgram_message(msg: dict) -> list[STTEvent]:
    """Map one Deepgram message to normalized events. Pure.

    ``Results`` → partial / final (+ utterance_end when ``speech_final``);
    ``UtteranceEnd`` → utterance_end (``utterance_end_fallback``; the caller
    applies speech_final dedup); ``Metadata`` / ``SpeechStarted`` / unknown /
    malformed → ``[]``. An empty transcript (Deepgram's empty interims) → ``[]``.
    """
    mtype = msg.get("type")
    if mtype == "Results":
        try:
            alts = msg["channel"]["alternatives"]
            transcript = (alts[0].get("transcript") or "") if alts else ""
        except (KeyError, IndexError, TypeError, AttributeError):
            return []
        if not transcript.strip():
            return []
        is_final = bool(msg.get("is_final", False))
        speech_final = bool(msg.get("speech_final", False))
        if not is_final:
            return [STTEvent(type=EVENT_PARTIAL, text=transcript)]
        events = [STTEvent(type=EVENT_FINAL, text=transcript)]
        if speech_final:
            events.append(
                STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final")
            )
        return events
    if mtype == "UtteranceEnd":
        return [STTEvent(type=EVENT_UTTERANCE_END, trigger="utterance_end_fallback")]
    return []


# --- error classification --------------------------------------------------


def _classify_ws_close(code: int | None) -> str:
    """Deepgram ws close code → STT_ERR_* class.

    1008 (DATA-0000, policy violation / bad audio) → bad_request; everything
    else (1011 server error, abnormal closes, connection loss) → network.
    """
    if code == 1008:
        return STT_ERR_BAD_REQUEST
    return STT_ERR_NETWORK


def _classify_handshake_status(status: int | None) -> str:
    """Handshake HTTP status → STT_ERR_* class (status only, never body)."""
    if status == 401 or status == 403:
        return STT_ERR_AUTH
    if status == 429:
        return STT_ERR_RATE_LIMIT
    if status is not None and 400 <= status < 500:
        return STT_ERR_BAD_REQUEST
    return STT_ERR_NETWORK


class _HandshakeFailed(Exception):
    """Raised when the ws handshake fails; carries the classified reason."""

    def __init__(self, reason: str, status: int | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# --- provider --------------------------------------------------------------


class DeepgramStreamProvider(STTStreamProvider):
    """Deepgram live STT over an aiohttp WebSocket."""

    provider_id = "deepgram"

    def __init__(
        self,
        cfg: "WebVoiceSttConfig",
        *,
        base_url: str = "wss://api.deepgram.com",
        keepalive_interval_s: float = 3.0,
        reconnect_rearm_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        voice_session_id: str = "",
    ) -> None:
        self._cfg = cfg
        self._base_url = base_url.rstrip("/")
        self._keepalive_interval = keepalive_interval_s
        self._reconnect_rearm_s = reconnect_rearm_s
        self._clock = clock
        self._vid = voice_session_id

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._events: asyncio.Queue[STTEvent | None] | None = None
        self._ready: asyncio.Event | None = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

        self._closing = False
        self._budget = 1
        self._connected_at = 0.0
        self._last_sent = 0.0
        # speech_final vs UtteranceEnd dedup: True once a final has arrived
        # since the last EOU; a fallback UtteranceEnd only fires when set.
        self._finals_since_eou = False

    # -- url ----------------------------------------------------------------

    def _url(self) -> str:
        # Honor a test base_url override while keeping build_deepgram_url pure.
        full = build_deepgram_url(self._cfg)
        return full.replace("wss://api.deepgram.com", self._base_url, 1)

    # -- connect ------------------------------------------------------------

    async def connect(self) -> None:
        self._events = asyncio.Queue()
        self._ready = asyncio.Event()
        self._session = aiohttp.ClientSession()
        try:
            await self._open_ws()  # raises _HandshakeFailed on handshake error
        except Exception:
            # Don't leak the ClientSession when the handshake fails.
            await self._session.close()
            self._session = None
            raise
        self._recv_task = asyncio.ensure_future(self._receive_loop())
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        log.info(
            "web.voice.stt.connected",
            voice_session_id=self._vid, provider=self.provider_id,
        )

    async def _open_ws(self) -> None:
        assert self._session is not None
        headers = {"Authorization": f"Token {self._cfg.api_key}"}
        try:
            self._ws = await self._session.ws_connect(self._url(), headers=headers)
        except aiohttp.WSServerHandshakeError as exc:
            status = getattr(exc, "status", None)
            reason = _classify_handshake_status(status)
            # §1.4: class + status ONLY — never str(exc) (can embed headers).
            log.warning(
                "web.voice.stt.error",
                voice_session_id=self._vid,
                error_class="WSServerHandshakeError",
                status=status, reason=reason, fatal=True,
            )
            raise _HandshakeFailed(reason, status) from None
        except Exception as exc:  # noqa: BLE001 — connection errors → network
            log.warning(
                "web.voice.stt.error",
                voice_session_id=self._vid,
                error_class=type(exc).__name__,
                status=None, reason=STT_ERR_NETWORK, fatal=True,
            )
            raise _HandshakeFailed(STT_ERR_NETWORK, None) from None
        self._connected_at = self._clock()
        self._last_sent = self._clock()
        self._ready.set()

    # -- feed / finalize ----------------------------------------------------

    async def feed(self, chunk: bytes) -> None:
        if self._closing:
            return
        # During a reconnect the ready flag is cleared; wait bounded, else drop.
        try:
            await asyncio.wait_for(self._ready.wait(), _FEED_RECONNECT_WAIT_S)
        except (asyncio.TimeoutError, TimeoutError):
            return
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_bytes(chunk)
            self._last_sent = self._clock()
        except (ConnectionError, aiohttp.ClientError):
            # The receive loop will observe the same death and drive reconnect.
            self._ready.clear()

    async def finalize(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(json.dumps({"type": "Finalize"}))
            except Exception:  # noqa: BLE001 — best-effort flush
                pass

    # -- keepalive ----------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._keepalive_interval)
            if self._closing:
                return
            ws = self._ws
            if ws is None or ws.closed:
                continue
            if self._clock() - self._last_sent >= self._keepalive_interval:
                try:
                    await ws.send_str(json.dumps({"type": "KeepAlive"}))
                except Exception:  # noqa: BLE001 — best-effort
                    pass

    # -- receive loop + reconnect -------------------------------------------

    async def _receive_loop(self) -> None:
        while not self._closing:
            ws = self._ws
            if ws is None:
                break
            close_code: int | None = None
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_text(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        close_code = ws.close_code
                        break
            except Exception:  # noqa: BLE001 — connection error → reconnect path
                close_code = None
            if self._closing:
                break
            # Unexpected end → reconnect or fail fatally.
            if not await self._reconnect(close_code):
                break
        self._events.put_nowait(None)  # end events()

    def _handle_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "Metadata":
            return  # graceful stream marker; nothing to emit
        for ev in parse_deepgram_message(msg):
            if ev.type == EVENT_FINAL:
                self._finals_since_eou = True
                self._events.put_nowait(ev)
            elif ev.type == EVENT_UTTERANCE_END:
                if ev.trigger == "utterance_end_fallback" and not self._finals_since_eou:
                    continue  # dedup: speech_final already closed this utterance
                self._finals_since_eou = False
                self._events.put_nowait(ev)
            else:
                self._events.put_nowait(ev)

    async def _reconnect(self, close_code: int | None) -> bool:
        """One reconnect attempt (re-armed after healthy uptime). Returns
        True on success, False (after emitting a fatal error) on exhaustion."""
        # Re-arm the budget if the prior connection was healthy long enough.
        if self._clock() - self._connected_at >= self._reconnect_rearm_s:
            self._budget = 1
        if self._budget <= 0:
            reason = _classify_ws_close(close_code)
            log.warning(
                "web.voice.stt.error",
                voice_session_id=self._vid, reason=reason,
                close_code=close_code, fatal=True,
                detail="reconnect budget exhausted",
            )
            self._events.put_nowait(
                STTEvent(type=EVENT_ERROR, reason=reason,
                         detail=f"ws_close={close_code}", fatal=True)
            )
            return False
        self._budget -= 1
        self._ready.clear()
        try:
            await self._open_ws()  # re-sets _ready on success
        except _HandshakeFailed as exc:
            self._events.put_nowait(
                STTEvent(type=EVENT_ERROR, reason=exc.reason,
                         detail=f"reconnect_status={exc.status}", fatal=True)
            )
            return False
        log.info(
            "web.voice.stt.reconnect",
            voice_session_id=self._vid, outcome="ok",
        )
        return True

    # -- close --------------------------------------------------------------

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001 — best-effort
                pass
        # Let the receive loop drain a trailing final, then tear down.
        if self._recv_task is not None:
            try:
                await asyncio.wait_for(self._recv_task, _CLOSE_DRAIN_S)
            except (asyncio.TimeoutError, TimeoutError):
                self._recv_task.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        if self._events is not None:
            self._events.put_nowait(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev
