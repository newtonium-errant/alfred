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
import functools
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

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
    attachments,
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
    vision,
    voice_train,
)
from .config import TalkerConfig
from .session import (
    Session,
    append_document,
    append_image,
    append_outbound_failure,
)
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
# Voice-train multi-message paste buffer (Bug #58). dict[chat_id, PendingPaste].
# Lives on bot_data so on_train / on_method_source / on_text all see the same
# state. Single-event-loop concurrency means no lock needed.
_KEY_VOICE_TRAIN_BUFFERS = "voice_train_pending"
# Ticket #69 — voice_train pending-cluster registry. After a /train without
# --cluster successfully saves, we ask "which cluster?" and remember the
# saved record's relative path so the operator's next message can be
# applied as a cluster tag without them re-uploading.
# Shape: dict[chat_id, _PendingClusterAsk]. See ``_PendingClusterAsk``.
_KEY_VOICE_TRAIN_PENDING_CLUSTER = "voice_train_pending_cluster"
# Ticket #69 — how long the pending-cluster ask stays "live" after the
# bot's "Which cluster?" message. Past this, an unrelated message in the
# chat is NOT mistaken for a cluster reply. 5 minutes is generous for
# the operator to type a single word.
_VOICE_TRAIN_CLUSTER_ASK_TIMEOUT_SECONDS = 300
# Ticket #69 — cluster-name shape. Single token of letters/digits/
# hyphens/underscores; max 30 chars; no leading dash; no command prefix.
# Excludes whitespace + slash so essay sentences and slash commands
# can't masquerade as a cluster reply.
_CLUSTER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,29}$")
# The "skip clustering" sentinel — operator typed this to opt out of
# clustering for the current /train. Case-insensitive match.
_CLUSTER_OPT_OUT_SENTINEL = "general"


# --- Instance-name normalisation ------------------------------------------
#
# The router emits peer-route targets in canonical lowercase form
# (``kal-le``, ``stay-c``). The local instance name arrives from
# :class:`InstanceConfig` as either the casual form (``Salem``) or the
# canonical form (``S.A.L.E.M.``). To compare the two reliably we lowercase,
# strip dots, and map spaces to dashes — the same transform the transport
# layer uses for peer keys in ``config.transport.peers``.
#
# The actual normaliser lives in ``alfred.telegram._compat`` so this module
# and ``alfred.telegram.speed_pref`` share one definition (the two used to
# carry independent copies that risked silent divergence on the legacy
# ``alfred`` → ``salem`` mapping). Re-exported here under the original
# name so existing call sites and tests continue to work unchanged.

from ._compat import _normalize_instance_name  # noqa: E402, F401


# --- Application-level inbound pre-pass ----------------------------------
#
# The idle-tick heartbeat counter has TWO surfaces post 2026-06-06 c1:
#
# * ``heartbeat.record_inbound()`` — the **total** counter, called from
#   the application-level pre-pass below so EVERY inbound update
#   increments it (recognised commands, unrecognised commands, plain
#   text, voice notes, photos, documents, edited messages, callback
#   queries — anything PTB delivers).
#
# * ``heartbeat.record_handled()`` — the **handled** counter, called as
#   the first line of each entry handler (``on_text`` / ``on_voice`` /
#   ``on_photo`` / ``on_document``) AFTER the allowlist gate.
#   Increments when an inbound reaches a registered handler.
#
# Tick log emits ``inbound_in_window=T`` (total, legacy alias) plus
# ``inbound_handled=H`` and ``inbound_unhandled=U`` where ``U = T - H``.
# Silent-drops show as ``inbound_unhandled > 0``.
#
# Why the split (2026-06-06 incident)
# -----------------------------------
# Andrew sent a PDF to Hypatia at 13:57 ADT. PTB had no handler
# registered for ``filters.Document``, so the document update fell
# through all routing — but the pre-pass at group=-1 still incremented
# the (then-single) counter. The heartbeat reported
# ``inbound_in_window=1`` which was indistinguishable from a healthy
# short-of-quiet tick; the actual silent-drop was invisible. The
# handled/unhandled split surfaces this case explicitly: when an
# operator sees ``inbound_unhandled > 0``, they know messages came in
# but nothing routed them — register a handler for that update type
# (or check the allowlist).
#
# Earlier incident (2026-04-22)
# -----------------------------
# The original commit (5a26d13) called ``record_inbound`` from inside
# ``on_text`` and ``on_voice`` only. Unrecognised commands (e.g.
# ``/calibration`` when only ``/calibrate`` is registered) bypass both
# — PTB's ``CommandHandler`` matches the recognised ones first and the
# text MessageHandler is gated by ``~filters.COMMAND``, so the
# unknown-command update fell through every handler without ever
# bumping the counter. Result: a ``inbound_in_window=0`` heartbeat
# emitted while Telegram had clearly delivered the message — caught
# from a real ``/calibration`` typo. The fix moved the increment to a
# ``TypeHandler`` at group=-1 so it observes every Update before
# per-handler routing fires. Same asyncio loop = no thread safety
# required; the pre-pass returns normally so subsequent groups still
# match exactly as before (no ``ApplicationHandlerStop``).
#
# PTB mechanism: ``TypeHandler(Update, …)`` matches every update and
# group=-1 puts it ahead of the default group (0) where the real
# handlers live. PTB only fires one handler per group, so registering
# this in its own dedicated negative group keeps it off the routing
# critical path.


async def _pre_record_inbound(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Pre-pass: bump the TOTAL heartbeat counter for every inbound update.

    Registered at group=-1 via :class:`TypeHandler` so it fires before
    any per-handler routing. Returns normally (does NOT raise
    :class:`ApplicationHandlerStop`) so the rest of the handler chain
    runs unchanged. Wraps the increment in a try/except so a counter
    bug can never break message delivery.

    The **handled** counter is bumped separately by each entry handler
    via :func:`heartbeat.record_handled` after the allowlist gate. See
    the block comment above and the on_text / on_voice / on_photo /
    on_document handlers for the split rationale.
    """
    try:
        heartbeat.record_inbound()
    except Exception:  # noqa: BLE001
        log.exception("talker.bot.record_inbound_failed")


# --- System-prompt shape normalisation -----------------------------------
#
# ``bot_data[_KEY_SYSTEM]`` may carry EITHER a zero-arg provider callable
# (the production path; the daemon passes
# ``build_system_prompt_provider(skills_dir, config)`` so SKILL.md edits
# on disk take effect on the next turn) OR a plain string (legacy +
# direct-test-fixture path; many tests bypass ``build_app`` entirely
# and write the static text into ``bot_data`` themselves). Both shapes
# are valid inputs to ``build_app`` per its docstring contract; the
# read site mirrors that contract via this helper so test fixtures
# don't need to wrap their static prompts in lambdas.
#
# Pre-2026-05-09: the read site at ``update_handler`` assumed the
# ``build_app`` wrap was always in place, calling
# ``ctx.application.bot_data[_KEY_SYSTEM]()`` unconditionally. Tests that
# wrote a string directly into ``bot_data`` exploded with
# ``TypeError: 'str' object is not callable`` on 32 different tests.
# Centralising the normalisation here closes that drift class without
# requiring fixture sweeps across 14 test files.


def _resolve_system_prompt(provider_or_text: Callable[[], str] | str) -> str:
    """Resolve ``bot_data[_KEY_SYSTEM]`` to a static string for one turn.

    Accepts the same shapes as ``build_app``'s ``system_prompt_provider``
    parameter — zero-arg callable or plain string. A callable is invoked
    fresh per call (so the daemon's hot-reload provider re-reads SKILL.md
    on each turn without a restart); a string is returned as-is.

    Anything else is coerced via ``str()`` to keep the contract
    fail-loud-not-crash — operators who stash an unexpected shape get a
    string back rather than a runtime exception inside the message
    handler. The defensive ``str()`` mirrors the legacy ``str(...)``
    coercion already in ``build_app`` for the non-callable branch.
    """
    if callable(provider_or_text):
        return provider_or_text()
    return str(provider_or_text)


# --- Application assembly -------------------------------------------------


def build_app(
    config: TalkerConfig,
    state_mgr: StateManager,
    anthropic_client: Any,
    system_prompt_provider: Callable[[], str] | str | None = None,
    vault_context_str: str = "",
    raw_config: dict | None = None,
    *,
    system_prompt: str | None = None,
) -> Application:
    """Build a PTB :class:`Application` wired with handlers and bot_data.

    Callers add their own post-init hooks (gap sweeper, signal handlers) via
    :mod:`daemon`. This function only does handler registration.

    System-prompt input — TWO accepted shapes:

    - ``system_prompt_provider`` — the canonical kwarg. Accepts EITHER a
      zero-arg callable (production path; the daemon passes
      ``build_system_prompt_provider(skills_dir, config)`` so SKILL.md
      is read fresh per turn — i.e. every inbound Telegram message
      routed through ``handle_message`` — closing the same-cycle SKILL
      ship gap from QA 2026-05-04) OR a static string (legacy path for
      tests + ad-hoc callers that don't need hot-reload). When a string
      is passed, it's wrapped in a constant-returning lambda so the
      handler's call shape stays uniform.

    - ``system_prompt`` — keyword-only legacy compat alias (issue #49 /
      Batch A 2026-05-09). 13 telegram-suite tests called
      ``build_app(... system_prompt="", ...)`` from a pre-rename world;
      rather than sweep 13 fixtures, accept the legacy kwarg here and
      normalise it through the same provider-wrapping path. Mirrors the
      defensive-accept-both shape already used by ``_resolve_system_prompt``
      at the read site (handle_message line ~3884) — symmetry at the
      write site.

    Exactly one of the two must be supplied (passing both raises
    :class:`ValueError`). Passing neither falls back to an empty static
    prompt — useful for handler-introspection tests that build an app
    purely to assert command-registration shape and never actually run
    the LLM turn.

    ``raw_config`` — Stage 3.5 addition. The peer-route dispatcher
    needs the full unified config dict to build a TransportConfig
    at forward time (peer URLs + tokens live under ``transport.peers``).
    ``None`` disables peer routing cleanly.
    """
    if system_prompt is not None and system_prompt_provider is not None:
        raise ValueError(
            "build_app: pass either ``system_prompt_provider`` OR "
            "``system_prompt``, not both. The two are aliases for the "
            "same input slot — passing both leaves the precedence "
            "ambiguous. Use ``system_prompt_provider`` for the canonical "
            "kwarg (callable or string); ``system_prompt`` is a legacy "
            "string-only alias retained for fixture compat."
        )
    # Coalesce the two kwargs into one provider input. Empty default
    # ("") rather than skipping the wrap so the bot_data slot always
    # holds a callable — every read site can call it without a
    # type-check (per the historical wrap contract). The read site
    # ALSO accepts non-callable shapes via _resolve_system_prompt, so
    # this default is belt-and-braces, not load-bearing on the read
    # path.
    provider_input: Callable[[], str] | str
    if system_prompt is not None:
        provider_input = system_prompt
    elif system_prompt_provider is not None:
        provider_input = system_prompt_provider
    else:
        provider_input = ""

    app = Application.builder().token(config.bot_token).build()

    # Normalize the provider: callers may pass a callable (the daemon
    # path, hot-reload-enabled) or a plain string (tests, legacy
    # callers that don't need hot-reload). Wrap the string case in a
    # constant-returning lambda so the read site at the message
    # handler always invokes a callable.
    if callable(provider_input):
        provider: Callable[[], str] = provider_input
    else:
        _static_prompt = str(provider_input)
        def provider() -> str:  # noqa: E306
            return _static_prompt

    app.bot_data[_KEY_CONFIG] = config
    app.bot_data[_KEY_STATE] = state_mgr
    app.bot_data[_KEY_CLIENT] = anthropic_client
    app.bot_data[_KEY_SYSTEM] = provider
    app.bot_data[_KEY_VAULT_CTX] = vault_context_str
    app.bot_data[_KEY_LOCKS] = {}
    # Bug #58 buffer registry — opened by /train + /method_source,
    # appended to by on_text, drained by the debounce flush callback.
    app.bot_data[_KEY_VOICE_TRAIN_BUFFERS] = {}
    # Ticket #69 pending-cluster registry — populated by
    # _finalize_train_paste when /train was issued without --cluster,
    # consumed by on_text when the operator's next message looks like
    # a cluster name.
    app.bot_data[_KEY_VOICE_TRAIN_PENDING_CLUSTER] = {}
    app.bot_data["raw_config"] = raw_config

    # Application-level pre-pass: every inbound update bumps the
    # heartbeat counter, regardless of which handler eventually routes
    # it (or whether nothing routes it, e.g. an unrecognised command).
    # See the comment block above ``_pre_record_inbound`` for the
    # coverage-gap incident this guards against.
    app.add_handler(TypeHandler(Update, _pre_record_inbound), group=-1)

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("end", on_end))
    # Phase 1.x (2026-05-16): /end-zettel and /end-note variants force
    # the extraction target type for the just-closing capture session.
    # PTB only allows [a-z0-9_] in command names, so the canonical
    # registrations use underscores (mirrors the /method_source decision
    # at line ~371). Operators typing the dash form get the legacy
    # unknown-command behaviour; the underscore form works.
    app.add_handler(CommandHandler("end_zettel", on_end_zettel))
    app.add_handler(CommandHandler("end_note", on_end_note))
    # Queue #10 (2026-05-18): /recap — mid-session summary on an OPEN
    # capture session. Read-only; does not close the session. Default
    # mode is brief (2 buckets); ``/recap verbose`` gives the full
    # 6-bucket structured summary.
    app.add_handler(CommandHandler("recap", on_recap))
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
    # Hypatia Phase 2.5: /fiction <title> conditionally registered.
    # Only fires the registration when the operator explicitly opts in
    # via ``telegram.fiction.command_enabled: true`` in their instance
    # config. Salem (and any other operational-vault instance) leaves
    # the block out → command not registered → Telegram's "unknown
    # command" handles user error. Per the Phase 2.5 contract: the
    # natural-language path (Hypatia's SKILL detecting "let's start a
    # fiction project called X") and this slash command produce
    # identical on-disk shapes — see
    # :func:`alfred.telegram.fiction.scaffold_fiction_project`.
    if config.fiction is not None and config.fiction.command_enabled:
        app.add_handler(CommandHandler("fiction", on_fiction))
        log.info("talker.bot.fiction_command_registered")

    # 2026-05-07 voice_train arc: /train + /method-source — Hypatia-only
    # in Phase 1, gated by the same shape as fiction. PTB rejects ``-``
    # in command names (must be ``[a-z0-9_]``) so ``/method-source`` is
    # registered as ``method_source`` at the bot layer; users typing
    # ``/method-source`` get the legacy unknown-command behaviour while
    # ``/method_source`` works. The user-facing spec calls it
    # ``/method-source`` for naming consistency with the directory; we
    # accept BOTH spellings via the inline-text path. The slash-command
    # registration uses the underscore form because PTB requires it.
    if (
        config.voice_train is not None
        and config.voice_train.command_enabled
    ):
        app.add_handler(CommandHandler("train", on_train))
        app.add_handler(CommandHandler("method_source", on_method_source))
        log.info("talker.bot.voice_train_commands_registered")

    # Phase 4 Sub-arc C (2026-05-18): /questions + /research-pointers
    # — grouped-by-MOC views over question/ and research-pointer/
    # records with the same predicates that drive Sub-arc B's
    # inventory MOCs. Hypatia-only via the ``inventory_views`` config
    # gate; Salem + KAL-LE don't have these record types so the gate
    # matches the data shape. PTB requires ``[a-z0-9_]`` so
    # ``/research-pointers`` registers as ``research_pointers`` — the
    # dash form falls through to Telegram's unknown-command behaviour
    # on instances that don't have the underscore form registered.
    if (
        config.inventory_views is not None
        and config.inventory_views.command_enabled
    ):
        app.add_handler(CommandHandler("questions", on_questions))
        app.add_handler(
            CommandHandler("research_pointers", on_research_pointers),
        )
        log.info("talker.bot.inventory_views_commands_registered")

    # Tier Phase 2A (2026-05-28): /today — Salem-only glance-view
    # mini-brief composing tier + routines + upcoming-events sections
    # as one Telegram reply. Salem opts in via
    # ``telegram.today_command.enabled: true``; KAL-LE / Hypatia leave
    # the block absent and Telegram's unknown-command behaviour fires.
    if (
        config.today_command is not None
        and config.today_command.enabled
    ):
        app.add_handler(CommandHandler("today", on_today))
        log.info("talker.bot.today_command_registered")

    # Phase 5 Sub-arc D2 (2026-05-19): /moc-suggestions + /accept-moc +
    # /reject-moc — view + act on the cluster→MOC suggestion queue
    # written by surveyor's Stage 8 (D1 ship). PTB requires
    # ``[a-z0-9_]`` so the dash forms register as underscore aliases;
    # the dash form falls through to Telegram's unknown-command
    # behaviour on instances that don't have the underscore form
    # registered. Hypatia-only via the ``moc_suggestions`` config
    # gate; Salem + KAL-LE leave the block absent.
    if (
        config.moc_suggestions is not None
        and config.moc_suggestions.command_enabled
    ):
        app.add_handler(
            CommandHandler("moc_suggestions", on_moc_suggestions),
        )
        app.add_handler(CommandHandler("accept_moc", on_accept_moc))
        app.add_handler(CommandHandler("reject_moc", on_reject_moc))
        log.info("talker.bot.moc_suggestions_commands_registered")

    # Email-surfacing c2: Daily Sync slash commands. /calibrate fires
    # an out-of-cycle Daily Sync sample; /calibration_ok flips per-tier
    # confidence flags read by the (future) c3/c4/c5 surfacing layers.
    app.add_handler(CommandHandler("calibrate", on_calibrate))
    app.add_handler(CommandHandler("calibration_ok", on_calibration_ok))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    # Vision phase 2: photo handler. ``filters.PHOTO`` matches Telegram
    # ``photo`` updates (multi-resolution screenshots / camera-roll
    # uploads). Forwarded documents (any image attached as a file rather
    # than a Telegram-compressed photo) take a separate ``filters.Document
    # .IMAGE`` path which is out-of-scope for this commit — Andrew's
    # screenshot-share workflow goes through the photo path. Per-instance
    # ``vision.enabled=false`` short-circuits with a user-facing reply.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    # 2026-06-06 c1: document handler — PDFs and any other ``document``
    # update Telegram delivers. ``filters.Document.ALL`` matches every
    # document; the MIME allowlist check lives inside ``on_document``
    # so non-PDFs get an EXPLICIT user-facing reply ("I can only read
    # PDFs right now — got <mime>") rather than a silent filter-drop.
    # Per ``feedback_intentionally_left_blank.md``: silent absence and
    # explicit-replied absence are operationally different — the
    # silent-PDF-drop incident this commit closes was the
    # ``filters.PHOTO``-only registration treating every document as
    # "not for me," which collapsed to "Telegram delivered it but the
    # bot never replied." Registering ``filters.Document.ALL`` here
    # plus the in-handler allowlist guarantees every document update
    # produces SOME visible outcome — extracted-text-and-replied for
    # PDFs, "I can only read PDFs" for everything else.
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

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


# --- Outbound transport (chunking + failure surfacing) --------------------
#
# Telegram's per-message limit is 4096 characters. Before this helper, the
# bot called ``reply_text(response_text)`` once and logged the resulting
# error on a 400. The user saw nothing — no Telegram delivery, no text in
# the chat — and the session record was written as if the turn had been
# delivered. The 2026-04-28 Hypatia incident (4852-char reply, 73 minutes of
# user-side silence) was the trigger for this fix.
#
# Three layers:
#   L1 — chunk above 3900 chars at paragraph / sentence / hard boundaries
#        (delegated to ``alfred.transport.utils.chunk_for_telegram``).
#   L2 — when any chunk fails to send (rare for length post-chunking; common
#        for rate-limit / network), push a short user-visible alert that
#        DOES fit. If the alert itself fails, log and give up.
#   L3 — annotate the active session with an ``outbound_failures`` entry so
#        the eventual session-record frontmatter carries the failure
#        context. Field is omitted from frontmatter when no failures
#        occurred — existing-shape consumers are unaffected.

# Telegram per-message hard cap is 4096; 3900 leaves 196 chars of headroom
# for MarkdownV2 escape overhead and rendering edge cases.
_OUTBOUND_CHUNK_LIMIT = 3900


def _format_outbound_alert(
    session: Session,
    error: str,
) -> str:
    """Compose the short, length-safe failure-alert message.

    Phrasing matches the silent-drop ticket exactly. The full session
    record path is unknown at outbound time (it's built at close), so we
    point at the stable session id — the user can grep the vault for it
    after session close. The alert is constructed to stay well under the
    1000-char self-imposed cap (no chunking risk, no recursion).
    """
    short_id = (session.session_id or "").split("-")[0] or "unknown"
    # Truncate the error so a verbose traceback can never push the alert
    # past the cap. 200 chars is plenty for "Message is too long",
    # "Too Many Requests: retry after 30", etc.
    short_error = (str(error) or "unknown error")[:200]
    return (
        "⚠️ Reply failed to deliver via Telegram. "
        f"Full text saved in vault: `session/{short_id}` "
        f"— error: `{short_error}`"
    )


async def _send_outbound_chunked(
    *,
    update: Update,
    state_mgr: StateManager,
    session: Session,
    chat_id: int,
    response_text: str,
) -> None:
    """Send ``response_text`` to Telegram, chunking and surfacing failures.

    Always splits at the configured threshold (paragraph → sentence →
    hard-wrap) before any send call, then dispatches each chunk
    sequentially. Any failed chunk aborts the loop and triggers the
    user-visible alert (Layer 2) and the session annotation (Layer 3).
    Successful single-chunk sends preserve the wk1 ``ok=True`` log shape;
    multi-chunk sends emit ``talker.bot.outbound_chunked`` summarising
    chunk count.
    """
    from alfred.transport.utils import chunk_for_telegram

    total_length = len(response_text)
    chunks = chunk_for_telegram(response_text, max_chars=_OUTBOUND_CHUNK_LIMIT)
    chunks_attempted = len(chunks)

    chunks_sent = 0
    failure_error: str | None = None
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk)
            chunks_sent += 1
        except Exception as exc:  # noqa: BLE001
            failure_error = str(exc) or exc.__class__.__name__
            log.warning(
                "talker.bot.outbound",
                chat_id=chat_id,
                length=total_length,
                chunks_attempted=chunks_attempted,
                chunks_sent=chunks_sent,
                ok=False,
                error=failure_error,
            )
            break

    if failure_error is None:
        # All chunks delivered. Single-chunk path retains the wk1 log
        # shape (``length`` + ``ok=True``); multi-chunk emits the new
        # summary log key so log scrapers can distinguish "had to chunk"
        # from "fit in one send".
        if chunks_attempted == 1:
            log.info(
                "talker.bot.outbound",
                chat_id=chat_id,
                length=total_length,
                ok=True,
            )
        else:
            log.info(
                "talker.bot.outbound_chunked",
                chat_id=chat_id,
                length=total_length,
                chunks_attempted=chunks_attempted,
                chunks_sent=chunks_sent,
                ok=True,
            )
        return

    # Layer 3: annotate the session BEFORE attempting the user alert so
    # the failure record survives even if the alert send also crashes.
    # ``len(session.transcript) - 1`` is the just-appended assistant turn
    # whose text we tried to deliver.
    turn_index = max(0, len(session.transcript) - 1)
    try:
        append_outbound_failure(
            state_mgr,
            session,
            turn_index=turn_index,
            error=failure_error,
            length=total_length,
            chunks_attempted=chunks_attempted,
            chunks_sent=chunks_sent,
        )
    except Exception as exc:  # noqa: BLE001
        # Don't let a state-write failure mask the original outbound
        # problem — log loudly and continue to the alert path.
        log.warning(
            "talker.bot.outbound_state_annotation_failed",
            chat_id=chat_id,
            error=str(exc),
        )

    # Layer 2: user-visible alert. Capped at 1 attempt — if the alert
    # itself fails (pathological rate-limit or network outage) we log and
    # give up rather than recurse.
    alert = _format_outbound_alert(session, failure_error)
    try:
        await update.message.reply_text(alert)
    except Exception as alert_exc:  # noqa: BLE001
        log.warning(
            "talker.bot.outbound_alert_failed",
            chat_id=chat_id,
            error=str(alert_exc),
            original_error=failure_error,
        )


# --- Allowlist helper -----------------------------------------------------


def _entry_id(entry: Any) -> int | None:
    """Return the user id of an allowlist entry, tolerating both shapes.

    VERA MVP (2026-06-09) — ``config.allowed_users`` is normally
    ``list[AllowedUser]`` (the loader coerces all YAML into that), but
    test fixtures and other direct ``TalkerConfig(...)`` constructors
    sometimes pass bare ints (the pre-VERA shape). Accept either: an
    ``AllowedUser`` (read ``.id``), or a bare int. Returns ``None`` for
    anything else (bool / malformed), which the caller skips.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    eid = getattr(entry, "id", None)
    return eid if isinstance(eid, int) and not isinstance(eid, bool) else None


def _entry_role(entry: Any) -> str:
    """Return the role of an allowlist entry, tolerating both shapes.

    ``AllowedUser`` → its ``.role``; bare int (legacy / direct-construct
    fixtures) → ``"owner"`` (back-compat default). See :func:`_entry_id`.
    """
    role = getattr(entry, "role", None)
    return role if isinstance(role, str) and role else "owner"


def _entry_name(entry: Any) -> str | None:
    """Return the display name of an allowlist entry, or None.

    VERA reporter follow-up (2026-06-09). ``AllowedUser`` → its ``.name``
    (``None`` when unset); bare int (legacy / direct-construct fixtures)
    → ``None``. See :func:`_entry_id`.
    """
    name = getattr(entry, "name", None)
    return name if isinstance(name, str) and name else None


def _is_allowed(update: Update, config: TalkerConfig) -> bool:
    """Return True iff the message's user_id is in config.allowed_users.

    ``config.allowed_users`` is ``list[AllowedUser]`` (VERA MVP, 2026-06-09).
    Flat-list instances (Salem / KAL-LE / Hypatia) normalize their bare-int
    YAML to ``AllowedUser(id, "owner")`` at load time, so the id-set match
    here is byte-equivalent to the pre-VERA ``user.id in allowed`` check.
    ``_entry_id`` tolerates direct-construct fixtures that still pass bare
    ints in the field.
    """
    user = update.effective_user
    if user is None:
        return False
    allowed = config.allowed_users or []
    ids = {eid for eid in (_entry_id(e) for e in allowed) if eid is not None}
    return user.id in ids


def _role_for(update: Update, config: TalkerConfig) -> str:
    """Return the sending user's role (``"owner"`` / ``"ops"`` / ...).

    VERA MVP (2026-06-09). Looks up the message's user_id in the
    role-bearing allowlist and returns its role. Defaults to ``"owner"``
    when the user is absent OR allowed-but-unmatched — back-compat: a
    flat-list instance whose entries all normalize to ``"owner"`` returns
    ``"owner"`` for every allowed user, exactly the pre-VERA behaviour
    (full owner powers). Callers should still gate on :func:`_is_allowed`
    first; this only resolves the role of an already-allowed user.

    Tolerates both ``AllowedUser`` and bare-int entries via
    :func:`_entry_id` / :func:`_entry_role`.
    """
    user = update.effective_user
    if user is None:
        return "owner"
    for entry in config.allowed_users or []:
        if _entry_id(entry) == user.id:
            return _entry_role(entry)
    return "owner"


def _name_for(update: Update, config: TalkerConfig) -> str | None:
    """Return the sending user's configured display name, or None.

    VERA reporter follow-up (2026-06-09). Looks up the message's user_id
    in the allowlist and returns the entry's ``name`` (``AllowedUser.name``).
    Returns ``None`` when the user is absent, unmatched, or matched but
    nameless — every single-user / flat-list instance returns ``None`` for
    every user (no name configured), so the downstream sender-identity
    injection is inert there. The talker turn falls back to the role label
    when this is ``None`` (see the sender-identity block in
    ``conversation.run_turn``).

    Tolerates both ``AllowedUser`` and bare-int entries via
    :func:`_entry_id` / :func:`_entry_name`.
    """
    user = update.effective_user
    if user is None:
        return None
    for entry in config.allowed_users or []:
        if _entry_id(entry) == user.id:
            return _entry_name(entry)
    return None


def _require_owner(update: Update, config: TalkerConfig, command: str) -> bool:
    """Return True iff the sending user has the ``owner`` role.

    VERA MVP (2026-06-09) — the command-layer half of the two-layer
    "Ben can't recode the instance" guarantee. This is an ADDITIVE gate:
    owner-only command handlers call it AFTER the existing
    :func:`_is_allowed` check. On deny it logs ``talker.bot.role_denied``
    (mirroring the silent-drop shape of the ``talker.bot.unauthorized``
    path) and returns False so the handler returns without acting.

    Non-owner-gated commands are unaffected. On every single-role instance
    (Salem / KAL-LE / Hypatia) every allowed user is ``owner``, so this
    always returns True there — no behaviour change.
    """
    role = _role_for(update, config)
    if role == "owner":
        return True
    log.info(
        "talker.bot.role_denied",
        user_id=update.effective_user.id if update.effective_user else None,
        command=command,
        role=role,
    )
    return False


def owner_only(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator — gate a command handler to the ``owner`` role.

    VERA MVP (2026-06-09) — applied to owner-only command handlers
    (calibration, model/session control, brief/today/status). Wraps the
    ``(update, ctx)`` handler: reads config off ``ctx.application.bot_data``,
    runs :func:`_require_owner`, and on deny returns WITHOUT invoking the
    wrapped handler (silent-drop shape — the ops user simply gets no
    response, matching the unauthorized-path UX).

    Applied at the def site so it covers BOTH dispatch paths in one place:
    the PTB ``CommandHandler`` registration AND the inline ``_dispatch_
    inline_command`` map (both reference the same ``on_*`` symbol).

    Inert on single-role instances — every allowed user there is
    ``owner``, so the wrapper always falls through to the real handler.

    ``_is_allowed`` is NOT re-checked here: every gated handler retains
    its own ``_is_allowed`` check as the first fence, and this decorator
    adds the role fence on top. An unauthorized non-allowlisted user is
    dropped by the handler's own ``_is_allowed`` exactly as before; an
    allowed-but-ops user is dropped by ``_require_owner`` here.
    """

    @functools.wraps(handler)
    async def _wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
        command = getattr(handler, "__name__", "owner_only_command")
        if not _require_owner(update, config, command):
            return
        await handler(update, ctx)

    return _wrapped


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


@owner_only
async def on_recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/recap — mid-session structured summary on an OPEN capture session.

    Queue #10 (2026-05-18). Read-only: does NOT close the session, does
    NOT create vault records, does NOT mutate session state. The
    session stays open; operator continues capturing after seeing the
    recap.

    Argument parsing (via PTB ``context.args``):
      * ``/recap``         → brief (default; 2 buckets — topics +
                             key_insights)
      * ``/recap brief``   → explicit brief
      * ``/recap verbose`` → full 6-bucket structured summary (same
                             extraction as ``/end`` produces, minus
                             the re-encounter section — that's a
                             post-close vault scan)
      * Anything else      → help reply (no state change)

    Capture-session gate:
      * No active session → "(no active capture session — start with
                            /capture first)" reply
      * Active session but ``_session_type != "capture"`` → same
                            no-active-capture message (the operator
                            is in a regular chat session, not a
                            capture monologue)

    Failure path: LLM call failure is swallowed inside
    :func:`alfred.telegram.capture_extract.summarize_capture_session_so_far`
    — it returns an error-message markdown rather than raising. The
    handler renders that directly. Operator sees an error in chat,
    NEVER a broken bot.
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
        await update.message.reply_text(
            "(no active capture session — start with /capture first)"
        )
        return
    session_type = active.get("_session_type", "note")
    if session_type != "capture":
        # Active session exists but it's a regular chat session, not
        # a capture monologue. Recap is capture-mode-specific.
        await update.message.reply_text(
            "(no active capture session — /recap works on capture "
            "sessions. Start one with /capture, then mid-session "
            "/recap shows what's been said so far.)"
        )
        return

    # Argument parsing. PTB drops the leading slash + command word
    # and gives us the remainder as ``ctx.args`` (split on whitespace).
    # ``/recap`` → [], ``/recap brief`` → ["brief"], etc.
    args = list(ctx.args) if ctx.args else []
    if len(args) == 0:
        mode = capture_extract.RECAP_MODE_BRIEF
    elif len(args) == 1 and args[0].lower() in (
        capture_extract.RECAP_MODE_BRIEF,
        capture_extract.RECAP_MODE_VERBOSE,
    ):
        mode = args[0].lower()
    else:
        # Garbage args → help reply. Don't fire an LLM call on
        # ambiguous intent.
        await update.message.reply_text(
            "usage: /recap (brief, default) | /recap brief | "
            "/recap verbose"
        )
        return

    transcript = list(active.get("transcript") or [])
    model = config.anthropic.model or "claude-sonnet-4-6"
    log.info(
        "talker.recap.invoked",
        chat_id=chat_id,
        mode=mode,
        transcript_turns=len(transcript),
    )

    # summarize_capture_session_so_far is failure-isolated — it
    # returns an error markdown rather than raising. We forward the
    # markdown directly to the operator. No try/except needed here.
    md = await capture_extract.summarize_capture_session_so_far(
        client=client,
        transcript=transcript,
        model=model,
        mode=mode,
    )
    await update.message.reply_text(md)

    # Read-only contract: confirm we didn't mutate the active session.
    # (No state.save() call; no active dict mutations above.)
    log.info(
        "talker.recap.done",
        chat_id=chat_id,
        mode=mode,
        reply_chars=len(md),
    )


async def _on_inventory_view(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    record_type: str,
    command_name: str,
) -> None:
    """Shared handler body for ``/questions`` and ``/research-pointers``.

    Phase 4 Sub-arc C (2026-05-18). Read-only — calls
    :func:`alfred.telegram.inventory_views.collect_records` (vault
    scan, no writes) then renders the result as a grouped-by-MOC
    Markdown reply.

    Cross-instance scope: this handler is only registered when
    ``telegram.inventory_views.command_enabled: true`` in the
    instance config. Salem + KAL-LE don't have ``question/`` or
    ``research-pointer/`` records, so the registration gate matches
    the data shape. Defensive fallback if the gate is misconfigured:
    the empty-state message renders the same way regardless of
    whether the underlying directories are missing or the records
    just don't match the predicate.

    Failure isolation: any unexpected exception in the collection /
    rendering path logs and replies with a generic error rather
    than crashing the handler. The vault is canonical; the slash
    command is a glance-view.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command=command_name,
        )
        return
    if update.message is None:
        return

    # Per-group cap from config (default 20). Operator-tunable.
    per_group_cap = 20
    if (
        config.inventory_views is not None
        and config.inventory_views.per_group_cap
    ):
        per_group_cap = config.inventory_views.per_group_cap

    try:
        from . import inventory_views as _iv
        records = _iv.collect_records(
            Path(config.vault.path), record_type,
        )
        reply = _iv.render_inventory(
            record_type, records, per_group_cap=per_group_cap,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.inventory_view_failed",
            command=command_name,
            record_type=record_type,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Could not load {record_type}s ({type(exc).__name__})"
        )
        return

    # Telegram has a ~4096 char body limit. Cap defensively at 4000
    # chars so we never trigger the per-message rejection; if the
    # rendering overflows, append a truncation note rather than
    # silently chopping (per intentionally_left_blank discipline).
    truncated = False
    if len(reply) > 4000:
        reply = reply[:3950].rstrip() + "\n\n…(truncated; bump per_group_cap or see MOC/_*.md)"
        truncated = True

    await update.message.reply_text(reply)

    log.info(
        "talker.bot.inventory_view_done",
        command=command_name,
        record_type=record_type,
        record_count=len(records),
        reply_chars=len(reply),
        truncated=truncated,
    )


async def on_questions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/questions — grouped-by-MOC list of question/ records with
    ``status in {open, refined}``.

    Read-only. Mirrors the data surfaced by
    ``MOC/_Open Questions.md`` (Sub-arc B inventory MOC) but
    grouped by topic-MOC membership rather than flat.

    Hypatia-only via the ``inventory_views`` config gate.
    """
    await _on_inventory_view(
        update, ctx,
        record_type="question",
        command_name="/questions",
    )


async def on_research_pointers(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/research-pointers — grouped-by-MOC list of research-pointer/
    records with ``status == open``.

    PTB only allows ``[a-z0-9_]`` in command names so the
    registration is ``research_pointers`` (underscore form). Users
    typing ``/research-pointers`` (dash form) get the legacy
    unknown-command behaviour; ``/research_pointers`` works. The
    docstring + reply messaging use the dash form for naming
    consistency with the directory.

    Read-only. Mirrors the data surfaced by
    ``MOC/_Open Research Pointers.md`` (Sub-arc B inventory MOC).

    Hypatia-only via the ``inventory_views`` config gate.
    """
    await _on_inventory_view(
        update, ctx,
        record_type="research-pointer",
        command_name="/research-pointers",
    )


# ---------------------------------------------------------------------------
# Tier Phase 2A (2026-05-28): /today — Salem-only glance-view mini-brief
# ---------------------------------------------------------------------------
#
# Composes the brief's tier + routines + upcoming-events sections as a
# single Telegram reply. Read-only — no vault writes, no session record.
# Salem-only via the ``today_command`` config gate; KAL-LE / Hypatia
# leave the block absent and Telegram's unknown-command behaviour fires.


@owner_only
async def on_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/today`` — glance-view mini-brief (tier + routines + upcoming).

    Salem-only Phase 2A (Tier system 2026-05-28). Composes the same
    three sections the morning brief surfaces at the top, re-rendered
    live from current vault state. The operator types ``/today`` from
    their phone mid-afternoon and sees what's still on the list,
    what routines are due, and what's coming up.

    Read-only: does NOT write to the vault, does NOT mutate session
    state, does NOT create or edit records. Matches the
    glance-view-as-separate-surface rationale in
    ``inventory_views.py:8-25``.

    Hypatia / KAL-LE leave the ``today_command`` config block absent —
    the registration gate at :func:`build_app` skips this handler
    entirely, so ``/today`` on a non-Salem instance falls through to
    Telegram's "unknown command" behaviour.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command="/today",
        )
        return
    if update.message is None:
        return

    # Defensive: handler may be invoked directly (e.g. from tests or a
    # future routing layer) without the config gate firing in
    # build_app. Re-check here so the handler stays correct in
    # isolation and the gate's "registration absent" semantics still
    # hold when the call shape changes.
    if config.today_command is None or not config.today_command.enabled:
        log.info(
            "talker.bot.today_command_not_configured",
            user_id=update.effective_user.id if update.effective_user else None,
        )
        return

    # Compose the mini-brief. ``now`` is resolved in the instance's
    # configured timezone (defaults to Salem's America/Halifax). The
    # tier section uses the full datetime instant for deadline-distance
    # math; routines + upcoming events use the date.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.today_command.timezone)
    now_local = datetime.now(tz)

    try:
        from . import today_command as _tc
        reply = _tc.compose_today_reply(
            Path(config.vault.path),
            now_local,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.today_command_failed",
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Could not compose /today ({type(exc).__name__})"
        )
        return

    await update.message.reply_text(reply)

    log.info(
        "talker.bot.today_command_done",
        reply_chars=len(reply),
        date=now_local.date().isoformat(),
    )


# ---------------------------------------------------------------------------
# Phase 5 Sub-arc D2 (2026-05-19): /moc-suggestions + /accept-moc +
# /reject-moc — view + act on the MOC suggestion queue.
# ---------------------------------------------------------------------------
#
# Three commands consume one JSONL queue. ``/moc-suggestions`` is
# read-only (lists pending grouped by target). ``/accept-moc <id>`` is
# the write surface: edits each candidate member's ``mocs:`` frontmatter
# to append the target MOC, triggering Phase 4 Sub-arc A's existing
# hook → MOC's ``# Contents`` gets a wikilink. ``/reject-moc <id>``
# flips status to ``rejected`` (no vault write; negative-learning
# persists per ratified Q5).
#
# PTB only allows ``[a-z0-9_]`` in command names, so ``/accept-moc`` +
# ``/reject-moc`` register as ``accept_moc`` + ``reject_moc`` (underscore
# form). The docstring + reply messaging use the dash form for
# operator readability.
#
# All three are gated on ``telegram.moc_suggestions.command_enabled:
# true``. Hypatia opts in; Salem + KAL-LE leave the block absent so
# Telegram's unknown-command behaviour fires.


def _resolve_moc_suggestions_queue_path(
    config: TalkerConfig,
) -> str | None:
    """Resolve the configured queue path.

    Reads ``config.moc_suggestions.queue_path``. Returns None when the
    config block is missing or the path is unset — bot handlers
    interpret None as "command not properly configured" and reply with
    a recognizable failure message rather than crashing.
    """
    if config.moc_suggestions is None:
        return None
    return config.moc_suggestions.queue_path


async def on_moc_suggestions(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/moc-suggestions — list pending MOC suggestions, grouped by target.

    Read-only. Calls
    :func:`alfred.telegram.moc_suggestion_views.collect_pending` (queue
    file read, no writes) then renders the result as a grouped-by-MOC
    Markdown reply via ``render_suggestions``.

    Hypatia-only via the ``moc_suggestions`` config gate. Empty-state
    per ``intentionally_left_blank``: explicit "no pending" message,
    never a blank reply.

    Failure isolation: any unexpected exception in collect/render path
    logs and replies with a generic error rather than crashing the
    handler. The queue is canonical; the slash command is a glance-
    view.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command="/moc-suggestions",
        )
        return
    if update.message is None:
        return

    queue_path = _resolve_moc_suggestions_queue_path(config)
    if queue_path is None:
        await update.message.reply_text(
            "❌ /moc-suggestions is not configured "
            "(missing telegram.moc_suggestions.queue_path)."
        )
        log.warning(
            "talker.bot.moc_suggestions_no_queue_path",
            command="/moc-suggestions",
        )
        return

    try:
        from . import moc_suggestion_views as _msv
        suggestions = _msv.collect_pending(queue_path)
        reply = _msv.render_suggestions(suggestions)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.moc_suggestions_failed",
            command="/moc-suggestions",
            queue_path=queue_path,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Could not load MOC suggestions ({type(exc).__name__})"
        )
        return

    # Cap defensively at 4000 chars to stay under Telegram's 4096 limit.
    # If overflow, append a truncation note rather than silently chop.
    truncated = False
    if len(reply) > 4000:
        reply = reply[:3950].rstrip() + "\n\n…(truncated; queue has more — see queue file directly)"
        truncated = True

    await update.message.reply_text(reply)

    log.info(
        "talker.bot.moc_suggestions_done",
        command="/moc-suggestions",
        suggestion_count=len(suggestions),
        reply_chars=len(reply),
        truncated=truncated,
    )


def _parse_id_argument(update: Update, command: str) -> str | None:
    """Extract the suggestion id argument from ``/accept-moc <id>`` /
    ``/reject-moc <id>``.

    Returns None when no id is supplied (handler renders the usage hint).
    Defensive against the message-text-is-None path; whitespace tolerant.
    """
    text = (update.message.text or "") if update.message else ""
    parts = text.strip().split()
    # parts[0] is the command itself (e.g. "/accept_moc"); parts[1:]
    # carries any args. Ignore extras silently — the id is positional.
    if len(parts) < 2:
        return None
    return parts[1].strip()


async def on_accept_moc(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/accept-moc <id> — apply a pending MOC suggestion.

    Per the ratified Q3.5 design (b): the accept path edits each
    candidate member record's ``mocs:`` frontmatter to append the
    target MOC, triggering Phase 4 Sub-arc A's existing append hook
    on the MOC's ``# Contents``. ONE write surface (member-side
    frontmatter); the MOC body update is downstream of that.

    For ``propose_new`` suggestions, the new MOC is created via
    ``vault_create`` before iterating members.

    State machine:
      * Status pending → accepted (transitional). If queue's state
        machine refuses (already non-pending), reply explains.
      * vault_create (propose-new) or skip (existing target).
      * Per-member vault_edit. Failures captured per-member; loop
        continues so partial success is preserved.
      * Final: status → applied if every member succeeded; else
        pending with first error in ``last_apply_error``.

    PTB registers this as ``accept_moc`` (underscore); operator types
    ``/accept_moc <id>`` (underscore form). The dash form
    ``/accept-moc`` falls through to Telegram's unknown-command
    behaviour because PTB only accepts ``[a-z0-9_]`` in registrations.
    See registration block in :func:`build_app`.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command="/accept-moc",
        )
        return
    if update.message is None:
        return

    suggestion_id = _parse_id_argument(update, "/accept-moc")
    if suggestion_id is None:
        await update.message.reply_text(
            "Usage: `/accept-moc <id>` — id is the `ms-YYYYMMDD-xxxxxxxx` "
            "value from /moc-suggestions."
        )
        return

    queue_path = _resolve_moc_suggestions_queue_path(config)
    if queue_path is None:
        await update.message.reply_text(
            "❌ /accept-moc is not configured "
            "(missing telegram.moc_suggestions.queue_path)."
        )
        return

    from . import moc_suggestion_views as _msv

    try:
        suggestion = _msv.lookup_suggestion(queue_path, suggestion_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.moc_lookup_failed",
            command="/accept-moc",
            suggestion_id=suggestion_id,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Could not load MOC suggestion queue ({type(exc).__name__})"
        )
        return

    if suggestion is None:
        await update.message.reply_text(
            f"❌ No suggestion found with ID `{suggestion_id}`. "
            "Use /moc-suggestions to list pending."
        )
        log.info(
            "talker.bot.moc_accept_unknown_id",
            suggestion_id=suggestion_id,
        )
        return

    if suggestion.status != "pending":
        await update.message.reply_text(
            f"❌ Suggestion `{suggestion_id}` is `{suggestion.status}`, "
            "not pending. Only pending suggestions can be accepted."
        )
        log.info(
            "talker.bot.moc_accept_non_pending",
            suggestion_id=suggestion_id,
            current_status=suggestion.status,
        )
        return

    # Apply path. Failures-isolated within ``apply_accept``; the
    # function returns an ``ApplyResult`` regardless of outcome.
    #
    # Scope plumbing: derive from the running instance's identity.
    # Fail-loud rather than silent fallback to a single-instance literal
    # per ``feedback_hardcoding_and_alfred_naming.md`` — the talker
    # daemon never reaches this code without a populated
    # ``config.instance.name`` (loader fails on missing instance block),
    # but the defensive guard preserves that guarantee at the call site
    # rather than silently routing a misconfigured instance through
    # one instance's scope.
    if not (config.instance and config.instance.name):
        raise RuntimeError(
            "config.instance.name required for /accept-moc scope plumbing"
        )
    try:
        result = _msv.apply_accept(
            suggestion=suggestion,
            queue_path=queue_path,
            vault_path=Path(config.vault.path),
            scope=config.instance.name.lower(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.moc_apply_unhandled",
            command="/accept-moc",
            suggestion_id=suggestion_id,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Apply failed unexpectedly ({type(exc).__name__})."
        )
        return

    # Render reply based on the ApplyResult shape.
    if result.all_succeeded:
        target_note = (
            f"created new MOC `{result.target_label}`"
            if result.new_moc_created
            else f"target `{result.target_label}`"
        )
        reply = (
            f"✓ Applied suggestion `{suggestion_id}` — "
            f"added {result.members_total} member(s) to {target_note}."
        )
    elif result.new_moc_create_error:
        reply = (
            f"⚠️ Suggestion `{suggestion_id}` apply failed at MOC creation. "
            f"Status reverted to pending. Error: `{result.first_error}`"
        )
    else:
        succ = len(result.members_succeeded)
        fail = len(result.members_failed)
        reply = (
            f"⚠️ Suggestion `{suggestion_id}` partially applied — "
            f"{succ} succeeded, {fail} failed. Status reverted to "
            f"pending with error: `{result.first_error}`"
        )

    await update.message.reply_text(reply)

    log.info(
        "talker.bot.moc_accept_done",
        command="/accept-moc",
        suggestion_id=suggestion_id,
        members_succeeded=len(result.members_succeeded),
        members_failed=len(result.members_failed),
        new_moc_created=result.new_moc_created,
        outcome=(
            "applied" if result.all_succeeded
            else "create_failed" if result.new_moc_create_error
            else "partial"
        ),
    )


async def on_reject_moc(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """/reject-moc <id> — flip a pending MOC suggestion to rejected.

    No vault write. Status → rejected. Per ratified Q5,
    rejected suggestions persist in the queue indefinitely so
    surveyor's idempotent upsert never re-proposes the same
    (members, target) pair — negative-learning surface.

    PTB registers this as ``reject_moc`` (underscore); operator types
    ``/reject_moc <id>``. The dash form falls through to Telegram's
    unknown-command behaviour (PTB-imposed; same as /accept-moc).
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            command="/reject-moc",
        )
        return
    if update.message is None:
        return

    suggestion_id = _parse_id_argument(update, "/reject-moc")
    if suggestion_id is None:
        await update.message.reply_text(
            "Usage: `/reject-moc <id>` — id is the `ms-YYYYMMDD-xxxxxxxx` "
            "value from /moc-suggestions."
        )
        return

    queue_path = _resolve_moc_suggestions_queue_path(config)
    if queue_path is None:
        await update.message.reply_text(
            "❌ /reject-moc is not configured "
            "(missing telegram.moc_suggestions.queue_path)."
        )
        return

    from . import moc_suggestion_views as _msv

    try:
        suggestion = _msv.lookup_suggestion(queue_path, suggestion_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.moc_lookup_failed",
            command="/reject-moc",
            suggestion_id=suggestion_id,
            error=str(exc),
        )
        await update.message.reply_text(
            f"❌ Could not load MOC suggestion queue ({type(exc).__name__})"
        )
        return

    if suggestion is None:
        await update.message.reply_text(
            f"❌ No suggestion found with ID `{suggestion_id}`. "
            "Use /moc-suggestions to list pending."
        )
        log.info(
            "talker.bot.moc_reject_unknown_id",
            suggestion_id=suggestion_id,
        )
        return

    if suggestion.status != "pending":
        await update.message.reply_text(
            f"❌ Suggestion `{suggestion_id}` is `{suggestion.status}`, "
            "not pending. Only pending suggestions can be rejected."
        )
        log.info(
            "talker.bot.moc_reject_non_pending",
            suggestion_id=suggestion_id,
            current_status=suggestion.status,
        )
        return

    success = _msv.reject_suggestion(
        queue_path=queue_path, suggestion_id=suggestion_id,
    )

    if success:
        target_label = (
            suggestion.target_moc_rel_path
            if suggestion.target_moc_rel_path
            else f"new MOC: {suggestion.proposed_new_moc_name or '(unnamed)'}"
        )
        reply = (
            f"✓ Rejected suggestion `{suggestion_id}` — "
            f"target `{target_label}` will not be re-proposed."
        )
    else:
        reply = (
            f"❌ Could not reject `{suggestion_id}` "
            "(state machine refused; check current status via /moc-suggestions)."
        )

    await update.message.reply_text(reply)

    log.info(
        "talker.bot.moc_reject_done",
        command="/reject-moc",
        suggestion_id=suggestion_id,
        success=success,
    )


async def _stamp_extract_target_override_on_active(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    override: str,
) -> bool:
    """Stamp ``_extract_target_override`` on the active session dict.

    Helper shared by ``/end-zettel`` and ``/end-note`` (Phase 1.x,
    2026-05-16). The override field is read by
    :func:`alfred.telegram.session._snapshot_for_post_close` so the
    capture-batch orchestrator sees the operator's choice after
    ``close_session`` pops the active dict.

    Returns ``True`` when the override was applied (active session
    exists, override is canonical); ``False`` and replies to the user
    when there's no active session — caller short-circuits.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return False
    if update.message is None or update.effective_chat is None:
        return False
    chat_id = update.effective_chat.id
    active = state_mgr.get_active(chat_id)
    if not active:
        # No active session — same error shape as /end's no-active path.
        await update.message.reply_text("no active session.")
        return False
    active["_extract_target_override"] = override
    # Persist via the state-manager's save path so the snapshot pickup
    # post-close sees the same on-disk shape (mirrors how other active-
    # dict fields are mutated — e.g. _continues_from in the routing
    # handler).
    state_mgr.save()
    log.info(
        "talker.bot.extract_target_override_stamped",
        chat_id=chat_id,
        override=override,
    )
    return True


@owner_only
async def on_end_zettel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/end-zettel — close session with operator override forcing
    ``zettel/`` extraction target regardless of source-anchor state.

    Phase 1.x (2026-05-16) per the three-tier-discriminator rework.
    Marks the active session with ``_extract_target_override = "zettel"``
    and then delegates to :func:`on_end` for the actual close flow.
    The override is persisted to the session record's
    ``capture_extract_target_override:`` frontmatter field via
    :func:`alfred.telegram.capture_batch.process_capture_session`, so
    a deferred ``/extract`` invocation minutes later still honours
    the choice.

    No-active-session case → "no active session." reply, same as /end.

    Interaction with the memo branch (intentional, ratified
    2026-05-16): if the session has ≤1 user message AND the
    instance is Hypatia, the memo branch fires inside
    :func:`alfred.telegram.capture_batch.process_capture_session`
    BEFORE the discriminator runs. The override field still gets
    stamped on the active dict + written to the memo'd session
    record's frontmatter, but nothing on the memo path consults
    it — memo is its own tier (atomic single-thought capture),
    distinct from the zettel/note multi-message discrimination.
    Operator wanting a 1-message thought to become a permanent
    zettel is uncommon; memo wins for now. If real-use friction
    surfaces (operator regularly types ``/end-zettel`` on
    single-message sessions and wants the structured-extraction
    path), the override could flow up to cancel the memo branch
    — that's a follow-up commit, not part of Phase 1.x.
    """
    if not await _stamp_extract_target_override_on_active(
        update, ctx, override="zettel",
    ):
        return
    await on_end(update, ctx)


@owner_only
async def on_end_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/end-note — close session with operator override forcing
    ``note/`` extraction target regardless of source-anchor state.

    Mirror of :func:`on_end_zettel` with override = ``"note"``. Used
    when the operator wants the capture filed as a fleeting note even
    though the session has source-anchor wikilinks (e.g. operator
    caught a wrong anchor or deliberately wants the record as a
    note rather than a zettel).
    """
    if not await _stamp_extract_target_override_on_active(
        update, ctx, override="note",
    ):
        return
    await on_end(update, ctx)


@owner_only
async def on_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/end — explicitly close the current session, return vault record path.

    Wk3 commit 7: after the session record is written, run
    :func:`calibration.propose_updates` over the transcript and apply any
    proposals via :func:`calibration.apply_proposals`. For dial 4 (default),
    applied proposals are surfaced inline in the close reply so Andrew
    can confirm / object. Errors in the calibration write are logged but
    never block the close reply.

    Phase 1.x (2026-05-16) variants: ``/end-zettel`` and ``/end-note``
    (handled by :func:`on_end_zettel` / :func:`on_end_note`) stamp an
    ``_extract_target_override`` field on the active session before
    delegating to this handler; the override flows through the
    snapshot helper + capture-batch orchestrator into session
    frontmatter so the later ``/extract`` honours it.
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
    # we want from it must be copied out first. The post-close hook
    # (substance-slug rename) reads transcript + session_id; the helper
    # encodes that contract centrally so it stays in sync across all
    # three close paths (bot /end, daemon shutdown, timeout sweeper).
    post_close_snap = session._snapshot_for_post_close(active)
    transcript_snapshot = post_close_snap["transcript"]
    session_id_snapshot = post_close_snap["session_id"]
    # Phase 1.x: ``/end-zettel`` / ``/end-note`` set _extract_target_override
    # on the active dict before delegating to /end; the snapshot above
    # captures it so we can pass it through to the capture-batch
    # orchestrator after close pops the active dict.
    extract_target_override = post_close_snap.get(
        "extract_target_override", ""
    )
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

    # --- Substance-slug rename (Phase 2 deferred-enhancement #1) --------
    # When ``telegram.session.derive_slug_from_substance`` is enabled (off
    # by default; on for Hypatia), the just-closed session record is
    # renamed to use a topic-derived slug instead of the opening-text
    # slug. Failure-isolated: any error keeps the original filename.
    try:
        rel_path = await session.maybe_apply_substance_slug(
            state_mgr,
            enabled=config.session.derive_slug_from_substance,
            client=client,
            model=config.anthropic.model,
            vault_path_root=active.get("_vault_path_root") or config.vault.path,
            rel_path=rel_path,
            transcript=transcript_snapshot,
            session_id=session_id_snapshot,
        )
    except Exception:  # noqa: BLE001 — never break the close flow
        log.exception(
            "talker.bot.substance_slug_unhandled", chat_id=chat_id,
        )

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
            # Capture-source-anchor (2026-05-16): pass the per-instance
            # scope key so the orchestrator can fire the opening-pattern
            # resolver (``I'm reading X by Y``) → source/author records.
            # Empty string for non-Hypatia instances; their scopes don't
            # carry the ``author`` create-allowlist entry today.
            anchor_scope = (
                "hypatia" if (config.instance.tool_set or "").lower() == "hypatia"
                else ""
            )
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
                    anchor_scope=anchor_scope,
                    # Phase 1.x (2026-05-16): operator's explicit
                    # /end-zettel / /end-note choice (or ``""`` for
                    # plain /end). process_capture_session writes the
                    # value to session frontmatter so the later
                    # /extract honours it via the three-tier
                    # discriminator.
                    extract_target_override=extract_target_override,
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


@owner_only
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


@owner_only
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


@owner_only
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


@owner_only
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
    # Per-instance extraction target (Phase 1 Zettelkasten cutover,
    # 2026-05-16): Hypatia produces ``zettel/`` records; everyone else
    # (Salem, unset) produces ``note/``. Empty string for non-Hypatia
    # instances preserves legacy Salem behaviour. Mirrors the
    # ``anchor_scope`` derivation in ``on_end`` so both source-anchor
    # resolution and post-anchor extraction route the same way.
    anchor_scope = (
        "hypatia" if (config.instance.tool_set or "").lower() == "hypatia"
        else ""
    )
    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=_Path(config.vault.path),
        short_id=short_id,
        model=model,
        agent_slug=_agent_slug_for_extract(config),
        anchor_scope=anchor_scope,
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


@owner_only
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


@owner_only
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


# --- Hypatia fiction posture (Phase 2.5) ---------------------------------


async def on_fiction(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/fiction <title>`` — scaffold a fiction project directory.

    Hypatia-only by config gate (``telegram.fiction.command_enabled``);
    only registered when that knob is True. Salem and other
    operational-vault instances never see this command.

    Produces the per-element directory shape Hypatia's SKILL revision
    expects:

      ``draft/fiction/<slug>/``
        ``continuity.md``  — orientation index Hypatia reads first
        ``story.md``       — working manuscript
        ``structure.md``   — framework placeholder
        ``world.md``       — setting / world details
        ``voice.md``       — narrator register / voice contract
        ``characters/``    — character files added later (.gitkeep
                             keeps the empty dir alive in git)

    Idempotent: if the project directory already exists, the user
    gets an informative reply and nothing is overwritten. The
    natural-language scaffolding path (Hypatia's SKILL) goes through
    ``vault_create`` calls but produces the same shape — both paths
    share the contract documented in
    :func:`alfred.telegram.fiction.scaffold_fiction_project`.
    """
    from alfred.telegram.fiction import scaffold_fiction_project

    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return

    # Title can come from PTB's parsed args OR (for inline-style
    # invocations like "/fiction The Glass Forest" with extra
    # whitespace shapes) from the raw message text. Prefer args when
    # available — PTB normalizes whitespace.
    raw_text = update.message.text or ""
    if ctx.args:
        title = " ".join(ctx.args).strip()
    else:
        # Strip the leading "/fiction" (with possible "@botname" suffix
        # from group chats) and use the rest as the title.
        without_cmd = raw_text.split(maxsplit=1)
        title = without_cmd[1].strip() if len(without_cmd) > 1 else ""

    if not title:
        await update.message.reply_text(
            "usage: /fiction <title>  (e.g. /fiction The Glass Forest)"
        )
        return

    vault_root = Path(config.vault.path)
    if not vault_root.exists():
        log.warning(
            "talker.fiction.vault_root_missing",
            chat_id=update.effective_chat.id,
            vault_path=str(vault_root),
        )
        await update.message.reply_text(
            "Vault path is not accessible from the daemon — can't "
            "scaffold a fiction project. Check vault.path in config."
        )
        return

    log.info(
        "talker.fiction.invoked",
        chat_id=update.effective_chat.id,
        title=title[:80],
    )

    result = scaffold_fiction_project(vault_root, title)

    if result.status == "already_exists":
        await update.message.reply_text(result.detail)
        return

    # status == "created"
    await update.message.reply_text(
        f"{result.detail}\n\n"
        f"Files created:\n"
        + "\n".join(f"  • {p}" for p in result.created_files)
    )


# --- Voice/method training slash commands (2026-05-07 arc) ---------------


def _resolve_voice_train_input(
    update: Update,
    state_mgr: StateManager,
    config: TalkerConfig,
    *,
    body_arg: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Return (text, image_metadata) for a /train or /method-source call.

    Resolution order:
      1. Caption text on the slash-message itself (if the user attached
         media to the slash command).
      2. The arg the user typed after the slash.
      3. The most-recent qualifying user paste in the active session
         transcript.

    Image metadata is ALWAYS pulled from the active session transcript
    (the spec ships /method-source vision via "send the screenshot,
    then send /method-source") AND from the caption-attached path
    (when the user sends the slash command in the same message as the
    image — less common but supported).

    Returns ``("", [])`` when nothing resolves — the handler renders a
    "no recent paste" reply.
    """
    # Step 1+2: explicit body wins.
    text = body_arg or ""

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return text, []

    # Most-recent paste fallback.
    if not text:
        active = state_mgr.get_active(chat_id) or {}
        transcript = active.get("transcript") or []
        min_chars = (
            config.voice_train.min_paste_chars
            if config.voice_train is not None
            else 200
        )
        text = voice_train.find_most_recent_user_paste(
            transcript, min_chars=min_chars,
        )

    # Image metadata: pull from the session's images list (the photo
    # handler stamps them onto session.images via append_image).
    image_metadata: list[dict[str, Any]] = []
    active = state_mgr.get_active(chat_id) or {}
    images = active.get("images") or []
    # Take the most-recent image (last entry). The slash-command
    # workflow is "send image then send slash"; we attach the most-
    # recent one. Operators wanting multiple images attached should
    # send them as one Telegram media-group, which lands as multiple
    # images on consecutive turns — out of scope for Phase 1.
    if images:
        image_metadata = [images[-1]]
    return text, image_metadata


async def on_train(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/train [--cluster <name>] [<text>]`` — train Hypatia on Andrew's voice.

    Saves the raw essay record at ``document/essay/<slug>.md`` and
    enqueues an async voice-profile extraction job. Bug #58 changed
    the timing: pastes that span multiple Telegram messages (long
    Substack essays exceed the 4096-char per-message limit and get
    chunked into 2-4 messages by the client) now buffer for
    ``debounce_seconds`` of operator silence before flushing, so the
    full text is captured rather than just the first chunk.

    Resolution: explicit body in the slash-arg → most-recent long
    paste in conversation memory. Empty body opens an empty buffer —
    the operator can paste their chunks in the next few messages.

    The async worker (started by the daemon when voice_train is
    configured) picks up the job, calls Opus, writes the structured
    voice profile, and DMs the operator on completion.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return
    if config.voice_train is None or not config.voice_train.command_enabled:
        # Defensive — registration is gated, but if a future code path
        # routes here without the gate (e.g. inline-command) we still
        # refuse cleanly.
        await update.message.reply_text(
            "/train is not enabled on this instance."
        )
        return

    raw_text = update.message.text or ""
    cluster, body_arg = voice_train.parse_train_args(raw_text, ctx.args)
    initial_text, image_metadata = _resolve_voice_train_input(
        update, state_mgr, config, body_arg=body_arg,
    )

    chat_id = update.effective_chat.id
    log.info(
        "talker.bot.voice_train.invoked",
        chat_id=chat_id,
        kind="voice",
        text_len=len(initial_text),
        cluster=cluster,
    )
    await _open_or_extend_voice_train_buffer(
        ctx,
        chat_id=chat_id,
        kind="voice",
        cluster=cluster,
        initial_text=initial_text,
        image_metadata=image_metadata,
        reply_target=update.message,
    )


async def on_method_source(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """``/method-source [<text>]`` (registered as /method_source).

    Saves the raw source record at ``source/<slug>.md`` and enqueues
    the method-extraction job. Same multi-message paste handling as
    :func:`on_train` (Bug #58) — pastes that span multiple Telegram
    messages buffer for ``debounce_seconds`` of operator silence
    before flushing.

    Image attachments: when an image is on the active session's
    ``images`` list, the source body includes a reference to it AND
    the LLM call gets vision-transcription wrapping in a future
    Phase 1.5. Phase 1 ships the image-reference + raw-text path.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    if not _is_allowed(update, config):
        return
    if update.message is None or update.effective_chat is None:
        return
    if config.voice_train is None or not config.voice_train.command_enabled:
        await update.message.reply_text(
            "/method-source is not enabled on this instance."
        )
        return

    raw_text = update.message.text or ""
    body_arg = voice_train.parse_method_source_args(raw_text, ctx.args)
    initial_text, image_metadata = _resolve_voice_train_input(
        update, state_mgr, config, body_arg=body_arg,
    )

    chat_id = update.effective_chat.id
    log.info(
        "talker.bot.voice_train.invoked",
        chat_id=chat_id,
        kind="method",
        text_len=len(initial_text),
        has_image=bool(image_metadata),
    )
    await _open_or_extend_voice_train_buffer(
        ctx,
        chat_id=chat_id,
        kind="method",
        cluster=None,
        initial_text=initial_text,
        image_metadata=image_metadata,
        reply_target=update.message,
    )


# --- Voice-train buffer helpers (Bug #58) -------------------------------


# --- Ticket #69 cluster-ask helpers ------------------------------------
#
# When /train is issued WITHOUT ``--cluster``, the legacy path saved
# the essay with ``cluster: null`` and the operator had to vault_edit
# the field manually post-extraction to get clustering. The new flow
# asks "Which cluster?" in the confirmation message, then watches the
# next user message for a cluster-shaped reply. A successful reply
# fires ``vault_edit`` on the just-saved fixture's ``cluster`` field
# (and re-enqueues so the cluster-tier rebuild runs against the now-
# tagged leaf). ``general`` is the explicit opt-out — same shape as
# Andrew's prior workflow when he didn't want clustering. Anything
# else is treated as a cluster name verbatim.
#
# /method-source path: cluster is /train-only by design. ``source/``
# records do not carry a ``cluster:`` field and the worker never reads
# one. So /method-source completion does NOT trigger this prompt — the
# field would have nowhere to land. Symmetric coverage IS possible if
# we extend the source schema later, but that requires schema +
# extraction-prompt + worker changes orthogonal to this ticket.


def _make_cluster_ask_entry(
    *,
    raw_rel_path: str,
    raw_name: str,
    raw_body: str,
    instance: str,
) -> dict[str, Any]:
    """Build a pending-cluster entry. Keys are stable for test access.

    Carries enough state to (a) tag the raw essay with the cluster
    field via vault_edit AND (b) re-enqueue a fresh extraction job
    that will write the voice profile with the cluster baked in. The
    re-enqueue is the cluster-tag race fix — see the docstring on
    :func:`_handle_cluster_ask_reply` for the full failure mode.

    ``raw_body`` is held in-memory only — never persisted. The 5-min
    expiry caps the worst-case memory cost; once the operator answers
    or the entry expires, the body is dropped.
    """
    return {
        "raw_rel_path": raw_rel_path,
        "raw_name": raw_name,
        "raw_body": raw_body,
        "instance": instance,
        "expires_at": datetime.now(timezone.utc).timestamp()
        + _VOICE_TRAIN_CLUSTER_ASK_TIMEOUT_SECONDS,
    }


def _is_pending_cluster_live(entry: dict[str, Any]) -> bool:
    """True when ``entry`` hasn't expired. Stale entries are dropped."""
    expires_at = float(entry.get("expires_at", 0.0))
    return datetime.now(timezone.utc).timestamp() < expires_at


def _consume_pending_cluster_ask(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
) -> dict[str, Any] | None:
    """Pop the pending-cluster entry for ``chat_id`` if live; else None.

    Drops expired entries silently — the operator moved on; we don't
    want to mis-tag the next unrelated message. Lives in ``bot_data``
    so all handlers (on_train, on_text) see one source of truth.
    """
    pending: dict[int, dict[str, Any]] = (
        ctx.application.bot_data.get(_KEY_VOICE_TRAIN_PENDING_CLUSTER) or {}
    )
    entry = pending.get(chat_id)
    if entry is None:
        return None
    if not _is_pending_cluster_live(entry):
        pending.pop(chat_id, None)
        log.info(
            "talker.bot.voice_train.cluster_ask_expired", chat_id=chat_id,
        )
        return None
    return pending.pop(chat_id)


def _looks_like_cluster_reply(text: str) -> bool:
    """True when ``text`` matches the cluster-name shape.

    Single token (no whitespace), letters/digits/hyphens/underscores
    only, max 30 chars, no command prefix (``/``). The ``general``
    opt-out sentinel passes too — we treat it specially in the caller.
    Anything else (sentences, command typos, URLs) returns False so
    the message falls through to the normal conversation handler.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("/"):
        return False
    if " " in stripped or "\t" in stripped or "\n" in stripped:
        return False
    return bool(_CLUSTER_NAME_PATTERN.match(stripped))


async def _handle_cluster_ask_reply(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    entry: dict[str, Any],
) -> None:
    """Apply a cluster-name reply to the pending fixture.

    ``text`` has already passed :func:`_looks_like_cluster_reply` so
    we know it's a clean single-token candidate. The opt-out sentinel
    ``general`` short-circuits without touching the vault; any other
    name is written via ``vault_edit`` on the recorded raw_rel_path.

    Cluster-tag race fix (P1 from #69 review, 2026-05-08): the
    /train job was enqueued with ``cluster=None``, so the worker's
    voice-profile write would land WITHOUT a cluster field even after
    we tag the raw essay here. ``_list_voice_leaves_with_cluster``
    reads the VOICE PROFILE frontmatter (NOT the raw essay), so an
    untagged voice profile gets excluded from the cluster-tier
    rebuild — silently breaking the headline promise of the cluster
    ask UX.

    Fix is Option A.2 (re-enqueue): after the raw-essay vault_edit
    succeeds, append a fresh ExtractionJob with ``cluster=<name>``.
    The worker re-runs extraction (now with cluster carried through
    job.cluster → ``_write_structured_record(cluster=...)``) and
    body_replaces the prior voice-profile body with the cluster-
    tagged version. Hypatia scope already permits ``body_replace``
    on ``voice`` records (scope.py:436), so the second worker pass
    succeeds where the first wrote without cluster. Cost: ~17s of
    extra Opus extraction time; benefit: deterministic outcome with
    no polling complexity. If the first extraction is still running
    when the second job lands, both run in queue order — final
    state is the cluster-tagged voice profile either way (FIFO
    drain order + body_replace overwriting).

    On success we send a brief confirmation; failures fall back to a
    "couldn't tag" reply (the essay is saved either way — this is a
    nice-to-have on top of the already-shipped extraction).
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    bot_obj = ctx.bot
    cluster = text.strip()
    raw_rel_path = entry.get("raw_rel_path") or ""
    raw_name = entry.get("raw_name") or ""
    raw_body = entry.get("raw_body") or ""

    if cluster.lower() == _CLUSTER_OPT_OUT_SENTINEL:
        # Opt-out — leave cluster field unset; that was the legacy
        # behaviour for /train without --cluster.
        log.info(
            "talker.bot.voice_train.cluster_ask_opt_out",
            chat_id=chat_id,
            raw_rel_path=raw_rel_path,
        )
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text="ok — leaving uncategorized.",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.cluster_ask_optout_reply_failed",
                chat_id=chat_id,
            )
        return

    # Step 1: apply the cluster tag to the raw essay frontmatter.
    try:
        from alfred.vault import ops as _ops
        _ops.vault_edit(
            Path(config.vault.path),
            raw_rel_path,
            set_fields={"cluster": cluster},
            scope=_voice_train_scope_for(config),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.voice_train.cluster_ask_apply_failed",
            chat_id=chat_id,
            raw_rel_path=raw_rel_path,
            cluster=cluster,
            error=str(exc),
        )
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=(
                    f"couldn't tag `{raw_rel_path}` with cluster "
                    f"`{cluster}`: {exc}. essay is still saved + "
                    f"queued; tag manually with vault_edit if needed."
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.cluster_ask_apply_failed_reply_failed",
                chat_id=chat_id,
            )
        return

    # Step 2: re-enqueue a fresh extraction job with the cluster
    # value baked in. This rewrites the voice profile via the
    # worker's body_replace path, ensuring the structured record
    # carries the cluster field so the cluster-tier rebuild filter
    # (`_list_voice_leaves_with_cluster`) actually picks it up.
    #
    # Failure of the re-enqueue is logged but does NOT roll back
    # the raw-essay tag — the operator can still re-run /train with
    # --cluster to retry. We surface a "tagged but re-extraction
    # didn't enqueue" warning in the reply when this happens.
    reextract_warning = ""
    if not raw_name or not raw_body:
        # Defensive — older entry shape (pre-fix) lacked these
        # fields. Per intentionally-left-blank, surface explicitly.
        log.warning(
            "talker.bot.voice_train.cluster_ask_reextract_skipped",
            chat_id=chat_id,
            raw_rel_path=raw_rel_path,
            reason="missing_raw_data",
        )
        reextract_warning = (
            " (re-extraction skipped — missing raw fixture data; "
            "the voice profile may not carry the cluster tag)"
        )
    else:
        queue_path = _resolve_queue_path(config)
        try:
            reextract_job = voice_train.make_job(
                kind="voice",
                raw_rel_path=raw_rel_path,
                raw_name=raw_name,
                raw_body=raw_body,
                cluster=cluster,
                chat_id=chat_id,
                instance=config.instance.name or "",
            )
            voice_train.enqueue_job(queue_path, reextract_job)
            log.info(
                "talker.bot.voice_train.cluster_ask_reextract_enqueued",
                chat_id=chat_id,
                raw_rel_path=raw_rel_path,
                cluster=cluster,
                job_id=reextract_job.job_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.bot.voice_train.cluster_ask_reextract_failed",
                chat_id=chat_id,
                raw_rel_path=raw_rel_path,
                cluster=cluster,
                error=str(exc),
            )
            reextract_warning = (
                f" (re-extraction couldn't enqueue: {exc} — the "
                f"voice profile may not carry the cluster tag; "
                f"re-run /train --cluster {cluster} to retry)"
            )

    log.info(
        "talker.bot.voice_train.cluster_ask_applied",
        chat_id=chat_id,
        raw_rel_path=raw_rel_path,
        cluster=cluster,
    )
    try:
        await bot_obj.send_message(
            chat_id=chat_id,
            text=(
                f"tagged `{raw_rel_path}` with cluster `{cluster}`"
                f"{reextract_warning}. the cluster-tier rebuild will "
                f"run when ≥2 leaves share this tag."
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "talker.bot.voice_train.cluster_ask_apply_reply_failed",
            chat_id=chat_id,
        )


async def _open_or_extend_voice_train_buffer(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    kind: Literal["voice", "method"],
    cluster: str | None,
    initial_text: str,
    image_metadata: list[dict[str, Any]],
    reply_target: Any,
) -> None:
    """Open a paste buffer for this chat (or append to an existing one).

    The flow:
      1. If a buffer is already open for this chat_id, the operator
         issued a SECOND command before the first one flushed. Flush
         the prior buffer immediately (carrying its kind/cluster) and
         then open a fresh buffer for this command.
      2. Append the slash-command's initial body chunk (if any).
      3. Schedule the debounce-flush task. Subsequent text messages
         in this chat hit ``_voice_train_buffer_append`` (called from
         ``on_text``) which appends + resets the timer.
      4. Reply to the operator with a brief "buffering" ack.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    buffers: dict[int, voice_train.PendingPaste] = (
        ctx.application.bot_data[_KEY_VOICE_TRAIN_BUFFERS]
    )

    # Step 1: flush any pre-existing buffer (operator switched modes).
    existing = buffers.get(chat_id)
    if existing is not None and not existing.flushed:
        log.info(
            "talker.bot.voice_train.buffer_preempted",
            chat_id=chat_id,
            old_kind=existing.kind,
            new_kind=kind,
        )
        await _flush_voice_train_buffer(ctx, chat_id, reason="preempted")

    # Step 2: open the new buffer + seed with the initial chunk.
    pending = voice_train.PendingPaste(
        chat_id=chat_id,
        kind=kind,
        cluster=cluster,
        image_metadata=list(image_metadata or []),
    )
    if initial_text:
        voice_train.append_paste_chunk(pending, initial_text)
    buffers[chat_id] = pending

    # Step 3: schedule the flush.
    debounce = (
        config.voice_train.debounce_seconds
        if config.voice_train is not None
        else 10
    )
    _schedule_voice_train_flush(ctx, chat_id, delay_seconds=debounce)

    # Step 4: ack.
    initial_chars = len(initial_text)
    cluster_msg = f" (cluster: {cluster})" if cluster else ""
    if initial_chars > 0:
        await reply_target.reply_text(
            f"buffering {initial_chars} chars{cluster_msg} — append more "
            f"chunks within {debounce}s, or wait for auto-flush."
        )
    else:
        if kind == "method":
            empty_ack = (
                f"/method-source ready{cluster_msg} — paste your text "
                f"in the next message(s); I'll flush after {debounce}s "
                f"of silence."
            )
        else:
            empty_ack = (
                f"/train ready{cluster_msg} — paste your essay in the "
                f"next message(s); I'll flush after {debounce}s of silence."
            )
        await reply_target.reply_text(empty_ack)


def _schedule_voice_train_flush(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, *, delay_seconds: int,
) -> None:
    """Replace the active flush task with a new debounce-delayed one.

    Cancelling + replacing the task is the debounce reset. The flush
    task itself is a fire-and-forget asyncio task — we don't await it
    here; on_text returns immediately so Telegram delivery isn't
    blocked.

    Ticket #70 (2026-05-07): two early-flush paths added.
      * **End-marker fast-path** — if the assembled buffer contains a
        recognized Substack-export end marker (footnote-tail, sign-off,
        author bio opener, ``Subscribed`` block), the flush task uses
        ``rapid_arrival_seconds`` (default 3s) instead of the full
        debounce. The shorter delay IS itself the silence guard: a
        chunk arriving inside the window cancels + reschedules, and
        the next call re-checks the marker against the now-extended
        text. Saves ~7s on every complete-essay paste.
      * **Ceiling enforcement** — unchanged from Bug #58: even an
        operator who keeps typing flushes at ``max_buffer_seconds``
        past ``opened_at``.
    """
    buffers: dict[int, voice_train.PendingPaste] = (
        ctx.application.bot_data[_KEY_VOICE_TRAIN_BUFFERS]
    )
    pending = buffers.get(chat_id)
    if pending is None:
        return
    # Cancel the prior flush task if it's still scheduled (debounce
    # reset). A flush already running is left alone — its early
    # ``flushed=True`` flag prevents a subsequent append from
    # double-processing.
    if pending.flush_task is not None:
        try:
            pending.flush_task.cancel()
        except Exception:  # noqa: BLE001
            pass

    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    max_buffer = (
        config.voice_train.max_buffer_seconds
        if config.voice_train is not None
        else 60
    )
    # Compute the effective sleep — never beyond the max-buffer ceiling
    # measured from when the buffer first opened. This keeps a chatty
    # operator from extending the buffer indefinitely.
    elapsed = (
        datetime.now(timezone.utc) - pending.opened_at
    ).total_seconds()
    remaining_until_ceiling = max(0.0, max_buffer - elapsed)
    effective_sleep = min(float(delay_seconds), remaining_until_ceiling)

    # Ticket #70 — end-marker fast-path. If the buffer carries a
    # recognized end-of-essay signal, shorten the flush delay to
    # ~rapid_arrival_seconds so the operator gets their ack quickly
    # while still allowing a tail chunk to land. The shorter delay IS
    # itself the silence guard: any chunk arriving inside the 3s
    # window resets the timer (cancel + reschedule), and the next
    # schedule call will redo this end-marker check on the now-
    # extended assembled text.
    #
    # Skipped when remaining_until_ceiling already drove
    # effective_sleep below the rapid-arrival window — the ceiling
    # path takes over.
    rapid_arrival = (
        config.voice_train.rapid_arrival_seconds
        if config.voice_train is not None
        else 3.0
    )
    early_flush_reason: str | None = None
    if (
        effective_sleep > rapid_arrival
        and voice_train.buffer_has_end_marker(pending.assembled_text())
    ):
        effective_sleep = float(rapid_arrival)
        early_flush_reason = "end_marker"
        log.info(
            "talker.bot.voice_train.end_marker_detected",
            chat_id=chat_id,
            kind=pending.kind,
            assembled_chars=sum(len(c) for c in pending.chunks),
        )

    if effective_sleep <= 0:
        # We're already past the ceiling — flush immediately.
        async def _flush_now() -> None:
            await _flush_voice_train_buffer(ctx, chat_id, reason="ceiling")
        pending.flush_task = asyncio.create_task(_flush_now())
        return

    flush_reason = early_flush_reason or "debounce"

    async def _delayed_flush() -> None:
        try:
            await asyncio.sleep(effective_sleep)
        except asyncio.CancelledError:
            return
        await _flush_voice_train_buffer(ctx, chat_id, reason=flush_reason)

    pending.flush_task = asyncio.create_task(_delayed_flush())


def _voice_train_buffer_append(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
) -> bool:
    """Append a plain-text chunk to an open buffer, if one exists.

    Returns ``True`` when the chunk was consumed by the buffer (caller
    skips the natural-language conversation path). ``False`` when no
    buffer is open or when the buffer has already flushed (caller
    proceeds normally).

    Resets the debounce timer on every successful append.
    """
    buffers: dict[int, voice_train.PendingPaste] = (
        ctx.application.bot_data.get(_KEY_VOICE_TRAIN_BUFFERS) or {}
    )
    pending = buffers.get(chat_id)
    if pending is None or pending.flushed:
        return False
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    voice_train.append_paste_chunk(pending, text)
    debounce = (
        config.voice_train.debounce_seconds
        if config.voice_train is not None
        else 10
    )
    _schedule_voice_train_flush(ctx, chat_id, delay_seconds=debounce)
    log.info(
        "talker.bot.voice_train.buffer_chunk_appended",
        chat_id=chat_id,
        kind=pending.kind,
        chunk_chars=len(text),
        total_chunks=len(pending.chunks),
    )
    return True


async def _flush_voice_train_buffer(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, *, reason: str,
) -> None:
    """Drain a chat's pending buffer and run the save+enqueue path.

    Removes the buffer from the registry FIRST (so a late append
    arriving during the flush opens a new buffer rather than trying
    to extend a dying one). Then runs the kind-appropriate finalize
    helper on the assembled text.
    """
    buffers: dict[int, voice_train.PendingPaste] = (
        ctx.application.bot_data[_KEY_VOICE_TRAIN_BUFFERS]
    )
    pending = buffers.pop(chat_id, None)
    if pending is None:
        return
    if pending.flushed:
        # Another flush already ran (e.g. preempted then debounce
        # fired late) — nothing to do.
        return
    pending.flushed = True
    text = pending.assembled_text()
    log.info(
        "talker.bot.voice_train.buffer_flushed",
        chat_id=chat_id,
        kind=pending.kind,
        reason=reason,
        chunk_count=len(pending.chunks),
        total_chars=len(text),
    )
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    bot_obj = ctx.bot

    if not text or (
        config.voice_train is not None
        and len(text) < config.voice_train.min_paste_chars
    ):
        # No usable input. Per intentionally-left-blank, emit an
        # explicit reply rather than silent absence.
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=(
                    "no usable paste — buffer flushed empty. Send the "
                    "essay text after the command, or paste it as one "
                    "or more messages within the buffer window."
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.flush_empty_reply_failed",
                chat_id=chat_id,
            )
        log.info(
            "talker.bot.voice_train.no_input",
            chat_id=chat_id,
            kind=pending.kind,
            reason=reason,
        )
        return

    if pending.kind == "voice":
        await _finalize_train_paste(
            ctx,
            chat_id=chat_id,
            text=text,
            cluster=pending.cluster,
        )
    else:
        await _finalize_method_source_paste(
            ctx,
            chat_id=chat_id,
            text=text,
            image_metadata=pending.image_metadata,
        )


async def _finalize_train_paste(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    text: str,
    cluster: str | None,
) -> None:
    """Run the save_raw_essay + enqueue path on assembled text.

    Extracted from the original :func:`on_train` body — the only
    difference is the reply target uses ``ctx.bot.send_message`` (the
    flush callback runs outside the inbound-message handler so we
    don't have ``update.message`` available).
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    bot_obj = ctx.bot
    raw_result = voice_train.save_raw_essay(
        Path(config.vault.path),
        text=text,
        cluster=cluster,
        scope=_voice_train_scope_for(config),
    )
    if not raw_result.success:
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=f"couldn't save raw essay record: {raw_result.error}",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.flush_save_failed_reply_failed",
                chat_id=chat_id,
            )
        return

    queue_path = _resolve_queue_path(config)
    job = voice_train.make_job(
        kind="voice",
        raw_rel_path=raw_result.rel_path,
        raw_name=raw_result.name,
        raw_body=text,
        cluster=cluster,
        chat_id=chat_id,
        instance=config.instance.name or "",
    )
    try:
        voice_train.enqueue_job(queue_path, job)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.voice_train.enqueue_failed",
            error=str(exc),
            raw_rel_path=raw_result.rel_path,
        )
        try:
            from alfred.vault import ops as _ops
            _ops.vault_edit(
                Path(config.vault.path),
                raw_result.rel_path,
                set_fields={
                    "extraction_status": "failed",
                    "extraction_failure_reason": f"enqueue: {exc}",
                },
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.mark_failed_failed",
                rel_path=raw_result.rel_path,
            )
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=(
                    f"saved raw essay at `{raw_result.rel_path}`, but the "
                    f"extraction queue couldn't accept the job: {exc}. "
                    f"Re-run /train to retry."
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.flush_enqueue_failed_reply_failed",
                chat_id=chat_id,
            )
        return

    cluster_msg = f" (cluster: {cluster})" if cluster else ""
    # Ticket #69 — when /train fired without --cluster, append a
    # follow-up question to the success reply AND park a pending-
    # cluster ask in bot_data so the operator's next message is
    # interpreted as a cluster reply (caught in ``on_text``).
    cluster_question = ""
    if cluster is None:
        pending_asks: dict[int, dict[str, Any]] = (
            ctx.application.bot_data.setdefault(
                _KEY_VOICE_TRAIN_PENDING_CLUSTER, {},
            )
        )
        pending_asks[chat_id] = _make_cluster_ask_entry(
            raw_rel_path=raw_result.rel_path,
            raw_name=raw_result.name,
            raw_body=text,
            instance=config.instance.name or "",
        )
        cluster_question = (
            "\n\nWhich cluster? (e.g., veteran, personal, tech-essays — "
            "or `general` to skip clustering.)"
        )
        log.info(
            "talker.bot.voice_train.cluster_ask_armed",
            chat_id=chat_id,
            raw_rel_path=raw_result.rel_path,
        )
    try:
        await bot_obj.send_message(
            chat_id=chat_id,
            text=(
                f"saved `{raw_result.rel_path}`{cluster_msg} — voice "
                f"extraction queued ({len(text)} chars). I'll DM you "
                f"when the profile is ready.{cluster_question}"
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "talker.bot.voice_train.flush_ok_reply_failed",
            chat_id=chat_id,
        )


async def _finalize_method_source_paste(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    text: str,
    image_metadata: list[dict[str, Any]],
) -> None:
    """Run the save_raw_source + enqueue path on assembled text.

    Extracted from the original :func:`on_method_source` body for the
    same reason as :func:`_finalize_train_paste` — flush callback
    runs outside the inbound-message handler.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    bot_obj = ctx.bot
    raw_result = voice_train.save_raw_source(
        Path(config.vault.path),
        text=text,
        scope=_voice_train_scope_for(config),
        image_metadata=image_metadata,
    )
    if not raw_result.success:
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=f"couldn't save raw source record: {raw_result.error}",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.flush_save_failed_reply_failed",
                chat_id=chat_id,
            )
        return

    queue_path = _resolve_queue_path(config)
    job = voice_train.make_job(
        kind="method",
        raw_rel_path=raw_result.rel_path,
        raw_name=raw_result.name,
        raw_body=text,
        image_metadata=image_metadata,
        chat_id=chat_id,
        instance=config.instance.name or "",
    )
    try:
        voice_train.enqueue_job(queue_path, job)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.voice_train.enqueue_failed",
            error=str(exc),
            raw_rel_path=raw_result.rel_path,
        )
        try:
            from alfred.vault import ops as _ops
            _ops.vault_edit(
                Path(config.vault.path),
                raw_result.rel_path,
                set_fields={
                    "extraction_status": "failed",
                    "extraction_failure_reason": f"enqueue: {exc}",
                },
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.mark_failed_failed",
                rel_path=raw_result.rel_path,
            )
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=(
                    f"saved raw source at `{raw_result.rel_path}`, but "
                    f"the extraction queue couldn't accept the job: "
                    f"{exc}. Re-run /method-source to retry."
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "talker.bot.voice_train.flush_enqueue_failed_reply_failed",
                chat_id=chat_id,
            )
        return

    image_msg = " (with image reference)" if image_metadata else ""
    try:
        await bot_obj.send_message(
            chat_id=chat_id,
            text=(
                f"saved `{raw_result.rel_path}`{image_msg} — method "
                f"extraction queued ({len(text)} chars). I'll DM you "
                f"when the profile is ready."
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "talker.bot.voice_train.flush_ok_reply_failed",
            chat_id=chat_id,
        )


def _voice_train_scope_for(config: TalkerConfig) -> str:
    """Return the vault scope string for voice_train writes.

    Phase 1: hardcoded mapping by ``instance.tool_set`` so a Hypatia
    daemon writes under the ``hypatia`` scope (admits the new ``essay``
    / ``voice`` / ``voice-cluster`` / ``method`` types via
    HYPATIA_CREATE_TYPES). A future Salem/KAL-LE adoption would extend
    TALKER_CREATE_TYPES / KALLE_CREATE_TYPES with the same types and
    flip the mapping below — config-flippable, not a refactor.
    """
    tool_set = (config.instance.tool_set or "").lower()
    if tool_set == "hypatia":
        return "hypatia"
    if tool_set == "kalle":
        return "kalle"
    return "talker"


def _resolve_queue_path(config: TalkerConfig) -> Path:
    """Resolve the JSONL queue path, honouring the per-instance default.

    Defaults to ``./data/<instance-slug>/extraction_queue.jsonl`` when
    the operator hasn't set ``voice_train.queue_path`` in config. The
    per-instance subdirectory keeps Salem / Hypatia / KAL-LE queues
    isolated even when they share a working directory.
    """
    if config.voice_train is not None and config.voice_train.queue_path:
        return Path(config.voice_train.queue_path)
    instance_slug = (
        (config.instance.name or "default").strip().lower()
        .replace(" ", "-").replace(".", "")
    )
    return Path("./data") / instance_slug / "extraction_queue.jsonl"


# --- Daily Sync slash commands (email-surfacing c2) -----------------------


@owner_only
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

    user_id = (
        _entry_id(config.allowed_users[0]) if config.allowed_users else 0
    ) or 0
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
        result = await fire_once(
            ds_config, vault_path, user_id,
            manual=True, raw_config=raw_config,
        )
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


@owner_only
async def on_calibration_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/calibration_ok [tier [value]]`` — manage per-tier surfacing confidence flags.

    Usage:
      * ``/calibration_ok``               → list current flags.
      * ``/calibration_ok <tier>``        → flip the tier's flag to True.
      * ``/calibration_ok <tier> <value>`` → explicit on/off (value is
        one of: ``on``/``off``, ``true``/``false``, ``yes``/``no``,
        ``1``/``0``, ``enable``/``disable``).

    The flags are read by surfacing consumers (c3 brief section, c4
    Obsidian view, c5 Telegram push) to gate per-tier surfacing on the
    operator's explicit approval. Flipping is idempotent — calling
    ``/calibration_ok high`` when the flag is already True (or
    ``/calibration_ok high off`` when already False) is a no-op
    response that confirms the current state.

    Task #57 (2026-06-02) added the explicit-value form. c5's high-tier
    Telegram push shipped 2026-06-01 (commit ``8f01640``); operators
    routinely need to disable an active push tier mid-calibration if
    notification volume turns out wrong, so the disable path is now
    discoverable from the same surface as the enable path.
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

    # Task #57 (2026-06-02) — parse the optional third token as an
    # explicit on/off. Reject unknown tokens with a friendly reply
    # rather than silently flipping True; an operator who typed
    # ``/calibration_ok high frobnicate`` almost certainly didn't
    # intend "enable" and would otherwise be confused when the disable
    # didn't take effect.
    value_arg = parts[2].lower().strip() if len(parts) > 2 else ""
    if value_arg in _CALIBRATION_OK_DISABLE_TOKENS:
        value = False
    elif value_arg in _CALIBRATION_OK_ENABLE_TOKENS or value_arg == "":
        value = True
    else:
        await update.message.reply_text(
            f"Unknown value `{value_arg}`; use one of: "
            "on/off, true/false, yes/no, 1/0, enable/disable."
        )
        return

    try:
        flags = set_confidence(
            ds_config.state.path, arg, value, seed=ds_config.confidence,
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    # Reply-text differentiates enable vs disable so the operator can
    # see at a glance which direction the flip went. Per
    # feedback_intentionally_left_blank.md, the "no-op already in this
    # state" case is NOT silent — the report below shows the current
    # full per-tier map so an operator who re-runs the same command
    # sees the unchanged state explicitly.
    if value:
        verb = "enabled"
    else:
        verb = "disabled"
    await update.message.reply_text(
        f"`{arg}` confidence {verb}.\n\n"
        + format_confidence_report(flags)
    )


# Task #57 (2026-06-02) — value-token vocab for ``/calibration_ok <tier> <value>``.
# Module-level so tests can import + assert on coverage without
# re-deriving the list. Lower-case tokens; the handler ``lower()``s
# the operator's input before lookup.
_CALIBRATION_OK_DISABLE_TOKENS = frozenset({
    "false", "off", "0", "no", "disable", "disabled",
})
_CALIBRATION_OK_ENABLE_TOKENS = frozenset({
    "true", "on", "1", "yes", "enable", "enabled",
})


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


@owner_only
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
        # Allowlist-rejected: do NOT call ``record_handled``. The
        # message was received (TOTAL counter bumped by the pre-pass)
        # but never handled — landing it in ``inbound_unhandled``
        # surfaces a misconfigured allowlist in the heartbeat. See the
        # block comment above ``_pre_record_inbound`` for the split
        # rationale.
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
    # 2026-06-06 c1: handled-counter split. Total inbound is bumped by
    # the application-level ``_pre_record_inbound`` pre-pass (group=-1)
    # so even unrecognised commands count. THIS counter bumps the
    # "handled" half — the difference at tick time
    # (``inbound_unhandled = total - handled``) surfaces silent-drops.
    heartbeat.record_handled()

    chat_id = update.effective_chat.id if update.effective_chat else None

    # Ticket #69 — pending-cluster ask. If the previous /train (without
    # --cluster) just finished and the operator is replying with a
    # cluster name, consume the reply HERE before the buffer-append or
    # natural-language paths fire. The check is cheap (dict lookup +
    # short regex) and short-circuits a known-shape reply.
    if chat_id is not None and _looks_like_cluster_reply(text):
        entry = _consume_pending_cluster_ask(ctx, chat_id)
        if entry is not None:
            await _handle_cluster_ask_reply(ctx, chat_id, text, entry)
            return

    # Bug #58 — multi-message paste capture. If a /train or /method-source
    # buffer is currently open for this chat, the text message is part
    # of the chunked paste rather than a conversation turn. Append to
    # the buffer (which resets the debounce timer) and skip the
    # natural-language conversation path. The buffer's flush callback
    # will run save_raw + enqueue once the operator goes silent.
    if chat_id is not None and _voice_train_buffer_append(ctx, chat_id, text):
        # React with a checkmark so the operator gets a visual receipt
        # for "chunk captured." Best-effort; failure here doesn't break
        # the buffer logic.
        try:
            await _post_capture_ack(update, ctx, chat_id)
        except Exception:  # noqa: BLE001
            log.debug(
                "talker.bot.voice_train.buffer_ack_failed",
                chat_id=chat_id,
            )
        return

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
        # Allowlist-rejected: do NOT call ``record_handled`` — see
        # the on_text counter-site comment for the unhandled-bucket
        # rationale.
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
    # 2026-06-06 c1: handled-counter split — see on_text counter site.
    heartbeat.record_handled()

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


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo message entry point — gate, download, save, dispatch.

    Mirrors the ``on_voice`` shape: allowlist check, log inbound, fetch
    bytes, then hand off to :func:`handle_message` with the multimodal
    ``image_blocks`` list. Per-instance ``vision.enabled`` gates the
    whole path so a PHI-sensitive instance with vision disabled never
    downloads, never persists, and never reaches Anthropic — the user
    gets the configured ``disabled_reply`` text instead.

    Caption text (or a default placeholder when the user sent the photo
    bare) becomes the user-message text; the image block(s) are paired
    with it via ``vision.build_user_content`` inside ``run_turn``.

    Per ``feedback_intentionally_left_blank.md`` — every failure path
    here emits an explicit user-facing reply, never silent drop.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            kind="photo",
        )
        # Allowlist-rejected: do NOT call ``record_handled`` — see
        # the on_text counter-site comment for the unhandled-bucket
        # rationale.
        return
    if update.message is None or not update.message.photo:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    photo_count = len(update.message.photo)
    log.info(
        "talker.bot.inbound",
        chat_id=chat_id,
        user_id=update.effective_user.id if update.effective_user else None,
        kind="photo",
        photo_sizes=photo_count,
        has_caption=bool(update.message.caption),
    )
    # 2026-06-06 c1: handled-counter split. Placed BEFORE the
    # vision-disabled check because a vision-disabled reply still
    # counts as handled — the message routed correctly, the handler
    # replied, the only difference vs. success is the feature is off.
    # Per Andrew (decision 2026-06-06): vision-disabled = handled. The
    # operationally-meaningful definition of "handled" is "routed to a
    # registered handler and replied to the user," not "produced a
    # successful LLM turn."
    heartbeat.record_handled()

    # Vision-disabled gate. ``vision.enabled=false`` (or ``vision``
    # missing in config — defaulted-on dataclass keeps it true) replies
    # with the configured user-visible text so the user knows the photo
    # was received and ignored, not silently dropped.
    if not config.vision.enabled:
        log.info(
            "talker.bot.vision_disabled_drop",
            chat_id=chat_id,
            instance=config.instance.name,
        )
        await update.message.reply_text(config.vision.disabled_reply)
        return

    # Pick the largest resolution and download bytes. Wrapped in one
    # try/except so any download / decoding failure produces one clear
    # user-facing reply instead of a stack trace.
    try:
        chosen = vision.select_largest_photo(list(update.message.photo))
        image_bytes = await vision.download_photo_bytes(chosen)
    except vision.VisionDownloadError as exc:
        log.warning("talker.bot.photo_download_failed", error=str(exc))
        await update.message.reply_text(
            "sorry, couldn't fetch the screenshot — try sending it again?"
        )
        return

    # Persist to <vault>/inbox/ for audit trail. Persistence failure is
    # treated as recoverable: the model can still see the image (we have
    # the bytes in memory). We log the failure and continue rather than
    # block the conversation — the user-visible behaviour (Andrew gets a
    # reply about his screenshot) matters more than the audit trail
    # being complete on every single message.
    file_unique_id = getattr(chosen, "file_unique_id", "") or ""
    saved_path: str | None = None
    try:
        saved = vision.save_image_to_inbox(
            image_bytes,
            config.vault.path,
            file_unique_id,
        )
        saved_path = str(saved)
    except Exception as exc:  # noqa: BLE001
        # Per ``feedback_intentionally_left_blank.md`` + the universal
        # "intentionally left blank" rule in CLAUDE.md: the decision NOT
        # to abort the conversation lives in the code (we fall through
        # below) but must also live in the LOG so an operator tailing
        # ``data/talker.log`` can grep ``action=continuing_to_llm_in_memory_only``
        # and see the policy without re-reading source. Without this
        # field the log line answers "save failed" but not "did the
        # turn proceed?" — which is the question that matters when
        # diagnosing a complaint like "Hypatia ignored my screenshot."
        log.warning(
            "talker.bot.photo_save_failed",
            error=str(exc),
            vault_path=config.vault.path,
            action="continuing_to_llm_in_memory_only",
        )
        # Continue — image is still in memory and will reach the model.

    # Build the Anthropic content block from the in-memory bytes.
    image_block = vision.build_image_block(image_bytes)

    # Caption text: use the user's caption verbatim, or a neutral
    # placeholder when none was provided. The placeholder is short and
    # explicit — better than feeding the model an empty string (which
    # Anthropic accepts but produces meandering replies).
    caption = (update.message.caption or "").strip()
    if not caption:
        caption = "(image attached, no caption)"

    await handle_message(
        update, ctx,
        text=caption,
        voice=False,
        image_blocks=[image_block],
        image_metadata=[{
            "path": saved_path,
            "file_unique_id": file_unique_id,
            "bytes": len(image_bytes),
        }] if saved_path else [],
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Document message entry point — MIME-gate, download, extract, dispatch.

    Parallel to :func:`on_photo` for Telegram ``document`` updates
    (PDFs, .docx, .txt/.md, .csv, .ics, and audio files attached as
    files rather than as photos / voice notes). The handler:

    1. Allowlist-gates the user (same shape as photo / voice / text).
    2. Looks up the MIME in :data:`attachments.SUPPORTED_DOCUMENT_MIME`
       to derive the kind tag (``pdf``, ``docx``, ``text``, ``csv``,
       ``ics``, ``audio``). Unknown MIMEs get an explicit user-facing
       reply listing every supported type — per
       ``feedback_intentionally_left_blank.md``, silent filter-drop on
       a non-supported document is the same bug class this commit
       closes for previously-unhandled types.
    3. Size-gates against the kind's entry in
       :data:`attachments.MAX_BYTES_BY_KIND`.
    4. Downloads bytes via :func:`attachments.download_document_bytes`.
    5. Dispatches to the kind's extractor:
       :func:`attachments.extract_pdf_text` / ``extract_docx_text`` /
       ``extract_text_decoded`` / ``extract_csv_text`` /
       ``extract_ics_text`` / ``extract_audio_transcript`` (async).
       Each extractor handles its own missing-dep case via lazy
       imports + :class:`AttachmentExtractError`, so a non-``[voice]``
       install gets clean user-facing replies rather than daemon
       crashes.
    6. Persists to ``<vault>/inbox/`` for audit. Audio uses the
       ``audio-`` prefix (via :func:`attachments.save_audio_to_inbox`);
       everything else uses ``document-`` (via
       :func:`attachments.save_document_to_inbox`). Persistence
       failure is treated as non-fatal: the model still sees the
       extracted text; only the audit trail is incomplete.
    7. Composes the user-message text via
       :func:`attachments.build_document_user_text` (with the kind
       tag for the right banner + fence label) and dispatches through
       :func:`handle_message`.

    Documents are NOT gated on ``vision.enabled`` — vision is an
    image-specific feature flag; documents / audio go through
    separate extraction paths that are always-on if the relevant
    backing libs are installed.

    Every failure mode produces an explicit user-facing reply — never
    silent drop, per ``feedback_intentionally_left_blank.md``.

    2026-06-06 P8: extended from PDF-only (c1 / 8ac333b) to cover
    five additional kinds per ``feedback_universal_filetype_support.md``
    (operator-ratified). Single dispatch table
    (:data:`attachments.SUPPORTED_DOCUMENT_MIME`) — no per-instance
    config gate.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    if not _is_allowed(update, config):
        log.info(
            "talker.bot.unauthorized",
            user_id=update.effective_user.id if update.effective_user else None,
            kind="document",
        )
        # Allowlist-rejected: do NOT call ``record_handled`` — see
        # the on_text counter-site comment for the unhandled-bucket
        # rationale.
        return
    if update.message is None or update.message.document is None:
        return

    document = update.message.document
    chat_id = update.effective_chat.id if update.effective_chat else None
    mime_type = document.mime_type or ""
    file_name = document.file_name or ""
    file_size = document.file_size or 0
    log.info(
        "talker.bot.inbound",
        chat_id=chat_id,
        user_id=update.effective_user.id if update.effective_user else None,
        kind="document",
        mime_type=mime_type,
        file_name=file_name,
        file_size=file_size,
        has_caption=bool(update.message.caption),
    )
    # 2026-06-06 c1: handled-counter split. Placed BEFORE the MIME /
    # size gates because rejecting an unsupported type or oversized
    # file still counts as handled — the message routed, the user got
    # a reply. "Handled" means "reached a registered handler and
    # produced a user-visible outcome," not "produced a successful
    # LLM turn."
    heartbeat.record_handled()

    # MIME allowlist gate via the kind-tag dispatch table. Unknown
    # MIME → user-facing reply listing every supported type. The
    # supported-types string is DERIVED from
    # :data:`attachments.SUPPORTED_DOCUMENT_MIME` at module-load time
    # (via ``attachments._supported_types_human()``), so a future c2
    # widening updates the user-facing text by extending the constant
    # — no scattered string-literal sweep.
    kind = attachments.SUPPORTED_DOCUMENT_MIME.get(mime_type)
    if kind is None:
        log.info(
            "talker.bot.document_unsupported_mime",
            chat_id=chat_id,
            mime_type=mime_type,
            file_name=file_name,
        )
        await update.message.reply_text(
            f"I can read {attachments._supported_types_human()}. "
            f"Got {mime_type or 'unknown type'}. "
            "Forward as a photo or paste the text and I can help."
        )
        return

    # Per-kind size gate. Each kind has its own cap (PDF / DOCX 10 MiB,
    # text / CSV 5 MiB, ICS 1 MiB, audio 25 MiB — the Groq Whisper
    # sync-endpoint cap). Cap value comes from
    # :data:`attachments.MAX_BYTES_BY_KIND` so the on_document handler
    # doesn't carry six per-kind constants. ``file_size`` may be 0 /
    # missing for some Telegram clients (rare); only enforce when
    # present.
    cap_bytes = attachments.MAX_BYTES_BY_KIND[kind]
    if file_size and file_size > cap_bytes:
        size_mb = file_size / (1024 * 1024)
        limit_mb = cap_bytes / (1024 * 1024)
        log.info(
            "talker.bot.document_oversized",
            chat_id=chat_id,
            kind=kind,
            file_size=file_size,
            limit=cap_bytes,
            file_name=file_name,
        )
        await update.message.reply_text(
            f"That file is {size_mb:.1f} MB — bigger than my "
            f"{limit_mb:.0f} MB limit for {kind} files. "
            "Can you trim it or share a shorter excerpt?"
        )
        return

    # Download bytes. Distinct error class from the extract failure
    # below so the user-facing reply can be more specific.
    try:
        raw_bytes = await attachments.download_document_bytes(document)
    except attachments.AttachmentDownloadError as exc:
        log.warning("talker.bot.document_download_failed", error=str(exc))
        await update.message.reply_text(
            f"sorry, couldn't fetch your {kind} file — try sending it again?"
        )
        return

    # Per-kind extraction dispatch. Each branch returns the extracted
    # text (or raises :class:`AttachmentExtractError`). Audio is the
    # only async branch (it awaits the Groq Whisper HTTP call); the
    # rest are sync. Wrapping in a single try / except keeps the
    # error-handling tidy — the user-facing reply pulls the kind
    # name + the exception message so the user sees actionable
    # detail ("couldn't read your DOCX — Failed to open ...").
    try:
        if kind == "pdf":
            extracted_text = attachments.extract_pdf_text(raw_bytes)
        elif kind == "docx":
            extracted_text = attachments.extract_docx_text(raw_bytes)
        elif kind == "text":
            extracted_text = attachments.extract_text_decoded(raw_bytes)
        elif kind == "csv":
            extracted_text = attachments.extract_csv_text(raw_bytes)
        elif kind == "ics":
            extracted_text = attachments.extract_ics_text(raw_bytes)
        elif kind == "audio":
            extracted_text = await attachments.extract_audio_transcript(
                raw_bytes, mime_type, config.stt,
            )
        else:
            # Defensive — the MIME-allowlist check above should have
            # rejected anything not in this dispatch tree. If a
            # future commit adds a MIME to ``SUPPORTED_DOCUMENT_MIME``
            # without adding the matching extractor branch, fail loud
            # rather than silently.
            log.error(
                "talker.bot.document_kind_no_extractor",
                kind=kind,
                mime_type=mime_type,
            )
            raise attachments.AttachmentExtractError(
                f"No extractor registered for kind {kind!r}"
            )
    except attachments.AttachmentExtractError as exc:
        log.warning(
            "talker.bot.document_extract_failed",
            error=str(exc),
            kind=kind,
            file_name=file_name,
        )
        await update.message.reply_text(
            f"sorry, couldn't read your {kind} file — {exc!s}."
        )
        return

    # Persist to ``<vault>/inbox/`` for audit. Audio uses the audio-
    # storage path (distinct ``audio-`` filename prefix); everything
    # else uses the document-storage path (``document-`` prefix). The
    # extension is derived from the kind + MIME so .m4a stays .m4a,
    # .ogg stays .ogg, etc. Persistence failure is treated as
    # recoverable (mirror of the on_photo save path): extracted text
    # is still in memory and will reach the model. We log the failure
    # with ``action=continuing_to_llm_in_memory_only`` so an operator
    # tailing the log can grep that field and see the policy decision
    # without re-reading source.
    file_unique_id = getattr(document, "file_unique_id", "") or ""
    extension = attachments.extension_for_kind(kind, mime_type)
    saved_path: str | None = None
    try:
        if kind == "audio":
            saved = attachments.save_audio_to_inbox(
                raw_bytes,
                config.vault.path,
                file_unique_id,
                extension=extension,
            )
        else:
            saved = attachments.save_document_to_inbox(
                raw_bytes,
                config.vault.path,
                file_unique_id,
                extension=extension,
            )
        saved_path = str(saved)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.bot.document_save_failed",
            error=str(exc),
            kind=kind,
            vault_path=config.vault.path,
            action="continuing_to_llm_in_memory_only",
        )
        # Continue — extracted text still reaches the model.

    # Compose the user-message text: kind-specific header banner +
    # optional caption + fenced extracted text. The kind tag picks
    # the right banner ("PDF attached" / "Audio transcript" / etc.)
    # and fence label ("Document text" / "Events" / "Transcript") so
    # the LLM sees the attachment shape for what it is.
    caption = (update.message.caption or "").strip()
    user_text = attachments.build_document_user_text(
        caption=caption,
        extracted_text=extracted_text,
        filename=file_name or f"document.{extension}",
        kind=kind,
    )

    await handle_message(
        update, ctx,
        text=user_text,
        voice=False,
        document_metadata=[{
            "path": saved_path,
            "file_unique_id": file_unique_id,
            "bytes": len(raw_bytes),
            "filename": file_name or f"document.{extension}",
            "mime_type": mime_type,
            "kind": kind,
        }] if saved_path else [],
    )


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

# Bleed-stop §2: how long a stashed ``_peer_route_target`` keeps
# auto-forwarding follow-up turns to the same peer WITHOUT re-classifying.
# After this window the next turn re-enters the router instead of blindly
# force-forwarding (the 2026-06-16 swallowing bug). A rapid back-and-forth
# with KAL-LE stays inside the window and stays sticky; a topic switch a
# couple of minutes later re-classifies. Reply-anchored continuation (a
# Telegram reply to a ``[KAL-LE] …`` relay) bypasses the TTL — an explicit
# reply is an unambiguous "still talking to the peer" signal regardless of
# elapsed time. 90s is the ratified value (design §2 / operator Q2).
_PEER_ROUTE_STICKINESS_TTL_SECONDS: float = 90.0

# Bleed-stop §2: the prefix every relayed peer reply carries
# (``[{target.upper()}] …`` — see ``_dispatch_peer_route``). Used to detect
# "this turn is a reply to a peer relay" for reply-anchored stickiness. The
# leading ``[`` + uppercase token + ``] `` shape is matched generically so
# any peer target (KAL-LE, STAY-C, …) qualifies without enumerating them.
_PEER_RELAY_REPLY_RE = re.compile(r"^\[[A-Z][A-Z0-9.\-]*\]\s")


def _is_reply_to_peer_relay(reply_to_message: Any) -> bool:
    """Return True iff the replied-to message is a ``[PEER] …`` peer relay.

    Bleed-stop §2 reply-anchored stickiness: when Andrew long-presses a
    relayed ``[KAL-LE] <reply>`` message and hits Reply, that turn is an
    unambiguous continuation of the peer conversation — keep auto-forwarding
    regardless of the TTL window. Generic over the peer name via
    :data:`_PEER_RELAY_REPLY_RE` (``[KAL-LE] ``, ``[STAY-C] ``, etc.).

    Tolerant of the MagicMock / missing-attribute shapes the test harness
    and photo-only replies produce — returns ``False`` rather than raising
    when the parent has no usable text (mirrors
    ``_build_reply_context_prefix``'s defensive ``getattr`` reads).
    """
    if reply_to_message is None:
        return False
    raw_text = getattr(reply_to_message, "text", None)
    if not isinstance(raw_text, str) or not raw_text:
        return False
    return bool(_PEER_RELAY_REPLY_RE.match(raw_text))


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
        config.instance.name or config.instance.canonical or "",
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
        # Issue #62: clear the peer-route stash so subsequent messages
        # in this session don't keep hitting the same dead peer. The
        # ``peer_unavailable`` branch above returns False and the
        # caller pops the stash via fall-through (see ``handle_message``
        # right after the dispatch); ``peer_route_failed`` returns True
        # (error surfaced), so we have to pop inline. Mirrors that
        # caller-side cleanup pattern.
        try:
            state_mgr_ref: StateManager | None = (
                ctx.application.bot_data.get(_KEY_STATE)
            )
        except Exception:  # noqa: BLE001 — defensive; bot_data must be a dict
            state_mgr_ref = None
        if state_mgr_ref is not None:
            active = state_mgr_ref.get_active(chat_id) or {}
            if active.pop("_peer_route_target", None) is not None:
                state_mgr_ref.set_active(chat_id, active)
                state_mgr_ref.save()
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


def _instance_peer_targets(
    raw_config: dict | None, self_name: str,
) -> set[str] | None:
    """Return the set of peer-key names THIS instance can route to.

    Issue #62: Hypatia's classifier emitted ``peer_route target=kal-le``
    on a vault-cleanup message, but Hypatia's ``transport.peers`` is
    ``[local, salem]`` — kal-le is not a configured peer. The router
    used to validate against a hardcoded global set of peer names, so
    the bogus target sailed past the gate and the bot tried to route to
    a peer it had no transport entry for. This helper sources the
    truth from the SAME config the actual transport dispatcher uses
    (``transport.peers`` keys), drops ``local`` (self alias) and the
    instance's own normalised name, and returns the residual set for
    the router to validate against.

    Returns ``None`` when the raw config is missing or the peers
    section is empty — the router falls back to its hardcoded global
    set in that case, preserving the legacy behaviour for installs
    that haven't configured ``transport.peers`` at all (single-
    instance default).
    """
    if not raw_config:
        return None
    transport_section = raw_config.get("transport") or {}
    peers_section = transport_section.get("peers") or {}
    if not isinstance(peers_section, dict) or not peers_section:
        return None
    targets: set[str] = set()
    for raw_name in peers_section.keys():
        normalised = _normalize_instance_name(str(raw_name))
        if not normalised:
            continue
        if normalised in {"local", self_name}:
            continue
        targets.add(normalised)
    return targets


async def _open_routed_session(
    state_mgr: StateManager,
    config: TalkerConfig,
    client: Any,
    chat_id: int,
    first_message: str,
    has_reply_context: bool = False,
    valid_peer_targets: set[str] | None = None,
    force_local_note: bool = False,
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

    ``valid_peer_targets`` (issue #62): the per-instance peer-key set
    the classifier should validate ``peer_route target=...`` against.
    Computed by the caller from ``transport.peers`` via
    :func:`_instance_peer_targets`. ``None`` falls back to the router's
    hardcoded global set — the safe default for tests and any caller
    that doesn't have ``raw_config`` in scope.

    ``force_local_note`` (peer-route bleed-stop §1a): when ``True``, skip
    the LLM classifier entirely and open a plain ``note`` session. The
    caller sets this when the deterministic confirm-guard
    (:func:`router.is_brief_or_status_confirm`) matched — a morning-brief
    / status confirmation must NEVER peer-route, so we don't even give the
    probabilistic router the chance to mis-classify it. Continuation
    pre-seeding is skipped too (a bare confirm isn't a continuation cue).
    Defaults to ``False`` — normal routing for every non-confirm message.
    """
    if force_local_note:
        # Deterministic confirm-guard short-circuit: no router call, no
        # peer-route possible. Open a note session on the note default
        # model. The caller has already emitted the ILB guard log with the
        # matched pattern; this path just opens the local session.
        type_defaults = session_types.defaults_for("note")
        sess = _open_session_with_stash(
            state_mgr,
            chat_id,
            config,
            model=type_defaults.model,
            session_type="note",
            continues_from=None,
            pushback_level=type_defaults.pushback_level,
        )
        log.info(
            "talker.bot.routed_open",
            chat_id=chat_id,
            session_type="note",
            model=type_defaults.model,
            continues=False,
            forced_local_note=True,
        )
        return sess

    recent = _recent_sessions_for_router(state_mgr)
    # Stage 3.5 hotfix c3: thread the local instance identity into the
    # router so the classifier knows who it is and can't route to self.
    # Uses the same name-first convention the self-target guard in
    # ``_dispatch_peer_route`` uses — see ``_normalize_instance_name``.
    self_name = _normalize_instance_name(
        config.instance.name or config.instance.canonical or "",
    )
    self_display_name = (
        config.instance.canonical or config.instance.name or ""
    )
    decision = await router.classify_opening_cue(
        client, first_message, recent,
        self_name=self_name,
        self_display_name=self_display_name,
        has_reply_context=has_reply_context,
        valid_peer_targets=valid_peer_targets,
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
    # subsequent turns can forward to the same peer.
    # ``_dispatch_peer_route`` reads this in ``handle_message`` before it
    # falls into the Anthropic turn.
    #
    # Bleed-stop §2: also stash the open timestamp. The auto-forward is no
    # longer unconditional-for-the-session-lifetime (the bug that swallowed
    # the 2026-06-16 follow-up messages). It now persists only within a
    # short TTL window (``_PEER_ROUTE_STICKINESS_TTL_SECONDS``) OR while the
    # turn is an explicit reply to a ``[KAL-LE] …`` relay — outside that,
    # the next turn re-enters the router. The timestamp is the TTL anchor.
    if decision.session_type == "peer_route" and decision.target:
        active["_peer_route_target"] = decision.target
        active["_peer_route_target_ts"] = (
            datetime.now(timezone.utc).timestamp()
        )
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
    instance_name = ""
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
            from alfred.audit import agent_slug_for
            instance_name = agent_slug_for(talker_config)
        except Exception as exc:  # noqa: BLE001
            log.warning("talker.bot.agent_slug_for_failed", error=str(exc))
            instance_name = ""

    try:
        result = handle_daily_sync_reply(
            ds_config,
            parent_msg_id,
            user_text,
            vault_path=vault_path,
            instance_scope=instance_scope,
            instance_name=instance_name,
            raw_config=raw_config,
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
    instance_name = ""
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
            from alfred.audit import agent_slug_for
            instance_name = agent_slug_for(talker_config)
        except Exception as exc:  # noqa: BLE001
            log.warning("talker.bot.agent_slug_for_failed", error=str(exc))
            instance_name = ""

    try:
        result = maybe_smart_route_reply(
            ds_config,
            user_text,
            vault_path=vault_path,
            instance_scope=instance_scope,
            instance_name=instance_name,
            raw_config=raw_config,
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
    image_blocks: list[dict[str, Any]] | None = None,
    image_metadata: list[dict[str, Any]] | None = None,
    document_metadata: list[dict[str, Any]] | None = None,
) -> None:
    """Shared pipeline — open/reuse session, run Anthropic turn, reply.

    Serialises calls per chat_id via a shared asyncio.Lock: two messages
    from the same chat must not hit Anthropic in parallel because they'd
    race on the session transcript and double-increment counters.

    ``image_blocks`` (vision phase 2) is a list of pre-built Anthropic
    image content blocks. When non-empty, the user turn is composed as
    a multimodal content list (image-then-text) and threaded through
    ``run_turn`` via the ``image_blocks`` kwarg. ``image_metadata`` is
    the parallel list of ``{path, file_unique_id, bytes}`` dicts; we
    record one ``append_image`` row per entry against the active session
    so the saved-file paths land on the session-record frontmatter at
    close time. ``None`` / empty preserves the wk1 single-modal flow
    byte-for-byte.

    ``document_metadata`` (2026-06-06 c1: PDF document handler) is the
    parallel list for document attachments —
    ``{path, file_unique_id, bytes, filename, mime_type}`` dicts. Unlike
    images, documents do NOT produce a separate content block: the
    extracted text is inlined into the ``text`` argument upstream by
    :func:`on_document` so the model sees it as part of the user
    message. ``document_metadata`` is the audit trail (saved path +
    Telegram metadata) that lands on the session-record frontmatter via
    one :func:`append_document` row per entry. ``None`` / empty
    preserves the existing flow byte-for-byte.
    """
    config: TalkerConfig = ctx.application.bot_data[_KEY_CONFIG]
    state_mgr: StateManager = ctx.application.bot_data[_KEY_STATE]
    client: Any = ctx.application.bot_data[_KEY_CLIENT]
    # _KEY_SYSTEM holds whatever the wiring layer stashed there — typically
    # a zero-arg provider callable (per ``build_app``'s normalisation;
    # invoked per-turn so SKILL.md edits on disk take effect on the next
    # user turn without daemon restart, closing the same-cycle SKILL ship
    # gap from QA 2026-05-04). Older callers / direct test fixtures may
    # still stash a plain string. ``_resolve_system_prompt`` accepts
    # both shapes so the read site mirrors ``build_app``'s input contract
    # — string OR callable — without forcing every test fixture to wrap
    # its static prompt in a lambda.
    system_prompt: str = _resolve_system_prompt(
        ctx.application.bot_data[_KEY_SYSTEM]
    )
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
        # Issue #62: compute the per-instance peer-target set ONCE here
        # so both router branches (rehydrate-failure + fresh-session)
        # validate against the same per-instance acceptance set. Sourced
        # from ``transport.peers`` via the same raw_config the dispatch
        # path uses; ``None`` if peers aren't configured (single-instance
        # default), which lets the router fall back to its global set.
        _self_name_for_peers = _normalize_instance_name(
            config.instance.name or config.instance.canonical or "",
        )
        _instance_peers = _instance_peer_targets(
            ctx.application.bot_data.get("raw_config"),
            _self_name_for_peers,
        )

        # Peer-route bleed-stop §1a: deterministic confirm-guard. A
        # morning-brief / status confirmation ("T1 confirm", "T2 add ...",
        # bare "done"/"confirmed", etc.) must NEVER peer-route — the
        # 2026-06-16 incident mis-routed exactly such a message to KAL-LE.
        # Computed ONCE here, against the ORIGINAL ``text`` (NOT
        # ``effective_text``): a reply-context prefix would push the confirm
        # verb past the start-anchor and break the match, same reason the
        # inline-command detector above runs against original ``text``.
        # ``confirm_guard_match`` is the matched-pattern label or ``None``;
        # it (a) forces local handling on a fresh open and (b) diverts the
        # active-session force-forward below. Emit the ILB log with the
        # matched pattern (self-correcting correction-signal substrate —
        # CLAUDE.md design standard).
        confirm_guard_match = router.is_brief_or_status_confirm(text)
        if confirm_guard_match is not None:
            log.info(
                "talker.router.confirm_guard_forced_local",
                chat_id=chat_id,
                matched_pattern=confirm_guard_match,
            )

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
                # ``force_local_note`` from the confirm-guard wins over the
                # router even on this path.
                sess = await _open_routed_session(
                    state_mgr, config, client, chat_id, effective_text,
                    has_reply_context=has_reply_context,
                    valid_peer_targets=_instance_peers,
                    force_local_note=confirm_guard_match is not None,
                )
        else:
            # No active session — the router runs. When this message is a
            # reply to a prior bot message we flag ``has_reply_context``
            # so the classifier tips toward continuation / note rather
            # than opening a fresh capture / journal / article session on
            # what is almost certainly a follow-up to existing context.
            # The confirm-guard (§1a) bypasses the router entirely when it
            # matched — a confirm can never open a peer_route session.
            sess = await _open_routed_session(
                state_mgr, config, client, chat_id, effective_text,
                has_reply_context=has_reply_context,
                valid_peer_targets=_instance_peers,
                force_local_note=confirm_guard_match is not None,
            )

        # Stage 3.5 peer-route flow + bleed-stop §2. When
        # ``_peer_route_target`` is stashed on the active dict we MAY
        # forward this turn to the named peer — but no longer
        # unconditionally-for-the-session-lifetime (the 2026-06-16 bug that
        # swallowed two follow-up confirms). The decision now has three
        # gates, in order:
        #   1. Confirm-guard short-circuit (§2 step 1+3): if THIS turn is a
        #      brief/status confirm, divert to local AND clear the stash
        #      (close-on-topic-change) — the operator changed topic, the
        #      peer session shouldn't keep swallowing.
        #   2. TTL / reply-anchored window (§2 step 2): force-forward only
        #      within ``_PEER_ROUTE_STICKINESS_TTL_SECONDS`` of the stash,
        #      OR when this turn is an explicit reply to a ``[PEER] …``
        #      relay. Outside that window → stop forwarding, handle locally
        #      (the next opening cue re-routes normally).
        #   3. Unreachable-peer fall-through (existing): a failed dispatch
        #      clears the stash so we don't keep hitting a dead peer.
        # Auto-forward also stops on ``/end`` (session close clears the
        # target).
        active_now = state_mgr.get_active(chat_id) or {}
        peer_target = active_now.get("_peer_route_target")
        if peer_target and confirm_guard_match is not None:
            # §2 step 1+3: a confirm must never be swallowed by an open
            # peer session. Divert locally + clear the stash so the topic
            # switch sticks. Emit BOTH ILB signals (divert + clear) with
            # the matched pattern — correction-signal substrate.
            log.info(
                "talker.bot.peer_route_followup_diverted_to_local",
                chat_id=chat_id,
                matched_pattern=confirm_guard_match,
                target=peer_target,
            )
            active_now.pop("_peer_route_target", None)
            active_now.pop("_peer_route_target_ts", None)
            state_mgr.set_active(chat_id, active_now)
            state_mgr.save()
            log.info(
                "talker.bot.peer_route_target_cleared_on_topic_change",
                chat_id=chat_id,
                matched_pattern=confirm_guard_match,
                target=peer_target,
            )
            # Fall through to local handling (the Anthropic turn below).
        elif peer_target:
            # §2 step 2: TTL / reply-anchored stickiness gate.
            stashed_ts = active_now.get("_peer_route_target_ts")
            now_ts = datetime.now(timezone.utc).timestamp()
            within_ttl = (
                isinstance(stashed_ts, (int, float))
                and (now_ts - stashed_ts) <= _PEER_ROUTE_STICKINESS_TTL_SECONDS
            )
            reply_anchored = _is_reply_to_peer_relay(
                update.message.reply_to_message
            )
            if not (within_ttl or reply_anchored):
                # Window expired and not an explicit peer-reply — stop
                # force-forwarding. Clear the stash and handle THIS turn
                # locally; a genuine new peer request will re-route via the
                # normal opening-cue path. ILB so the expiry is observable.
                log.info(
                    "talker.bot.peer_route_stickiness_expired",
                    chat_id=chat_id,
                    target=peer_target,
                    age_seconds=(
                        round(now_ts - stashed_ts, 1)
                        if isinstance(stashed_ts, (int, float))
                        else None
                    ),
                )
                active_now.pop("_peer_route_target", None)
                active_now.pop("_peer_route_target_ts", None)
                state_mgr.set_active(chat_id, active_now)
                state_mgr.save()
                # Fall through to local handling.
            else:
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
                    # Refresh the TTL anchor on a successful forward so an
                    # active back-and-forth stays sticky turn-over-turn
                    # (each forwarded turn re-opens the window).
                    active_now["_peer_route_target_ts"] = now_ts
                    state_mgr.set_active(chat_id, active_now)
                    state_mgr.save()
                    return  # Peer path completed (with reply or timeout).
                # Fall-through: peer was unreachable. Clear the target so
                # subsequent turns don't keep hitting a dead peer, and let
                # Salem handle the turn normally.
                active_now.pop("_peer_route_target", None)
                active_now.pop("_peer_route_target_ts", None)
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

        # Vision: record image-attachment metadata against the session
        # BEFORE ``run_turn`` is called. ``append_image`` uses
        # ``len(transcript)`` as the would-be turn_index, which will
        # match the user turn that ``run_turn`` is about to append next.
        # Doing this before the LLM call means the saved-path lands in
        # state even if the API call fails — the audit trail is preserved.
        if image_metadata:
            for meta in image_metadata:
                if not meta.get("path"):
                    continue
                append_image(
                    state_mgr,
                    sess,
                    path=meta["path"],
                    file_unique_id=meta.get("file_unique_id", ""),
                    bytes_size=int(meta.get("bytes", 0) or 0),
                )

        # 2026-06-06 c1: same shape as ``image_metadata`` above, for
        # document attachments. Records each saved-document row against
        # the session via :func:`append_document` BEFORE ``run_turn`` so
        # the audit trail is preserved even when the LLM call fails.
        # ``turn_index`` semantics match :func:`append_image` — the
        # would-be position of the next user turn.
        if document_metadata:
            for meta in document_metadata:
                if not meta.get("path"):
                    continue
                append_document(
                    state_mgr,
                    sess,
                    path=meta["path"],
                    file_unique_id=meta.get("file_unique_id", ""),
                    bytes_size=int(meta.get("bytes", 0) or 0),
                    filename=meta.get("filename", ""),
                    mime_type=meta.get("mime_type", ""),
                    # P8: kind tag from
                    # :data:`attachments.SUPPORTED_DOCUMENT_MIME.values()`.
                    # Defaults to "pdf" via append_document's signature
                    # so pre-P8 call sites (none currently in tree, but
                    # belt-and-braces against a future caller) still
                    # work.
                    kind=meta.get("kind", "pdf"),
                )

        # VERA MVP (2026-06-09): resolve the sending user's role so the
        # vault-tool dispatcher (``_execute_tool`` → ``resolve_scope``)
        # routes ops users to ``vera_ops`` and owners to ``vera``. On every
        # single-role instance this is always ``"owner"`` (flat allowlist
        # normalizes to owner), so the threaded value is inert there.
        user_role = _role_for(update, config)
        # VERA reporter follow-up (2026-06-09): resolve the sending user's
        # display name so the agent can attribute per-message authorship
        # (e.g. set a ticket ``reporter`` from the actual sender). ``None``
        # on every single-user / flat-list instance (no name configured),
        # so the sender-identity block in run_turn is omitted there —
        # byte-identical behaviour for Salem / KAL-LE / Hypatia.
        user_name = _name_for(update, config)
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
                image_blocks=image_blocks,
                user_role=user_role,
                user_name=user_name,
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

        # Reply with Claude's text. Telegram's per-message limit is 4096
        # characters; we chunk above 3900 to leave headroom for MarkdownV2
        # escapes and rendering quirks. See ``_send_outbound_chunked`` for
        # the full layered failure path (chunk → user-visible alert →
        # session-record annotation).
        if not response_text:
            response_text = "(no response generated)"
        await _send_outbound_chunked(
            update=update,
            state_mgr=state_mgr,
            session=sess,
            chat_id=chat_id,
            response_text=response_text,
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
