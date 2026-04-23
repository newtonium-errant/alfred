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
TALKER_VAULT_TOOLS: list[dict[str, Any]] = [
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
            "save something (task, note, decision, event) or names a new "
            "person who doesn't yet have a person/ record. The record name "
            "is the filename stem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["task", "note", "decision", "event", "person"],
                    "description": (
                        "Record type. Use ``person`` when the user mentions "
                        "a new individual (full name, role, relationship) "
                        "and a ``person/`` record doesn't yet exist."
                    ),
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


# Legacy alias — some tests + upstream code still import ``VAULT_TOOLS``.
# The talker's own pipeline (``run_turn``) now dispatches through
# ``VAULT_TOOLS_BY_SET`` so KAL-LE's ``kalle`` tool-set can add
# ``bash_exec`` without touching the talker code path.
VAULT_TOOLS: list[dict[str, Any]] = TALKER_VAULT_TOOLS


# Stage 3.5: KAL-LE's tool surface. Extends talker with ``bash_exec``
# for the coding instance. The kalle ``vault_create`` tool widens the
# type enum to include pattern + principle (kalle-only record types)
# and drops the talker-specific task/event types — kalle doesn't
# operate on Salem's operational vault.
_KALLE_VAULT_CREATE_TOOL = {
    "name": "vault_create",
    "description": (
        "Create a new vault record in ~/aftermath-lab/. Use when the user "
        "explicitly asks to save, note, or record something. KAL-LE "
        "creates curation + reflective record types; operational types "
        "(task, event) belong to Salem's vault."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [
                    "note", "session", "conversation",
                    "decision", "assumption", "synthesis",
                    "pattern", "principle",
                ],
                "description": "Record type — kalle-specific subset.",
            },
            "name": {
                "type": "string",
                "description": "Record name (becomes the filename stem).",
            },
            "set_fields": {
                "type": "object",
                "description": "Frontmatter fields to set.",
            },
            "body": {
                "type": "string",
                "description": "Markdown body for the record.",
            },
        },
        "required": ["type", "name"],
    },
}


# ``bash_exec`` schema — the executor module (c6) supplies the safety
# logic; this is just the LLM-facing contract. Placeholder ``execute:
# False`` default is the fail-closed shape — if anyone constructs the
# schema ahead of the c6 executor wiring, calls will still be inert.
_BASH_EXEC_TOOL_SCHEMA = {
    "name": "bash_exec",
    "description": (
        "Run a shell command inside one of the four allowed repos "
        "(~/aftermath-lab, ~/aftermath-alfred, ~/aftermath-rrts, "
        "~/alfred). Command is split via shlex and executed via "
        "subprocess.exec — NOT a shell. No pipes, redirects, or "
        "expansion. First token must be in the allowlist "
        "(pytest, npm, git [with subcommand], grep, etc.). "
        "300s timeout. stdout/stderr truncated to 10 KB each."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Single-line command, e.g. 'pytest tests/janitor -q'."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Absolute path to an allowed repo root. Paths outside "
                    "the allowlist reject without running."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "If true, don't run — return the parsed argv + "
                    "allowlist decision. Destructive-keyword commands "
                    "force dry_run=true regardless of this flag."
                ),
            },
        },
        "required": ["command", "cwd"],
    },
}


KALLE_VAULT_TOOLS: list[dict[str, Any]] = [
    TALKER_VAULT_TOOLS[0],  # vault_search
    TALKER_VAULT_TOOLS[1],  # vault_read
    _KALLE_VAULT_CREATE_TOOL,
    TALKER_VAULT_TOOLS[3],  # vault_edit
    _BASH_EXEC_TOOL_SCHEMA,
]


# Tool-set registry — selected by ``telegram.instance.tool_set`` in
# config.yaml (c1 wiring). Default ``"talker"`` preserves Salem's
# existing behaviour; KAL-LE's ``config.kalle.yaml`` sets
# ``tool_set: "kalle"`` to pick up bash_exec.
VAULT_TOOLS_BY_SET: dict[str, list[dict[str, Any]]] = {
    "talker": TALKER_VAULT_TOOLS,
    "kalle": KALLE_VAULT_TOOLS,
}


def tools_for_set(set_name: str) -> list[dict[str, Any]]:
    """Return the tool schema list for ``set_name`` (default ``talker``)."""
    return VAULT_TOOLS_BY_SET.get(set_name) or TALKER_VAULT_TOOLS


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


async def _dispatch_bash_exec(
    *,
    tool_input: dict[str, Any],
    session: Session,
    config: TalkerConfig | None,
) -> str:
    """Dispatch one ``bash_exec`` tool_use block.

    Stage 3.5 — KAL-LE. Every safety guardrail lives in
    :mod:`alfred.telegram.bash_exec`. This function is the thin adapter
    between the Anthropic tool_use schema and the executor:

    1. Tool-set gating. Only instances configured with
       ``telegram.instance.tool_set == "kalle"`` may invoke this; Salem
       should never see ``bash_exec`` in its tool list, but we still
       refuse explicitly here as a second-line defence against
       prompt-injection / classifier drift.
    2. Config plumbing. :class:`BashExecConfig` lives on
       ``TalkerConfig.bash_exec`` and carries the audit-log path.
       ``None`` or missing config → structured refusal.
    3. Executor call. ``bash_exec.execute`` is async and always returns
       a dict — we pass its shape back to the model verbatim.
    4. Subprocess-failure contract. Non-zero exit codes that weren't
       produced by the executor's own refusal path (``reason=""`` means
       the command actually ran) emit a ``talker.bash_exec.nonzero_exit``
       event with the ``stdout_tail`` sentinel per builder.md.

    Returns a JSON-stringified dict the conversation loop feeds back as
    a ``tool_result`` block.
    """
    from . import bash_exec as bash_exec_mod

    # --- Tool-set gating -------------------------------------------------
    # Runs before any argument parsing so the refusal message is clean
    # and deterministic — a Salem instance that somehow receives a
    # bash_exec tool_use block gets a structured error, not a crash.
    tool_set = ""
    if config is not None:
        tool_set = config.instance.tool_set or ""
    if tool_set != "kalle":
        log.warning(
            "talker.bash_exec.wrong_tool_set",
            tool_set=tool_set or "(none)",
            session_id=session.session_id,
        )
        return _dumps({
            "error": "bash_exec not available on this instance",
            "tool_set": tool_set or "talker",
        })

    # --- Config presence check -------------------------------------------
    if config is None or config.bash_exec is None:
        log.warning(
            "talker.bash_exec.config_missing",
            session_id=session.session_id,
        )
        return _dumps({"error": "bash_exec disabled in config"})

    # --- Argument parsing ------------------------------------------------
    # Model is expected to supply ``command`` + ``cwd`` per the schema;
    # ``dry_run`` is optional. Defensive typing — the Anthropic SDK hands
    # us whatever the model emitted, which in rare cases may not match
    # the schema.
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    cwd = tool_input.get("cwd", "") if isinstance(tool_input, dict) else ""
    dry_run_raw = tool_input.get("dry_run") if isinstance(tool_input, dict) else None
    dry_run = bool(dry_run_raw) if dry_run_raw is not None else False

    if not isinstance(command, str) or not command.strip():
        return _dumps({"error": "bash_exec requires a non-empty 'command'"})
    if not isinstance(cwd, str) or not cwd.strip():
        return _dumps({"error": "bash_exec requires a 'cwd' under an allowed repo root"})

    # --- Execute ---------------------------------------------------------
    log.info(
        "talker.bash_exec.invoke",
        session_id=session.session_id,
        cwd=cwd,
        dry_run=dry_run,
        # Truncate command in logs — the audit log (bash_exec.jsonl)
        # holds the full command; structlog lines don't need to carry
        # arbitrarily long payloads.
        command_preview=command[:200],
    )
    try:
        result = await bash_exec_mod.execute(
            command=command,
            cwd=cwd,
            dry_run=dry_run,
            audit_path=config.bash_exec.audit_path,
            session_id=session.session_id,
        )
    except Exception as exc:  # noqa: BLE001 — tool errors must reach the model
        log.warning(
            "talker.bash_exec.unexpected_error",
            session_id=session.session_id,
            error=str(exc),
        )
        return _dumps({"error": f"bash_exec crashed: {exc}"})

    # --- Subprocess-failure-contract logging -----------------------------
    # Only fires when the command actually ran (``reason == ""``) and
    # returned a non-zero code. Executor-level refusals (denylist, cwd,
    # allowlist miss, timeout, parse error) all set ``reason`` to a
    # non-empty gate name and emit their own ``talker.bash_exec.*``
    # warning events inside the executor.
    exit_code = result.get("exit_code", -1)
    reason = result.get("reason", "") or ""
    if exit_code != 0 and not reason:
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        # The ``stdout_tail=""`` sentinel is load-bearing — emit
        # explicitly so the "no diagnostic output at all" signature is
        # grep-able. See builder.md / CLAUDE.md subprocess-failure
        # contract.
        log.warning(
            "talker.bash_exec.nonzero_exit",
            chat_id=session.chat_id,
            session_id=session.session_id,
            command=command[:200],
            code=exit_code,
            stderr=stderr[:500],
            stdout_tail=stdout[-2000:] if stdout else "",
        )

    return _dumps(result)


# --- Attribution-marker wiring (calibration audit gap, c2) ---------------


def _agent_slug(config: TalkerConfig | None) -> str:
    """Return the agent slug used in attribution markers.

    Derived from ``config.instance.name`` lowercased ("Salem" → "salem",
    "KAL-LE" → "kal-le", "Alfred" → "alfred"). Defaults to ``"talker"``
    when no config is threaded in (legacy callers, tests that skip the
    config plumb-through). Lowercase-only because the marker_id contract
    expects ``[\\w-]+`` and downstream surfacers will group by agent.
    """
    if config is None:
        return "talker"
    name = (config.instance.name or "").strip().lower()
    return name or "talker"


def _section_title_for_create(name: str, body: str | None) -> str:
    """Pick a human-readable section title for a vault_create marker.

    Preference: record name (always present on create — it's the filename
    stem) → first ``#``/``##`` heading in body → ``"talker-write"``
    placeholder. The talker always has the ``name`` so this is mostly the
    record-name path; the heading fallback is here so the same helper can
    serve future write paths that don't carry a separate name.
    """
    if name:
        return name
    if body:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                # Strip leading '#'s and surrounding whitespace.
                return stripped.lstrip("#").strip() or "talker-write"
    return "talker-write"


def _section_title_for_edit_append(body_append: str, rel_path: str) -> str:
    """Section title for a body_append edit.

    First heading inside the appended fragment if present, else the file
    stem (e.g. ``"Email Triage Rules"``), else the placeholder. The
    fragment-heading path is the common case when the model appends a
    new ``## ...`` block to a living rules document.
    """
    for line in body_append.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or "talker-write"
    if rel_path:
        # Strip directory and extension. ``person/Andrew Newton.md`` →
        # ``Andrew Newton``.
        from pathlib import Path as _P
        return _P(rel_path).stem or "talker-write"
    return "talker-write"


def _attribution_reason(session: Session) -> str:
    """Short reason string for the attribution audit entry.

    Just identifies the write origin — ``"talker conversation turn"`` plus
    the session id for trace. Richer reasons (which user message triggered
    the write, what the model claimed it was doing) are Phase 3 territory.
    """
    sid = getattr(session, "session_id", "") or ""
    if sid:
        return f"talker conversation turn (session={sid})"
    return "talker conversation turn"


async def _execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    vault_path: str,
    state: StateManager,
    session: Session,
    config: TalkerConfig | None = None,
) -> str:
    """Execute one tool_use block and return JSON-stringified result.

    Errors are caught and returned as ``{"error": "..."}`` so Anthropic sees
    them as tool output and can recover gracefully (apologise, ask for
    clarification, pick a different tool) rather than raising.

    ``config`` (Stage 3.5) is threaded in so the ``bash_exec`` branch can
    read :class:`BashExecConfig` + the instance tool_set off
    :class:`TalkerConfig`. Kept optional for backwards compatibility with
    callers that predate bash_exec; when ``None`` the bash_exec branch
    refuses with a clear error rather than crashing.
    """
    from pathlib import Path

    # Local imports — ops pulls heavy deps; we only want to pay that cost
    # when a tool actually fires. ``attribution`` is light (stdlib + a
    # dataclass) so importing it alongside is essentially free.
    from alfred.vault import attribution, ops, scope

    # ``bash_exec`` (KAL-LE) — safety-critical subprocess path. Handled
    # before the vault-op lookup because it isn't a vault op; the
    # executor in bash_exec.py owns every allowlist / denylist / cwd /
    # timeout / destructive-keyword gate. This branch is just the
    # dispatcher glue: tool-set gating, config plumbing, structured
    # error returns, and subprocess-failure-contract logging.
    if tool_name == "bash_exec":
        return await _dispatch_bash_exec(
            tool_input=tool_input,
            session=session,
            config=config,
        )

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

            # Attribution-marker wiring (calibration audit gap, c2). The
            # talker invokes vault_create as a side-effect of an LLM
            # turn — every body that lands this way is, by definition,
            # agent-inferred prose, not Andrew-typed text. Wrap it so a
            # future Daily Sync confirm/reject flow can surface it for
            # explicit confirmation. No-op when ``body`` is None (the
            # template-default-body path); the model only triggers wrapping
            # when it composed body content itself.
            sf = dict(set_fields) if isinstance(set_fields, dict) else {}
            if body:
                wrapped_body, audit_entry = attribution.with_inferred_marker(
                    body,
                    section_title=_section_title_for_create(name, body),
                    agent=_agent_slug(config),
                    reason=_attribution_reason(session),
                )
                attribution.append_audit_entry(sf, audit_entry)
                body = wrapped_body

            result = ops.vault_create(
                vault_path_obj,
                record_type,
                name,
                set_fields=sf or None,
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

            # Attribution-marker wiring (calibration audit gap, c2). For
            # body_append, wrap ONLY the appended fragment — the existing
            # record body is left as-is (it may contain Andrew-typed prose
            # that already shipped). Merge the new audit entry with any
            # entries already on the record so prior inferred sections
            # aren't lost when this edit lands.
            sf = dict(set_fields) if isinstance(set_fields, dict) else {}
            if body_append:
                # Read existing frontmatter so we can preserve prior
                # attribution_audit entries. Read failures (file missing,
                # malformed YAML) propagate as VaultError just like a
                # plain edit would — the wrapping shouldn't mask a real
                # underlying problem.
                existing = ops.vault_read(vault_path_obj, rel_path)
                existing_fm = existing.get("frontmatter") or {}
                # Carry forward existing entries first, then layer this
                # edit's set_fields on top (caller wins on real conflicts,
                # but attribution_audit is merged below).
                merged_fm: dict = {}
                if isinstance(existing_fm.get("attribution_audit"), list):
                    merged_fm["attribution_audit"] = list(
                        existing_fm["attribution_audit"]
                    )
                # Caller-supplied set_fields go on top of the merged base.
                merged_fm.update(sf)

                wrapped_append, audit_entry = attribution.with_inferred_marker(
                    body_append,
                    section_title=_section_title_for_edit_append(
                        body_append, rel_path,
                    ),
                    agent=_agent_slug(config),
                    reason=_attribution_reason(session),
                )
                attribution.append_audit_entry(merged_fm, audit_entry)
                body_append = wrapped_append
                sf = merged_fm

            result = ops.vault_edit(
                vault_path_obj,
                rel_path,
                set_fields=sf or None,
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
    # Stage 3.5: pick the tool list per instance tool_set. Salem
    # ("talker") gets vault-only; KAL-LE ("kalle") gets vault + bash_exec.
    # Defaults to the talker set so any misconfigured instance can't
    # accidentally surface bash_exec.
    instance_tools = tools_for_set(config.instance.tool_set)

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            create_kwargs: dict[str, Any] = {
                "model": session.model,
                "max_tokens": config.anthropic.max_tokens,
                "system": system_blocks,
                "messages": _messages_for_api(session.transcript),
                "tools": instance_tools,
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
                    config=config,
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
