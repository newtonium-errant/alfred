"""The capture service — the chain, assembled (task #81, stage 1).

Feed it audio; it holds a ring, listens locally, and on a cue extracts a
window, transcribes it, classifies the verb and dispatches. Every component
it drives is a seam, so the whole chain runs in the suite against synthetic
audio, a scripted detector and a stub STT backend — no microphone, no model
files, no network.

DISPATCH, and where each verb's output lands:

* **MARK-DOWN** → the local log (:mod:`.marklog`). Stays on the device.
* **ROUTE** → the injected sink (stage 2's peer intake route). Leaves.
* **MISS REPORT** → the local log, tagged ``miss``, plus its telemetry row.
  The scarce signal: cue false-negatives are invisible by construction, so
  this is the only labelled negative example the system can ever get.
* **NONE** → a telemetry row with its reason, and nothing else. A cue fired
  and no verb matched; that is data, not an error.

**A ROUTE with no sink stays LOCAL.** It is written to the mark log with a
loud warning rather than dropped or retried. Dropping would lose the
operator's words for a configuration mistake he cannot see from the garage;
"send it somewhere" for an unconfigured destination is the one thing a
capture device must never improvise.

**The gate runs FIRST**, before extraction and before any network call
(:mod:`.gate`). A refused capture costs nothing and leaves one logged
decision. That ordering is the difference between a fence and a filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import structlog

from . import cues, marklog, stt, telemetry, window
from .audio import AudioChunk, AudioFormat, AudioSource
from .config import JeevesConfig
from .gate import JeevesCaptureRefused, guard_capture
from .ring import RingBuffer
from .wake import WakeEvent, WakeWordDetector, build_detector

log = structlog.get_logger(__name__)

#: What happened to one cued capture. Distinct from the cue VERB: a ROUTE
#: cue whose sink was unconfigured has verb=route and disposition=marked.
DISPOSITION_MARKED = "marked"
DISPOSITION_ROUTED = "routed"
DISPOSITION_MISS_LOGGED = "miss_logged"
DISPOSITION_DROPPED = "dropped"
DISPOSITION_REFUSED = "refused"


@dataclass(frozen=True)
class RoutedCapture:
    """What leaves the device on a ROUTE cue.

    A TRANSCRIPT and content-free capture facts. NEVER audio — the window's
    bytes are freed when this is built, and there is no field here that
    could carry them. That is fence 2 surviving all the way to the wire.
    """

    transcript: str
    verb: str
    captured_at: str
    target: str
    #: The same content-free facts the telemetry row carries.
    capture_facts: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CaptureSink(Protocol):
    """Where a ROUTE capture goes. Stage 2 supplies the peer-route sink."""

    async def send(self, capture: RoutedCapture) -> bool:  # pragma: no cover
        ...


SinkCallable = Callable[[RoutedCapture], Awaitable[bool]]


@dataclass(frozen=True)
class CaptureOutcome:
    """One cue, start to finish — the service's unit of observable work."""

    verb: str
    disposition: str
    reason: str = ""
    transcript_chars: int = 0
    lookback_used_seconds: float = 0.0
    stt_calls: int = 0
    telemetry_written: bool = False


class JeevesService:
    """Drives the capture chain over a stream of audio chunks."""

    def __init__(
        self,
        config: JeevesConfig,
        *,
        audio_format: AudioFormat | None = None,
        detector: WakeWordDetector | None = None,
        stt_backend: Any = None,
        route_sink: SinkCallable | None = None,
        provenance: Any = None,
    ) -> None:
        self.config = config
        self.audio_format = audio_format or AudioFormat(
            sample_rate=config.ring.sample_rate,
            sample_width=config.ring.sample_width,
            channels=config.ring.channels,
        )
        self.ring = RingBuffer(self.audio_format, config.ring.seconds)
        self.detector = detector if detector is not None else build_detector(
            config.wake, self.audio_format,
        )
        self._stt_backend = stt_backend
        self._route_sink = route_sink
        #: Provenance for the mode gate, stored VERBATIM and never coerced.
        #: The gate's synthetic test is deliberately strict about type, so
        #: normalising a string or a None into a dict here would quietly
        #: relax the very check it exists to make. Absent (the default) is
        #: REFUSED in synthetic mode — the correct posture for a service
        #: constructed without anyone declaring what it is listening to.
        self._provenance = provenance
        self._pending: _PendingCapture | None = None
        self._cues_seen = 0
        self._captures_completed = 0

    # -- the audio path --------------------------------------------------

    async def feed(self, chunk: AudioChunk) -> list[CaptureOutcome]:
        """Push one chunk through the whole chain.

        Returns the outcomes that COMPLETED on this chunk — usually empty,
        because a cue's lookahead spans many chunks.
        """
        self.ring.append(chunk.data)

        outcomes: list[CaptureOutcome] = []
        if self._pending is not None:
            if self._pending.lookahead.feed(chunk):
                outcomes.append(await self._finish_pending())
            # A cue DURING a lookahead is folded into the running capture
            # rather than queued. Two cues three seconds apart are one
            # thought, and treating them as two captures would double the
            # STT spend to produce two overlapping records.
            return outcomes

        for event in self.detector.feed(chunk):
            self._cues_seen += 1
            self._pending = _PendingCapture(
                event=event,
                lookahead=window.LookaheadCollector(
                    self.audio_format, self.config.window,
                ),
            )
            log.info(
                "jeeves.service.cue_fired",
                position_s=round(event.position_s, 3),
                confidence=round(event.confidence, 4),
                model=event.model,
            )
            break  # one capture at a time; the rest of this chunk feeds it
        return outcomes

    async def flush(self) -> list[CaptureOutcome]:
        """Complete any capture still collecting lookahead (stream end)."""
        if self._pending is None:
            return []
        self._pending.lookahead.close()
        return [await self._finish_pending()]

    async def run(self, source: AudioSource) -> list[CaptureOutcome]:
        """Drive the whole of ``source`` and return every outcome.

        The convenience entry point for a file-backed replay and for tests.
        A live mic adapter calls :meth:`feed` from its own loop instead.
        """
        outcomes: list[CaptureOutcome] = []
        for chunk in source.chunks():
            outcomes.extend(await self.feed(chunk))
        outcomes.extend(await self.flush())
        if not outcomes:
            # Intentionally-left-blank: a run that captured nothing is the
            # NORMAL case for a quiet garage and must be visibly distinct
            # from a run that never happened.
            log.info(
                "jeeves.service.idle",
                cues_seen=self._cues_seen,
                audio_seconds=round(self.ring.position_s, 3),
                ring_held_seconds=round(self.ring.held_seconds, 3),
                detail="ran, nothing to do — no cue produced a capture",
            )
            self._emit_telemetry(telemetry.TelemetryRow(
                at=telemetry.now_iso(),
                event=telemetry.EVENT_SERVICE_IDLE,
                ring_held_seconds=round(self.ring.held_seconds, 3),
            ))
        return outcomes

    # -- capture completion ----------------------------------------------

    async def _finish_pending(self) -> CaptureOutcome:
        pending = self._pending
        self._pending = None
        assert pending is not None  # only called with one in flight
        self._captures_completed += 1

        source_id = f"cue@{pending.event.position_s:.3f}s"
        try:
            guard_capture(
                self.config, provenance=self._provenance, source_id=source_id,
            )
        except JeevesCaptureRefused as exc:
            # Refused BEFORE extraction and before any network call — no
            # audio leaves, nothing is written, one decision is logged.
            outcome = CaptureOutcome(
                verb=cues.CUE_NONE, disposition=DISPOSITION_REFUSED,
                reason=exc.reason,
            )
            self._emit_telemetry(telemetry.TelemetryRow(
                at=telemetry.now_iso(),
                event=telemetry.EVENT_CAPTURE_DROPPED,
                reason=exc.reason,
                confidence=round(pending.event.confidence, 4),
                ring_held_seconds=round(self.ring.held_seconds, 3),
            ))
            return outcome

        lookback = window.resolve_lookback(None, self.config.window)
        extracted = window.extract(
            self.ring,
            cue_position_s=pending.event.position_s,
            lookback=lookback,
            lookahead_audio=pending.lookahead.audio,
            lookahead_end_reason=pending.lookahead.end_reason,
        )
        transcript = await self._transcribe(extracted.audio)
        stt_calls = 1

        classification = cues.classify(
            transcript.text,
            cue_config=self.config.cues,
            wake_word=self.config.wake.wake_word,
            confusable_terms=tuple(self.config.wake.confusable_terms),
            extended_lookback_seconds=self.config.window.extended_lookback_seconds,
        )

        # The second pass — see :mod:`.window` for why it is two passes and
        # not one. Only a SPOKEN modifier triggers it.
        if classification.lookback_seconds is not None:
            wider = window.resolve_lookback(
                classification.lookback_seconds, self.config.window,
            )
            if wider.seconds > extracted.lookback_used_seconds:
                log.info(
                    "jeeves.service.modifier_reextract",
                    modifier=classification.modifier,
                    first_lookback_seconds=round(
                        extracted.lookback_used_seconds, 3),
                    requested_lookback_seconds=round(wider.seconds, 3),
                    detail="a spoken lookback modifier asked for more than the "
                           "default window; re-extracting and re-transcribing",
                )
                extracted = window.extract(
                    self.ring,
                    cue_position_s=pending.event.position_s,
                    lookback=wider,
                    lookahead_audio=pending.lookahead.audio,
                    lookahead_end_reason=pending.lookahead.end_reason,
                )
                wider_transcript = await self._transcribe(extracted.audio)
                stt_calls = 2
                if wider_transcript.ok and wider_transcript.text:
                    transcript = wider_transcript

        return await self._dispatch(
            pending.event, classification, transcript, extracted, stt_calls,
        )

    async def _transcribe(self, audio: bytes) -> stt.JeevesTranscript:
        return await stt.transcribe_window(
            audio,
            config=self.config.stt,
            sample_rate=self.audio_format.sample_rate,
            sample_width=self.audio_format.sample_width,
            channels=self.audio_format.channels,
            backend=self._stt_backend,
        )

    # -- dispatch ---------------------------------------------------------

    async def _dispatch(
        self,
        event: WakeEvent,
        classification: cues.CueClassification,
        transcript: stt.JeevesTranscript,
        extracted: window.ExtractedWindow,
        stt_calls: int,
    ) -> CaptureOutcome:
        facts = self._capture_facts(event, extracted, transcript, stt_calls)

        if classification.verb == cues.CUE_MISS_REPORT:
            wrote = marklog.append_mark(
                self.config.mark_log_path, transcript.text,
                kind=marklog.KIND_MISS, provenance=facts,
            )
            self._emit_telemetry(self._row(
                telemetry.EVENT_MISS_REPORTED, classification, extracted,
                transcript, event, stt_calls,
            ))
            self._note_miss_audio_inert()
            return CaptureOutcome(
                verb=classification.verb,
                disposition=DISPOSITION_MISS_LOGGED if wrote
                else DISPOSITION_DROPPED,
                reason="" if wrote else "marklog_write_failed",
                transcript_chars=len(transcript.text),
                lookback_used_seconds=extracted.lookback_used_seconds,
                stt_calls=stt_calls,
                telemetry_written=True,
            )

        if classification.verb == cues.CUE_ROUTE:
            return await self._dispatch_route(
                event, classification, transcript, extracted, stt_calls, facts,
            )

        if classification.verb == cues.CUE_MARK_DOWN:
            wrote = marklog.append_mark(
                self.config.mark_log_path, transcript.text,
                kind=marklog.KIND_MARK, provenance=facts,
            )
            self._emit_telemetry(self._row(
                telemetry.EVENT_CAPTURE_MARKED, classification, extracted,
                transcript, event, stt_calls,
            ))
            return CaptureOutcome(
                verb=classification.verb,
                disposition=DISPOSITION_MARKED if wrote else DISPOSITION_DROPPED,
                reason="" if wrote else "marklog_write_failed",
                transcript_chars=len(transcript.text),
                lookback_used_seconds=extracted.lookback_used_seconds,
                stt_calls=stt_calls,
                telemetry_written=True,
            )

        # CUE_NONE — a cue fired and no verb matched. Real data about cue
        # precision, and the reason distinguishes "wrong words" from
        # "route target unset" from "the window transcribed to nothing".
        log.info(
            "jeeves.service.no_verb",
            reason=classification.reason,
            transcript_chars=len(transcript.text),
            lookback_used_seconds=round(extracted.lookback_used_seconds, 3),
            detail="a cue fired but the transcript carried no known verb — "
                   "nothing was written and nothing left the device",
        )
        self._emit_telemetry(self._row(
            telemetry.EVENT_CAPTURE_DROPPED, classification, extracted,
            transcript, event, stt_calls,
        ))
        return CaptureOutcome(
            verb=cues.CUE_NONE, disposition=DISPOSITION_DROPPED,
            reason=classification.reason,
            transcript_chars=len(transcript.text),
            lookback_used_seconds=extracted.lookback_used_seconds,
            stt_calls=stt_calls, telemetry_written=True,
        )

    async def _dispatch_route(
        self,
        event: WakeEvent,
        classification: cues.CueClassification,
        transcript: stt.JeevesTranscript,
        extracted: window.ExtractedWindow,
        stt_calls: int,
        facts: dict[str, Any],
    ) -> CaptureOutcome:
        if self._route_sink is None:
            # Keep it LOCAL. See the module docstring: dropping loses the
            # operator's words for a mistake he cannot see from the garage,
            # and improvising a destination is worse than both.
            log.error(
                "jeeves.service.route_sink_unconfigured",
                target=classification.route_target,
                detail="a ROUTE cue fired but no capture sink is wired, so "
                       "the transcript was written to the LOCAL mark log "
                       "instead of being sent. Nothing was lost and nothing "
                       "left the device.",
            )
            wrote = marklog.append_mark(
                self.config.mark_log_path, transcript.text,
                kind=marklog.KIND_MARK,
                provenance={**facts, "route_fallback": True},
            )
            self._emit_telemetry(self._row(
                telemetry.EVENT_CAPTURE_MARKED, classification, extracted,
                transcript, event, stt_calls, reason="route_sink_unconfigured",
            ))
            return CaptureOutcome(
                verb=classification.verb,
                disposition=DISPOSITION_MARKED if wrote else DISPOSITION_DROPPED,
                reason="route_sink_unconfigured",
                transcript_chars=len(transcript.text),
                lookback_used_seconds=extracted.lookback_used_seconds,
                stt_calls=stt_calls, telemetry_written=True,
            )

        if not transcript.text.strip():
            # Never route an empty capture: it would create a vault record
            # with no content, which the operator then has to read and
            # delete at morning review.
            log.info(
                "jeeves.service.route_skipped_empty",
                detail="a ROUTE cue transcribed to nothing; no record created",
            )
            self._emit_telemetry(self._row(
                telemetry.EVENT_CAPTURE_DROPPED, classification, extracted,
                transcript, event, stt_calls, reason=stt.STT_EMPTY_RESULT,
            ))
            return CaptureOutcome(
                verb=classification.verb, disposition=DISPOSITION_DROPPED,
                reason=stt.STT_EMPTY_RESULT, stt_calls=stt_calls,
                lookback_used_seconds=extracted.lookback_used_seconds,
                telemetry_written=True,
            )

        capture = RoutedCapture(
            transcript=transcript.text,
            verb=classification.verb,
            captured_at=telemetry.now_iso(),
            target=classification.route_target,
            capture_facts=facts,
        )
        try:
            sent = await self._route_sink(capture)
        except Exception as exc:  # noqa: BLE001 — a sink must not kill the loop
            log.error(
                "jeeves.service.route_failed",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
                target=classification.route_target,
            )
            sent = False

        if not sent:
            # The send failed. Fall back to the local log so the words
            # survive; the operator can move them by hand.
            marklog.append_mark(
                self.config.mark_log_path, transcript.text,
                kind=marklog.KIND_MARK,
                provenance={**facts, "route_failed": True},
            )
            self._emit_telemetry(self._row(
                telemetry.EVENT_CAPTURE_MARKED, classification, extracted,
                transcript, event, stt_calls, reason="route_send_failed",
            ))
            return CaptureOutcome(
                verb=classification.verb, disposition=DISPOSITION_MARKED,
                reason="route_send_failed",
                transcript_chars=len(transcript.text),
                lookback_used_seconds=extracted.lookback_used_seconds,
                stt_calls=stt_calls, telemetry_written=True,
            )

        self._emit_telemetry(self._row(
            telemetry.EVENT_CAPTURE_ROUTED, classification, extracted,
            transcript, event, stt_calls,
        ))
        return CaptureOutcome(
            verb=classification.verb, disposition=DISPOSITION_ROUTED,
            transcript_chars=len(transcript.text),
            lookback_used_seconds=extracted.lookback_used_seconds,
            stt_calls=stt_calls, telemetry_written=True,
        )

    # -- observability helpers -------------------------------------------

    def _capture_facts(
        self,
        event: WakeEvent,
        extracted: window.ExtractedWindow,
        transcript: stt.JeevesTranscript,
        stt_calls: int,
    ) -> dict[str, Any]:
        """Content-free provenance travelling with a stored/routed capture."""
        return {
            "cue_confidence": round(event.confidence, 4),
            "lookback_used_seconds": round(extracted.lookback_used_seconds, 3),
            "requested_lookback_seconds": round(
                extracted.requested_lookback_seconds, 3),
            "lookahead_used_seconds": round(
                extracted.lookahead_used_seconds, 3),
            "lookahead_end_reason": extracted.lookahead_end_reason,
            "truncated_by_ring": extracted.truncated_by_ring,
            "stt_calls": stt_calls,
            "stt_backend": transcript.backend_id,
            "stt_confidence_raw": transcript.confidence_raw,
        }

    def _row(
        self,
        event_kind: str,
        classification: cues.CueClassification,
        extracted: window.ExtractedWindow,
        transcript: stt.JeevesTranscript,
        event: WakeEvent,
        stt_calls: int,
        reason: str = "",
    ) -> telemetry.TelemetryRow:
        return telemetry.TelemetryRow(
            at=telemetry.now_iso(),
            event=event_kind,
            verb=classification.verb,
            reason=reason or classification.reason,
            matched_phrase=classification.matched_phrase,
            wake_variant=classification.wake_variant,
            confidence=round(event.confidence, 4),
            lookback_used_seconds=round(extracted.lookback_used_seconds, 3),
            requested_lookback_seconds=round(
                extracted.requested_lookback_seconds, 3),
            lookahead_used_seconds=round(extracted.lookahead_used_seconds, 3),
            lookahead_end_reason=extracted.lookahead_end_reason,
            truncated_by_ring=extracted.truncated_by_ring,
            lookback_clamped=extracted.lookback_clamped,
            stt_calls=stt_calls,
            stt_latency_ms=transcript.latency_ms,
            stt_confidence_raw=transcript.confidence_raw,
            has_speech_signal=transcript.has_speech_signal,
            # A CHARACTER COUNT, never the characters. The one number about
            # the transcript that helps read the lookback distribution.
            transcript_chars=len(transcript.text),
            ring_held_seconds=round(self.ring.held_seconds, 3),
        )

    def _emit_telemetry(self, row: telemetry.TelemetryRow) -> bool:
        """Write a row, never letting a telemetry problem stop a capture."""
        try:
            return telemetry.append_row(self.config.telemetry_path, row)
        except telemetry.TelemetryRefused as exc:
            log.error(
                "jeeves.telemetry.refused",
                reason=exc.reason,
                field=exc.field_name,
                detail=exc.detail[:300],
            )
            return False
        except OSError as exc:
            log.error(
                "jeeves.telemetry.write_failed",
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return False

    def _note_miss_audio_inert(self) -> None:
        """Say out loud that miss-report AUDIO is not being kept.

        The design calls a miss report the highest-value training data this
        system can produce, and v1 keeps only its transcript. An operator
        reporting misses deserves to know the audio behind them is not
        being retained — otherwise he is producing a dataset that does not
        exist.
        """
        if not self.config.miss_audio_dir:
            log.info(
                "jeeves.service.miss_audio_not_retained",
                detail="jeeves.miss_audio_dir is unset (the v1 default), so "
                       "the miss report's transcript was kept and its AUDIO "
                       "was not. Retaining audio is a deliberate decision "
                       "that has not been made.",
            )


@dataclass
class _PendingCapture:
    """A cue whose lookahead is still collecting."""

    event: WakeEvent
    lookahead: window.LookaheadCollector
