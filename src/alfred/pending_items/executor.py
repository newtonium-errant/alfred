"""Action plan executor — runs structured resolution actions.

Phase 1 implements two action types:

* ``noop`` — flips the queue item's status to ``resolved`` without
  any side effects. Used by the ``noted`` resolution option.
* ``deliver_text`` — re-fetches the session record + the assistant
  turn at ``turn_index``, runs it through the Telegram chunker, and
  dispatches via the existing outbound-push transport. The session
  frontmatter's ``outbound_failures`` entry stays in place — the
  delivery happens via the transport's own audit path; the queue
  resolution is the user-facing closure.

Phase 3 will add ``merge_records``, ``rewrite_wikilinks``,
``delete_record``, ``edit_frontmatter`` with atomicity + rollback +
idempotence.

This module runs locally on the originating instance. Salem's
resolver dispatches to the right instance via
:func:`pending_items_resolve` peer call; the originator's handler
then invokes :func:`execute_action_plan` against its own queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter
import structlog

from .queue import (
    ActionPlan,
    PendingItem,
    find_by_id,
    mark_resolved,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers — re-fetch failed text from the session record
# ---------------------------------------------------------------------------


def _resolve_session_path(
    vault_path: Path,
    session_id: str,
    session_subpath: str = "session",
) -> Path | None:
    """Walk the session vault for a record whose telegram.session_id matches.

    Session records are filed under ``session/<Mode> — <date> <slug>.md``
    (per :mod:`alfred.telegram.session`). We can't compute the filename
    from session_id alone, so we walk the directory and match the
    ``telegram.session_id`` frontmatter field.
    """
    base = vault_path / session_subpath
    if not base.exists() or not base.is_dir():
        return None
    for record_path in base.glob("*.md"):
        try:
            post = frontmatter.load(str(record_path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        telegram = fm.get("telegram") or {}
        if isinstance(telegram, dict) and str(telegram.get("session_id") or "") == session_id:
            return record_path
        # Fallback: match on file stem (covers non-talker session records
        # that don't have a telegram block).
        if record_path.stem == session_id:
            return record_path
    return None


def _extract_assistant_turn_text(
    record_path: Path,
    turn_index: int,
) -> str | None:
    """Read the assistant turn at ``turn_index`` from the session record body.

    Session records render transcripts as Markdown with a turn-per-line
    pattern (``**Alfred** (HH:MM): ...``). We don't try to robustly parse
    that — it's fragile across instances. Instead we re-read the persisted
    ``transcript`` array out of the live talker state if available.

    Phase 1 simplification: the session frontmatter doesn't carry the
    full transcript (the body does, in display form). For this Phase
    we read the frontmatter ``telegram.transcript_path`` if present;
    otherwise fall back to scanning the markdown body for the Nth
    ``**Alfred** (...):`` line. This is best-effort — if the session
    record's render-format changes, the executor surfaces that as an
    error string and the user sees "couldn't reconstruct" rather than
    silently delivering garbage.
    """
    try:
        post = frontmatter.load(str(record_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pending_items.executor.session_read_failed",
            path=str(record_path),
            error=str(exc),
        )
        return None

    body = post.content or ""
    # Walk lines, count Alfred turns, return the body of the Nth one.
    # The renderer in ``session._build_session_body`` emits
    # ``**Alfred** (HH:MM): TEXT`` per line. Voice/text user lines have
    # the pattern ``**Andrew** (HH:MM[ · voice]): TEXT``.
    #
    # turn_index is the global transcript index (user + assistant
    # combined). We map global → assistant-only by counting both
    # speakers and stopping when ``turn_index`` matches.
    lines = body.splitlines()
    line_idx = 0
    user_count = 0
    assistant_count = 0
    for line in lines:
        if line.startswith("**Andrew**"):
            if line_idx == turn_index:
                # The failed turn was a user turn — that should not
                # happen for an outbound_failure (only assistant turns
                # outbound), but defensive: return the line so the
                # operator sees something rather than nothing.
                return line.split(":", 1)[1].strip() if ":" in line else line
            line_idx += 1
            user_count += 1
            continue
        if line.startswith("**Alfred**"):
            if line_idx == turn_index:
                # Single-line case: ``**Alfred** (HH:MM): text``
                if ":" in line:
                    head, _, tail = line.partition(":")
                    # Skip the timestamp colon — the partition above
                    # split at the FIRST colon, which is inside
                    # ``(HH:MM)``. Find the colon AFTER the closing
                    # paren instead.
                    paren_idx = line.find("):")
                    if paren_idx != -1:
                        return line[paren_idx + 2:].strip()
                    return tail.strip()
                return line
            line_idx += 1
            assistant_count += 1
            continue
    log.warning(
        "pending_items.executor.turn_not_found",
        path=str(record_path),
        turn_index=turn_index,
        assistant_count=assistant_count,
        user_count=user_count,
    )
    return None


# ---------------------------------------------------------------------------
# Public executor
# ---------------------------------------------------------------------------


async def execute_action_plan(
    *,
    plan: ActionPlan,
    vault_path: Path,
    user_id: int,
) -> dict[str, Any]:
    """Run one action plan. Returns a result dict.

    Result shape::

        {"executed": <bool>, "summary": "<text>", "error": "<text|none>"}

    Phase 1 only handles ``noop`` + ``deliver_text``. Other types
    return ``{"executed": false, "error": "phase_3_not_yet_implemented"}``
    so the resolver can surface a clear "not yet supported" reply.
    """
    type_ = (plan.type or "").lower()
    if type_ == "noop":
        return {
            "executed": True,
            "summary": "noop",
            "error": None,
        }

    if type_ == "deliver_text":
        return await _execute_deliver_text(plan=plan, vault_path=vault_path, user_id=user_id)

    # Phase 3 categories — stored shapes that don't yet execute.
    log.info(
        "pending_items.executor.unsupported_type",
        plan_type=type_,
    )
    return {
        "executed": False,
        "summary": f"unsupported action plan type: {type_}",
        "error": "phase_3_not_yet_implemented",
    }


async def _execute_deliver_text(
    *,
    plan: ActionPlan,
    vault_path: Path,
    user_id: int,
) -> dict[str, Any]:
    """Re-deliver the assistant turn referenced by the action plan.

    Looks up the session record by id, extracts the Nth assistant
    turn from the markdown body, dispatches via the existing
    ``send_outbound_batch`` transport client.

    Failure modes (each returns ``executed=False``):
      * session record not found
      * turn_index out of range
      * transport send fails

    Success returns ``executed=True`` plus the transport's response
    summary so the operator can correlate.
    """
    params = plan.params
    session_id = str(params.get("session_id") or "").strip()
    try:
        turn_index = int(params.get("turn_index", -1))
    except (TypeError, ValueError):
        turn_index = -1

    if not session_id or turn_index < 0:
        return {
            "executed": False,
            "summary": "invalid deliver_text params",
            "error": "missing session_id or turn_index",
        }

    if user_id <= 0:
        return {
            "executed": False,
            "summary": "no telegram user configured",
            "error": "user_id_unset",
        }

    record_path = _resolve_session_path(vault_path, session_id)
    if record_path is None:
        return {
            "executed": False,
            "summary": f"session record for {session_id} not found",
            "error": "session_not_found",
        }

    text = _extract_assistant_turn_text(record_path, turn_index)
    if text is None or not text.strip():
        return {
            "executed": False,
            "summary": f"could not reconstruct assistant turn {turn_index}",
            "error": "turn_extraction_failed",
        }

    # Dispatch via the outbound transport — same path the talker uses
    # for chunked replies. Dedupe key includes the session id + turn
    # so an accidental double-execute is server-side-deduped.
    from alfred.transport.client import send_outbound_batch
    from alfred.transport.exceptions import TransportError
    from alfred.transport.utils import chunk_for_telegram

    chunks = chunk_for_telegram(text)
    if not chunks:
        return {
            "executed": False,
            "summary": "chunk_for_telegram returned empty",
            "error": "chunker_returned_empty",
        }

    short_session = (session_id or "unknown")[:12]
    dedupe_key = f"pending-deliver-{short_session}-{turn_index}"
    try:
        response = await send_outbound_batch(
            user_id=user_id,
            chunks=chunks,
            dedupe_key=dedupe_key,
            client_name="pending_items",
        )
    except TransportError as exc:
        return {
            "executed": False,
            "summary": f"transport send failed: {exc}",
            "error": exc.__class__.__name__,
        }
    except Exception as exc:  # noqa: BLE001 — defensive
        return {
            "executed": False,
            "summary": f"unexpected error: {exc}",
            "error": exc.__class__.__name__,
        }

    return {
        "executed": True,
        "summary": (
            f"delivered {len(text)} chars in {len(chunks)} chunk(s) "
            f"for session {short_session} turn {turn_index}"
        ),
        "error": None,
        "transport_response": response,
    }


# ---------------------------------------------------------------------------
# Top-level: resolve a queue item by id
# ---------------------------------------------------------------------------


async def resolve_local_item(
    *,
    queue_path: str | Path,
    item_id: str,
    resolution_id: str,
    vault_path: Path,
    user_id: int,
) -> dict[str, Any]:
    """Resolve one local pending item by id + resolution choice.

    Looks up the item in ``queue_path``, runs the matching
    :class:`ResolutionOption`'s action plan via
    :func:`execute_action_plan`, then marks the item as resolved.

    Returns a dict::

        {
          "ok": <bool>,
          "executed": <bool>,
          "summary": "<text>",
          "error": "<str|none>",
          "item_id": "<id>",
          "resolution": "<resolution_id>"
        }

    If the action plan fails (executed=False), the queue item stays
    pending so a future retry can re-resolve. This satisfies the
    Phase 1 "atomicity" hand-wave: deliver_text is the only Phase 1
    executor that touches I/O, and a partial delivery (some chunks
    sent, transport then failed) leaves the queue item open so the
    operator sees the unresolved state.
    """
    item: PendingItem | None = find_by_id(queue_path, item_id)
    if item is None:
        return {
            "ok": False,
            "executed": False,
            "summary": "item not found in local queue",
            "error": "item_not_found",
            "item_id": item_id,
            "resolution": resolution_id,
        }

    option = next(
        (o for o in item.resolution_options if o.id == resolution_id),
        None,
    )
    if option is None:
        return {
            "ok": False,
            "executed": False,
            "summary": (
                f"resolution '{resolution_id}' not in options for "
                f"item {item_id}"
            ),
            "error": "resolution_not_found",
            "item_id": item_id,
            "resolution": resolution_id,
        }

    if option.action_plan is None:
        # Noted-only option — flip status, no executor.
        ok = mark_resolved(queue_path, item_id, resolution_id)
        return {
            "ok": ok,
            "executed": True,
            "summary": "noted (no action)",
            "error": None if ok else "mark_resolved_failed",
            "item_id": item_id,
            "resolution": resolution_id,
        }

    exec_result = await execute_action_plan(
        plan=option.action_plan,
        vault_path=vault_path,
        user_id=user_id,
    )
    if not exec_result.get("executed"):
        # Action plan failed — leave the item pending. Phase 3 will
        # add proper rollback semantics; Phase 1's "atomic" model is
        # "succeed or stay pending".
        return {
            "ok": False,
            "executed": False,
            "summary": exec_result.get("summary", "action plan failed"),
            "error": exec_result.get("error", "unknown"),
            "item_id": item_id,
            "resolution": resolution_id,
        }

    ok = mark_resolved(queue_path, item_id, resolution_id)
    return {
        "ok": ok,
        "executed": True,
        "summary": exec_result.get("summary", "resolved"),
        "error": None if ok else "mark_resolved_failed",
        "item_id": item_id,
        "resolution": resolution_id,
    }


__all__ = [
    "execute_action_plan",
    "resolve_local_item",
]
