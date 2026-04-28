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
from datetime import timezone
from pathlib import Path
from typing import Any

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import (
    calibration,
    capture_batch,
    capture_extract,
    conversation,
    heartbeat,
    model_calibration,
    router,
    session,
    session_types,
    speed_pref,
    transcribe,
    tts as tts_mod,
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


# --- Instance-name normalisation ------------------------------------------
#
# The router emits peer-route targets in canonical lowercase form
# (``kal-le``, ``stay-c``). The local instance name arrives from
# :class:`InstanceConfig` as either the casual form (``Salem``) or the
# canonical form (``S.A.L.E.M.``). To compare the two reliably we lowercase,
# strip dots, and map spaces to dashes — the same transform the transport
# layer uses for peer keys in ``config.transport.peers``.
#
# Legacy mapping: the default ``InstanceConfig`` ships ``name="Alfred"``
# for backwards compatibility, but the peer-name table keys off ``salem``.
# We collapse ``alfred`` → ``salem`` here so the self-target check still
# fires when Andrew runs a default-configured instance.


def _normalize_instance_name(s: str) -> str:
    """Return the canonical peer-key form of an instance name.

    Lowercases, strips dots, and maps spaces to dashes. The legacy
    ``alfred`` → ``salem`` mapping is applied so a default-configured
    install still matches the ``salem`` peer key.
    """
    normalized = (s or "").lower().replace(".", "").replace(" ", "-")
    if normalized == "alfred":
        return "salem"
    return normalized


# --- Application-level inbound pre-pass ----------------------------------
#
# The idle-tick heartbeat counter (``heartbeat.record_inbound``) lives at
# application scope so EVERY inbound update increments it — recognised
# commands, unrecognised commands, plain text, voice notes, edited
# messages, callback queries, anything PTB delivers. Anything narrower
# leaves coverage gaps.
#
# History: the original commit (5a26d13) called ``record_inbound`` from
# inside ``on_text`` and ``on_voice`` only. Unrecognised commands
# (e.g. ``/calibration`` when only ``/calibrate`` is registered) bypass
# both — PTB's ``CommandHandler`` matches the recognised ones first and
# the text MessageHandler is gated by ``~filters.COMMAND``, so the
# unknown-command update fell through every handler without ever
# bumping the counter. Result: a ``inbound_in_window=0`` heartbeat
# emitted while Telegram had clearly delivered the message — caught
# 2026-04-22 from a real ``/calibration`` typo. The fix moves the
# increment to a TypeHandler at group=-1 so it observes every Update
# before the per-handler routing fires. Same asyncio loop = no thread
# safety required; the pre-pass returns normally so subsequent groups
# still match exactly as before (no ``ApplicationHandlerStop``).
#
# PTB mechanism: ``TypeHandler(Update, …)`` matches every update and
# group=-1 puts it ahead of the default group (0) where the real
# handlers live. PTB only fires one handler per group, so registering
# this in its own dedicated negative group keeps it off the routing
# critical path.


async def _pre_record_inbound(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Pre-pass: bump the heartbeat counter for every inbound update.

    Registered at group=-1 via :class:`TypeHandler` so it fires before
    any per-handler routing. Returns normally (does NOT raise
    :class:`ApplicationHandlerStop`) so the rest of the handler chain
    runs unchanged. Wraps the increment in a try/except so a counter
    bug can never break message delivery.
    """
    try:
        heartbeat.record_inbound()
    except Exception:  # noqa: BLE001
        log.exception("talker.bot.record_inbound_failed")


# --- Application assembly -------------------------------------------------


def build_app(
    config: TalkerConfig,
    state_mgr: StateManager,
    anthropic_client: Any,
    system_prompt: str,
    vault_context_str: str,
    raw_config: dict | None = None,
) -> Application:
    """Build a PTB :class:`Application` wired with handlers and bot_data.

    Callers add their own post-init hooks (gap sweeper, signal handlers) via
    :mod:`daemon`. This function only does handler registration.

    ``raw_config`` — Stage 3.5 addition. The peer-route dispatcher
    needs the full unified config dict to build a TransportConfig
    at forward time (peer URLs + tokens live under ``transport.peers``).
    ``None`` disables peer routing cleanly.
    """
    app = Application.builder().token(config.bot_token).build()

    app.bot_data[_KEY_CONFIG] = config
    app.bot_data[_KEY_STATE] = state_mgr
    app.bot_data[_KEY_CLIENT] = anthropic_client
    app.bot_data[_KEY_SYSTEM] = system_prompt
    app.bot_data[_KEY_VAULT_CTX] = vault_context_str
    app.bot_data[_KEY_LOCKS] = {}
    app.bot_data["raw_config"] = raw_config

    # Application-level pre-pass: every inbound update bumps the
    # heartbeat counter, regardless of which handler eventually routes
    # it (or whether nothing routes it, e.g. an unrecognised command).
    # See the comment block above ``_pre_record_inbound`` for the
    # coverage-gap incident this guards against.
    app.add_handler(TypeHandler(Update, _pre_record_inbound), group=-1)

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
    # wk2b c4: /extract <short-id> — opt-in note extraction from a
    # capture session. Takes one positional arg (the 8-char short-id
    # emitted in the /end reply); idempotent if the session already has
    # derived_notes populated.
    app.add_handler(CommandHandler("extract", on_extract))
    # wk2b c5: /brief <short-id> — audio summary via ElevenLabs Turbo v2.5.
    # Compresses the structured summary to ~300 words of prose, synthesises,
    # and sends as a Telegram voice message. Opt-in: requires telegram.tts
    # section in config; degrades gracefully to a "not configured" reply.
    app.add_handler(CommandHandler("brief", on_brief))
    # Stage 2b+ polish: /speed — per-instance, per-user ElevenLabs TTS speed
    # preference. Reads/writes ``preferences.voice`` on the primary-user's
    # person record. Applies to every ElevenLabs TTS path (today: /brief).
    app.add_handler(CommandHandler("speed", on_speed))
    # Email-surfacing c2: Daily Sync slash commands. /calibrate fires
    # an out-of-cycle Daily Sync sample; /calibration_ok flips per-tier
    # confidence flags read by the (future) c3/c4/c5 surfacing layers.
    app.add_handler(CommandHandler("calibrate", on_calibrate))
    app.add_handler(CommandHandler("calibration_ok", on_calibration_ok))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))

    return app


# --- Capture-mode ack helpers --------------------------------------------

# Telegram reaction emoji used as a receipt ack during capture sessions.
# Must be drawn from the "free" reaction set (checkmark is universally
# available). See Telegram Bot API docs:
# https://core.telegram.org/bots/api#setmessagereaction — "free" emojis
# are rendered without forcing the user into a Premium prompt.
_CAPTURE_REACTION_EMOJI = "\N{HEAVY CHECK MARK}"  # ✔


async def _post_capture_ack(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Post a receipt-ack reaction emoji on the user's message.

    Uses PTB 21+'s ``Bot.set_message_reaction`` (aliased from Telegram's
    ``setMessageReaction`` endpoint). Raises on any failure so the caller
    can trigger the text fallback path. Kept tiny and dependency-free —
    the reaction emoji is a constant and the call targets the message
    that triggered this turn.
    """
    if update.message is None:
        return
    from telegram import ReactionTypeEmoji
    await ctx.bot.set_message_reaction(
        chat_id=chat_id,
        message_id=update.message.message_id,
        reaction=[ReactionTypeEmoji(emoji=_CAPTURE_REACTION_EMOJI)],
    )


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
            # Per-instance session-save shape: all registered tool_sets
            # emit ``<mode>-<date>-<slug>-<short-id>``; unknown / empty
            # ``tool_set`` falls back to the wk1 ``Voice Session — ...``
            # filename. ``InstanceConfig.tool_set`` is the contract-key.
            tool_set=config.instance.tool_set or "",
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

        from alfred.audit import agent_slug_for as _agent_slug_for_calib
        result = calibration.apply_proposals(
            vault_path=Path(config.vault.path),
            user_rel_path=user_rel,
            proposals=proposals,
            session_record_path=rel_path,
            confirmation_dial=calibration.DEFAULT_CONFIRMATION_DIAL,
            agent_slug=_agent_slug_for_calib(config),
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

    # wk2b c3: capture-mode batch structuring pass. Fires as a detached
    # task so /end returns fast; the structured summary + follow-up
    # Telegram message land asynchronously once Sonnet responds.
    if session_type == "capture":
        short_id = (active.get("session_id", "") or "").split("-")[0]
        # Show the "processing..." marker up front so Andrew sees SOMETHING
        # immediately even if the batch pass takes a few seconds.
        close_reply = (
            f"session closed. saved to: {rel_path}\ncapture processing…"
        )

        # Pre-bind chat_id so the orchestrator doesn't need a bot context.
        async def _send_follow_up(text: str) -> None:
            await ctx.bot.send_message(chat_id=chat_id, text=text)

        try:
            from pathlib import Path as _Path
            from alfred.audit import agent_slug_for
            batch_model = config.anthropic.model or "claude-sonnet-4-6"
            asyncio.create_task(
                capture_batch.process_capture_session(
                    client=client,
                    vault_path=_Path(config.vault.path),
                    session_rel_path=rel_path,
                    transcript=transcript_snapshot,
                    model=batch_model,
                    send_follow_up=_send_follow_up,
                    short_id=short_id,
                    agent_slug=agent_slug_for(config),
                )
            )
        except Exception as exc:  # noqa: BLE001 — scheduling shouldn't block close reply
            log.warning(
                "talker.capture.schedule_failed",
                chat_id=chat_id,
                error=str(exc),
            )

        await update.message.reply_text(close_reply + suffix)
        return

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


async def on_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/extract <short-id> — extract standalone notes from a capture session.

    Idempotency: if the session record already has a populated
    ``derived_notes`` frontmatter list, we refuse and surface the
    existing note paths rather than appending a second batch. The user
    must delete the existing notes to re-run.

    Implicit chain: if the session doesn't yet have a
    ``## Structured Summary`` block (e.g. the batch pass failed or
    hasn't fired), the extraction call runs a synthetic structuring
    pass first.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    client: Any = ctx.application.bot_data[_KEY_CLIENT]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    short_id = _parse_short_id_arg(update.message.text or "", ctx.args)
    if not short_id:
        await update.message.reply_text(
            "usage: /extract <short-id>  (the 8-char id from the /end reply)"
        )
        return

    from pathlib import Path as _Path
    model = config.anthropic.model or "claude-sonnet-4-6"
    log.info(
        "talker.extract.invoked",
        chat_id=update.effective_chat.id,
        short_id=short_id,
    )

    from alfred.audit import agent_slug_for as _agent_slug_for_extract
    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=_Path(config.vault.path),
        short_id=short_id,
        model=model,
        agent_slug=_agent_slug_for_extract(config),
    )

    if result.skipped_reason == "already_extracted":
        joined = ", ".join(result.created_paths[:5])
        if len(result.created_paths) > 5:
            joined += f", +{len(result.created_paths) - 5} more"
        await update.message.reply_text(
            f"Already extracted {len(result.created_paths)} notes "
            f"({joined}). Delete first to re-run."
        )
        return

    if result.skipped_reason == "no_session":
        await update.message.reply_text(
            f"No session found for short-id {short_id!r}. Is it closed?"
        )
        return

    if result.skipped_reason == "no_record":
        await update.message.reply_text(
            f"Session record missing for short-id {short_id!r}."
        )
        return

    if result.skipped_reason.startswith("llm_error"):
        await update.message.reply_text(
            f"Extraction failed: {result.skipped_reason[len('llm_error: '):]}"
        )
        return

    if not result.created_paths:
        await update.message.reply_text(
            "No notes extracted — nothing in this session warranted a "
            "standalone note."
        )
        return

    joined = "\n".join(f"• {p}" for p in result.created_paths)
    await update.message.reply_text(
        f"Extracted {len(result.created_paths)} notes:\n{joined}"
    )


async def on_brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief <short-id> — audio summary via ElevenLabs.

    Flow:
      1. Resolve short-id → session record path.
      2. Load ``## Structured Summary`` block (or run batch pass first
         if missing — implicit chain).
      3. Compress to ~word_target words of prose via Sonnet.
      4. Synthesize via ElevenLabs Turbo v2.5.
      5. Send as Telegram voice message (or document for >50 MB audio).

    Graceful degradation:
      * tts section absent → "not configured" text reply
      * ElevenLabs API down → fall back to text summary
      * Audio >50 MB → send as document instead of voice
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    client: Any = ctx.application.bot_data[_KEY_CLIENT]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    short_id = _parse_short_id_arg(update.message.text or "", ctx.args)
    if not short_id:
        await update.message.reply_text(
            "usage: /brief <short-id>  (the 8-char id from the /end reply)"
        )
        return

    if config.tts is None:
        await update.message.reply_text(
            "TTS is not configured. Add telegram.tts to config.yaml to "
            "enable /brief."
        )
        return

    chat_id = update.effective_chat.id
    log.info(
        "talker.brief.invoked",
        chat_id=chat_id,
        short_id=short_id,
    )

    from pathlib import Path as _Path
    model = config.anthropic.model or "claude-sonnet-4-6"
    vault_path = _Path(config.vault.path)

    # Resolve short-id → session record path.
    session_rel = capture_extract._find_session_by_short_id(state_mgr, short_id)
    if session_rel is None:
        await update.message.reply_text(
            f"No session found for short-id {short_id!r}. Is it closed?"
        )
        return

    post = capture_extract._load_session_record(vault_path, session_rel)
    if post is None:
        await update.message.reply_text(
            f"Session record missing for short-id {short_id!r}."
        )
        return

    summary_block = capture_extract._extract_summary_from_post(post)
    if not summary_block:
        # Implicit chain — try to structure first.
        try:
            from alfred.audit import agent_slug_for
            transcript = capture_extract._synthetic_transcript_from_body(
                post.content
            )
            summary = await capture_batch.run_batch_structuring(
                client, transcript, model,
            )
            summary_block = capture_batch.render_summary_markdown(summary)
            await capture_batch.write_summary_to_session_record(
                vault_path, session_rel, summary_block, "true",
                agent_slug=agent_slug_for(config),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.brief.implicit_structure_failed",
                short_id=short_id,
                error=str(exc),
            )
            await update.message.reply_text(
                f"Couldn't structure this session for a brief ({exc})."
            )
            return

    # Compress to spoken prose.
    try:
        prose = await tts_mod.compress_summary_for_tts(
            client=client,
            summary_markdown=summary_block,
            model=model,
            word_target=config.tts.summary_word_target,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.brief.compress_failed", short_id=short_id, error=str(exc),
        )
        await update.message.reply_text(
            f"Couldn't compress summary for brief ({exc})."
        )
        return

    if not prose:
        await update.message.reply_text(
            "Compressed summary came back empty — try /extract instead?"
        )
        return

    # Synthesize. Resolve the per-(instance, user) TTS speed preference
    # before the call so Andrew's /speed calibration flows through every
    # ElevenLabs path uniformly. Default 1.0 when unset — matches
    # ElevenLabs' own default.
    instance_for_speed = (
        config.instance.name or config.instance.canonical or "Alfred"
    )
    user_rel_for_speed = (
        config.primary_users[0] if config.primary_users else ""
    )
    speed = speed_pref.resolve_tts_speed(
        vault_path, user_rel_for_speed, instance_for_speed,
    )
    try:
        audio = await tts_mod.synthesize(prose, config.tts, speed=speed)
    except tts_mod.TtsNotConfigured:
        await update.message.reply_text(
            "TTS is not configured. Add telegram.tts.api_key to config.yaml."
        )
        return
    except tts_mod.TtsError as exc:
        # API down — fall back to the prose as a text reply so the user
        # still gets content.
        log.warning(
            "talker.brief.tts_failed", short_id=short_id, error=str(exc),
        )
        await update.message.reply_text(
            "Audio synthesis failed — here's the text summary:\n\n" + prose
        )
        return

    # Per-session cost log line — approximate cost per 1k chars for
    # Turbo v2.5 is ~$0.30 / 1M chars. Rendered in structlog output and
    # grep-able as ``tts.cost_estimate``.
    log.info(
        "talker.brief.cost_estimate",
        short_id=short_id,
        chars=len(prose),
        # $0.30 per 1M chars for Turbo v2.5 PAYG (approximate).
        dollars_estimate=round(len(prose) * 0.30 / 1_000_000, 4),
    )

    try:
        send_result = await tts_mod.send_voice_to_telegram(
            bot=ctx.bot,
            chat_id=chat_id,
            audio_bytes=audio,
            caption=f"brief for {short_id}",
            filename=f"brief-{short_id}.mp3",
        )
        log.info(
            "talker.brief.sent",
            short_id=short_id,
            mode=send_result.mode,
            size_bytes=send_result.size_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.brief.send_failed",
            short_id=short_id,
            error=str(exc),
        )
        await update.message.reply_text(
            "Synthesised audio but couldn't upload — here's the text:\n\n"
            + prose
        )


async def on_speed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/speed [value | default]`` — manage per-instance TTS speed preference.

    Usage:
        * ``/speed``              → report current + last 3 history entries.
        * ``/speed 1.2``          → set current speed (must be 0.7-1.2).
        * ``/speed 1.2 too slow`` → set with free-text note stashed in history.
        * ``/speed default``      → reset to 1.0 (``by=reset`` history entry).

    The preference is keyed by (instance, user). Each instance carries its
    own voice, so a speed that suits Salem's Rachel may not suit STAY-C's
    clinical narrator — the per-instance scoping is the point.

    Writes through ``ops.vault_edit`` (via :mod:`speed_pref`) so the
    talker scope's existing person-record edit permission covers the
    call. The allowlist-less talker scope allows the ``preferences``
    field to be written.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    instance_name = (
        config.instance.name or config.instance.canonical or "Alfred"
    )
    user_rel = config.primary_users[0] if config.primary_users else ""
    vault_path = Path(config.vault.path)

    # Parse off the raw text (supports both CommandHandler and inline paths).
    raw_text = update.message.text or ""
    mode, value, note = speed_pref.parse_speed_command(raw_text)

    if mode == "report":
        reply = speed_pref.format_report(vault_path, user_rel, instance_name)
        await update.message.reply_text(reply)
        log.info(
            "talker.speed.report",
            chat_id=chat_id, instance=instance_name,
        )
        return

    if mode == "error":
        await update.message.reply_text(
            note or "Couldn't parse that. Try /speed 1.2 or /speed default."
        )
        return

    if mode == "reset":
        summary = speed_pref.set_tts_speed(
            vault_path, user_rel, instance_name,
            speed_pref.SPEED_DEFAULT,
            by="reset",
            note=note,
        )
        if not summary["written"]:
            await update.message.reply_text(
                f"Couldn't reset speed: {summary['reason']}."
            )
            return
        await update.message.reply_text(
            f"{instance_name} speed reset to {speed_pref.SPEED_DEFAULT}."
        )
        return

    # mode == "set"
    assert value is not None  # parse_speed_command contract
    try:
        validated = speed_pref.validate_speed(value)
    except speed_pref.SpeedValidationError as exc:
        await update.message.reply_text(str(exc))
        return

    summary = speed_pref.set_tts_speed(
        vault_path, user_rel, instance_name,
        validated,
        by="slash_command",
        note=note,
    )
    if not summary["written"]:
        await update.message.reply_text(
            f"Couldn't save speed: {summary['reason']}."
        )
        return

    suffix = f" (note saved)" if note else ""
    await update.message.reply_text(
        f"{instance_name} speed set to {validated}.{suffix}"
    )


# --- Daily Sync slash commands (email-surfacing c2) -----------------------


async def on_calibrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/calibrate`` — fire a fresh Daily Sync sample out of cycle.

    Useful when Andrew wants to validate a few classifier outputs right
    now rather than waiting for 09:00. Bypasses the
    ``last_fired_date`` dedup guard but reuses the same dispatch path
    (assemble → push → persist batch) so the reply parser sees a fresh
    set of message_ids to match against.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    raw_config = ctx.application.bot_data.get("raw_config") or {}
    try:
        from alfred.daily_sync.config import load_from_unified as load_ds
        from alfred.daily_sync.daemon import fire_once
    except ImportError:
        await update.message.reply_text(
            "Daily Sync module not available — check install."
        )
        return

    ds_config = load_ds(raw_config)
    if not ds_config.enabled:
        await update.message.reply_text(
            "Daily Sync isn't configured. Add a `daily_sync:` block to "
            "config.yaml and set `enabled: true`."
        )
        return

    user_id = config.allowed_users[0] if config.allowed_users else 0
    if not user_id:
        await update.message.reply_text(
            "No primary Telegram user configured — can't dispatch."
        )
        return

    vault_path = Path(config.vault.path)
    log.info(
        "talker.bot.calibrate_invoked",
        chat_id=update.effective_chat.id,
        user_id=user_id,
    )
    await update.message.reply_text("Daily Sync sample firing now…")
    try:
        # ``manual=True`` swaps the transport dedupe key from the
        # date-only ``daily-sync-{date}`` (which collides with the auto
        # 09:00 fire and any prior /calibrate today) to a unique
        # ``daily-sync-{date}-calibrate-{uuid8}``. Without this the
        # second /calibrate of a day silently short-circuits at the
        # transport server's idempotency check and Andrew sees the
        # "firing now…" ack but no second message.
        result = await fire_once(ds_config, vault_path, user_id, manual=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("talker.bot.calibrate_failed")
        await update.message.reply_text(
            f"Couldn't fire Daily Sync: {exc.__class__.__name__}: {exc}"
        )
        return

    if result["items_count"] == 0:
        await update.message.reply_text(
            "Daily Sync sent, but no calibratable items in the vault yet."
        )
    # Otherwise the dispatched batch IS the user-visible feedback —
    # they'll see it as a separate message and can reply to it.


async def on_calibration_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/calibration_ok [tier]`` — manage per-tier surfacing confidence flags.

    Usage:
      * ``/calibration_ok``         → list current flags.
      * ``/calibration_ok high``    → flip the ``high`` flag to True.

    The flags are read by future surfacing consumers (c3 brief section,
    c4 Obsidian view, c5 Telegram push) to gate per-tier surfacing on
    Andrew's explicit approval. Flipping is idempotent — calling
    ``/calibration_ok high`` when the flag is already True is a no-op
    response that confirms the current state.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    raw_config = ctx.application.bot_data.get("raw_config") or {}
    try:
        from alfred.daily_sync.config import load_from_unified as load_ds
        from alfred.daily_sync.confidence import (
            format_confidence_report,
            list_confidence,
            set_confidence,
        )
    except ImportError:
        await update.message.reply_text(
            "Daily Sync module not available — check install."
        )
        return

    ds_config = load_ds(raw_config)
    if not ds_config.enabled:
        await update.message.reply_text(
            "Daily Sync isn't configured. Add a `daily_sync:` block to "
            "config.yaml and set `enabled: true`."
        )
        return

    raw_text = (update.message.text or "").strip()
    parts = raw_text.split()
    arg = parts[1].lower().strip() if len(parts) > 1 else ""

    if not arg:
        flags = list_confidence(ds_config.state.path, ds_config.confidence)
        await update.message.reply_text(format_confidence_report(flags))
        return

    try:
        flags = set_confidence(
            ds_config.state.path, arg, True, seed=ds_config.confidence,
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.reply_text(
        f"Flipped `{arg}` confidence to True.\n\n"
        + format_confidence_report(flags)
    )


# Inline ``/extract abc123`` detection. PTB's CommandHandler fires when
# the message STARTS with /extract — the inline path here matches
# ``please /extract abc123`` at end-of-line. The short-id is parsed
# from the text ourselves because the inline path doesn't populate
# ``ctx.args``.
_INLINE_EXTRACT_RE = re.compile(r"(?:^|[.!?;:,]\s+)/extract\s+(\w+)\s*$|^/extract\s+(\w+)\b")


def _parse_short_id_arg(text: str, args: Any) -> str:
    """Return the short-id argument, tolerating both CommandHandler + inline forms.

    ``args`` may be ``None`` (inline path didn't populate it), a list
    (CommandHandler populated from the command suffix), or a stray
    object like a MagicMock (some test paths). We defensively check for
    list-shape before indexing, and fall back to regex extraction from
    the raw message text in every other case.
    """
    if isinstance(args, list) and args:
        return str(args[0]).strip()
    first_line = text.splitlines()[0] if text else ""
    match = _INLINE_EXTRACT_RE.search(first_line)
    if match:
        return (match.group(1) or match.group(2) or "").strip()
    # Also try the with-arg regex for inline ``/extract <id>`` forms.
    # Group layout: (1,2)=eol extract/brief+arg, (3,4)=start extract/brief+arg,
    # (5,6)=eol speed+arg, (7,8)=start speed+arg. Pull the short-id out of
    # group 2 (eol form) or group 4 (start form), but only when the matching
    # command name is "extract".
    match2 = _INLINE_CMD_WITH_ARG_RE.search(first_line)
    if match2:
        if (match2.group(1) or "").lower() == "extract":
            return match2.group(2).strip()
        if (match2.group(3) or "").lower() == "extract":
            return match2.group(4).strip()
    return ""


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
    # Idle-tick heartbeat counter is bumped by the application-level
    # ``_pre_record_inbound`` pre-pass (group=-1), not here — that
    # ensures unrecognised commands and other non-text-handler updates
    # still count. See the pre-pass comment block above ``build_app``.
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
    # Idle-tick heartbeat counter is bumped by the application-level
    # ``_pre_record_inbound`` pre-pass (group=-1) — see ``on_text``.

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
    # Per-instance session-save shape contract: all registered tool_sets
    # emit ``<mode>-<date>-<slug>-<short-id>``; unknown / empty
    # ``tool_set`` falls back to the wk1 ``Voice Session — ...`` filename.
    # Stashed at open so timeout / startup-sweep close paths can route
    # correctly even when the config object isn't accessible from the
    # close site.
    active["_tool_set"] = config.instance.tool_set or ""
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


# Stage 3.5: wait-ping interval when a peer-forwarded turn is in flight.
# If the peer hasn't replied within this many seconds, send a "still
# thinking…" message to Andrew so the silence isn't confusing.
_PEER_MID_WAIT_PING_SECONDS: float = 20.0

# Maximum time Salem will wait for KAL-LE before giving up and
# replying with the timeout message.
_PEER_MAX_WAIT_SECONDS: float = 45.0


async def _dispatch_peer_route(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    target: str,
    text: str,
    chat_id: int,
    originating_session_id: str,
) -> bool:
    """Forward a turn to ``target`` peer and relay its reply.

    Returns True iff the forward + relay path completed (even if the
    peer replied with an error). Returns False when the peer was
    unreachable — the caller falls through to Salem's normal handling
    so Andrew isn't left in limbo.

    Flow:
      1. Send an immediate "→ KAL-LE" acknowledgement so Andrew sees
         something in the chat before the round-trip.
      2. POST /peer/send to the target with the user's message.
      3. Kick off a mid-wait ping timer (20s) alongside the response
         wait (45s cap).
      4. When the peer replies via its own /peer/send to us (correlation
         id round-trip through ``await_response``), relay the reply
         prefixed with ``[KAL-LE] ``.
    """
    from alfred.transport import peers as peers_module
    from alfred.transport.client import peer_send
    from alfred.transport.config import load_from_unified as load_transport
    from alfred.transport.exceptions import (
        TransportError, TransportServerDown,
    )

    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if update.message is None:
        return False

    # Self-target guard: if the router classified peer_route with a
    # target matching our own instance name, there's no peer to forward
    # to — handle the turn locally. Runs BEFORE the "→ KAL-LE" ack so
    # Andrew doesn't see a spurious arrow followed by a "couldn't
    # reach…" error. Returning False makes the caller fall through to
    # Salem's normal handling, same as the TransportServerDown branch.
    #
    # ``name`` (not ``canonical``) is the peer-key source: ``KAL-LE``
    # normalizes to ``kal-le`` (matches the peer key), whereas
    # ``K.A.L.L.E.`` normalizes to ``kalle`` (no dashes — wouldn't
    # match). This mirrors ``transport.health._infer_self_name``.
    self_name = _normalize_instance_name(
        config.instance.name or config.instance.canonical or "salem",
    )
    target_normalized = _normalize_instance_name(target)
    if target_normalized == self_name:
        log.info(
            "talker.bot.peer_route_self_target",
            chat_id=chat_id,
            target=target,
            self_name=self_name,
            reason="router classified peer_route to own instance; handling locally",
        )
        return False

    try:
        ack_msg = await update.message.reply_text(f"→ {target.upper()}")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.peer_ack_failed",
            chat_id=chat_id, error=str(exc),
        )
        ack_msg = None

    # Build the transport config from the raw config the daemon stashed
    # on startup. If it's missing we can't peer-route — caller falls
    # back to normal handling.
    raw_config = ctx.application.bot_data.get("raw_config")
    if not raw_config:
        log.warning(
            "talker.bot.peer_route_no_raw_config",
            chat_id=chat_id,
        )
        return False

    try:
        transport_config = load_transport(raw_config)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.peer_route_config_load_failed",
            chat_id=chat_id, error=str(exc),
        )
        return False

    correlation_id = peers_module._prune_orphans or None  # sanity import
    from alfred.transport.peers import _prune_orphans  # noqa: F401

    import uuid
    correlation_id = uuid.uuid4().hex[:16]

    # ``self_name`` already computed above for the self-target guard —
    # reuse it verbatim so the peer_send wire protocol and the self-
    # check agree on the same canonical form.

    try:
        await peer_send(
            target,
            kind="message",
            payload={
                "user_id": update.effective_user.id if update.effective_user else 0,
                "text": text,
                "originating_session": originating_session_id,
                "chat_id": chat_id,
            },
            config=transport_config,
            self_name=self_name,
            correlation_id=correlation_id,
        )
    except TransportServerDown as exc:
        log.warning(
            "talker.bot.peer_unavailable",
            target=target, chat_id=chat_id, error=str(exc),
        )
        await update.message.reply_text(
            f"{target.upper()} is offline — can't route. I'll try to "
            "answer directly but coding-specific commands won't work."
        )
        return False  # Fall through to Salem's own handling
    except TransportError as exc:
        log.warning(
            "talker.bot.peer_route_failed",
            target=target, chat_id=chat_id, error=str(exc),
        )
        await update.message.reply_text(
            f"Couldn't reach {target.upper()}: {exc}"
        )
        return True  # Don't fall through — error already surfaced

    # --- Wait for the peer to call back via /peer/send ----------------
    mid_wait_ping_task: asyncio.Task | None = None

    async def _mid_wait_ping() -> None:
        try:
            await asyncio.sleep(_PEER_MID_WAIT_PING_SECONDS)
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"{target.upper()} still working…",
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.info(
                "talker.bot.peer_mid_ping_failed",
                chat_id=chat_id, error=str(exc),
            )

    mid_wait_ping_task = asyncio.create_task(_mid_wait_ping())

    from alfred.transport.peers import await_response
    try:
        reply = await await_response(correlation_id, timeout=_PEER_MAX_WAIT_SECONDS)
    except asyncio.TimeoutError:
        log.warning(
            "talker.bot.peer_reply_timeout",
            target=target, correlation_id=correlation_id,
        )
        await update.message.reply_text(
            f"{target.upper()} didn't reply within {int(_PEER_MAX_WAIT_SECONDS)}s — "
            "try again, or DM the bot directly."
        )
        return True
    finally:
        if mid_wait_ping_task and not mid_wait_ping_task.done():
            mid_wait_ping_task.cancel()

    reply_text = str(reply.get("text") or reply.get("reply") or "")
    if not reply_text:
        await update.message.reply_text(
            f"[{target.upper()}] (empty reply)"
        )
        return True

    await update.message.reply_text(f"[{target.upper()}] {reply_text}")
    return True


async def _open_routed_session(
    state_mgr: StateManager,
    config: TalkerConfig,
    client: Any,
    chat_id: int,
    first_message: str,
    has_reply_context: bool = False,
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

    ``has_reply_context`` (reply-context consumer): when the incoming
    turn is a Telegram reply to a prior bot message, pass ``True`` so
    the classifier can tip its default away from fresh cue-driven
    sessions (capture / journal / article) and toward continuation /
    note. Reply-to-bot-message is a strong "this is a follow-up"
    signal. Defaults to ``False`` for legacy callers and the rehydrate-
    failure path when the bug that triggered it wasn't a reply.
    """
    recent = _recent_sessions_for_router(state_mgr)
    # Stage 3.5 hotfix c3: thread the local instance identity into the
    # router so the classifier knows who it is and can't route to self.
    # Uses the same name-first convention the self-target guard in
    # ``_dispatch_peer_route`` uses — see ``_normalize_instance_name``.
    self_name = _normalize_instance_name(
        config.instance.name or config.instance.canonical or "salem",
    )
    self_display_name = (
        config.instance.canonical or config.instance.name or "Alfred"
    )
    decision = await router.classify_opening_cue(
        client, first_message, recent,
        self_name=self_name,
        self_display_name=self_display_name,
        has_reply_context=has_reply_context,
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
    # Stage 3.5: stash peer-route target on the active session so
    # every subsequent turn forwards to the same peer without
    # re-classifying. ``_dispatch_peer_route`` reads this in
    # ``handle_message`` before it falls into the Anthropic turn.
    if decision.session_type == "peer_route" and decision.target:
        active["_peer_route_target"] = decision.target
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


# --- Reply-context consumer ----------------------------------------------
#
# When a Telegram user long-presses a bot message and hits "Reply," the
# Bot API attaches the full parent message via ``Message.reply_to_message``.
# python-telegram-bot 22 exposes this as ``update.message.reply_to_message``.
# We consume it by prepending a machine-generated context prefix to the
# turn text BEFORE the router classifier or Anthropic turn runs, so the
# model has explicit context about what Andrew's "book it" / "done" /
# "explain the second failure" is replying to.
#
# Reply-to-bot-message is also a strong "continuation" signal for the
# router: we pass ``has_reply_context`` to ``classify_opening_cue`` so a
# reply to an earlier Salem message tips the default away from opening a
# fresh cue-driven session (capture / journal / article) and toward
# continuing the existing line of thought.
#
# Parent text cap: 500 characters (ElevenLabs TTS briefs and Salem
# summaries can easily exceed this; we want the prefix to stay compact
# enough that it doesn't dominate the turn). Truncation suffix is
# ``... (truncated)`` so the model knows the quote is incomplete.

# Maximum characters from the parent message to inline into the prefix.
# Longer parents are truncated with `... (truncated)` so the quote stays
# compact while still signalling there was more.
_REPLY_CTX_QUOTE_LIMIT: int = 500

# Suffix appended when the parent message is truncated. Chosen as prose
# rather than an ellipsis-only sentinel so the LLM reading the prefix
# knows the quote is machine-truncated (not the author's own ellipsis).
_REPLY_CTX_TRUNCATION_SUFFIX: str = "... (truncated)"


def _build_reply_context_prefix(
    reply_to_message: Any,
    instance_name: str = "Salem",
) -> str | None:
    """Return a `[You are replying to ...]\\n\\n` prefix, or None.

    Consumes python-telegram-bot's ``Message.reply_to_message`` and renders
    a single-line attribution + quoted parent body. Returns ``None`` when
    the parent has no usable text (photo-only, sticker, etc.) so the
    caller can silently fall through to the normal flow.

    ``instance_name`` is the casual persona name from
    ``TalkerConfig.instance.name`` ("Salem", "Hypatia", "KAL-LE", ...). It
    appears verbatim in the bot-attribution branch ("Hypatia's earlier
    message"). Default ``"Salem"`` preserves the original prefix shape
    for direct test callers and keeps Salem-installed bots byte-for-byte
    identical to pre-templating behaviour.

    Format::

        [You are replying to <instance_name>'s earlier message at <ISO-time>: "<quote>"]

        <blank line above separates the prefix from user's reply text>

    The caller is responsible for concatenating the user's actual text
    after the returned prefix.

    Edge cases:

    - Photo / sticker / voice reply with no ``text`` or ``caption`` →
      returns ``None``; no prefix is prepended (the parent has nothing
      to quote).
    - Parent longer than ``_REPLY_CTX_QUOTE_LIMIT`` chars →
      truncated to that many chars plus ``... (truncated)`` suffix.
    - Parent from the bot itself (``from_user.is_bot == True``) →
      attribution "<instance_name>'s earlier message"; parent from the
      same user → "your earlier message". Multi-user chats are future work.
    - Parent text contains literal ``"`` or other special characters →
      rendered verbatim inside the prefix. The surrounding double quotes
      are the JSON-safe delimiter of choice (backtick or triple-quote
      would also work, but double-quotes match Salem's conversational
      register and survive the Anthropic prompt round-trip — the turn
      text is passed as a JSON string by the SDK so quote escaping is
      handled there).
    """
    if reply_to_message is None:
        return None
    # python-telegram-bot exposes the parent body as either ``text``
    # (regular text message) or ``caption`` (photo / video with caption).
    # Either is fine for our purposes — we just need something to quote.
    #
    # ``isinstance(..., str)`` check is load-bearing: existing tests that
    # MagicMock the whole Update object will have ``reply_to_message``
    # auto-instantiated as a MagicMock (truthy, non-None) whose ``.text``
    # is another MagicMock. We want those tests to behave as "no reply"
    # (no prefix), not crash or emit a MagicMock-shaped quote. The real
    # PTB ``Message.text`` is ``str | None``, so the type check excludes
    # nothing legitimate.
    raw_text = getattr(reply_to_message, "text", None)
    raw_caption = getattr(reply_to_message, "caption", None)
    parent_text = ""
    if isinstance(raw_text, str) and raw_text.strip():
        parent_text = raw_text.strip()
    elif isinstance(raw_caption, str) and raw_caption.strip():
        parent_text = raw_caption.strip()
    if not parent_text:
        return None

    if len(parent_text) > _REPLY_CTX_QUOTE_LIMIT:
        parent_text = (
            parent_text[:_REPLY_CTX_QUOTE_LIMIT] + _REPLY_CTX_TRUNCATION_SUFFIX
        )

    # Attribution: reply-to-bot vs reply-to-own-message. Multi-user
    # chats aren't supported today (Telegram's bot allowlist is one
    # Andrew-shaped entry), so "your earlier message" is the only
    # non-bot case we need to cover.
    from_user = getattr(reply_to_message, "from_user", None)
    is_bot = bool(from_user and getattr(from_user, "is_bot", False))
    attribution = (
        f"{instance_name}'s earlier message" if is_bot else "your earlier message"
    )

    # Timestamp. ``Message.date`` is a tz-aware datetime per PTB's
    # contract. Normalise to UTC and format to second precision —
    # microseconds are noise in a human-readable prefix.
    raw_date = getattr(reply_to_message, "date", None)
    try:
        iso_ts = raw_date.astimezone(timezone.utc).isoformat(
            timespec="seconds",
        )
    except Exception:  # noqa: BLE001 — defensive: naive datetime edge case
        # Fall back to str() which still produces something human-readable.
        # Not worth crashing the turn over a timestamp format quirk.
        iso_ts = str(raw_date) if raw_date is not None else ""

    return (
        f'[You are replying to {attribution} at {iso_ts}: '
        f'"{parent_text}"]\n\n'
    )


# --- Daily Sync reply pre-check ------------------------------------------
#
# Email-surfacing c2: when the user replies (Telegram reply thread) to
# the most recent Daily Sync push, the terse reply is routed to the
# corpus writer instead of the normal conversation pipeline. This is
# intentional — Daily Sync replies are calibration signals, not prose
# for Salem to respond to.
#
# Identification shape: ``update.message.reply_to_message.message_id``
# matches one of the Telegram message_ids the Daily Sync daemon pushed
# and stashed in ``daily_sync_state.json``. The bot reads config via
# ``bot_data["raw_config"]`` so Daily Sync can be enabled / disabled
# independently of the talker.


def _extract_reply_message_id(reply_to_message: Any) -> int | None:
    """Return the integer ``message_id`` of the replied-to message, or None.

    Defensive against MagicMock-backed test shapes: an int check
    rejects the auto-instantiated MagicMock ``message_id`` that
    existing tests carry through unrelated fixtures.
    """
    if reply_to_message is None:
        return None
    raw = getattr(reply_to_message, "message_id", None)
    if isinstance(raw, int):
        return raw
    return None


async def _maybe_handle_daily_sync_reply(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    parent_msg_id: int,
    user_text: str,
) -> bool:
    """Try to handle ``user_text`` as a Daily Sync reply. Returns True iff handled.

    Keeps the bot module's import surface small by lazy-importing the
    daily_sync reply dispatcher — if the module isn't available (e.g.
    an old install) the caller falls through to the normal pipeline.
    Never raises into the caller; logs and returns False on any error.
    """
    raw_config = ctx.application.bot_data.get("raw_config") or {}
    try:
        from alfred.daily_sync.config import load_from_unified as load_ds
        from alfred.daily_sync.reply_dispatch import (
            handle_daily_sync_reply,
            reply_targets_daily_sync,
        )
    except ImportError:
        return False

    try:
        ds_config = load_ds(raw_config)
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.bot.daily_sync_config_failed", error=str(exc))
        return False

    if not ds_config.enabled:
        return False

    try:
        if not reply_targets_daily_sync(ds_config, parent_msg_id):
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.bot.daily_sync_state_read_failed", error=str(exc))
        return False

    # Thread the vault path through so attribution-item confirms /
    # rejects can read + write the affected records. Email items don't
    # need it (the email-calibration corpus is path-agnostic), but
    # Phase 2 attribution items operate directly on vault files.
    talker_config = ctx.application.bot_data.get(_KEY_CONFIG)
    vault_path: Path | None = None
    instance_scope = "talker"
    if talker_config is not None:
        try:
            vault_path = Path(talker_config.vault.path)
        except Exception:  # noqa: BLE001
            vault_path = None
        # Mirror of conversation._execute_tool's per-instance scope
        # routing (commit b8c843d). On a Hypatia / KAL-LE bot, a
        # canonical-record proposal confirm must dispatch to the right
        # SCOPE_RULES entry — "talker" denies hypatia-only types like
        # ``document``.
        try:
            instance_scope = talker_config.instance.tool_set or "talker"
        except Exception:  # noqa: BLE001
            instance_scope = "talker"

    try:
        result = handle_daily_sync_reply(
            ds_config,
            parent_msg_id,
            user_text,
            vault_path=vault_path,
            instance_scope=instance_scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.bot.daily_sync_reply_failed", error=str(exc))
        return False

    if result is None:
        return False

    if update.message is not None:
        try:
            await update.message.reply_text(result.get("message", "ok."))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.bot.daily_sync_ack_failed", error=str(exc),
            )
    log.info(
        "talker.bot.daily_sync_reply",
        parent_msg_id=parent_msg_id,
        confirmed=result.get("confirmed_count", 0),
        all_ok=result.get("all_ok", False),
        unparsed=len(result.get("unparsed", [])),
    )
    return True


async def _maybe_smart_route_daily_sync_reply(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> bool:
    """Try to smart-route ``user_text`` as a Daily Sync reply (Option B).

    Companion to :func:`_maybe_handle_daily_sync_reply` — that helper
    requires Telegram's reply-to-message context, this one runs when
    Andrew sent a fresh message (no reply context) that LOOKS like a
    calibration reply AND the latest Daily Sync batch hasn't been
    replied to yet. Returns True iff handled (caller skips the rest
    of the conversation pipeline). Never raises into the caller.

    The shape-detection heuristic + false-positive guard live in
    ``daily_sync.reply_dispatch.maybe_smart_route_reply`` — this
    helper is just the bot-side adapter.
    """
    raw_config = ctx.application.bot_data.get("raw_config") or {}
    try:
        from alfred.daily_sync.config import load_from_unified as load_ds
        from alfred.daily_sync.reply_dispatch import (
            maybe_smart_route_reply,
        )
    except ImportError:
        return False

    try:
        ds_config = load_ds(raw_config)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.daily_sync_smart_route_config_failed",
            error=str(exc),
        )
        return False

    if not ds_config.enabled:
        return False

    talker_config = ctx.application.bot_data.get(_KEY_CONFIG)
    vault_path: Path | None = None
    instance_scope = "talker"
    if talker_config is not None:
        try:
            vault_path = Path(talker_config.vault.path)
        except Exception:  # noqa: BLE001
            vault_path = None
        try:
            instance_scope = talker_config.instance.tool_set or "talker"
        except Exception:  # noqa: BLE001
            instance_scope = "talker"

    try:
        result = maybe_smart_route_reply(
            ds_config,
            user_text,
            vault_path=vault_path,
            instance_scope=instance_scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.daily_sync_smart_route_failed",
            error=str(exc),
        )
        return False

    if result is None:
        return False

    if update.message is not None:
        try:
            await update.message.reply_text(result.get("message", "ok."))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.bot.daily_sync_smart_route_ack_failed",
                error=str(exc),
            )
    log.info(
        "talker.bot.daily_sync_smart_route",
        confirmed=result.get("confirmed_count", 0),
        all_ok=result.get("all_ok", False),
        unparsed=len(result.get("unparsed", [])),
    )
    return True


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
    # Commands taking an argument — detected via _INLINE_CMD_WITH_ARG_RE
    # below. For these, the inline path forwards the full message text
    # to the handler so it can parse the arg via _parse_short_id_arg.
    "extract",
    "brief",
    # /speed parses its own args inside on_speed (via speed_pref.parse_speed_command)
    # so the inline path just forwards the full text verbatim.
    "speed",
}

# Require either start-of-message OR sentence-terminating punctuation
# (``.,!?;:``) plus whitespace before the slash. The earlier regex
# `(?:^|\s)/(\w+)\s*$` was too permissive: it fired on plain prose like
# ``Goodbye /end`` and ``the road came to a /end`` because ANY whitespace
# token before the slash satisfied it. Anchoring to punctuation+space
# keeps the legitimate forms (``Good. /end``, ``back to basics, /sonnet``,
# ``Note: /extract abc``) while rejecting bare-prose mid-sentence cases.
# End-of-line variant has ``\s*$`` so trailing whitespace is tolerated;
# start-of-message variant uses ``\b`` so ``/end something`` still matches
# as start-form. Mid-word tokens like ``foo/end`` still don't trigger
# because the punctuation/start-of-line anchor disallows them.
_INLINE_CMD_BOUNDARY = r"(?:^|[.!?;:,]\s+)"
_INLINE_CMD_RE = re.compile(rf"{_INLINE_CMD_BOUNDARY}/(\w+)\s*$|^/(\w+)\b")

# Commands that take an argument. Matched as ``/cmd <arg>`` at
# end-of-line (``Note: /extract abc123``) or at start-of-message
# (``/extract abc123 now``). ``extract`` / ``brief`` take a short-id
# bare word. ``speed`` takes a float (or the literal ``default``) plus
# an optional free-text note — the arg regex matches "rest of line"
# so on_speed can re-parse via speed_pref.parse_speed_command. The
# preceding-punctuation anchor here mirrors the no-arg regex above so
# bare-prose mid-sentence shapes like ``the file we want to /extract abc``
# don't false-positive.
_INLINE_CMD_WITH_ARG_RE = re.compile(
    rf"{_INLINE_CMD_BOUNDARY}/(extract|brief)\s+(\w+)\b"
    rf"|^/(extract|brief)\s+(\w+)\b"
    rf"|{_INLINE_CMD_BOUNDARY}/(speed)\s+(\S.*?)\s*$"
    rf"|^/(speed)\s+(\S.*?)\s*$"
)


def _detect_inline_command(text: str) -> str | None:
    """Return the lower-cased inline command name, or None.

    Only inspects the first line of ``text`` so a user typing a multi-line
    message where the second line happens to end in ``/end`` doesn't get
    their session closed silently — commands are line-local by convention.

    Also detects ``/extract <short-id>`` / ``/brief <short-id>`` embedded
    in prose — the with-arg matcher fires before the no-arg matcher so
    ``please /extract abc123`` routes to the extract handler (not ignored
    because the no-arg regex's ``\\s*$`` tail rejects the trailing arg).
    """
    if not text:
        return None
    first_line = text.splitlines()[0]
    # With-arg form has priority — it's the more specific pattern. The
    # regex has four alternations: end-of-line and start-of-message for
    # each of (extract|brief) and speed. Command name lands in group 1
    # (eol extract/brief), 3 (start extract/brief), 5 (eol speed), or 7
    # (start speed). Other groups are None. Coalesce to pick the non-None.
    match_arg = _INLINE_CMD_WITH_ARG_RE.search(first_line)
    if match_arg:
        cmd_arg = (
            match_arg.group(1)
            or match_arg.group(3)
            or match_arg.group(5)
            or match_arg.group(7)
            or ""
        ).lower()
        if cmd_arg in _INLINE_COMMANDS:
            return cmd_arg
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

    # Reply-context consumer: when the user long-pressed a bot message and
    # hit "Reply," Telegram attaches the full parent via ``reply_to_message``.
    # Build a machine-generated context prefix and prepend it to ``text``
    # BEFORE the inline-command check, router classifier, and Anthropic
    # turn — so every downstream path sees the reply attribution inline
    # with the user's actual words. Returns ``None`` for photo-only replies
    # (no usable quoted text) in which case we fall through silently.
    #
    # NOTE: the inline-command detector still runs against the ORIGINAL
    # text (no prefix) — a "/end" reply is still intended to close the
    # session; we don't want the reply prefix to accidentally block
    # inline-command detection by pushing the slash past the first line.
    # Pass the instance name so the prefix attribution matches the bot
    # that's actually replying ("Hypatia's earlier message" on the
    # Hypatia bot, "Salem's earlier message" on Salem).
    # ``InstanceConfig.name`` is required (no default) since 2026-04-26
    # — a successfully-loaded config always has a non-empty
    # ``config.instance.name``. The ``or canonical or "Alfred"``
    # fallback chain is dead code under the new contract; it stays as
    # belt-and-braces in case some future config-builder path bypasses
    # the dataclass (e.g. inline ``InstanceConfig.__init__`` with an
    # empty ``name=""`` literal in a test).
    instance_name = (
        config.instance.name or config.instance.canonical or "Alfred"
    )
    reply_prefix = _build_reply_context_prefix(
        update.message.reply_to_message,
        instance_name=instance_name,
    )
    has_reply_context = reply_prefix is not None
    effective_text = f"{reply_prefix}{text}" if reply_prefix else text

    # Daily Sync reply pre-check (email-surfacing c2): when the user is
    # replying to a recent Daily Sync message (matched by Telegram
    # message_id against the persisted batch), parse the terse reply
    # and append corpus rows. Returns ``None`` when the reply isn't a
    # Daily Sync match — fall through to the normal pipeline. Runs
    # BEFORE the inline-command check so a Daily Sync reply doesn't
    # accidentally trip a slash-command match in Andrew's free text
    # (e.g. "2: cancel /opus next time" should write to corpus, not
    # flip the model).
    parent_msg_id = _extract_reply_message_id(update.message.reply_to_message)
    if parent_msg_id is not None:
        handled = await _maybe_handle_daily_sync_reply(
            update, ctx, parent_msg_id, text,
        )
        if handled:
            return
    else:
        # Option B (Phase 2): smart-routing for the FIRST message
        # after a Daily Sync push that looks like a calibration
        # response, even when Andrew didn't use Telegram's
        # reply-to-message context. Heuristic + false-positive guard
        # live in ``daily_sync.reply_dispatch.maybe_smart_route_reply``.
        # Falls through silently when the message doesn't look like a
        # calibration reply or the latest batch was already replied to.
        handled = await _maybe_smart_route_daily_sync_reply(
            update, ctx, text,
        )
        if handled:
            return

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
                # Rehydrate-failure route-open: pass the effective (prefixed)
                # text so the router sees the reply-to-bot context as the
                # cue, and flag ``has_reply_context`` so the classifier
                # prefers continues/note over a fresh cue-driven session.
                sess = await _open_routed_session(
                    state_mgr, config, client, chat_id, effective_text,
                    has_reply_context=has_reply_context,
                )
        else:
            # No active session — the router runs. When this message is a
            # reply to a prior bot message we flag ``has_reply_context``
            # so the classifier tips toward continuation / note rather
            # than opening a fresh capture / journal / article session on
            # what is almost certainly a follow-up to existing context.
            sess = await _open_routed_session(
                state_mgr, config, client, chat_id, effective_text,
                has_reply_context=has_reply_context,
            )

        # Stage 3.5 peer-route flow. On the first message of a
        # peer_route session AND on every subsequent turn while
        # ``_peer_route_target`` stays stashed on the active dict, we
        # forward to the named peer and relay its reply. Auto-forward
        # stops when the user says ``/end`` (session close clears the
        # target) or starts a new opening cue after the gap timeout
        # fires.
        active_now = state_mgr.get_active(chat_id) or {}
        peer_target = active_now.get("_peer_route_target")
        if peer_target:
            handled = await _dispatch_peer_route(
                update, ctx,
                target=peer_target,
                # Forward with the reply-prefix so the peer has the same
                # context the user is holding in their head ("explain the
                # second failure" is meaningless without the prior
                # [KAL-LE] pytest output it's referencing).
                text=effective_text,
                chat_id=chat_id,
                originating_session_id=active_now.get("session_id", ""),
            )
            if handled:
                return  # Peer path completed (with reply or timeout).
            # Fall-through: peer was unreachable. Clear the target so
            # subsequent turns don't keep hitting a dead peer, and let
            # Salem handle the turn normally.
            active_now.pop("_peer_route_target", None)
            state_mgr.set_active(chat_id, active_now)
            state_mgr.save()

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
        active_session_type = active_for_turn.get("_session_type", "note")

        try:
            response_text = await conversation.run_turn(
                client=client,
                state=state_mgr,
                session=sess,
                # Use the prefix-augmented text so the model sees the
                # reply-to-bot context inline with the user's words.
                # Non-reply turns are identical to before (effective_text
                # == text when ``reply_prefix`` is None).
                user_message=effective_text,
                config=config,
                vault_context_str=vault_context_str,
                system_prompt=system_prompt,
                user_kind="voice" if voice else "text",
                calibration_str=calibration_str,
                pushback_level=pushback_level,
                session_type=active_session_type,
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

        # wk2b c2: capture-mode silent reply. ``run_turn`` returned the
        # capture sentinel instead of an assistant text — post a receipt-
        # ack emoji reaction via the Bot API's setMessageReaction endpoint
        # and return. If the reaction call fails (network, rate limit,
        # rare PTB shape drift), fall back to a minimal dot text reply so
        # the user still sees something.
        if response_text == conversation.CAPTURE_SENTINEL:
            try:
                await _post_capture_ack(update, ctx, chat_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "talker.bot.capture_ack_fallback",
                    chat_id=chat_id,
                    error=str(exc),
                )
                try:
                    await update.message.reply_text(".")
                except Exception as fallback_exc:  # noqa: BLE001
                    log.warning(
                        "talker.bot.capture_ack_fallback_failed",
                        chat_id=chat_id,
                        error=str(fallback_exc),
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
    "extract": on_extract,
    "brief": on_brief,
    "speed": on_speed,
})
