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

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import StateManager
from .utils import get_logger

# NOTE: the state half above the "Span extraction" divider imports NOTHING
# heavy — ``session.py`` imports this module for the close-time
# ``spans_frontmatter`` call, so the extraction half below does its
# session/capture_batch/capture_extract/vault imports LAZILY inside the
# functions (the codebase's standing anti-cycle convention).

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


# ---------------------------------------------------------------------------
# Span extraction — the preserved capture machinery, aimed at a span
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanExtractResult:
    """Outcome of one span extraction.

    ``skipped_reason`` names WHY nothing (or less than expected) happened
    — ``""`` on full success, else one of ``no_session`` /
    ``span_not_found`` / ``span_open`` / ``already_extracted`` /
    ``empty_span`` or a pass-through from the note extractor
    (``llm_error: ...`` etc.). A refusal always names its reason; the
    caller maps reasons to statuses.
    """

    record: str = ""
    notes: list[str] = field(default_factory=list)
    skipped_reason: str = ""


def _parse_turn_ts(turn: dict[str, Any] | None) -> datetime | None:
    raw = (turn or {}).get("_ts") or ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def extract_span_material(
    client: Any,
    vault_path: Path,
    turns: list[dict[str, Any]],
    *,
    parent_session_id: str,
    parent_chat_id: int,
    span_start: int,
    span_end: int,
    model: str,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    tool_set: str = "",
    user_vault_path: str = "",
) -> SpanExtractResult:
    """Run the preserved capture pipeline over one span's turns.

    The span becomes its own ``session/`` record — the old whole-session
    capture's record shape, which is exactly what the preserved machinery
    targets — and then rides it end to end:

    1. Write the span record (``capture-<date>-<slug>-<shortid>-s<start>``,
       ``mode: capture``) via the same frontmatter/body builders the close
       path uses, plus span provenance fields (parent session id, range).
       The ``-s<start>`` suffix is the span's start index: stable across
       the live and post-close paths and unique within a session.
    2. ``capture_batch.process_capture_session`` — the FULL existing
       rules: source/author anchor resolution from the span's first turn,
       the Hypatia ≤1-message memo branch, noise filtering, structuring,
       task emission.
    3. Unless the memo branch terminated it, ``extract_notes_from_record``
       — the preserved ``/extract`` machinery: three-tier target
       discriminator (note/ vs zettel/), anchor preservation, source
       quotes, cross-links, ``derived_notes``.
    4. Hypatia only: flip ``processed: true`` + ``extracted_to`` — unlike
       the bot-era manual ``/extract``, span extraction runs to
       completion here, so leaving the record queued in the "Unprocessed
       captures" Bases view would be a standing lie.

    Never raises; failures degrade to a named ``skipped_reason``.
    """
    from alfred.vault import ops as vault_ops

    from . import capture_batch, capture_extract
    from .session import (
        Session,
        _build_session_body,
        _build_session_frontmatter,
        _first_user_text,
        _slug_from_date,
        _slug_from_topic,
    )

    if not turns:
        return SpanExtractResult(skipped_reason="empty_span")

    now = datetime.now(timezone.utc)
    started_at = _parse_turn_ts(turns[0]) or now
    ended_at = _parse_turn_ts(turns[-1]) or now
    synth = Session(
        session_id=parent_session_id,
        chat_id=parent_chat_id,
        started_at=started_at,
        last_message_at=ended_at,
        model=model,
        transcript=list(turns),
    )
    short_id = (parent_session_id or "").split("-")[0]
    slug = _slug_from_topic(_first_user_text(turns))
    name = (
        f"capture-{_slug_from_date(started_at)}-{slug}-{short_id}"
        f"-s{span_start}"
    )
    fm = _build_session_frontmatter(
        synth,
        ended_at=ended_at,
        reason="capture_span",
        user_vault_path=user_vault_path or None,
        session_type="capture",
        tool_set=tool_set,
        mode="capture",
    )
    # Span provenance — queryable strings, not wikilinks: the PARENT
    # record does not exist yet on the live path (it is written at session
    # close, which stamps the parent-side ``capture_spans`` wikilinks).
    fm["capture_span_parent"] = parent_session_id
    fm["capture_span_range"] = f"{span_start}-{span_end}"

    try:
        result = vault_ops.vault_create(
            vault_path, "session", name, set_fields=fm,
            body=_build_session_body(synth),
        )
    except vault_ops.VaultError as exc:
        log.warning(
            "talker.capture_span.record_create_failed",
            parent_session_id=parent_session_id,
            span=f"{span_start}-{span_end}",
            error=str(exc),
        )
        return SpanExtractResult(skipped_reason=f"record_error: {exc}")
    span_rel = result["path"]
    log.info(
        "talker.capture_span.record_written",
        record=span_rel,
        parent_session_id=parent_session_id,
        span=f"{span_start}-{span_end}",
        turns=len(turns),
    )

    # Full preserved structuring pipeline (memo branch, anchors, tasks).
    await capture_batch.process_capture_session(
        client,
        vault_path,
        span_rel,
        list(turns),
        model,
        send_follow_up=None,
        short_id=short_id,
        agent_slug=agent_slug,
        anchor_scope=anchor_scope,
    )

    # Memo branch terminal? (≤1 user message on Hypatia — the existing
    # rule: the memo IS the artifact; no note extraction follows.)
    import frontmatter as _frontmatter

    notes: list[str] = []
    skipped = ""
    try:
        post = _frontmatter.load(vault_path / span_rel)
    except Exception:  # noqa: BLE001 — record just written; read-back is best-effort
        post = None
    if post is not None and str(post.get("capture_structured")) == "memo":
        memo = str(post.get("memo_record") or "")
        notes = [memo] if memo else []
        skipped = "memo"
        log.info(
            "talker.capture_span.memo_terminal",
            record=span_rel,
            memo=memo,
        )
    else:
        extract = await capture_extract.extract_notes_from_record(
            client,
            vault_path,
            span_rel,
            model,
            agent_slug=agent_slug,
            anchor_scope=anchor_scope,
        )
        notes = list(extract.created_paths)
        skipped = extract.skipped_reason

    if tool_set == "hypatia":
        # Span extraction ran to completion — the record must not sit in
        # the "Unprocessed captures" queue forever. Best-effort.
        try:
            vault_ops.vault_edit(
                vault_path,
                span_rel,
                set_fields={
                    "processed": True,
                    "extracted_to": [f"[[{p}]]" for p in notes],
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.capture_span.processed_flip_failed",
                record=span_rel,
                error=str(exc),
            )

    log.info(
        "talker.capture_span.extracted",
        record=span_rel,
        parent_session_id=parent_session_id,
        span=f"{span_start}-{span_end}",
        notes=len(notes),
        skipped_reason=skipped,
    )
    return SpanExtractResult(record=span_rel, notes=notes,
                             skipped_reason=skipped)


async def extract_capture_span(
    client: Any,
    state: StateManager,
    vault_path: Path,
    chat_id: int,
    span_index: int,
    *,
    model: str,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    tool_set: str = "",
    user_vault_path: str = "",
) -> SpanExtractResult:
    """LIVE-session span extraction (the toggle-off offer's accept path).

    Validates the span (must exist, be CLOSED, and not yet extracted —
    each refusal names its reason), runs :func:`extract_span_material`,
    then marks the span extracted in state so the record path and note
    list survive to the close-time frontmatter.
    """
    active = state.get_active(chat_id)
    if active is None:
        return SpanExtractResult(skipped_reason="no_session")
    spans = raw_spans(active)
    if not (0 <= span_index < len(spans)) or not isinstance(
        spans[span_index], dict
    ):
        return SpanExtractResult(skipped_reason="span_not_found")
    span = spans[span_index]
    if span.get("end") is None:
        return SpanExtractResult(skipped_reason="span_open")
    if bool(span.get("extracted")):
        return SpanExtractResult(
            record=str(span.get("record") or ""),
            notes=list(span.get("notes") or []),
            skipped_reason="already_extracted",
        )
    start, end = int(span.get("start", 0)), int(span["end"])
    result = await extract_span_material(
        client,
        vault_path,
        span_turns(active, span),
        parent_session_id=str(active.get("session_id") or ""),
        parent_chat_id=int(active.get("chat_id") or chat_id),
        span_start=start,
        span_end=end,
        model=model,
        agent_slug=agent_slug,
        anchor_scope=anchor_scope,
        tool_set=tool_set,
        user_vault_path=user_vault_path,
    )
    if result.record:
        mark_span_extracted(
            state, chat_id, span_index,
            record=result.record, notes=result.notes,
        )
    return result


async def finalize_spans_post_close(
    client: Any,
    vault_path: Path,
    active_snapshot: dict[str, Any],
    parent_rel_path: str,
    *,
    model: str,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    tool_set: str = "",
    user_vault_path: str = "",
) -> list[SpanExtractResult]:
    """The close-time backstop: extract every span that never got its offer.

    Runs AFTER ``close_session`` wrote the parent record (whose
    ``capture_spans`` rows still show ``extracted: false`` — the
    intentionally-left-blank signal this drains). ``active_snapshot`` is
    the pre-close active dict (or a reconstruction carrying
    ``transcript`` + ``_capture_spans``). Each extracted span then gets
    its wikilink patched into the PARENT record's ``capture_spans`` rows,
    so the parent-side linkage exists on the post-close path exactly as
    the close path stamps it for live-extracted spans. Failure-isolated
    per span.
    """
    from alfred.vault import ops as vault_ops

    spans = normalized_spans(active_snapshot)
    pending = [s for s in spans if not bool(s.get("extracted"))]
    if not pending:
        log.info(
            "talker.capture_span.finalize_nothing_pending",
            parent=parent_rel_path,
            spans=len(spans),
            detail="ran, nothing to extract — every span already handled",
        )
        return []
    results: list[SpanExtractResult] = []
    for span in pending:
        try:
            result = await extract_span_material(
                client,
                vault_path,
                span_turns(active_snapshot, span),
                parent_session_id=str(
                    active_snapshot.get("session_id") or ""
                ),
                parent_chat_id=int(active_snapshot.get("chat_id") or 0),
                span_start=int(span["start"]),
                span_end=int(span["end"]),
                model=model,
                agent_slug=agent_slug,
                anchor_scope=anchor_scope,
                tool_set=tool_set,
                user_vault_path=user_vault_path,
            )
        except Exception as exc:  # noqa: BLE001 — one span must not sink the rest
            log.warning(
                "talker.capture_span.finalize_span_failed",
                parent=parent_rel_path,
                span=f"{span.get('start')}-{span.get('end')}",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            result = SpanExtractResult(skipped_reason=f"error: {exc}")
        results.append(result)
        if result.record:
            span["extracted"] = True
            span["record"] = result.record
            span["notes"] = list(result.notes)

    # Patch the parent record's capture_spans rows with the outcomes.
    try:
        vault_ops.vault_edit(
            vault_path,
            parent_rel_path,
            set_fields={
                "capture_spans": spans_frontmatter(
                    {
                        "transcript": active_snapshot.get("transcript") or [],
                        CAPTURE_SPANS_KEY: spans,
                    }
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 — linkage patch is best-effort
        log.warning(
            "talker.capture_span.parent_patch_failed",
            parent=parent_rel_path,
            error=str(exc),
        )
    log.info(
        "talker.capture_span.finalized_post_close",
        parent=parent_rel_path,
        extracted=sum(1 for r in results if r.record),
        failed=sum(1 for r in results if not r.record),
    )
    return results


# Retain-set for detached finalization tasks — the same asyncio footgun
# guard capture_batch._STRUCTURING_TASKS exists for: a task nobody holds
# can be garbage-collected mid-flight, and the web reopen handler returns
# immediately after scheduling.
_FINALIZE_TASKS: set[asyncio.Task] = set()


def schedule_span_finalization(
    *,
    client: Any,
    vault_path: Path,
    active_snapshot: dict[str, Any],
    parent_rel_path: str,
    model: str,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    tool_set: str = "",
    user_vault_path: str = "",
) -> "asyncio.Task | None":
    """Detached-task wrapper for :func:`finalize_spans_post_close`.

    Returns the task, or ``None`` when no loop is running (CLI close
    paths) — the parent record's ``extracted: false`` rows remain the
    discoverable signal in that case, logged here so the skip is a
    decision, not silence.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.info(
            "talker.capture_span.finalize_not_scheduled",
            parent=parent_rel_path,
            reason="no running event loop — capture_spans rows on the "
                   "record stay extracted:false (discoverable via grep)",
        )
        return None
    task = loop.create_task(
        finalize_spans_post_close(
            client,
            vault_path,
            active_snapshot,
            parent_rel_path,
            model=model,
            agent_slug=agent_slug,
            anchor_scope=anchor_scope,
            tool_set=tool_set,
            user_vault_path=user_vault_path,
        )
    )
    _FINALIZE_TASKS.add(task)
    task.add_done_callback(_FINALIZE_TASKS.discard)
    return task
