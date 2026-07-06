"""V1 voice turn driver — STT utterances → run_turn_streaming → datachannel.

``VoiceTurnDriver`` owns the reply plane of an assistant-pipeline voice
session: it consumes end-of-utterance text from the STT worker (facet 1),
drives ``run_turn_streaming`` (the SAME engine the chat UI uses), and streams
the incremental reply back over the WebRTC **datachannel** (label ``voice``).
Voice turns land in the EXACT ``/chat/history`` the browser renders — same
``StateManager``, same ``append_turn`` → ``_persist`` path — so a spoken turn
and a typed turn interleave in one session.

Wire contract (v:1, contract §1.1) — server→client events + a two-type
client→server set (hello / cancel), with a hello-gate (aiortc#212). Never
touches aiortc: the datachannel is duck-typed (``.readyState`` / ``.send`` /
``.bufferedAmount``) so the driver unit-tests without aiortc, same discipline
as ``voice_session``'s injectable seams.

Concurrency discipline:
* ONE turn in flight per voice session; a depth-1 LATEST-WINS queue (an
  utterance finalized mid-turn supersedes any queued one — the freshest is
  the walkie-talkie intent). The in-flight turn is NOT cancelled by new
  speech (barge-in is V3).
* Shares ``KEY_WEB_INFLIGHT`` with ``/chat/turn`` + ``/chat/stream`` so a
  voice turn and a typed turn never double-append; the slot is reserved with
  an atomic check-then-add and released in ``finally``.
* After the reservation wait, RE-VERIFY the binding (contract §1.2) — a
  ``/chat/open`` may have replaced the active session while we waited.

The per-event loop body is AWAIT-FREE (``dc.send`` is sync, contract §1.16)
so a cancellation can't interleave mid-emit. Cancellation (client ``cancel``
or ``manager.close`` → :meth:`aclose`) relies on the pinned engine contract:
a ``CancelledError`` mid-turn flushes a well-formed tool_results tail + a
SYNCHRONOUS ``_persist``, so the persisted record is never corrupt.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebConfig
    from .identity import WebIdentity

log = get_logger(__name__)

# Protocol constants (NOT config — no sprawl; forward-compat notes in module
# docstring). ``v`` bumps only on a breaking shape change.
EVENT_VERSION = 1
MAX_DC_EVENT_BYTES = 15 * 1024   # cross-browser SCTP safety (<16 KiB)
MAX_DC_CLIENT_BYTES = 4096
TURN_SLOT_WAIT_S = 60.0

_DC_BUFFER_LIMIT = 1024 * 1024   # 1 MiB SCTP send buffer → drop
_INFLIGHT_POLL_S = 0.25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TurnDeps:
    """Everything a voice turn needs, read off ``request.app[KEY_WEB_*]`` at
    offer time (all stashed by ``register_web_routes`` before voice mounts).

    ``run_turn_streaming_fn`` is an injectable seam (default ``None`` → the
    real engine, lazy-imported) so the driver unit-tests without the engine.
    """

    client: Any
    state_mgr: Any
    talker_config: Any
    web_config: "WebConfig"
    system_prompt_provider: Callable[[], str]
    vault_context_str: str
    in_flight: set
    identity: "WebIdentity"
    chat_session_key: str
    run_turn_streaming_fn: Callable[..., Any] | None = None


class VoiceTurnDriver:
    """One per assistant-pipeline voice session. Sole owner of the datachannel."""

    def __init__(self, deps: TurnDeps, voice_session_id: str) -> None:
        self._deps = deps
        self._vid = voice_session_id
        self._channel: Any = None
        self._hello_received = False
        self._closed = False

        # depth-1 latest-wins queue
        self._pending: tuple[str, str] | None = None
        self._wake = asyncio.Event()
        self._current_task: asyncio.Task | None = None
        self._current_turn_id: str | None = None
        self._seq = 0
        self._loop_task = asyncio.ensure_future(self._loop())

        # utterance-id correlation (partials share the id of the utterance
        # they belong to; the id rotates when the utterance is submitted)
        self._utt_id: str | None = None

        # Callbacks fired when the client's hello arrives — the STT worker
        # registers ``worker.allow_feed`` here so the provider connects/feeds
        # ONLY after a live datachannel is confirmed (contract §17b).
        self._hello_callbacks: list[Callable[[], None]] = []

        # V2 TTS talk-back plane (all None-safe when no worker is attached →
        # V1 behaviour byte-identical). ``_speaking_turn_id`` is the half-duplex
        # gate (contract §1.9): utterance finals arriving while it is set are
        # discarded. ``_tts_off_session`` latches TTS off on a fatal provider
        # error (§1.4); TTS failure NEVER closes the session.
        self._tts: Any = None
        self._tts_max_chars = 4096
        self._speaking_turn_id: str | None = None
        self._tts_off_session = False
        self._tts_error_emitted = False
        self._tts_chars_fed = 0
        self._tts_capped = False

        # observability latches / counters
        self._drops: dict[str, int] = {}
        self._unknown_types_logged: set[str] = set()
        self._malformed_logged = False
        self._binary_logged = False
        self._oversize_client_logged = False
        self._wrong_version_logged = False

    # -- datachannel attach + client frames ---------------------------------

    def attach_channel(self, channel: Any) -> None:
        """Attach the ``voice`` datachannel. The server sends NOTHING until
        the client's first valid ``hello`` frame (hello-gate)."""
        self._channel = channel

    def on_client_message(self, raw: Any) -> None:
        """Validate + dispatch a client→server frame (contract §1.3).

        Binary + oversize frames are dropped BEFORE ``json.parse`` (W5).
        Only ``hello`` and ``cancel`` are accepted; everything else is
        ignored + logged once.
        """
        if isinstance(raw, (bytes, bytearray, memoryview)):
            if not self._binary_logged:
                self._binary_logged = True
                log.info("web.voice.dc_binary_ignored", voice_session_id=self._vid)
            return
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DC_CLIENT_BYTES:
            if not self._oversize_client_logged:
                self._oversize_client_logged = True
                log.info(
                    "web.voice.dc_client_frame_oversize",
                    voice_session_id=self._vid, cap=MAX_DC_CLIENT_BYTES,
                )
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            if not self._malformed_logged:
                self._malformed_logged = True
                log.info("web.voice.dc_malformed_client", voice_session_id=self._vid)
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            if not self._malformed_logged:
                self._malformed_logged = True
                log.info("web.voice.dc_malformed_client", voice_session_id=self._vid)
            return
        # v:1 strict (contract §17b.v): a missing / other protocol version is
        # dropped. Forward-compat is unknown-TYPE tolerance WITHIN v:1, not
        # cross-version. Hardens the privileged DC input surface.
        if msg.get("v") != EVENT_VERSION:
            if not self._wrong_version_logged:
                self._wrong_version_logged = True
                log.info(
                    "web.voice.dc_wrong_version",
                    voice_session_id=self._vid, given=msg.get("v"),
                )
            return

        mtype = msg["type"]
        if mtype == "hello":
            self._on_hello()
        elif mtype == "cancel":
            self._on_cancel(msg.get("turn_id"))
        else:
            if mtype not in self._unknown_types_logged:
                self._unknown_types_logged.add(mtype)
                log.info(
                    "web.voice.dc_unknown_client_type",
                    voice_session_id=self._vid, client_type=mtype,
                )

    def add_hello_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback fired on the client's hello (the STT worker's
        ``allow_feed``). Fires immediately if hello already arrived."""
        self._hello_callbacks.append(callback)
        if self._hello_received:
            callback()

    def _on_hello(self) -> None:
        if self._hello_received:
            return  # idempotent — repeat hellos ignored
        self._hello_received = True
        self.emit({
            "v": EVENT_VERSION, "type": "state", "state": "ready",
            "chat_session_key": self._deps.chat_session_key,
            "voice_session_id": self._vid,
        })
        # Release the STT hello-gate — a live DC is now confirmed (§17b).
        for callback in self._hello_callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 — a bad callback must not wedge hello
                log.warning("web.voice.hello_callback_error", voice_session_id=self._vid)

    def _on_cancel(self, turn_id: Any) -> None:
        current = self._current_turn_id
        if turn_id is not None and turn_id != current:
            log.info(
                "web.voice.cancel_stale",
                voice_session_id=self._vid, given=turn_id, current=current,
            )
            return
        # Clear any queued utterance (walkie-talkie "never mind").
        self._pending = None
        # Audio dies FIRST — the user must never hear stale speech after
        # cancelling; then the CancelledError branch emits turn_cancelled
        # (ordering pinned: speaking_done → turn_cancelled).
        self.interrupt_speech("client_cancel")
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
        else:
            log.info("web.voice.cancel_noop", voice_session_id=self._vid)

    # -- V2 TTS seam (talk-back plane) --------------------------------------

    def attach_tts(self, worker: Any) -> None:
        """Late-attach the TTS worker (mirrors :meth:`attach_channel`); wired in
        ``_wire_media`` once the playout source exists. None-safe everywhere —
        absent worker ⇒ V1 behaviour byte-identical."""
        self._tts = worker
        self._tts_max_chars = getattr(worker, "max_chars_per_turn", 4096)

    def on_speaking_started(self, turn_id: str) -> None:
        """Worker callback — first TTS audio enqueued for ``turn_id`` (sets the
        half-duplex gate + emits the distinct-type DC event, contract §1.1)."""
        self._speaking_turn_id = turn_id
        self.emit({
            "v": EVENT_VERSION, "type": "speaking_started", "turn_id": turn_id,
        })

    def on_speaking_done(self, turn_id: str, reason: str) -> None:
        """Worker callback — playout for ``turn_id`` drained / cancelled /
        errored. Clears the gate + emits ``speaking_done`` (paired 1:1 with
        ``speaking_started``; guarded against a double-emit)."""
        if self._speaking_turn_id != turn_id:
            return
        self._speaking_turn_id = None
        self.emit({
            "v": EVENT_VERSION, "type": "speaking_done",
            "turn_id": turn_id, "reason": reason,
        })

    def on_tts_fatal(self, ev: Any) -> None:
        """Worker callback — TTS latched off for the session (auth/bad-request,
        or 3 consecutive transient failures, contract §1.4). Emits
        ``tts_unavailable`` ONCE; the session LIVES (text-only degrade)."""
        self._tts_off_session = True
        if self._speaking_turn_id is not None:
            # Fail-open the half-duplex gate — a lost done must not deafen us.
            done_turn = self._speaking_turn_id
            self._speaking_turn_id = None
            self.emit({
                "v": EVENT_VERSION, "type": "speaking_done",
                "turn_id": done_turn, "reason": "error",
            })
        if not self._tts_error_emitted:
            self._tts_error_emitted = True
            self._emit_error("tts_unavailable", detail=getattr(ev, "reason", ""))
        log.warning(
            "web.voice.tts.degraded_text_only", voice_session_id=self._vid,
            reason=getattr(ev, "reason", ""),
        )

    def interrupt_speech(self, reason: str) -> None:
        """The single audio-plane cancel funnel (contract §1.7) — SYNC, callable
        from await-free contexts. Flushes the worker's playout (via the worker's
        own flush-first-then-abort primitive) and emits ``speaking_done`` if a
        turn was mid-speech. Call sites: client cancel, CancelledError branch,
        engine-error branch, new-turn stale-audio flush, aclose. **V3 barge-in
        calls this exact function** from :meth:`_utterance_while_speaking`."""
        if self._tts is not None:
            self._tts.interrupt_speech(reason)
        if self._speaking_turn_id is not None:
            done_turn = self._speaking_turn_id
            self._speaking_turn_id = None
            self.emit({
                "v": EVENT_VERSION, "type": "speaking_done",
                "turn_id": done_turn, "reason": "cancelled",
            })

    def _utterance_while_speaking(self, utterance_id: str, text: str) -> None:
        """THE V3 policy seam (contract §1.9) — the SOLE decision point for a
        final arriving while a turn is speaking. V2 (half-duplex): discard +
        notice, do NOT queue. V3 (barge-in): swap this body to
        ``interrupt_speech("barge_in")`` + re-submit."""
        log.info(
            "web.voice.utterance_discarded_speaking",
            voice_session_id=self._vid, utterance_id=utterance_id,
        )
        self.emit({
            "v": EVENT_VERSION, "type": "utterance_discarded",
            "utterance_id": utterance_id,
        })

    # -- facet-1 seam (STT worker callbacks) --------------------------------

    async def emit_stt_partial(self, text: str) -> None:
        """Worker ``on_partial`` — forward a live interim transcript."""
        if self._utt_id is None:
            self._utt_id = uuid4().hex
        self.emit({
            "v": EVENT_VERSION, "type": "stt_partial",
            "utterance_id": self._utt_id, "text": text, "ts": _now_iso(),
        })

    async def submit_utterance(self, text: str) -> None:
        """Worker ``on_utterance`` — EOU fired; queue a turn (latest-wins)."""
        uid = self._utt_id or uuid4().hex
        self._utt_id = None
        self.emit({
            "v": EVENT_VERSION, "type": "stt_final",
            "utterance_id": uid, "text": text, "ts": _now_iso(),
        })
        if self._closed:
            log.info(
                "web.voice.utterance_after_close",
                voice_session_id=self._vid, utterance_id=uid,
            )
            return
        # Half-duplex gate (contract §1.9): a final arriving WHILE a turn is
        # speaking is discarded (browser AEC needs 2-5 s to adapt; first-
        # exchange echo leak is expected). stt_final was still emitted above
        # (transcript honesty). The queue / latest-wins path is untouched when
        # nothing is playing (LLM-thinking window = genuine speech).
        if self._speaking_turn_id is not None:
            self._utterance_while_speaking(uid, text)
            return
        if self._pending is not None:
            dropped_id = self._pending[0]
            log.info(
                "web.voice.utterance_superseded",
                voice_session_id=self._vid, dropped=dropped_id, replaced_by=uid,
            )
            self.emit({
                "v": EVENT_VERSION, "type": "state", "state": "superseded",
                "utterance_id": dropped_id,
            })
        self._pending = (uid, text)
        self._wake.set()

    # -- serial turn loop ---------------------------------------------------

    async def _loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if self._closed:
                return
            item = self._pending
            self._pending = None
            if item is None:
                continue
            utterance_id, text = item
            self._current_task = asyncio.ensure_future(
                self._run_one_turn(utterance_id, text)
            )
            try:
                await self._current_task
            except asyncio.CancelledError:
                # Client cancel / teardown of THIS turn — the loop survives.
                pass
            except Exception as exc:  # noqa: BLE001 — a bad turn must not kill the loop
                log.warning(
                    "web.voice.turn_loop_error",
                    voice_session_id=self._vid,
                    error=str(exc), error_type=type(exc).__name__,
                )
            finally:
                self._current_task = None

    async def _run_one_turn(self, utterance_id: str, text: str) -> None:
        d = self._deps
        owner = d.identity.synthetic_chat_id
        turn_id = ""
        self._current_turn_id = None

        # New-turn stale-audio flush: a previous turn's audio may still be
        # draining (playout lags text; the inflight slot released at stream
        # end). Latest-wins in the audio plane matches the walkie-talkie intent.
        if self._speaking_turn_id is not None:
            self.interrupt_speech("new_turn")
        self._tts_chars_fed = 0
        self._tts_capped = False

        reserved = await self._reserve_inflight()
        if not reserved:
            self._emit_error("turn_slot_timeout", utterance_id=utterance_id)
            log.warning(
                "web.voice.turn_slot_timeout",
                voice_session_id=self._vid, utterance_id=utterance_id,
            )
            return
        try:
            # §1.2 RE-VERIFY after the wait — a /chat/open may have replaced
            # the active session while we waited for the slot.
            active = d.state_mgr.get_active(owner)
            if active is None or active.get("session_id") != d.chat_session_key:
                self._emit_error("no_such_session", utterance_id=utterance_id)
                log.info(
                    "web.voice.turn_session_gone",
                    voice_session_id=self._vid, utterance_id=utterance_id,
                )
                return

            from alfred.telegram.session import Session

            session_obj = Session.from_dict(active)
            pre_len = len(session_obj.transcript)
            turn_id = uuid4().hex
            self._current_turn_id = turn_id
            self.emit({
                "v": EVENT_VERSION, "type": "turn_started", "turn_id": turn_id,
                "utterance_id": utterance_id, "session_key": d.chat_session_key,
                "ts": _now_iso(),
            })
            # Pre-warm the TTS provider WS so the ~150-400 ms connect hides in
            # the LLM's turn_started→first-sentence gap (contract §1.3).
            if self._tts is not None and not self._tts_off_session:
                self._tts.begin_turn(turn_id)

            reply = await self._drive_stream(turn_id, session_obj, text)

            # Flush the TTS turn (force final generation + drain) — only on a
            # SUCCESSFUL stream (never on error/cancel — those interrupt).
            if self._tts is not None and not self._tts_off_session:
                self._tts.end_of_reply(turn_id)

            transcript = session_obj.transcript or []
            assistant_ts = transcript[-1].get("_ts", "") if transcript else ""
            user_ts = (
                transcript[pre_len].get("_ts", "")
                if len(transcript) > pre_len else ""
            )
            final_event: dict[str, Any] = {
                "v": EVENT_VERSION, "type": "turn_final", "turn_id": turn_id,
                "reply": reply, "ts": assistant_ts, "user_ts": user_ts,
                "reply_chars": len(reply or ""), "truncated": False,
            }
            if self._tts is not None:
                # Additive spoken-vs-shown delta (contract §1 turn-facet); old
                # FEs strip these unknown keys.
                final_event["tts_chars"] = self._tts_chars_fed
                final_event["tts_capped"] = self._tts_capped
            self.emit(final_event)
            log.info(
                "web.voice.turn_complete",
                voice_session_id=self._vid, turn_id=turn_id,
                reply_chars=len(reply or ""),
            )
        except asyncio.CancelledError:
            # Audio dies first (idempotent if _on_cancel already flushed), then
            # the turn_cancelled state — ordering pinned speaking_done →
            # turn_cancelled. Covers teardown-initiated cancels (aclose path)
            # where _on_cancel never ran.
            self.interrupt_speech("turn_cancelled")
            self.emit({
                "v": EVENT_VERSION, "type": "state", "state": "turn_cancelled",
                "turn_id": turn_id,
            })
            raise
        except Exception as exc:  # noqa: BLE001 — engine error → wire error, session lives
            # A half-spoken reply followed by an error frame is worse than
            # silence+error — flush the audio (symmetric with cancel).
            self.interrupt_speech("engine_error")
            self._emit_error("engine_error", detail=str(exc), turn_id=turn_id)
            log.warning(
                "web.voice.engine_error",
                voice_session_id=self._vid, turn_id=turn_id,
                error=str(exc), error_type=type(exc).__name__,
            )
        finally:
            d.in_flight.discard(d.chat_session_key)
            self._current_turn_id = None

    async def _drive_stream(self, turn_id: str, session_obj: Any, text: str) -> str:
        """Iterate ``run_turn_streaming``; emit turn_text/turn_tool per yield.

        The per-yield loop body is AWAIT-FREE (``emit`` is synchronous) so a
        cancellation cannot interleave mid-emit (contract §1.16)."""
        d = self._deps
        from .routes_chat import _user_name_for

        rts = d.run_turn_streaming_fn
        if rts is None:
            from alfred.telegram.conversation import run_turn_streaming as rts

        agen = rts(
            client=d.client,
            state=d.state_mgr,
            session=session_obj,
            user_message=text,
            config=d.talker_config,
            vault_context_str=d.vault_context_str,
            system_prompt=d.system_prompt_provider(),
            user_kind="voice",
            user_role=d.identity.role,
            user_name=_user_name_for(d.identity, d.web_config),
            channel="web",
            on_event=None,
        )
        reply = ""
        async for chunk in agen:
            ctype = chunk.get("type")
            if ctype == "text":
                txt = chunk.get("text", "")
                self.emit({
                    "v": EVENT_VERSION, "type": "turn_text", "turn_id": turn_id,
                    "seq": self._seq, "text": txt,
                })
                self._seq += 1
                # Feed the sentence chunk to TTS — SYNC put_nowait, keeps this
                # loop body await-free (§1.16). Per-turn char cap on whole-
                # sentence boundaries (contract §1.3): the sentence that WOULD
                # cross the cap is not fed; the fed prefix still speaks.
                # The cap is a clean PREFIX STOP: once capped, feed nothing more.
                # Without ``not self._tts_capped`` a later SHORT sentence would
                # slip under the cap (the skipped long one wasn't counted in
                # _tts_chars_fed), giving spoken-prefix → gap → resume (QA NOTE 1).
                if (self._tts is not None and not self._tts_off_session
                        and not self._tts_capped):
                    if self._tts_chars_fed + len(txt) > self._tts_max_chars:
                        self._tts_capped = True
                        log.info(
                            "web.voice.tts.turn_capped",
                            voice_session_id=self._vid, turn_id=turn_id,
                            chars_fed=self._tts_chars_fed,
                        )
                    else:
                        self._tts.feed_text(turn_id, txt)
                        self._tts_chars_fed += len(txt)
            elif ctype == "tool":
                self.emit({
                    "v": EVENT_VERSION, "type": "turn_tool", "turn_id": turn_id,
                    "tool": chunk.get("tool", ""),
                    "iteration": chunk.get("iteration", 0),
                })
            elif ctype == "final":
                reply = chunk.get("reply", "")
        return reply

    async def _reserve_inflight(self) -> bool:
        """Atomic check-then-add on the shared ``KEY_WEB_INFLIGHT`` set,
        bounded by ``TURN_SLOT_WAIT_S``. Returns True on reserve, False on
        timeout. The check-and-add has NO await between them (single event
        loop → race-free), mirroring ``/chat/turn``'s guard."""
        key = self._deps.chat_session_key
        s = self._deps.in_flight
        self._seq = 0
        waited = 0.0
        while True:
            if key not in s:
                s.add(key)
                return True
            if waited >= TURN_SLOT_WAIT_S:
                return False
            await asyncio.sleep(_INFLIGHT_POLL_S)
            waited += _INFLIGHT_POLL_S

    # -- emit ---------------------------------------------------------------

    def emit(self, event: dict) -> None:
        """Serialize + send a server→client frame (hello-gate + drop policy).

        Synchronous (``dc.send`` is sync). Dropped when: pre-hello, oversize
        (turn_final falls back to reply:"" truncated:true; others dropped),
        channel absent / not open, or bufferedAmount over the SCTP limit. All
        drops are COUNTED (aggregate in the close summary) + first-per-type
        logged — never silent."""
        etype = event.get("type", "?")
        if not self._hello_received:
            self._count_drop(etype, "pre_hello")
            return
        try:
            payload = json.dumps(event, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        if len(payload.encode("utf-8")) > MAX_DC_EVENT_BYTES:
            if etype == "turn_final":
                trimmed = dict(event)
                trimmed["reply"] = ""
                trimmed["truncated"] = True
                payload = json.dumps(trimmed, separators=(",", ":"))
                log.info(
                    "web.voice.dc_event_truncated",
                    voice_session_id=self._vid,
                    reply_chars=event.get("reply_chars", 0),
                )
            else:
                self._count_drop(etype, "oversize")
                return
        ch = self._channel
        if ch is None or getattr(ch, "readyState", "") != "open":
            self._count_drop(etype, "not_open")
            return
        if getattr(ch, "bufferedAmount", 0) > _DC_BUFFER_LIMIT:
            self._count_drop(etype, "backpressure")
            log.warning(
                "web.voice.dc_backpressure_drop",
                voice_session_id=self._vid, type=etype,
            )
            return
        try:
            ch.send(payload)
        except Exception:  # noqa: BLE001 — a dead channel must not raise
            self._count_drop(etype, "send_error")

    def _emit_error(self, code: str, *, detail: str = "",
                    turn_id: str = "", utterance_id: str = "") -> None:
        event: dict[str, Any] = {"v": EVENT_VERSION, "type": "error", "code": code}
        if detail:
            # The FE's zod schema caps detail at 1024 chars and drops the WHOLE
            # frame if exceeded — a giant engine exception would leave the user
            # with a dead turn and no error. Truncate, never let it reject.
            event["detail"] = detail[:1024]
        if turn_id:
            event["turn_id"] = turn_id
        if utterance_id:
            event["utterance_id"] = utterance_id
        self.emit(event)

    def emit_stt_unavailable(self, reason: str = "") -> None:
        """Fatal-STT wire signal (worker ``on_fatal`` → DC error). The session
        close (reason=stt_failed) is scheduled by the manager wiring."""
        self._emit_error("stt_unavailable", detail=reason)

    def _count_drop(self, etype: str, why: str) -> None:
        n = self._drops.get(etype, 0) + 1
        self._drops[etype] = n
        if n == 1:
            log.info(
                "web.voice.dc_drop",
                voice_session_id=self._vid, type=etype, why=why,
            )

    # -- close --------------------------------------------------------------

    async def aclose(self, reason: str = "driver_close") -> None:
        """Idempotent teardown: cancel+await the current turn (full-tail-flush
        pinned by the engine contract) + the loop, drop any queued utterance.
        Bounded by the caller (manager.close, 10 s)."""
        if self._closed:
            return
        self._closed = True
        # Flush any in-flight TTS audio first (the worker's own aclose is driven
        # separately by _drain_pipeline). None-safe when no worker attached.
        self.interrupt_speech("driver_close")
        if self._pending is not None:
            log.info(
                "web.voice.queued_utterance_dropped",
                voice_session_id=self._vid, utterance_id=self._pending[0],
            )
            self._pending = None
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._wake.set()  # unblock the loop so it can observe _closed
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.info(
            "web.voice.driver_closed",
            voice_session_id=self._vid, reason=reason,
            drops={k: v for k, v in self._drops.items()},
        )
