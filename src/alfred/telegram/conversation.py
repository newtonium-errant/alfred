"""Anthropic turn + tool-use loop for the talker.

Responsibilities:
    * Hold the 4 vault-bridge tool schemas exposed to the model.
    * Run one user-turn through ``client.messages.create`` with prompt caching
      (system + vault-context as two cache breakpoints).
    * Dispatch each ``tool_use`` block through the scope-enforced vault ops
      bridge, feed results back as a ``tool_result`` user message, and loop
      until the model emits ``end_turn``.
    * Append every turn to the session transcript and record vault mutations.

This module is deliberately Telegram-agnostic: it takes a pre-built vault
context string from the caller (bot.py in commit 4) and surfaces errors as
exceptions. The Telegram layer handles rate-limit translation and user replies.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Final

import anthropic

from .config import TalkerConfig
from .session import Session, append_turn, append_vault_op
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


def _json_default(obj: Any) -> Any:
    """Fallback for ``json.dumps`` — handle ``date``/``datetime`` cleanly.

    Vault frontmatter routinely contains ``date`` values (``created``,
    ``due``), and ``json.dumps`` chokes on them without this hook.
    """
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


# --- Tool surface ---------------------------------------------------------

# Kept narrow for wk1 — the ``talker`` scope in vault/scope.py allows more
# record types (``TALKER_CREATE_TYPES``) than we expose here. The Python
# layer will still refuse anything outside the scope set even if the prompt
# is later loosened; this enum just keeps the LLM on rails for MVP.
VAULT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "vault_search",
        "description": (
            "Search the vault. Pass ``glob`` (e.g. ``project/*.md``) or "
            "``grep`` (substring) or both. Returns a list of "
            "``{path, name, type, status}`` dicts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {
                    "type": "string",
                    "description": "Glob pattern relative to the vault root.",
                },
                "grep": {
                    "type": "string",
                    "description": "Case-insensitive substring to match in file content.",
                },
            },
        },
    },
    {
        "name": "vault_read",
        "description": (
            "Read a single vault record. Returns ``{path, frontmatter, body}``."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path, e.g. ``project/Alfred.md``.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "vault_create",
        "description": (
            "Create a new vault record. Use when the user explicitly asks to "
            "save something (task, note, decision, event). The record name is "
            "the filename stem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["task", "note", "decision", "event"],
                    "description": "Record type — kept narrow for wk1.",
                },
                "name": {
                    "type": "string",
                    "description": "Record name (becomes the filename stem).",
                },
                "set_fields": {
                    "type": "object",
                    "description": (
                        "Frontmatter fields to set, e.g. "
                        "``{\"status\": \"todo\", \"due\": \"2026-05-01\"}``."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body for the record.",
                },
            },
            "required": ["type", "name"],
        },
    },
    {
        "name": "vault_edit",
        "description": (
            "Edit an existing vault record. Use ``set_fields`` to overwrite "
            "frontmatter, ``append_fields`` to add to list fields, and "
            "``body_append`` to append Markdown to the body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path of the record to edit.",
                },
                "set_fields": {
                    "type": "object",
                    "description": "Frontmatter fields to overwrite.",
                },
                "append_fields": {
                    "type": "object",
                    "description": "Fields to append to (list fields).",
                },
                "body_append": {
                    "type": "string",
                    "description": "Markdown to append to the body.",
                },
            },
            "required": ["path"],
        },
    },
]


# tool_name -> vault scope operation name
_TOOL_TO_OP = {
    "vault_search": "search",
    "vault_read": "read",
    "vault_create": "create",
    "vault_edit": "edit",
}


# Safety cap — a runaway loop is the one failure mode tool_use makes
# cheap to hit, so gate it hard. Ten turns is well beyond anything a real
# voice session should need.
MAX_TOOL_ITERATIONS = 10


def _messages_for_api(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip metadata-only keys (``_ts``, ``_kind``, etc.) before API send.

    wk2 stamps timing/kind metadata onto each transcript turn for session-
    record rendering. The Anthropic Messages API strictly validates message
    schemas and rejects unknown fields (400: ``Extra inputs are not
    permitted``). Keep metadata on the persisted transcript; send only the
    Anthropic-schema fields.
    """
    return [
        {k: v for k, v in turn.items() if not k.startswith("_")}
        for turn in transcript
    ]


# --- Prompt assembly ------------------------------------------------------


# --- Pushback copy ---------------------------------------------------------

# Per-level pushback directive text rendered into the system blocks.
# Level 0 = never push back (task mode — just execute). Level 5 is reserved
# for a deliberately confrontational mode we haven't validated yet; treated
# as "max pushback" for now.
# Keyed by int so the lookup is O(1) and unknown levels fall through to a
# neutral ``3`` (matches the plan's "default to 4 during validation" rule —
# 4 is the most common session-type default so the fallback should be close).
_PUSHBACK_DIRECTIVES: Final[dict[int, str]] = {
    0: (
        "Pushback level 0 (task mode): do not challenge the user's framing "
        "or assumptions. Confirm, execute, and reply concisely. Ask a "
        "clarifying question only when the request is ambiguous enough that "
        "proceeding would produce the wrong result."
    ),
    1: (
        "Pushback level 1 (capture mode): acknowledge and capture. Do not "
        "probe unless the user invites it. If you spot a factual error, "
        "correct it briefly; otherwise defer to their framing."
    ),
    2: (
        "Pushback level 2 (light): ask one clarifying question per turn "
        "when it would materially sharpen the output. Do not argue."
    ),
    3: (
        "Pushback level 3 (active): surface tensions you notice, ask \"are "
        "you sure?\" when a claim contradicts prior vault content or earlier "
        "in this session, and propose one alternative framing when it "
        "genuinely adds value. Disagree politely, then defer."
    ),
    4: (
        "Pushback level 4 (strong): actively challenge assumptions. Name "
        "contradictions explicitly. Offer alternative framings and stress-"
        "test the user's logic — this session benefits from friction. Do "
        "not agree just to be agreeable; flagging weak reasoning is the "
        "value you add here. Still respectful, never scolding."
    ),
    5: (
        "Pushback level 5 (confrontational): challenge the premise of the "
        "conversation if it's shaky. Demand evidence. Call out rationalisation. "
        "Reserved for sessions where the user has explicitly asked for a hard "
        "devil's-advocate partner."
    ),
}


def _pushback_directive(level: int) -> str:
    """Return the per-level directive text, falling back to level 3."""
    if level in _PUSHBACK_DIRECTIVES:
        return _PUSHBACK_DIRECTIVES[level]
    # Out-of-range → neutral middle (active). We avoid defaulting to the
    # extremes so a typo in config can't silently lobotomise the assistant
    # (level 0) or make it hostile (level 5).
    return _PUSHBACK_DIRECTIVES[3]


def _build_system_blocks(
    system_prompt: str,
    vault_context_str: str,
    calibration_str: str | None = None,
    pushback_level: int | None = None,
) -> list[dict[str, Any]]:
    """Return ``system`` as a list of cacheable text blocks.

    Up to four cache breakpoints (Anthropic-recommended for agents):
        1. The frozen SKILL.md-style system prompt (almost never changes).
        2. The vault context snapshot (changes across sessions but stable
           within one, so turn 2+ hits the cache).
        3. The per-user calibration block (wk3 — Alfred's current model
           of the user; stable within a session, updated at session close).
        4. The per-session pushback directive (wk3 — derived from session
           type's ``pushback_level``, stable within a session).

    Order matters for caching: the most-stable prefix first, the most-
    volatile last. System prompt > vault context > calibration > pushback,
    because the system prompt is frozen across every session, the vault
    context rolls over between sessions (on a cadence measured in days),
    calibration updates at session close (days to weeks), and pushback is
    determined per-session by the router.

    See claude-api skill → shared/prompt-caching.md.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if vault_context_str:
        blocks.append({
            "type": "text",
            "text": vault_context_str,
            "cache_control": {"type": "ephemeral"},
        })
    if calibration_str:
        blocks.append({
            "type": "text",
            "text": (
                "## Alfred's calibration for this user\n\n"
                + calibration_str
            ),
            "cache_control": {"type": "ephemeral"},
        })
    if pushback_level is not None:
        blocks.append({
            "type": "text",
            "text": (
                "## Session pushback directive\n\n"
                + _pushback_directive(pushback_level)
            ),
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


# --- Tool bridge ----------------------------------------------------------


async def _execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    vault_path: str,
    state: StateManager,
    session: Session,
) -> str:
    """Execute one tool_use block and return JSON-stringified result.

    Errors are caught and returned as ``{"error": "..."}`` so Anthropic sees
    them as tool output and can recover gracefully (apologise, ask for
    clarification, pick a different tool) rather than raising.
    """
    from pathlib import Path

    # Local imports — ops pulls heavy deps; we only want to pay that cost
    # when a tool actually fires.
    from alfred.vault import ops, scope

    op = _TOOL_TO_OP.get(tool_name)
    if op is None:
        return _dumps({"error": f"Unknown tool: {tool_name}"})

    rel_path = tool_input.get("path", "") if isinstance(tool_input, dict) else ""
    record_type = tool_input.get("type", "") if isinstance(tool_input, dict) else ""
    set_fields = tool_input.get("set_fields") if isinstance(tool_input, dict) else None

    vault_path_obj = Path(vault_path)

    # Scope enforcement — the scope check happens BEFORE the op so we never
    # attempt a denied mutation.
    try:
        scope.check_scope(
            "talker",
            op,
            rel_path=rel_path,
            record_type=record_type,
            frontmatter=set_fields if isinstance(set_fields, dict) else None,
        )
    except scope.ScopeError as exc:
        log.info("talker.tool.scope_denied", tool=tool_name, error=str(exc))
        return _dumps({"error": f"scope denied: {exc}"})

    try:
        if tool_name == "vault_search":
            result = ops.vault_search(
                vault_path_obj,
                glob_pattern=tool_input.get("glob") or None,
                grep_pattern=tool_input.get("grep") or None,
            )
            return _dumps({"results": result})

        if tool_name == "vault_read":
            result = ops.vault_read(vault_path_obj, rel_path)
            return _dumps(result)

        if tool_name == "vault_create":
            name = tool_input.get("name", "")
            body = tool_input.get("body")
            result = ops.vault_create(
                vault_path_obj,
                record_type,
                name,
                set_fields=set_fields if isinstance(set_fields, dict) else None,
                body=body,
            )
            # Mutation is already tracked in ``session.vault_ops`` (via
            # ``append_vault_op`` → session-record frontmatter) and in
            # ``data/vault_audit.log`` once that wiring lands. The
            # ``mutation_log`` module is JSONL-file scoped and expects a
            # session *file path*; passing a UUID here created a stray
            # file at the repo root. Dropped entirely — no functional loss.
            append_vault_op(state, session, "create", result["path"])
            return _dumps(result)

        if tool_name == "vault_edit":
            append_fields = tool_input.get("append_fields")
            body_append = tool_input.get("body_append")
            result = ops.vault_edit(
                vault_path_obj,
                rel_path,
                set_fields=set_fields if isinstance(set_fields, dict) else None,
                append_fields=append_fields if isinstance(append_fields, dict) else None,
                body_append=body_append,
            )
            append_vault_op(state, session, "edit", result["path"])
            return _dumps(result)

        return _dumps({"error": f"unhandled tool: {tool_name}"})

    except ops.VaultError as exc:
        log.info(
            "talker.tool.vault_error",
            tool=tool_name,
            error=str(exc),
            details=getattr(exc, "details", None),
        )
        payload: dict[str, Any] = {"error": str(exc)}
        details = getattr(exc, "details", None)
        if details:
            payload["details"] = details
        return _dumps(payload)

    except Exception as exc:  # noqa: BLE001 — tool errors must reach the model
        log.warning(
            "talker.tool.unexpected_error",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({"error": f"unexpected error: {exc}"})


# --- Implicit escalation detection ----------------------------------------

# Ship-list per wk3 team-lead decision on open question #5. Each signal is
# a cheap heuristic — we deliberately don't ML-classify this because the
# offer is always an *offer*: the user just ignores the suggestion if it's
# off-base. False positives cost one line of text, not an expensive
# escalation.

# Keyword phrases that strongly imply "I want more thinking here". Matched
# case-insensitive, substring (not word-boundary) because voice
# transcription routinely produces "think harder about this" with the
# final "about this" tacked on and boundary-matching would miss it.
_ESCALATION_KEYWORDS: Final[tuple[str, ...]] = (
    "think harder",
    "more depth",
    "go deeper",
    "dig into this",
)

# Length thresholds for the long-user/short-assistant signal. Calibrated
# to typical voice-transcription turn lengths:
#   - User turns over 400 chars (~60-70 words) are almost always
#     "thinking out loud" about something substantive.
#   - Assistant responses under 150 chars (~25 words) are almost always
#     one-line acknowledgements, which is under-serving a substantive turn.
# Wider windows tend to produce a lot of false negatives in testing; these
# are a reasonable starting point and can be tuned from production logs.
_LONG_USER_MIN_CHARS: Final[int] = 400
_SHORT_ASSISTANT_MAX_CHARS: Final[int] = 150

# Minimum number of prior user turns required to evaluate the "rephrase"
# signal. Fewer than 2 means there's no prior user turn to compare to.
_REPHRASE_MIN_TURNS: Final[int] = 2
# Jaccard-similarity threshold for "substantially the same content". Set
# high because we want repeated dissatisfaction, not topically adjacent
# follow-ups.
_REPHRASE_SIM_THRESHOLD: Final[float] = 0.55

# Minimum turn-index gap between successive escalation offers. Without
# this, the offer would be appended on every qualifying turn after the
# first — which is noisy. Five turns is a reasonable debounce window:
# long enough that the user has had time to either accept or ignore, but
# short enough that if the escalation signal is still firing we surface
# it again.
_ESCALATION_COOLDOWN_TURNS: Final[int] = 5

_ESCALATION_SUFFIX: Final[str] = (
    "\n\n— want me to switch to Opus for the rest of this session? "
    "/opus to confirm."
)


def _jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity between two short strings.

    Simple enough for voice transcripts — both turns are the user's own
    words, so identical wording trips Jaccard cleanly. Punctuation and
    case differences shouldn't knock us below threshold, so we lowercase
    and split on whitespace (close-enough tokenisation).
    """
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _detect_escalation_signal(
    session: Session,
    user_message: str,
    assistant_text: str,
) -> str | None:
    """Return the name of the first-firing escalation signal, or ``None``.

    Signals, checked in order:
        - ``keyword``: the user message contains a phrase like "think
          harder" / "go deeper" / etc.
        - ``long_user_short_assistant``: this user turn is substantive
          but the assistant's response is terse.
        - ``rephrase``: this user turn is highly similar to an earlier
          one in the same session (user dissatisfaction signal).

    Returns the signal name so the caller can log it. Returning a string
    rather than ``bool`` costs one extra dispatch per turn and makes
    log-correlation possible ("which signal fired most on this session?").
    """
    lower = user_message.lower()
    for kw in _ESCALATION_KEYWORDS:
        if kw in lower:
            return "keyword"

    # Long user / short assistant — both thresholds must hold.
    if (
        len(user_message) >= _LONG_USER_MIN_CHARS
        and len(assistant_text) <= _SHORT_ASSISTANT_MAX_CHARS
    ):
        return "long_user_short_assistant"

    # Rephrase against prior user turns in this session (only plain-text
    # user turns, not tool_result lists).
    prior_user_texts = [
        t.get("content") for t in session.transcript
        if t.get("role") == "user" and isinstance(t.get("content"), str)
    ]
    # The current message hasn't been appended yet at call time; guard
    # anyway by skipping empty lists.
    if len(prior_user_texts) >= _REPHRASE_MIN_TURNS:
        # Check the last 3 prior user turns (excluding the very last,
        # which would often be the message we're evaluating).
        for prior in prior_user_texts[-4:-1]:
            if _jaccard(prior, user_message) >= _REPHRASE_SIM_THRESHOLD:
                return "rephrase"

    return None


def _should_offer_escalation(
    active: dict[str, Any],
    session: Session,
) -> bool:
    """Cooldown / disable-flag check for the implicit escalation offer.

    Returns False when:
        - the user has toggled ``_auto_escalate_disabled`` this session
          (``/no-auto-escalate``),
        - the session is already on Opus (no need to offer what's active),
        - we offered within the cooldown window.
    """
    if active.get("_auto_escalate_disabled"):
        return False
    if session.model == "claude-opus-4-7" or session.model == "claude-opus-4-5":
        return False
    last_offered = active.get("_escalation_offered_at_turn")
    if last_offered is None:
        return True
    try:
        last_offered_int = int(last_offered)
    except (TypeError, ValueError):
        return True
    current_turn = len(session.transcript)
    return (current_turn - last_offered_int) > _ESCALATION_COOLDOWN_TURNS


# --- Main turn ------------------------------------------------------------


# --- Silent-capture sentinel ---------------------------------------------

# Returned by ``run_turn`` when the session is a capture-type session.
# Capture mode suppresses the conversational LLM call entirely: the
# user's message is appended to the transcript so downstream /extract
# and /brief have data to work with, but no assistant turn is generated.
#
# The bot layer (``bot.handle_message``) interprets this sentinel as
# "do not send a text reply — post a receipt-ack emoji reaction
# instead". Kept as a module-level string constant so both sides compare
# against the same literal, not a duplicated magic value. Leading
# underscore signals "internal protocol, not model output".
CAPTURE_SENTINEL: Final[str] = "__ALFRED_CAPTURE_SILENT__"


async def run_turn(
    client: Any,
    state: StateManager,
    session: Session,
    user_message: str,
    config: TalkerConfig,
    vault_context_str: str,
    system_prompt: str,
    user_kind: str = "text",
    calibration_str: str | None = None,
    pushback_level: int | None = None,
    session_type: str | None = None,
) -> str:
    """Run one user turn through the model, handling tool_use internally.

    ``user_kind`` is ``"text"`` or ``"voice"``; it lands on the user turn
    as ``_kind`` so ``_count_message_kinds`` can produce accurate voice /
    text totals in the session-record frontmatter at close time.

    ``calibration_str`` (wk3 commit 2) is Alfred's read of the user
    profile — injected as a third cache-control system block. ``None``
    skips the block entirely for backwards compat.

    ``pushback_level`` (wk3 commit 1) is the session-type-derived int
    0-5 that tunes how aggressively Alfred challenges the user. ``None``
    skips the directive block for backwards compat.

    Returns the final assistant text. Tool-use blocks and their results are
    appended to the session transcript (so the next turn sees the full
    context) and vault mutations are recorded against the session.

    Model resolution (wk3 commit 5 bug fix): the API call uses
    ``session.model`` — which the session-open router and the
    ``/opus`` / ``/sonnet`` command handlers write — not
    ``config.anthropic.model``. Wk2 accidentally read from config, which
    meant the router's model choice and explicit switches were silently
    ignored on every turn after open. Regression-tested in
    ``tests/telegram/test_run_turn_session_model.py``.
    """
    # Append the user's message first so it's visible inside the loop.
    append_turn(state, session, "user", user_message, kind=user_kind)

    # wk2b c2: capture-mode short-circuit. A ``capture`` session is silent
    # mid-session — the user's message has been appended to the transcript
    # (so /extract and /brief can see it later) but we DO NOT call the
    # LLM, DO NOT generate an assistant turn, and DO NOT run escalation
    # detection. The bot layer recognises the sentinel and posts a
    # receipt-ack emoji reaction instead of a text reply.
    if session_type == "capture":
        log.info(
            "talker.capture.silent_turn",
            chat_id=session.chat_id,
            session_id=session.session_id,
            user_kind=user_kind,
            turn_index=len(session.transcript),
        )
        return CAPTURE_SENTINEL

    system_blocks = _build_system_blocks(
        system_prompt,
        vault_context_str,
        calibration_str=calibration_str,
        pushback_level=pushback_level,
    )
    vault_path = config.vault.path

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            create_kwargs: dict[str, Any] = {
                "model": session.model,
                "max_tokens": config.anthropic.max_tokens,
                "system": system_blocks,
                "messages": _messages_for_api(session.transcript),
                "tools": VAULT_TOOLS,
            }
            # Opus 4.x deprecated the ``temperature`` param. Omit it for
            # Opus models; keep it for Sonnet/Haiku/older Claude families.
            if not session.model.startswith("claude-opus-"):
                create_kwargs["temperature"] = config.anthropic.temperature
            response = await client.messages.create(**create_kwargs)
        except anthropic.APIError:
            # Surface to caller — bot.py translates to a user-facing reply.
            log.warning("talker.api_error", iteration=iteration)
            raise

        stop_reason = getattr(response, "stop_reason", "end_turn")

        if stop_reason == "tool_use":
            # Append assistant turn (list of blocks) so the tool_use IDs are
            # preserved for the matching tool_result.
            append_turn(state, session, "assistant", _blocks_to_jsonable(response.content))

            # Execute every tool_use block in order, collect tool_results.
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                btype = getattr(block, "type", None)
                if btype != "tool_use":
                    continue
                tool_name = getattr(block, "name", "")
                tool_input = getattr(block, "input", {}) or {}
                tool_use_id = getattr(block, "id", "")

                log.info(
                    "talker.tool.invoke",
                    iteration=iteration,
                    tool=tool_name,
                )
                result_str = await _execute_tool(
                    tool_name,
                    tool_input if isinstance(tool_input, dict) else {},
                    vault_path,
                    state,
                    session,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                })

            # Feed tool results back as a single user message.
            append_turn(state, session, "user", tool_results)
            continue

        # end_turn (or any non-tool stop): extract text, record, run
        # escalation detection, return.
        text = _extract_text(response.content)
        append_turn(state, session, "assistant", _blocks_to_jsonable(response.content))

        # Wk3 commit 6: implicit escalation detection. Cheap heuristic —
        # if the turn looks like the user wants more thinking and we
        # aren't already on Opus and haven't offered recently, append an
        # offer to the assistant reply. The user types /opus to confirm
        # (commit 5 wiring), or ignores, or types /no-auto-escalate to
        # disable this for the rest of the session.
        active = state.get_active(session.chat_id)
        if active is not None:
            signal = _detect_escalation_signal(session, user_message, text)
            if signal is not None:
                if _should_offer_escalation(active, session):
                    log.info(
                        "talker.model.escalate_offered",
                        chat_id=session.chat_id,
                        session_id=session.session_id,
                        signal=signal,
                        turn_index=len(session.transcript),
                    )
                    text = text + _ESCALATION_SUFFIX
                    active["_escalation_offered_at_turn"] = len(
                        session.transcript
                    )
                    state.set_active(session.chat_id, active)
                    state.save()

        return text

    # Hit the safety cap. Record an explanatory assistant turn so the
    # transcript reflects what happened, then bail.
    warning = (
        "I hit my internal tool-use limit (10 iterations) on that turn — "
        "likely stuck in a loop. Please rephrase or try again."
    )
    append_turn(state, session, "assistant", warning)
    log.warning(
        "talker.run_turn.iteration_cap",
        cap=MAX_TOOL_ITERATIONS,
        session_id=session.session_id,
    )
    return warning


# --- Helpers --------------------------------------------------------------


def _extract_text(content: Any) -> str:
    """Pull the concatenated text from an Anthropic response's content list."""
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _blocks_to_jsonable(content: Any) -> list[dict[str, Any]]:
    """Convert an Anthropic response.content list to plain JSON-serialisable dicts.

    The SDK returns rich block objects (TextBlock, ToolUseBlock, ...), but the
    state file stores the transcript as JSON — we need plain dicts. On the
    next API call the SDK accepts either shape for the assistant side, so
    this trip-through-dicts is safe.
    """
    if not content:
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        # anthropic SDK blocks expose .model_dump(); fall back to attribute
        # access if someone hands us a plain dict already (tests / mocks).
        if hasattr(block, "model_dump"):
            out.append(block.model_dump())
        elif isinstance(block, dict):
            out.append(block)
        else:
            btype = getattr(block, "type", "unknown")
            if btype == "text":
                out.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                out.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                })
            else:
                out.append({"type": btype})
    return out
