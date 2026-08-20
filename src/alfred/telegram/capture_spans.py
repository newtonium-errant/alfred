"""Capture spans — capture as a MODE within any conversation (R1, 2026-08-20).

The operator's ruling: *"I'd like an unobtrusive capture button. It was
useful when I didn't want to be interrupted. Perhaps even a way to toggle
it on or off mid conversation when I do want a response."*

Capture is no longer a session TYPE (the bot-era ``/capture`` opener died
with Telegram, T4/T5) — it is a toggleable mode inside a normal
conversation. A session accumulates zero or more CAPTURE SPANS: contiguous
turn ranges during which capture was ON. While capture is on, turns are
received and persisted (the ``session_type == "capture"`` short-circuit in
``conversation._prepare_turn`` is the reuse seam — no model call, no
assistant turn) and the extraction machinery (``capture_extract`` /
``capture_batch``, deliberately preserved through T5 pending exactly this
ruling) later consumes each span. The old whole-session capture is the
degenerate case: one span covering everything.

SERVER TRUTH: span state lives on the active-session dict as stashed
``_*`` keys (the ``_session_type`` precedent — preserved by
``session._persist`` across every ``append_turn``), so a page refresh
mid-capture resumes capturing from state, never from client memory:

* ``_capture_active`` — bool; capture is ON right now.
* ``_capture_spans`` — list of ``{"start", "end", "extracted", "record",
  "notes"}`` dicts. ``start`` is the transcript index of the first
  captured turn (``len(transcript)`` at toggle-on — the toggle-on turn
  onward is captured; the turn sent one tick before is NOT). ``end`` is
  ``None`` while the span is open, else the EXCLUSIVE end index stamped
  at toggle-off. ``extracted`` flips when the span's material has been
  run through the extraction machinery; ``record`` / ``notes`` then carry
  the span session-record rel path and created note paths.

This module is deliberately transport-agnostic and instance-generic: the
web ``/chat/capture`` routes are today's producer; the CRT blank-screen
capture page can be a second producer later by driving the same
``begin_capture`` / ``end_capture`` / span-extraction seam. Nothing here
knows about aiohttp.
"""

from __future__ import annotations

from typing import Any

from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


# Stashed-key names — single source of truth for both the state mutators
# below and any reader (routes, close path, tests).
CAPTURE_ACTIVE_KEY = "_capture_active"
CAPTURE_SPANS_KEY = "_capture_spans"


def capture_active(active: dict[str, Any] | None) -> bool:
    """Is capture ON for this active-session dict? (Server truth.)"""
    return bool((active or {}).get(CAPTURE_ACTIVE_KEY))


def raw_spans(active: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The stored span list (possibly with a trailing open span). Never None."""
    spans = (active or {}).get(CAPTURE_SPANS_KEY)
    return list(spans) if isinstance(spans, list) else []


def normalized_spans(active: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Spans with any trailing OPEN span closed at the current transcript end.

    Pure — does not mutate state. This is the single derivation both the
    close path (record frontmatter) and the post-close finalizer use, so a
    session that closes while capture is still ON yields the same span the
    operator would have gotten by toggling off first: material is never
    lost to an idle timeout. Empty spans (``end == start`` — a toggle
    on/off with nothing said) are dropped here for the same reason
    ``end_capture`` drops them live.
    """
    transcript_len = len((active or {}).get("transcript") or [])
    out: list[dict[str, Any]] = []
    for span in raw_spans(active):
        if not isinstance(span, dict):
            continue
        start = int(span.get("start", 0))
        end = span.get("end")
        end_idx = transcript_len if end is None else int(end)
        if end_idx <= start:
            continue  # empty span — nothing was captured
        norm = dict(span)
        norm["start"] = start
        norm["end"] = end_idx
        out.append(norm)
    return out


def spans_summary(active: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Wire-shape span summary for the web surface (always-present fields).

    One row per stored span: ``{"index", "start", "end", "turns",
    "extracted"}``. ``end`` is ``None`` for the open span (capture ON) and
    ``turns`` then counts captured turns so far. Empty CLOSED spans never
    exist in storage (``end_capture`` drops them), so every closed row here
    has ``turns >= 1``.
    """
    transcript_len = len((active or {}).get("transcript") or [])
    out: list[dict[str, Any]] = []
    for i, span in enumerate(raw_spans(active)):
        if not isinstance(span, dict):
            continue
        start = int(span.get("start", 0))
        end = span.get("end")
        end_idx = transcript_len if end is None else int(end)
        out.append({
            "index": i,
            "start": start,
            "end": None if end is None else int(end),
            "turns": max(0, end_idx - start),
            "extracted": bool(span.get("extracted")),
        })
    return out


def span_turns(
    active: dict[str, Any] | None, span: dict[str, Any],
) -> list[dict[str, Any]]:
    """The transcript slice a (normalized) span covers."""
    transcript = list((active or {}).get("transcript") or [])
    start = int(span.get("start", 0))
    end = span.get("end")
    end_idx = len(transcript) if end is None else int(end)
    return transcript[start:end_idx]


def unextracted_spans(
    active: dict[str, Any] | None,
) -> list[tuple[int, dict[str, Any]]]:
    """(index, span) for every CLOSED, non-empty, unextracted span."""
    out: list[tuple[int, dict[str, Any]]] = []
    for i, span in enumerate(raw_spans(active)):
        if not isinstance(span, dict):
            continue
        end = span.get("end")
        if end is None:
            continue  # still open
        if int(end) <= int(span.get("start", 0)):
            continue  # defensive: empty (storage normally never holds these)
        if bool(span.get("extracted")):
            continue
        out.append((i, span))
    return out


def _persist_active(
    state: StateManager, chat_id: int, active: dict[str, Any],
) -> None:
    state.set_active(chat_id, active)
    state.save()


def begin_capture(state: StateManager, chat_id: int) -> dict[str, Any]:
    """Turn capture ON: open a new span at the current transcript end.

    Idempotent: if capture is already on, the existing open span is kept
    (no nested spans) and the current state is returned — a double-tap or
    a stale client can never fork the span list.

    Returns ``{"capture_active": bool, "spans": [...]}`` (the wire shape).
    """
    active = state.get_active(chat_id)
    if active is None:
        raise ValueError(f"No active session for chat_id={chat_id}")
    if capture_active(active):
        log.info(
            "talker.capture_span.begin_idempotent",
            chat_id=chat_id,
            session_id=active.get("session_id", ""),
            detail="capture already on — existing open span kept",
        )
        return {"capture_active": True, "spans": spans_summary(active)}
    spans = raw_spans(active)
    start = len(active.get("transcript") or [])
    spans.append({
        "start": start,
        "end": None,
        "extracted": False,
        "record": "",
        "notes": [],
    })
    active[CAPTURE_SPANS_KEY] = spans
    active[CAPTURE_ACTIVE_KEY] = True
    _persist_active(state, chat_id, active)
    log.info(
        "talker.capture_span.opened",
        chat_id=chat_id,
        session_id=active.get("session_id", ""),
        span_index=len(spans) - 1,
        start=start,
    )
    return {"capture_active": True, "spans": spans_summary(active)}


def end_capture(
    state: StateManager, chat_id: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Turn capture OFF: close the open span at the current transcript end.

    An EMPTY span (toggle on → off with nothing captured) is DROPPED from
    storage — "ran, nothing captured" is logged explicitly so the discard
    is observable, and no extraction offer follows.

    Idempotent: if capture is already off, returns the current state with
    ``closed_span=None``.

    Returns ``(wire_state, closed_span_summary | None)`` where the summary
    is ``{"index", "turns"}`` for the just-closed non-empty span.
    """
    active = state.get_active(chat_id)
    if active is None:
        raise ValueError(f"No active session for chat_id={chat_id}")
    if not capture_active(active):
        log.info(
            "talker.capture_span.end_idempotent",
            chat_id=chat_id,
            session_id=active.get("session_id", ""),
            detail="capture already off — nothing to close",
        )
        return (
            {"capture_active": False, "spans": spans_summary(active)},
            None,
        )
    spans = raw_spans(active)
    closed: dict[str, Any] | None = None
    end = len(active.get("transcript") or [])
    # Close the trailing open span (begin_capture only ever appends the
    # open span last; idempotency means there is at most one).
    for i in range(len(spans) - 1, -1, -1):
        span = spans[i]
        if isinstance(span, dict) and span.get("end") is None:
            start = int(span.get("start", 0))
            if end <= start:
                spans.pop(i)
                log.info(
                    "talker.capture_span.empty_span_discarded",
                    chat_id=chat_id,
                    session_id=active.get("session_id", ""),
                    start=start,
                    detail="ran, nothing captured — span dropped, no "
                           "extraction offer",
                )
            else:
                span["end"] = end
                closed = {"index": i, "turns": end - start}
                log.info(
                    "talker.capture_span.closed",
                    chat_id=chat_id,
                    session_id=active.get("session_id", ""),
                    span_index=i,
                    start=start,
                    end=end,
                    turns=end - start,
                )
            break
    active[CAPTURE_SPANS_KEY] = spans
    active[CAPTURE_ACTIVE_KEY] = False
    _persist_active(state, chat_id, active)
    return (
        {"capture_active": False, "spans": spans_summary(active)},
        closed,
    )


def mark_span_extracted(
    state: StateManager,
    chat_id: int,
    span_index: int,
    *,
    record: str,
    notes: list[str],
) -> bool:
    """Flip a span to extracted and record its outputs. False if gone.

    ``False`` (session or span no longer present — e.g. the session closed
    while extraction ran) is NOT an error for the caller: the close path's
    own finalizer owns the post-close bookkeeping. Logged either way so
    the outcome is observable.
    """
    active = state.get_active(chat_id)
    if active is None:
        log.info(
            "talker.capture_span.mark_extracted_no_session",
            chat_id=chat_id,
            span_index=span_index,
            detail="session closed while extraction ran — record keeps "
                   "the result; state has nothing to update",
        )
        return False
    spans = raw_spans(active)
    if not (0 <= span_index < len(spans)) or not isinstance(
        spans[span_index], dict
    ):
        log.warning(
            "talker.capture_span.mark_extracted_no_span",
            chat_id=chat_id,
            span_index=span_index,
            spans=len(spans),
        )
        return False
    spans[span_index]["extracted"] = True
    spans[span_index]["record"] = record
    spans[span_index]["notes"] = list(notes)
    active[CAPTURE_SPANS_KEY] = spans
    _persist_active(state, chat_id, active)
    log.info(
        "talker.capture_span.marked_extracted",
        chat_id=chat_id,
        session_id=active.get("session_id", ""),
        span_index=span_index,
        record=record,
        notes=len(notes),
    )
    return True


def spans_frontmatter(active: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ``capture_spans`` field for the close-time session record.

    One row per NON-EMPTY span (open spans are closed at the transcript
    end — ``normalized_spans``): ``{"span": "<start>-<end>", "turns",
    "extracted", "record"}``. ``record`` is a wikilink to the span's own
    session record when extraction has run, else ``""`` — always present,
    so an unextracted span is visibly unextracted on the record (the
    intentionally-left-blank signal the close-time backstop then drains).
    """
    out: list[dict[str, Any]] = []
    for span in normalized_spans(active):
        record = str(span.get("record") or "")
        if record.endswith(".md"):
            record = record[:-3]
        out.append({
            "span": f"{span['start']}-{span['end']}",
            "turns": int(span["end"]) - int(span["start"]),
            "extracted": bool(span.get("extracted")),
            "record": f"[[{record}]]" if record else "",
        })
    return out
