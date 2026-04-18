"""Talker session lifecycle — open, append, timeout, close.

A "session" here is a voice/text conversation between the user (via Telegram)
and Alfred via the Anthropic API. State lives in the :class:`StateManager`
(persisted JSON on disk); this module is pure logic.

Session records are written to the vault at close time via the ``talker`` scope.
Timeouts are checked on two axes: a periodic tick (``check_timeouts``) and a
one-shot startup sweep (``resolve_on_startup``) that recovers sessions orphaned
across a daemon restart.

The transcript is a list of Anthropic-style message dicts — ``role`` is
``"user"`` or ``"assistant"``, and ``content`` is either a string or a list of
content blocks (for tool_use / tool_result turns). The body renderer compacts
tool blocks into one-line summaries so session records stay human-readable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


# --- Session dataclass ---


@dataclass
class Session:
    """In-memory view of an active talker session.

    The canonical store is the JSON state file; this dataclass is a typed
    projection for callers that prefer attribute access. ``transcript`` holds
    Anthropic-format message dicts (``role`` + ``content``).
    """

    session_id: str
    chat_id: int
    started_at: datetime
    last_message_at: datetime
    model: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    vault_ops: list[dict[str, str]] = field(default_factory=list)

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "started_at": self.started_at.isoformat(),
            "last_message_at": self.last_message_at.isoformat(),
            "model": self.model,
            "transcript": self.transcript,
            "vault_ops": self.vault_ops,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            chat_id=int(data["chat_id"]),
            started_at=_parse_iso(data["started_at"]),
            last_message_at=_parse_iso(data["last_message_at"]),
            model=data["model"],
            transcript=list(data.get("transcript") or []),
            vault_ops=list(data.get("vault_ops") or []),
        )


# --- Helpers ---


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime, tolerating ``Z`` suffixes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _persist(state: StateManager, session: Session) -> None:
    """Sync the session dataclass back into state and save."""
    state.set_active(session.chat_id, session.to_dict())
    state.save()


def _slug_from_dt(dt: datetime) -> str:
    """Produce ``YYYY-MM-DD HHMM`` slug used in the session record name."""
    return dt.strftime("%Y-%m-%d %H%M")


# --- Public API ---


def open_session(
    state: StateManager,
    chat_id: int,
    model: str,
) -> Session:
    """Create and persist a new active session for ``chat_id``.

    Overwrites any existing active session for that chat — callers should close
    the prior session first if they need to preserve it.
    """
    now = _now_utc()
    session = Session(
        session_id=str(uuid.uuid4()),
        chat_id=int(chat_id),
        started_at=now,
        last_message_at=now,
        model=model,
    )
    _persist(state, session)
    log.info(
        "talker.session.opened",
        chat_id=chat_id,
        session_id=session.session_id,
        model=model,
    )
    return session


def append_turn(
    state: StateManager,
    session: Session,
    role: str,
    content: str | list[dict[str, Any]],
) -> None:
    """Append an Anthropic-format turn to the transcript and persist.

    ``content`` follows the SDK's shape: either a plain string (simple text
    turn) or a list of content blocks (tool_use / tool_result turns).
    """
    session.transcript.append({"role": role, "content": content})
    session.last_message_at = _now_utc()
    _persist(state, session)


def append_vault_op(
    state: StateManager,
    session: Session,
    op: str,
    path: str,
) -> None:
    """Record a vault mutation onto the session and persist.

    Feeds the ``outputs`` field in the eventual session-record frontmatter.
    """
    session.vault_ops.append({
        "op": op,
        "path": path,
        "ts": _now_utc().isoformat(),
    })
    _persist(state, session)


def resolve_on_startup(
    state: StateManager,
    now: datetime,
    gap_seconds: int,
) -> list[str]:
    """Sweep active sessions at daemon boot; close any that have timed out.

    Returns the list of vault paths written for closed sessions.

    Active sessions that have NOT exceeded the gap are left in place — the
    next user message reuses them.
    """
    closed_paths: list[str] = []
    active = dict(state.state.get("active_sessions", {}))
    for chat_id_str, raw in active.items():
        try:
            last = _parse_iso(raw.get("last_message_at", ""))
        except (ValueError, TypeError):
            log.warning(
                "talker.session.invalid_last_message",
                chat_id=chat_id_str,
            )
            continue
        if (now - last).total_seconds() < gap_seconds:
            continue

        vault_path_root = raw.get("_vault_path_root", "")
        # Caller didn't stash vault path — skip gracefully; daemon will
        # retry via check_timeouts once it has a config handle.
        if not vault_path_root:
            log.info(
                "talker.session.timeout_deferred",
                chat_id=chat_id_str,
                reason="no_vault_path_on_restart",
            )
            continue
        try:
            path = close_session(
                state,
                vault_path_root=vault_path_root,
                chat_id=int(chat_id_str),
                reason="timeout_on_restart",
                user_vault_path=raw.get("_user_vault_path"),
                stt_model_used=raw.get("_stt_model_used", ""),
                session_type=raw.get("_session_type", "note"),
                continues_from=raw.get("_continues_from"),
            )
            closed_paths.append(path)
        except Exception as exc:  # noqa: BLE001 — log and continue sweep
            log.warning(
                "talker.session.close_failed",
                chat_id=chat_id_str,
                error=str(exc),
            )
    return closed_paths


def check_timeouts(
    state: StateManager,
    now: datetime,
    gap_seconds: int,
) -> list[str]:
    """Periodic tick: close any sessions that have exceeded the gap.

    Returns vault paths of just-closed sessions. Relies on the daemon having
    stashed vault-path metadata onto each active session dict when it was
    created; sessions without that metadata are left alone and logged.
    """
    closed_paths: list[str] = []
    active = dict(state.state.get("active_sessions", {}))
    for chat_id_str, raw in active.items():
        try:
            last = _parse_iso(raw.get("last_message_at", ""))
        except (ValueError, TypeError):
            continue
        if (now - last).total_seconds() < gap_seconds:
            continue
        vault_path_root = raw.get("_vault_path_root", "")
        if not vault_path_root:
            continue
        try:
            path = close_session(
                state,
                vault_path_root=vault_path_root,
                chat_id=int(chat_id_str),
                reason="timeout",
                user_vault_path=raw.get("_user_vault_path"),
                stt_model_used=raw.get("_stt_model_used", ""),
                session_type=raw.get("_session_type", "note"),
                continues_from=raw.get("_continues_from"),
            )
            closed_paths.append(path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.session.timeout_close_failed",
                chat_id=chat_id_str,
                error=str(exc),
            )
    return closed_paths


def close_session(
    state: StateManager,
    vault_path_root: str,
    chat_id: int,
    reason: str,
    user_vault_path: str | None,
    stt_model_used: str,
    session_type: str = "note",
    continues_from: str | None = None,
) -> str:
    """Close the active session for ``chat_id`` and write a ``session/`` record.

    Removes the session from ``active_sessions``, appends a summary to
    ``closed_sessions``, and returns the vault-relative path of the new record.

    ``session_type`` / ``continues_from`` default to ``"note"`` / ``None`` so
    the timeout / shutdown close paths can fall back to the wk1 behaviour when
    the active dict was written before wk2 (``get("_session_type", "note")``).
    """
    # Import here to avoid a circular import at module load (ops pulls in
    # frontmatter + yaml which are heavier).
    from alfred.vault import ops as vault_ops

    active_dict = state.get_active(chat_id)
    if active_dict is None:
        raise ValueError(f"No active session for chat_id={chat_id}")

    session = Session.from_dict(active_dict)
    ended_at = _now_utc()

    fm = _build_session_frontmatter(
        session,
        ended_at=ended_at,
        reason=reason,
        user_vault_path=user_vault_path,
        stt_model_used=stt_model_used,
        session_type=session_type,
        continues_from=continues_from,
    )
    body = _build_session_body(session)

    # Unique record name — collisions across multiple same-minute closes would
    # otherwise fail vault_create, so append the short session id.
    short_id = session.session_id.split("-")[0]
    name = f"Voice Session — {_slug_from_dt(session.started_at)} {short_id}"

    vault_path = Path(vault_path_root)
    result = vault_ops.vault_create(
        vault_path,
        "session",
        name,
        set_fields=fm,
        body=body,
    )
    rel_path = result["path"]

    # State cleanup: pop active, append closed-summary, save once.
    # ``session_type`` / ``continues_from`` land here so the router can look up
    # the most recent article/journal/brainstorm session from state alone in
    # wk2 (plan open question #5 — state-only continuation for wk2; body-parser
    # fallback is a wk3 task).
    state.pop_active(chat_id)
    state.append_closed({
        "session_id": session.session_id,
        "chat_id": session.chat_id,
        "started_at": session.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "reason": reason,
        "record_path": rel_path,
        "message_count": len(session.transcript),
        "vault_ops": len(session.vault_ops),
        "session_type": session_type,
        "continues_from": continues_from,
    })
    state.save()

    log.info(
        "talker.session.closed",
        chat_id=chat_id,
        session_id=session.session_id,
        reason=reason,
        record_path=rel_path,
        messages=len(session.transcript),
        vault_ops=len(session.vault_ops),
    )
    return rel_path


# --- Frontmatter + body builders (pure, easy to test) ---


def _build_session_frontmatter(
    session: Session,
    ended_at: datetime,
    reason: str,
    user_vault_path: str | None = None,
    stt_model_used: str = "",
    session_type: str = "note",
    continues_from: str | None = None,
) -> dict[str, Any]:
    """Produce the ``session/`` record frontmatter.

    Pure function — no side effects, no imports of vault ops. Matches section
    4 of the voice-design doc, with the correction that ``outputs`` is
    populated from ``session.vault_ops`` rather than left empty.

    wk2 additions (plan open question #2):
    - Top-level ``session_type`` — one of ``note|task|journal|article|brainstorm``.
    - Top-level ``continues_from`` — wikilink string (``[[session/...]]``) or
      ``None``. Emitted as YAML null when absent so downstream queries can
      filter on ``continues_from != null``.
    - ``telegram.model`` stays as-is (not renamed to ``model_used``) so wk1
      records and wk2 records share the same telemetry schema.
    """
    voice_count, text_count = _count_message_kinds(session)
    participants = [f"[[{user_vault_path}]]"] if user_vault_path else []
    outputs = [f"[[{op['path']}]]" for op in session.vault_ops]
    slug = _slug_from_dt(session.started_at)

    return {
        "type": "session",
        "status": "completed",
        "name": f"Voice Session — {slug}",
        "created": session.started_at.date().isoformat(),
        "description": (
            f"Telegram talker session ({len(session.transcript)} turns, "
            f"{len(session.vault_ops)} vault ops, closed via {reason})."
        ),
        "intent": "Capture a voice/text conversation with Alfred and any "
                  "vault actions it produced.",
        "participants": participants,
        "project": [],
        "outputs": outputs,
        "related": [],
        "tags": ["voice", "telegram"],
        "session_type": session_type,
        "continues_from": continues_from,
        "telegram": {
            "chat_id": session.chat_id,
            "session_id": session.session_id,
            "started_at": session.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "close_reason": reason,
            "model": session.model,
            "stt_model": stt_model_used,
            "message_count": len(session.transcript),
            "voice_messages": voice_count,
            "text_messages": text_count,
            "vault_operations": list(session.vault_ops),
        },
    }


def _count_message_kinds(session: Session) -> tuple[int, int]:
    """Return ``(voice, text)`` counts from the transcript.

    Voice/text distinction is stored per-turn as ``_kind`` metadata on the
    message dict by the bot handler (commit 4). If absent, all turns count as
    text.
    """
    voice = 0
    text = 0
    for turn in session.transcript:
        if turn.get("role") != "user":
            continue
        kind = turn.get("_kind") or "text"
        if kind == "voice":
            voice += 1
        else:
            text += 1
    return voice, text


def _build_session_body(session: Session) -> str:
    """Render the transcript as readable Markdown.

    User turns: ``**Andrew** (HH:MM · voice): …``
    Assistant turns: ``**Alfred** (HH:MM): …``

    Tool-use / tool-result blocks inside a content list render as compact
    one-liners (``[tool_use: vault_search glob=project/*.md]``). This keeps
    the session record skimmable — JSON blobs would be unreadable.
    """
    lines: list[str] = ["# Transcript", ""]
    base_time = session.started_at

    for idx, turn in enumerate(session.transcript):
        role = turn.get("role", "user")
        content = turn.get("content", "")
        kind = turn.get("_kind") or "text"

        # Rough timestamp: if the turn has its own ``_ts``, use it;
        # otherwise fall back to the session start. Real timestamps arrive
        # once the bot handler (commit 4) stamps each turn.
        ts_raw = turn.get("_ts")
        ts = _parse_iso(ts_raw) if isinstance(ts_raw, str) else base_time
        hhmm = ts.strftime("%H:%M")

        if role == "user":
            speaker = "Andrew"
            meta = " · voice" if kind == "voice" else ""
            header = f"**{speaker}** ({hhmm}{meta}):"
        else:
            header = f"**Alfred** ({hhmm}):"

        rendered = _render_content(content)
        if rendered:
            lines.append(f"{header} {rendered}")
        else:
            lines.append(header)
        # Blank line between turns, except after the last one
        if idx < len(session.transcript) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


def _render_content(content: str | list[dict[str, Any]]) -> str:
    """Render Anthropic-format content into one-liner-friendly text."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input") or {}
            inp_summary = ", ".join(
                f"{k}={_summarize_value(v)}" for k, v in inp.items()
            )
            parts.append(f"[tool_use: {name} {inp_summary}]".rstrip())
        elif btype == "tool_result":
            tid = block.get("tool_use_id", "")
            err = " error" if block.get("is_error") else ""
            parts.append(f"[tool_result{err}: {tid[:8]}…]")
        else:
            parts.append(f"[{btype}]")
    return " ".join(parts)


def _summarize_value(value: Any) -> str:
    """Trim long values to keep the compact-block summary readable."""
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."
