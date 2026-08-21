"""Calibration CAPTURE door — draft proposals from a closed session (R4).

The first of the three doors. At web session close the analyzer
(:func:`alfred.telegram.calibration.propose_updates`, preserved from the dead
bot) reads the just-written session record and drafts style/preference
observations; every draft lands in the PENDING store and nothing else happens.

THIS MODULE WRITES NO VAULT. That is the property worth stating at the top,
because the module it delegates to (``calibration``) contains the writer: this
one imports ``propose_updates`` and ``read_calibration`` and never
``apply_proposals``. The apply door is
``calibration_store.approve_proposal``, which requires a named operator.

WHY IT READS THE WRITTEN RECORD rather than the in-memory session snapshot: the
record is the source of truth every other post-close consumer uses
(``capture_extract`` reads it the same way), it is already trimmed and
formatted, and it means this door works identically from any close path —
present or future — instead of coupling to one caller's snapshot shape.

FAILURE IS ALWAYS SWALLOWED. A calibration draft is the least important thing
happening at session close; an analyzer timeout, a malformed record, or a
missing file must never surface to the user or wedge the close. Every failure
path logs and returns an empty list.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


#: Strong refs to in-flight capture tasks — ``asyncio`` only holds weak ones, so
#: a detached task can be garbage-collected mid-flight. Same guard the span
#: finalizer uses.
_CAPTURE_TASKS: set[asyncio.Task] = set()


def _transcript_tail(body: str, max_turns: int) -> str:
    """The last ``max_turns`` transcript lines of a session record body.

    Mirrors ``capture_extract._extract_transcript_from_post``'s slice (everything
    after the ``# Transcript`` heading), then keeps the TAIL — the analyzer is
    told it is reading the last few turns, and paying the model for an entire
    session on every close is what the original's ``transcript_tail_turns``
    parameter existed to avoid.
    """
    idx = body.find("# Transcript")
    text = body[idx:].strip() if idx != -1 else body.strip()
    if max_turns <= 0:
        return text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # A "turn" is a non-blank line here — the record renders one per speaker
    # turn. Deliberately not parsed into speaker structure: the analyzer takes
    # free text, and a parser would be a second transcript grammar to maintain.
    return "\n".join(lines[-max_turns:]) if len(lines) > max_turns else text


async def capture_from_closed_session(
    *,
    client: Any,
    vault_path: Path,
    session_rel_path: str,
    calibration_config: Any,
    user_rel_path: str,
    session_type: str = "conversation",
) -> list:
    """Draft calibration proposals from a just-closed session into the pending store.

    Returns the list of newly-appended pending rows (empty when the analyzer
    proposed nothing new, which is the steady state).

    Never raises.
    """
    if not getattr(calibration_config, "capture_enabled", False):
        return []
    if not session_rel_path:
        # An EMPTY session closes with no record. Nothing to read, and this is
        # a normal outcome rather than a fault.
        log.info(
            "talker.calibration.capture_skipped",
            reason="no session record written (empty session)",
        )
        return []
    if not user_rel_path:
        # No calibration target configured. Say so once per close rather than
        # drafting proposals that could never be applied to anything.
        log.info(
            "talker.calibration.capture_skipped",
            reason="no telegram.primary_users configured — nothing to calibrate against",
            session=session_rel_path,
        )
        return []

    try:
        rel = session_rel_path if session_rel_path.endswith(".md") else f"{session_rel_path}.md"
        record = Path(vault_path) / rel
        if not record.exists():
            log.info(
                "talker.calibration.capture_skipped",
                reason="session record not found",
                session=session_rel_path,
            )
            return []
        body = record.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "talker.calibration.capture_read_failed",
            session=session_rel_path, error=str(exc),
        )
        return []

    tail_turns = int(getattr(calibration_config, "transcript_tail_turns", 20) or 20)
    transcript = _transcript_tail(body, tail_turns)
    if not transcript.strip():
        log.info(
            "talker.calibration.capture_skipped",
            reason="session record has an empty transcript",
            session=session_rel_path,
        )
        return []

    from . import calibration, calibration_store

    current = calibration.read_calibration(Path(vault_path), user_rel_path)

    try:
        drafts = await calibration.propose_updates(
            client,
            transcript,
            current,
            session_type,
            session_rel_path,
            model=getattr(calibration_config, "model", "claude-sonnet-4-6"),
            transcript_tail_turns=tail_turns,
        )
    except Exception as exc:  # noqa: BLE001 — a draft must never wedge a close
        log.warning(
            "talker.calibration.capture_propose_failed",
            session=session_rel_path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []

    cap = int(getattr(calibration_config, "max_proposals_per_session", 5) or 5)
    if cap > 0 and len(drafts) > cap:
        # The cap BITES IN THE LOG, never silently: a model returning fifty
        # proposals is a signal about the model, and truncating quietly would
        # hide it while still protecting the queue.
        log.warning(
            "talker.calibration.capture_capped",
            session=session_rel_path, drafted=len(drafts), cap=cap,
        )
        drafts = drafts[:cap]

    try:
        return calibration_store.record_proposals(
            calibration_config.pending_path,
            calibration_config.decided_path,
            drafts,
            source_session_rel=session_rel_path,
        )
    except OSError as exc:
        log.warning(
            "talker.calibration.capture_store_failed",
            session=session_rel_path, error=str(exc),
        )
        return []


def schedule_calibration_capture(
    *,
    client: Any,
    vault_path: Path,
    session_rel_path: str,
    calibration_config: Any,
    user_rel_path: str,
    session_type: str = "conversation",
) -> "asyncio.Task | None":
    """Detached-task wrapper for :func:`capture_from_closed_session`.

    Returns the task, or ``None`` when capture is disabled or no event loop is
    running (CLI close paths). Both skips are LOGGED rather than silent, so a
    dormant loop is distinguishable from a broken one.
    """
    if not getattr(calibration_config, "capture_enabled", False):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.info(
            "talker.calibration.capture_not_scheduled",
            session=session_rel_path,
            reason="no running event loop — calibration capture is a web-close "
                   "door and does not run on CLI close paths",
        )
        return None
    task = loop.create_task(
        capture_from_closed_session(
            client=client,
            vault_path=vault_path,
            session_rel_path=session_rel_path,
            calibration_config=calibration_config,
            user_rel_path=user_rel_path,
            session_type=session_type,
        )
    )
    _CAPTURE_TASKS.add(task)
    task.add_done_callback(_CAPTURE_TASKS.discard)
    return task


__all__ = [
    "capture_from_closed_session",
    "schedule_calibration_capture",
]
