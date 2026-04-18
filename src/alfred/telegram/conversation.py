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


# --- Main turn ------------------------------------------------------------


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
    """
    # Append the user's message first so it's visible inside the loop.
    append_turn(state, session, "user", user_message, kind=user_kind)

    system_blocks = _build_system_blocks(
        system_prompt,
        vault_context_str,
        calibration_str=calibration_str,
        pushback_level=pushback_level,
    )
    vault_path = config.vault.path

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = await client.messages.create(
                model=config.anthropic.model,
                max_tokens=config.anthropic.max_tokens,
                temperature=config.anthropic.temperature,
                system=system_blocks,
                messages=session.transcript,
                tools=VAULT_TOOLS,
            )
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

        # end_turn (or any non-tool stop): extract text, record, return.
        text = _extract_text(response.content)
        append_turn(state, session, "assistant", _blocks_to_jsonable(response.content))
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
