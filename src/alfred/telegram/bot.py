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

from . import calibration, conversation, router, session, session_types, transcribe
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
        "Hi — this is Alfred. Send a voice note or type a message and I'll "
        "reply. Use /end to close the current session, /status for stats."
    )


async def on_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/end — explicitly close the current session, return vault record path."""
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
            session_type=active.get("_session_type", "note"),
            continues_from=active.get("_continues_from"),
            pushback_level=active.get("_pushback_level"),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("talker.bot.close_failed", chat_id=chat_id)
        await update.message.reply_text(f"couldn't close session: {exc}")
        return

    log.info("talker.bot.session_closed", chat_id=chat_id, record=rel_path)
    await update.message.reply_text(f"session closed. saved to: {rel_path}")


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

    sess = _open_session_with_stash(
        state_mgr,
        chat_id,
        config,
        model=decision.model,
        session_type=decision.session_type,
        continues_from=f"[[{decision.continues_from}]]"
        if decision.continues_from
        else None,
        pushback_level=type_defaults.pushback_level,
    )

    # Wk3 commit 2: snapshot the calibration block at session open so every
    # turn in this session sees the same prefix. Reading on each turn would
    # (a) defeat prompt caching and (b) race with commit 7's close-time
    # writer. ``None`` is a valid value — the user may not have a
    # calibration block yet.
    from pathlib import Path
    user_rel = config.primary_users[0] if config.primary_users else ""
    calibration_snapshot = calibration.read_calibration(
        Path(config.vault.path), user_rel,
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
