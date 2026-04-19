"""Telegram bot integration — commands, message routing, and the shared turn pipeline.

Responsibilities:
    * Build the :class:`telegram.ext.Application` with command + message handlers.
    * Enforce the ``allowed_users`` allowlist (silent drop for unknowns — an
      unauthorised user gets no signal the bot exists at all).
    * On each message: open or reuse a session, serialise per-chat calls via
      an :class:`asyncio.Lock`, transcribe voice, run the Anthropic turn, and
      reply with the assistant text.

Handlers follow the stdlib PTB signature ``(update, context)`` and are async.
Shared dependencies live on ``application.bot_data`` — config, state manager,
Anthropic client, system prompt, vault context, and per-chat locks.

Forward contract with :mod:`session`: the active-session dict must carry
``_vault_path_root``, ``_user_vault_path``, ``_stt_model_used`` so timeout-
driven close paths (which run without a config handle) can persist the
session record. We stash these immediately after ``open_session``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import (
    calibration,
    conversation,
    model_calibration,
    router,
    session,
    session_types,
    transcribe,
)
from .config import TalkerConfig
from .session import Session
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


# --- bot_data keys --------------------------------------------------------

_KEY_CONFIG = "config"
_KEY_STATE = "state_mgr"
_KEY_CLIENT = "anthropic_client"
_KEY_SYSTEM = "system_prompt"
_KEY_VAULT_CTX = "vault_context_str"
_KEY_LOCKS = "chat_locks"


# --- Application assembly -------------------------------------------------


def build_app(
    config: TalkerConfig,
    state_mgr: StateManager,
    anthropic_client: Any,
    system_prompt: str,
    vault_context_str: str,
) -> Application:
    """Build a PTB :class:`Application` wired with handlers and bot_data.

    Callers add their own post-init hooks (gap sweeper, signal handlers) via
    :mod:`daemon`. This function only does handler registration.
    """
    app = Application.builder().token(config.bot_token).build()

    app.bot_data[_KEY_CONFIG] = config
    app.bot_data[_KEY_STATE] = state_mgr
    app.bot_data[_KEY_CLIENT] = anthropic_client
    app.bot_data[_KEY_SYSTEM] = system_prompt
    app.bot_data[_KEY_VAULT_CTX] = vault_context_str
    app.bot_data[_KEY_LOCKS] = {}

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("end", on_end))
    app.add_handler(CommandHandler("status", on_status))
    # Wk3 commit 5: explicit model overrides for the active session. Both
    # flip ``session.model`` on the active dict; the next ``run_turn`` reads
    # it and routes to the new model. If there's no active session the
    # command is a no-op (tersely reported — we don't want to open a
    # session just to flip its model).
    app.add_handler(CommandHandler("opus", on_opus))
    app.add_handler(CommandHandler("sonnet", on_sonnet))
    # Wk3 commit 6: disables the implicit escalation offer for the rest
    # of this session (state not persisted across sessions, per team-lead
    # call on open question #4). PTB only allows [a-z0-9_] in command
    # names, so the canonical command is ``no_auto_escalate``; the spec
    # called for ``no-auto-escalate`` but dashes aren't legal in PTB —
    # noting the deviation here and in the session note.
    app.add_handler(CommandHandler("no_auto_escalate", on_no_auto_escalate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))

    return app


# --- Allowlist helper -----------------------------------------------------


def _is_allowed(update: Update, config: TalkerConfig) -> bool:
    """Return True iff the message's user_id is in config.allowed_users."""
    user = update.effective_user
    if user is None:
        return False
    allowed = config.allowed_users or []
    return user.id in allowed


# --- Commands -------------------------------------------------------------


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — greeting + usage hint."""
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command="/start",
        )
        return
    if update.message is None:
        return
    await update.message.reply_text(
        f"Hi — this is {config.instance.name}. Send a voice note or type a "
        "message and I'll reply. Use /end to close the current session, "
        "/status for stats."
    )


async def on_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/end — explicitly close the current session, return vault record path.

    Wk3 commit 7: after the session record is written, run
    :func:`calibration.propose_updates` over the transcript and apply any
    proposals via :func:`calibration.apply_proposals`. For dial 4 (default),
    applied proposals are surfaced inline in the close reply so Andrew
    can confirm / object. Errors in the calibration write are logged but
    never block the close reply.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    client: Any = ctx.application.bot_data[_KEY_CLIENT]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    active = state_mgr.get_active(chat_id)
    if not active:
        await update.message.reply_text("no active session.")
        return

    # Snapshot transcript + user path BEFORE close_session pops the
    # active session — close_session removes the active dict, so anything
    # we want from it must be copied out first.
    transcript_snapshot = list(active.get("transcript") or [])
    user_rel = (
        active.get("_user_vault_path")
        or (config.primary_users[0] if config.primary_users else "")
    )
    session_type = active.get("_session_type", "note")
    calibration_snapshot = active.get("_calibration_snapshot")

    try:
        rel_path = session.close_session(
            state_mgr,
            vault_path_root=active.get("_vault_path_root") or config.vault.path,
            chat_id=chat_id,
            reason="explicit",
            user_vault_path=(
                active.get("_user_vault_path")
                or (config.primary_users[0] if config.primary_users else None)
            ),
            stt_model_used=active.get("_stt_model_used") or config.stt.model,
            session_type=session_type,
            continues_from=active.get("_continues_from"),
            pushback_level=active.get("_pushback_level"),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("talker.bot.close_failed", chat_id=chat_id)
        await update.message.reply_text(f"couldn't close session: {exc}")
        return

    log.info("talker.bot.session_closed", chat_id=chat_id, record=rel_path)

    # --- Calibration writes (wk3 commits 7 + 8) --------------------------
    # Runs after the session record is persisted so even a calibration
    # failure leaves the vault in a consistent state (session captured,
    # just no calibration delta). User-facing reply includes the applied
    # proposals inline when dial >= 4.
    suffix = ""
    try:
        from pathlib import Path
        transcript_text = _render_transcript_for_calibration(
            transcript_snapshot,
        )
        proposals = await calibration.propose_updates(
            client=client,
            transcript_text=transcript_text,
            current_calibration=calibration_snapshot,
            session_type=session_type,
            source_session_rel=rel_path,
        )

        # Commit 8: add a model-default flip proposal if the recent
        # history warrants it. Runs AFTER the close so the
        # ``closed_sessions`` entry for this session is already visible
        # to the threshold calculation.
        flip = model_calibration.propose_default_flip(session_type, state_mgr)
        if flip is not None:
            proposals.append(flip)

        result = calibration.apply_proposals(
            vault_path=Path(config.vault.path),
            user_rel_path=user_rel,
            proposals=proposals,
            session_record_path=rel_path,
            confirmation_dial=calibration.DEFAULT_CONFIRMATION_DIAL,
        )
        if result["written"] and result["applied"]:
            # Surface the applied proposals inline so Andrew can react
            # immediately. Dial 4 default — wk3 validation phase.
            lines = ["", "calibration updates applied:"]
            for p in result["applied"]:
                sub = p.subsection or "Notes"
                lines.append(f"• [{sub}] {p.bullet}")
            suffix = "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.calibration_write_failed",
            chat_id=chat_id,
            error=str(exc),
        )

    await update.message.reply_text(
        f"session closed. saved to: {rel_path}" + suffix
    )


def _render_transcript_for_calibration(
    transcript: list[dict[str, Any]],
    tail_turns: int = 20,
) -> str:
    """Render the last ``tail_turns`` turns as a compact transcript for Sonnet.

    Keeps the prompt bounded — even a long session gets capped at a few
    thousand characters here. Tool-use / tool-result blocks are elided
    to one-liners because the model doesn't need the full JSON dump to
    infer user patterns.
    """
    tail = transcript[-tail_turns:] if tail_turns > 0 else transcript
    lines: list[str] = []
    for turn in tail:
        role = turn.get("role", "?")
        content = turn.get("content", "")
        if isinstance(content, list):
            # Tool blocks — summarise.
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(block.get("text", "").strip())
                    else:
                        parts.append(f"[{btype}]")
            body = " ".join(p for p in parts if p).strip()
        elif isinstance(content, str):
            body = content.strip()
        else:
            body = str(content)
        if body:
            lines.append(f"{role.upper()}: {body}")
    return "\n".join(lines)


# --- Model-override commands ----------------------------------------------

# Canonical model IDs for the two supported overrides. Centralised so the
# commands, the log tags, and commit 8's calibration scaffold all read
# from the same source of truth. If the Opus alias 404s at runtime
# (wk3 instruction: fall back to ``claude-opus-4-5``), flip _OPUS_MODEL
# here and re-deploy — not via a defensive retry, which would hide the
# breakage.
_OPUS_MODEL = "claude-opus-4-7"
_SONNET_MODEL = "claude-sonnet-4-6"


def _switch_model(
    state_mgr: StateManager,
    chat_id: int,
    target: str,
    label: str,
) -> str | None:
    """Flip the active session's model. Return a reply string (or ``None``).

    Returns ``None`` when there's no active session — the caller renders
    a terse "no active session" reply. A successful switch returns a
    one-line confirmation ("switched to Opus.") with the new label.

    The switch is idempotent: flipping to the model a session is already
    on reports that without incrementing any counters.
    """
    active = state_mgr.get_active(chat_id)
    if not active:
        return None

    current = active.get("model", "")
    if current == target:
        return f"already on {label}."

    active["model"] = target
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    return f"switched to {label}."


async def on_opus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/opus`` — switch the active session to the Opus model.

    Wk3 commit 6: if an implicit escalation offer was made within the
    cooldown window just before this ``/opus``, log the flip as
    ``escalate_accepted`` so downstream metrics can track acceptance
    rate. Explicit un-prompted ``/opus`` still logs ``escalated`` with
    ``trigger="explicit"``.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    active = state_mgr.get_active(chat_id) or {}
    previous = active.get("model", "")
    offered_at = active.get("_escalation_offered_at_turn")

    reply = _switch_model(state_mgr, chat_id, _OPUS_MODEL, "Opus")
    if reply is None:
        await update.message.reply_text("no active session to switch.")
        return

    if previous != _OPUS_MODEL:
        # Offer → acceptance gets its own event so we can measure uptake.
        # The window matches ``_ESCALATION_COOLDOWN_TURNS`` in
        # conversation.py: if ``/opus`` arrives within that window of an
        # offer, we treat it as accepted. Outside the window, it's
        # "explicit" and independent of any prior offer.
        from . import conversation as _conv
        turn_index = len(active.get("transcript") or [])
        accepted = (
            offered_at is not None
            and (turn_index - int(offered_at)) <= _conv._ESCALATION_COOLDOWN_TURNS
        )
        if accepted:
            log.info(
                "talker.model.escalate_accepted",
                chat_id=chat_id,
                session_id=active.get("session_id", ""),
                **{"from": previous, "to": _OPUS_MODEL},
                turn_index=turn_index,
                offered_at_turn=int(offered_at),
            )
        else:
            log.info(
                "talker.model.escalated",
                chat_id=chat_id,
                session_id=active.get("session_id", ""),
                **{"from": previous, "to": _OPUS_MODEL},
                turn_index=turn_index,
                trigger="explicit",
            )
    await update.message.reply_text(reply)


async def on_no_auto_escalate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/no-auto-escalate`` — suppress the implicit-escalation offer for this session.

    Session-scoped only (team-lead call on open question #4) — the flag
    does not persist into ``closed_sessions`` or the next session. The
    rationale: auto-escalate tuning is a per-session policy, not a
    permanent preference. If Andrew wants it off across sessions, commit
    8's model-preferences calibration block is the right surface.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    active = state_mgr.get_active(chat_id)
    if not active:
        await update.message.reply_text("no active session.")
        return

    active["_auto_escalate_disabled"] = True
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    log.info(
        "talker.model.auto_escalate_disabled",
        chat_id=chat_id,
        session_id=active.get("session_id", ""),
    )
    await update.message.reply_text("auto-escalate off for this session.")


async def on_sonnet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/sonnet`` — switch the active session to the Sonnet model."""
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    active = state_mgr.get_active(chat_id) or {}
    previous = active.get("model", "")

    reply = _switch_model(state_mgr, chat_id, _SONNET_MODEL, "Sonnet")
    if reply is None:
        await update.message.reply_text("no active session to switch.")
        return

    turn_index = len(active.get("transcript") or [])
    if previous != _SONNET_MODEL:
        # Use ``escalated`` for both directions — the label is "model
        # changed", not "went bigger". Downstream aggregation cares about
        # the from/to pair, not the direction.
        log.info(
            "talker.model.escalated",
            chat_id=chat_id,
            session_id=active.get("session_id", ""),
            **{"from": previous, "to": _SONNET_MODEL},
            turn_index=turn_index,
            trigger="explicit",
        )
    await update.message.reply_text(reply)


async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — debug helper: active session count, turn count, last-message age."""
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    active_all = state_mgr.state.get("active_sessions", {}) or {}
    this_session = active_all.get(str(chat_id))

    lines = [f"active sessions (all chats): {len(active_all)}"]
    if this_session:
        transcript = this_session.get("transcript") or []
        lines.append(f"turns in this session: {len(transcript)}")
        last_raw = this_session.get("last_message_at", "")
        if last_raw:
            from datetime import datetime, timezone
            try:
                last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - last_dt
                lines.append(f"time since last message: {int(delta.total_seconds())}s")
            except ValueError:
                lines.append("time since last message: (invalid timestamp)")
    else:
        lines.append("no active session for this chat.")
    await update.message.reply_text("\n".join(lines))


# --- Message handlers -----------------------------------------------------


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text message entry point."""
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            kind="text",
        )
        return
    if update.message is None or update.message.text is None:
        return
    text = update.message.text
    log.info(
        "talker.bot.inbound",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        kind="text",
        length=len(text),
    )
    await handle_message(update, ctx, text=text, voice=False)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice message entry point — download, transcribe, then dispatch."""
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            kind="voice",
        )
        return
    if update.message is None or update.message.voice is None:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    log.info(
        "talker.bot.inbound",
        chat_id=chat_id,
        user_id=update.effective_user.id if update.effective_user else None,
        kind="voice",
        duration=update.message.voice.duration,
    )

    try:
        tg_file = await update.message.voice.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.bot.voice_download_failed", error=str(exc))
        await update.message.reply_text(
            "sorry, couldn't fetch the voice note — try again?"
        )
        return

    try:
        text = await transcribe.transcribe(audio_bytes, "audio/ogg", config.stt)
    except transcribe.TranscribeError as exc:
        log.info("talker.bot.transcribe_failed", error=str(exc))
        await update.message.reply_text(
            "sorry, couldn't transcribe — try again or send a text message?"
        )
        return
    except NotImplementedError as exc:
        log.warning("talker.bot.transcribe_unsupported", error=str(exc))
        await update.message.reply_text(
            "voice isn't configured right now — please send a text message."
        )
        return

    await handle_message(update, ctx, text=text, voice=True)


# --- Shared pipeline ------------------------------------------------------


def _open_session_with_stash(
    state_mgr: StateManager,
    chat_id: int,
    config: TalkerConfig,
    *,
    model: str | None = None,
    session_type: str = "note",
    continues_from: str | None = None,
    pushback_level: int | None = None,
) -> Session:
    """Open a new session and stash the forward-contract metadata.

    The stashed fields are required by timeout-driven close paths in
    :mod:`session`. If they're missing, timeout closes log and skip and the
    record never lands — so this helper exists to keep that contract tight
    and co-located with ``open_session``.

    wk2: ``model`` / ``session_type`` / ``continues_from`` are threaded
    through so the router (commit 3+4) can open a session on the right model
    and flag it as a continuation. All three have safe wk1-equivalent
    defaults (``note`` type on the config-default model, no continuation).

    wk3 commit 1: ``pushback_level`` (int 0-5) stashed as
    ``_pushback_level`` on the active dict so :func:`handle_message` can
    thread it into ``run_turn`` on every turn without a re-lookup.
    """
    sess = session.open_session(
        state_mgr, chat_id, model or config.anthropic.model,
    )
    # Re-read the active dict, stamp the contract fields, save.
    active = state_mgr.get_active(chat_id) or {}
    active["_vault_path_root"] = config.vault.path
    active["_user_vault_path"] = (
        config.primary_users[0] if config.primary_users else ""
    )
    active["_stt_model_used"] = config.stt.model
    active["_session_type"] = session_type
    active["_continues_from"] = continues_from
    if pushback_level is not None:
        active["_pushback_level"] = pushback_level
    # Voice / text counts are derived from per-turn ``_kind`` at close
    # time by ``_count_message_kinds`` — no state-dict counter needed.
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    return sess


def _recent_sessions_for_router(
    state_mgr: StateManager,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the most-recent-first slice of ``closed_sessions``.

    ``state.closed_sessions`` is append-only (oldest first) — reverse it
    and cap at ``limit`` so the router prompt stays bounded. Missing
    ``session_type`` (wk1 records) is tolerated by the router itself.
    """
    closed = state_mgr.state.get("closed_sessions", []) or []
    # Copy-slice then reverse so the newest session is first.
    return list(reversed(closed))[:limit]


def _find_closed_session(
    state_mgr: StateManager,
    record_path: str,
) -> dict[str, Any] | None:
    """Return the ``closed_sessions`` entry for ``record_path``, or None."""
    for entry in reversed(state_mgr.state.get("closed_sessions", []) or []):
        if entry.get("record_path") == record_path:
            return entry
    return None


async def _open_routed_session(
    state_mgr: StateManager,
    config: TalkerConfig,
    client: Any,
    chat_id: int,
    first_message: str,
) -> Session:
    """Classify the opening cue, open a new session with the right defaults.

    Flow:
        1. Query recent closed sessions from state.
        2. Call :func:`router.classify_opening_cue` — on any failure the
           router returns a safe ``note`` fallback, so this never raises.
        3. Open the session on the decision's model.
        4. If the router flagged a valid continuation (path present in
           state), seed the transcript with a single assistant-style
           "continuing from X" primer and stash ``_continues_from`` on
           the active dict.

    Returns the opened :class:`Session`. Caller is responsible for
    appending the user's actual first message via ``run_turn``.
    """
    recent = _recent_sessions_for_router(state_mgr)
    decision = await router.classify_opening_cue(
        client, first_message, recent,
    )

    # Pushback level from the session-type defaults — the router doesn't
    # currently override it (that's a wk4+ calibration hook), so a table
    # lookup is sufficient here.
    type_defaults = session_types.defaults_for(decision.session_type)

    # Wk3 commit 2 + 8: read the calibration snapshot BEFORE opening so
    # model preferences (commit 8) can override the router's model
    # choice for this type. The router picks a session-type default;
    # learned preferences in the calibration block let Andrew's
    # observed pattern override that default without a code change.
    from pathlib import Path
    user_rel = config.primary_users[0] if config.primary_users else ""
    calibration_snapshot = calibration.read_calibration(
        Path(config.vault.path), user_rel,
    )

    # Model preference override (commit 8). Empty dict = no preferences
    # set; missing type in the dict = fall back to router's choice.
    opening_model = decision.model
    model_prefs = model_calibration.parse_model_preferences(calibration_snapshot)
    pref = model_prefs.get(decision.session_type)
    if pref is not None and pref.model:
        log.info(
            "talker.model_cal.override",
            session_type=decision.session_type,
            router_model=decision.model,
            preferred_model=pref.model,
        )
        opening_model = pref.model

    sess = _open_session_with_stash(
        state_mgr,
        chat_id,
        config,
        model=opening_model,
        session_type=decision.session_type,
        continues_from=f"[[{decision.continues_from}]]"
        if decision.continues_from
        else None,
        pushback_level=type_defaults.pushback_level,
    )

    active = state_mgr.get_active(chat_id) or {}
    active["_calibration_snapshot"] = calibration_snapshot
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    # Continuation pre-seed: drop one context turn into the transcript so
    # the model knows what came before. We don't have the full prior
    # transcript in state (only a summary), so the primer references the
    # prior record by path — the model can use vault_read to fetch it if
    # it wants the full body.
    if decision.continues_from:
        prior = _find_closed_session(state_mgr, decision.continues_from)
        if prior is not None:
            primer = (
                f"[context: continuing from a prior {decision.session_type} "
                f"session ({prior.get('message_count', '?')} turns, ended "
                f"{prior.get('ended_at', '?')[:10]}). "
                f"Record: session/{decision.continues_from.split('/')[-1]}. "
                "Ask before assuming — you may need to read the record first.]"
            )
            # Assistant-style turn so it appears as "system context" above
            # the first user message, without claiming to be the user.
            session.append_turn(state_mgr, sess, "assistant", primer)

    log.info(
        "talker.bot.routed_open",
        chat_id=chat_id,
        session_type=decision.session_type,
        model=decision.model,
        continues=decision.continues_from is not None,
    )
    return sess


# --- Inline-command pre-check --------------------------------------------
#
# PTB's ``CommandHandler`` only fires when the message text *starts* with
# ``/command``. Real-world usage has the slash-command at the end of a
# sentence ("Good. /end") or embedded inside prose ("please /opus"). Those
# land on the text MessageHandler and get sent to Claude as conversational
# input — the command never fires, the session doesn't close, the model
# doesn't switch.
#
# This pre-check runs BEFORE the session pipeline in ``handle_message`` and
# dispatches to the matching ``on_*`` handler when it spots an inline
# command. The command is treated as the user's full intent; the surrounding
# prose is NOT sent to Claude (tokens are wasted and the reply would likely
# conflict with the command's reply).
#
# Dispatch rule: match ``/command`` at end-of-line OR at start-of-message
# only. End-of-line catches "Good. /end"; start-of-message is a safety net
# for anything the outer CommandHandler somehow missed (in practice it
# shouldn't, because a pure "/end" message is filtered out of the text
# MessageHandler by ``filters.TEXT & ~filters.COMMAND`` — but belt +
# braces). Mid-line matches like "maybe I'll /end later" intentionally
# fail so users can still discuss the commands in prose.
_INLINE_COMMANDS: set[str] = {
    "end",
    "opus",
    "sonnet",
    "no_auto_escalate",
    "status",
    "start",
}

# Require whitespace or start-of-string before the slash so mid-word
# tokens like ``foo/end`` don't trigger. End-of-line variant has ``\s*$``
# so trailing whitespace is tolerated; start-of-message variant uses
# ``\b`` so ``/end something`` still matches as start-form.
_INLINE_CMD_RE = re.compile(r"(?:^|\s)/(\w+)\s*$|^/(\w+)\b")


def _detect_inline_command(text: str) -> str | None:
    """Return the lower-cased inline command name, or None.

    Only inspects the first line of ``text`` so a user typing a multi-line
    message where the second line happens to end in ``/end`` doesn't get
    their session closed silently — commands are line-local by convention.
    """
    if not text:
        return None
    first_line = text.splitlines()[0]
    match = _INLINE_CMD_RE.search(first_line)
    if not match:
        return None
    cmd = (match.group(1) or match.group(2) or "").lower()
    if cmd in _INLINE_COMMANDS:
        return cmd
    return None


# Direct handler dispatch map. The command strings here MUST match the
# ``_INLINE_COMMANDS`` set above — keep them in sync when adding new
# commands. Reusing the ``on_*`` handlers (rather than factoring out
# shared logic) keeps the inline path byte-for-byte compatible with the
# CommandHandler path, including all the log events and reply prose.
async def _dispatch_inline_command(
    cmd: str,
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Invoke the ``on_*`` handler for ``cmd``. Returns True iff dispatched."""
    handler = _INLINE_HANDLERS.get(cmd)
    if handler is None:
        return False
    await handler(update, ctx)
    return True


# Populated at module-load time after the on_* handlers are defined
# above. Declared here as a forward reference so the tooling knows the
# shape; the actual mapping is assigned at the bottom of the module.
_INLINE_HANDLERS: dict[str, Any] = {}


async def handle_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    voice: bool = False,
) -> None:
    """Shared pipeline — open/reuse session, run Anthropic turn, reply.

    Serialises calls per chat_id via a shared asyncio.Lock: two messages
    from the same chat must not hit Anthropic in parallel because they'd
    race on the session transcript and double-increment counters.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    client: Any = ctx.application.bot_data[_KEY_CLIENT]
    system_prompt: str = ctx.application.bot_data[_KEY_SYSTEM]
    vault_context_str: str = ctx.application.bot_data[_KEY_VAULT_CTX]
    locks: dict[int, asyncio.Lock] = ctx.application.bot_data[_KEY_LOCKS]

    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id

    # Inline-command pre-check: "Good. /end" should close the session, not
    # get forwarded to Claude as prose. Runs BEFORE the lock + LLM call so
    # we don't waste tokens or mutate transcript state for a message whose
    # intent is a command. Pure ``/end`` never reaches here — PTB's
    # ``filters.TEXT & ~filters.COMMAND`` filter routes those to the
    # CommandHandler directly, so no double-fire risk.
    cmd = _detect_inline_command(text)
    if cmd is not None:
        log.info(
            "talker.bot.inline_command",
            chat_id=chat_id,
            command=cmd,
            text_length=len(text),
        )
        dispatched = await _dispatch_inline_command(cmd, update, ctx)
        if dispatched:
            return

    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock

    async with lock:
        # Open or resume session.
        # wk2: on a fresh session we run the opening-cue router to pick
        # session type / model / continuation. On rehydrate failure we
        # re-route too — the user's next message should feel like a fresh
        # session, not a continuation of whatever state corruption we just
        # discarded.
        active = state_mgr.get_active(chat_id)
        if active:
            try:
                sess = Session.from_dict(active)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "talker.bot.session_rehydrate_failed",
                    chat_id=chat_id,
                    error=str(exc),
                )
                state_mgr.pop_active(chat_id)
                state_mgr.save()
                sess = await _open_routed_session(
                    state_mgr, config, client, chat_id, text,
                )
        else:
            sess = await _open_routed_session(
                state_mgr, config, client, chat_id, text,
            )

        # Voice / text counts are tracked per-turn on the transcript
        # (``_kind`` metadata) — the state-dict counters were wk1
        # scaffolding that double-counted the same information. Dropped.

        # Typing indicator — best-effort; don't block the pipeline.
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as exc:  # noqa: BLE001
            log.debug("talker.bot.typing_action_failed", error=str(exc))

        # Re-read the active dict to pull the stashed pushback level and
        # calibration snapshot — both are written at session-open time by
        # ``_open_routed_session`` and are orthogonal to the
        # :class:`Session` dataclass. ``None`` on either means a pre-wk3
        # active dict (rehydrated from state) that was opened without a
        # stash; ``run_turn`` treats both as "skip that block entirely".
        active_for_turn = state_mgr.get_active(chat_id) or {}
        pushback_level = active_for_turn.get("_pushback_level")
        calibration_str = active_for_turn.get("_calibration_snapshot")

        try:
            response_text = await conversation.run_turn(
                client=client,
                state=state_mgr,
                session=sess,
                user_message=text,
                config=config,
                vault_context_str=vault_context_str,
                system_prompt=system_prompt,
                user_kind="voice" if voice else "text",
                calibration_str=calibration_str,
                pushback_level=pushback_level,
            )
        except anthropic.APIError as exc:
            log.warning(
                "talker.bot.api_error",
                chat_id=chat_id,
                error=str(exc),
            )
            await update.message.reply_text(
                "Sorry — I hit an API error. Try again in a moment?"
            )
            return
        except transcribe.TranscribeError as exc:
            # Tool-loop won't raise this, but belt-and-braces in case a
            # future tool chain routes audio through the model.
            log.warning("talker.bot.transcribe_error_in_turn", error=str(exc))
            await update.message.reply_text(
                "Sorry — transcription problem. Try text?"
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("talker.bot.turn_crash", chat_id=chat_id)
            await update.message.reply_text(
                f"Something went wrong — {exc.__class__.__name__}. "
                "Try again?"
            )
            return

        # Reply with Claude's text. Telegram has a 4096-char message cap;
        # if we ever exceed it, split — but a typical voice session turn
        # won't come close, so keep this simple for wk1.
        if not response_text:
            response_text = "(no response generated)"
        try:
            await update.message.reply_text(response_text)
            log.info(
                "talker.bot.outbound",
                chat_id=chat_id,
                length=len(response_text),
                ok=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.bot.outbound",
                chat_id=chat_id,
                length=len(response_text),
                ok=False,
                error=str(exc),
            )


# Populate the inline-command dispatch map now that the on_* handlers
# are defined. Kept at the bottom of the module so it can reference the
# handler symbols directly without forward-ref gymnastics. Must stay in
# sync with ``_INLINE_COMMANDS`` above — if either side gains a command,
# add it here too.
_INLINE_HANDLERS.update({
    "start": on_start,
    "end": on_end,
    "status": on_status,
    "opus": on_opus,
    "sonnet": on_sonnet,
    "no_auto_escalate": on_no_auto_escalate,
})
