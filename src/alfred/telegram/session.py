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

import re
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

    Wk3 commit 8: ``opening_model`` records the model the session was
    *opened* on (via the router + calibration overrides). ``model`` may
    be flipped mid-session by ``/opus`` / ``/sonnet`` / implicit
    escalation; ``opening_model`` stays fixed. The diff between the two
    at close time is the "session escalated" signal the model-preference
    calibration threshold counts on.
    """

    session_id: str
    chat_id: int
    started_at: datetime
    last_message_at: datetime
    model: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    vault_ops: list[dict[str, str]] = field(default_factory=list)
    opening_model: str = ""

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
            "opening_model": self.opening_model or self.model,
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
            # Missing opening_model (wk2 records) → use current model as
            # the opening snapshot. Conservative: a rehydrated wk2
            # session was opened on its ``model`` so this is correct.
            opening_model=data.get("opening_model") or data.get("model", ""),
        )


# --- Helpers ---


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime, tolerating ``Z`` suffixes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _persist(state: StateManager, session: Session) -> None:
    """Sync the session dataclass back into state and save.

    Preserves any stashed ``_*`` metadata (``_vault_path_root``,
    ``_session_type``, etc.) that the bot layer wrote onto the active
    dict — those fields are orthogonal to the :class:`Session` dataclass
    but the timeout / shutdown close paths depend on them. Without this
    merge, the first ``append_turn`` after ``_open_session_with_stash``
    would wipe them.
    """
    existing = state.get_active(session.chat_id) or {}
    merged = dict(existing)
    merged.update(session.to_dict())
    # Re-apply any stashed ``_*`` keys the dataclass doesn't know about.
    for key, value in existing.items():
        if key.startswith("_"):
            merged[key] = value
    state.set_active(session.chat_id, merged)
    state.save()


def _slug_from_dt(dt: datetime) -> str:
    """Produce ``YYYY-MM-DD HHMM`` slug used in the session record name."""
    return dt.strftime("%Y-%m-%d %H%M")


def _slug_from_date(dt: datetime) -> str:
    """Produce ``YYYY-MM-DD`` slug — used in Hypatia's mode-prefixed names."""
    return dt.strftime("%Y-%m-%d")


# Filename-safe slug: lowercase ASCII alphanumerics + dashes.
_TOPIC_SLUG_KEEP = re.compile(r"[^a-z0-9-]+")


def _slug_from_topic(text: str, *, max_words: int = 5) -> str:
    """Derive a filename-safe slug from arbitrary text.

    Used by Hypatia's mode-prefixed session names: takes the first
    ``max_words`` whitespace-delimited tokens of ``text`` (lowercased,
    non-alphanumerics dropped) and joins them with dashes. Empty input
    returns ``"untitled"`` so a session opened without any user text
    still produces a valid filename.
    """
    if not text:
        return "untitled"
    s = text.strip().lower()
    if not s:
        return "untitled"
    # First N whitespace-delimited tokens.
    tokens = s.split()[:max_words]
    joined = "-".join(tokens)
    # Drop everything that isn't a-z/0-9/-, then collapse runs and trim.
    joined = _TOPIC_SLUG_KEEP.sub("", joined)
    joined = re.sub(r"-{2,}", "-", joined).strip("-")
    return joined or "untitled"


def _first_user_text(transcript: list[dict[str, Any]]) -> str:
    """Extract the first user turn's text content, for slug derivation.

    Tolerates both string ``content`` and list-of-blocks ``content`` (the
    Anthropic SDK shape for tool turns). Returns empty string when no
    user turn is present or the first user turn has no text.
    """
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text") or "")
        return ""
    return ""


# --- Per-instance mode registry -----------------------------------------
#
# Each instance picks a session "mode" at close time. The mode becomes the
# filename prefix (``<mode>-<date>-<slug>-<id>.md``) and, for Hypatia, also
# lands as ``mode:`` in the frontmatter. The registry below is the single
# source of truth for which prefixes each instance is allowed to emit;
# extending an instance's mode set is a one-line change here.
#
# Order matters: the FIRST entry in each list is the instance's default
# fallback when mode-resolution can't infer anything specific.
INSTANCE_MODE_PREFIXES: dict[str, list[str]] = {
    "talker": ["voice", "conversation", "capture"],   # Salem
    "hypatia": ["conversation", "capture"],
    "kalle": ["coding", "review"],
}


def _has_voice_user_turn(transcript: list[dict[str, Any]]) -> bool:
    """True if any user turn was sent as voice (``_kind="voice"``).

    Salem stamps ``_kind`` on every user turn at append time. A session
    that received at least one voice message — even if the rest were
    typed — is classified as a ``voice`` session. The voice/text counts
    in ``_count_message_kinds`` use the same field, so this stays in
    sync with the telemetry summary.
    """
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        if turn.get("_kind") == "voice":
            return True
    return False


def _kalle_invoked_reviews(transcript: list[dict[str, Any]]) -> bool:
    """True if any ``bash_exec`` tool call ran ``alfred reviews ...``.

    KAL-LE drives the ``alfred reviews`` CLI through the ``bash_exec``
    tool surface. Detection is a substring scan across all tool_use
    blocks: any block named ``bash_exec`` whose ``input.command`` starts
    with the ``alfred reviews`` prefix flips the session into ``review``
    mode. False positives (e.g. a code block discussing the command in
    plain text) are ignored — only structured tool_use blocks count.
    """
    for turn in transcript:
        content = turn.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "bash_exec":
                continue
            inp = block.get("input") or {}
            cmd = (inp.get("command") or "").strip().lower()
            # Tolerate leading whitespace and the ``alfred reviews``
            # subcommand variants (``write``, ``list``, ``read``,
            # ``mark-addressed``) — substring match on the command head.
            if cmd.startswith("alfred reviews"):
                return True
    return False


def _resolve_mode_for_instance(
    tool_set: str,
    session: Session,
    session_type: str | None,
) -> str:
    """Pick the session ``mode`` for the given instance + transcript.

    Branches on ``tool_set`` to apply per-instance auto-detection:

    - **Salem** (``"talker"``): ``capture`` if the bot stashed
      ``session_type="capture"`` (the ``/capture`` opener), else
      ``voice`` if any user turn was sent as voice, else
      ``conversation``.
    - **Hypatia** (``"hypatia"``): ``capture`` if ``session_type=="capture"``,
      else ``conversation``. Same shape as wk2 ``_mode_from_session_type``
      to keep existing Hypatia behaviour unchanged.
    - **KAL-LE** (``"kalle"``): ``review`` if any ``bash_exec`` tool
      call ran ``alfred reviews ...``, else ``coding``.
    - Unknown / empty ``tool_set``: return ``""``. The caller's
      filename builder maps this to the wk1 ``Voice Session — ...``
      shape so legacy code paths (callers not threaded with
      ``tool_set``) keep working.

    Ambiguous cases default to the instance's first-listed prefix —
    e.g. a Salem session with no voice turns and no ``/capture`` becomes
    ``conversation`` (not ``voice``); detection has to *prove* the more
    specific mode.
    """
    prefixes = INSTANCE_MODE_PREFIXES.get(tool_set)
    if prefixes is None:
        return ""

    st = (session_type or "").lower()

    if tool_set == "talker":
        if st == "capture":
            return "capture"
        if _has_voice_user_turn(session.transcript):
            return "voice"
        return "conversation"

    if tool_set == "hypatia":
        if st == "capture":
            return "capture"
        return "conversation"

    if tool_set == "kalle":
        if _kalle_invoked_reviews(session.transcript):
            return "review"
        return "coding"

    # Registered tool_set without a dedicated branch — fall back to the
    # first-listed prefix so adding a new instance to the registry
    # always produces a well-formed filename even before its
    # detector is wired.
    return prefixes[0]


def _build_record_name(
    session: Session,
    *,
    tool_set: str,
    mode: str,
) -> str:
    """Pick the session-record filename per the instance's tool_set.

    All instances registered in :data:`INSTANCE_MODE_PREFIXES` use the
    mode-prefixed pattern ``<mode>-<YYYY-MM-DD>-<slug>-<short-id>``
    (per ``vault-hypatia/SKILL.md`` and ``~/library-alexandria/CLAUDE.md``,
    now generalized as the project-wide convention). ``slug`` is derived
    from the first user turn (first 5 words). The short id keeps same-day
    sessions on the same opening cue from colliding on ``vault_create``.

    Unknown / empty ``tool_set`` falls back to the wk1
    ``Voice Session — <date> <time> <short-id>`` filename so legacy
    callers (any code path not yet threaded with ``tool_set``) and
    pre-existing vault records stay readable. Existing legacy session
    files are NEVER renamed — backward compat is load-bearing.
    """
    short_id = session.session_id.split("-")[0]
    if tool_set in INSTANCE_MODE_PREFIXES:
        slug = _slug_from_topic(_first_user_text(session.transcript))
        return f"{mode}-{_slug_from_date(session.started_at)}-{slug}-{short_id}"
    return f"Voice Session — {_slug_from_dt(session.started_at)} {short_id}"


def _mode_from_session_type(session_type: str | None) -> str:
    """Map Salem-side ``session_type`` to Hypatia's ``mode`` field.

    Capture-mode sessions (``session_type="capture"``) become
    ``mode: capture`` with ``processed: false`` so the "Unprocessed
    captures" Bases view can read the queue. Everything else collapses
    to ``conversation``. Retained as a thin shim around the
    Hypatia-specific branch of :func:`_resolve_mode_for_instance` for
    callers that only have a session_type string in hand (no transcript).
    """
    if (session_type or "").lower() == "capture":
        return "capture"
    return "conversation"


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
        opening_model=model,
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
    kind: str = "text",
) -> None:
    """Append an Anthropic-format turn to the transcript and persist.

    ``content`` follows the SDK's shape: either a plain string (simple text
    turn) or a list of content blocks (tool_use / tool_result turns).

    wk2 commit 5:
    - Always stamp ``_ts`` (ISO 8601) on the turn so ``_build_session_body``
      renders real per-turn timestamps. Wk1 relied on the session start time
      for every turn, which made long sessions look like they happened in
      one minute.
    - ``kind`` (``"text"`` or ``"voice"``) is stamped as ``_kind`` on user
      turns. Assistant / tool turns always carry ``_kind="text"`` — they
      don't have an input modality. The voice/text counters in
      ``_count_message_kinds`` read this field at close time.
    """
    now = _now_utc()
    turn: dict[str, Any] = {
        "role": role,
        "content": content,
        "_ts": now.isoformat(),
    }
    if role == "user":
        turn["_kind"] = kind
    session.transcript.append(turn)
    session.last_message_at = now
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
                pushback_level=raw.get("_pushback_level"),
                tool_set=raw.get("_tool_set", ""),
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
                pushback_level=raw.get("_pushback_level"),
                tool_set=raw.get("_tool_set", ""),
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
    pushback_level: int | None = None,
    tool_set: str = "",
) -> str:
    """Close the active session for ``chat_id`` and write a ``session/`` record.

    Removes the session from ``active_sessions``, appends a summary to
    ``closed_sessions``, and returns the vault-relative path of the new record.

    ``session_type`` / ``continues_from`` default to ``"note"`` / ``None`` so
    the timeout / shutdown close paths can fall back to the wk1 behaviour when
    the active dict was written before wk2 (``get("_session_type", "note")``).

    ``tool_set`` selects per-instance session-save shape (filename pattern +
    frontmatter fields). All registered tool_sets emit the mode-prefixed
    ``<mode>-<date>-<slug>-<short-id>`` filename; unknown / empty
    ``tool_set`` falls back to the wk1 ``Voice Session — <date> <time> <id>``
    filename. ``"hypatia"`` additionally writes Hypatia-specific
    ``mode``/``processed``/``extracted_to`` frontmatter fields per
    ``vault-hypatia/SKILL.md``. Default ``""`` preserves the legacy wk1
    behaviour for any caller not yet threading the field.
    """
    # Import here to avoid a circular import at module load (ops pulls in
    # frontmatter + yaml which are heavier).
    from alfred.vault import ops as vault_ops

    active_dict = state.get_active(chat_id)
    if active_dict is None:
        raise ValueError(f"No active session for chat_id={chat_id}")

    session = Session.from_dict(active_dict)
    ended_at = _now_utc()
    # Per-instance mode resolution: registered tool_sets infer mode from
    # transcript + session_type; unknown/empty tool_set returns "" and
    # the wk1 ``Voice Session — ...`` filename is used.
    mode = _resolve_mode_for_instance(tool_set, session, session_type)

    fm = _build_session_frontmatter(
        session,
        ended_at=ended_at,
        reason=reason,
        user_vault_path=user_vault_path,
        stt_model_used=stt_model_used,
        session_type=session_type,
        continues_from=continues_from,
        pushback_level=pushback_level,
        tool_set=tool_set,
        mode=mode,
    )
    body = _build_session_body(session)

    # Unique record name — collisions across multiple same-minute closes would
    # otherwise fail vault_create, so the per-instance helpers append a
    # short session id.
    name = _build_record_name(session, tool_set=tool_set, mode=mode)

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
        # Wk3 commit 8: record the opening and closing model so
        # model_calibration.propose_default_flip can detect mid-session
        # escalation. ``opening_model`` falls back to current ``model``
        # for wk2 records being written during transition.
        "opening_model": session.opening_model or session.model,
        "closing_model": session.model,
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
    pushback_level: int | None = None,
    tool_set: str = "",
    mode: str = "conversation",
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

    Per-instance shape (``tool_set``):
    - All registered instances (``INSTANCE_MODE_PREFIXES``) use the
      mode-prefixed display name ``<Mode> — <date> <slug>``.
    - ``"hypatia"`` additionally adds her ``/extract``-workflow fields
      (``mode`` / ``processed`` / ``extracted_to`` / ``duration_minutes``)
      per ``vault-hypatia/SKILL.md`` and ``~/library-alexandria/CLAUDE.md``.
      Salem and KAL-LE deliberately do NOT gain those fields — they're
      tied to Hypatia's capture queue and would cause Bases-view drift
      on the other vaults.
    - Unknown / empty ``tool_set`` falls back to ``Voice Session —
      <date>`` for backward compat with wk1 records.
    """
    voice_count, text_count = _count_message_kinds(session)
    participants = [f"[[{user_vault_path}]]"] if user_vault_path else []
    outputs = [f"[[{op['path']}]]" for op in session.vault_ops]

    # Display name mirrors the filename pattern: registered instances
    # (talker, hypatia, kalle) use ``<Mode> — <date> <slug>``; legacy /
    # unknown tool_sets keep ``Voice Session — <date> <time>`` for
    # backward compat with wk1 records.
    if tool_set in INSTANCE_MODE_PREFIXES:
        # ``mode`` may be empty if the caller passed an unregistered
        # tool_set string by mistake — fall back to the instance's
        # first-listed prefix so the display name stays well-formed.
        display_mode = mode or INSTANCE_MODE_PREFIXES[tool_set][0]
        display_name = (
            f"{display_mode.capitalize()} — "
            f"{_slug_from_date(session.started_at)} "
            f"{_slug_from_topic(_first_user_text(session.transcript))}"
        )
    else:
        display_name = f"Voice Session — {_slug_from_dt(session.started_at)}"

    fm: dict[str, Any] = {
        "type": "session",
        "status": "completed",
        "name": display_name,
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
            # wk3 commit 1: record the session's pushback dial so the
            # distiller and vault-reviewer can correlate output style with
            # the directive that produced it. Emitted as None when the
            # session was opened before wk3 so wk2 records stay parseable.
            "pushback_level": pushback_level,
        },
    }

    if tool_set == "hypatia":
        # Per Hypatia SKILL spec + library-alexandria/CLAUDE.md: mode +
        # processed gate the "Unprocessed captures" Bases view; capture
        # sessions queue at ``processed: false`` until Hypatia runs the
        # extraction pass on /extract. Conversation sessions go straight
        # to ``processed: true`` (the structuring pass at close time IS
        # the processing for conversations). ``extracted_to`` is an
        # empty list placeholder; Hypatia populates it via vault_edit
        # set_fields when she creates downstream records.
        # ``duration_minutes`` is rounded — the spec ships round numbers
        # for the Bases view "Stale drafts" / "Unprocessed captures"
        # filters, and ended_at - started_at is the canonical source.
        fm["mode"] = mode
        fm["processed"] = (mode != "capture")
        fm["extracted_to"] = []
        elapsed = (ended_at - session.started_at).total_seconds()
        fm["duration_minutes"] = max(0, round(elapsed / 60))

    return fm


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
