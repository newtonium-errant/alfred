"""In-process Anthropic SDK executor for a single directive.

One ``PendingInstruction`` goes in, one ``ExecutionResult`` comes out.
The executor:

1. Checks the directive for destructive-keyword substrings
   (case-insensitive). If a match fires, runs the tool-use loop in
   **dry-run mode** — tool calls return ``"would do X"`` shapes without
   mutating the vault. The plan is written to
   ``alfred_instructions_last`` and the queue entry is cleared. The
   operator then re-issues a more specific directive.

2. Otherwise runs the tool-use loop against the Anthropic SDK with a
   narrow tool surface (``vault_read``, ``vault_edit``, ``vault_create``,
   ``vault_move``, ``vault_list``, ``vault_search``, ``vault_context``).
   Each tool call is gated through ``check_scope("instructor", ...)``
   in ``_dispatch_tool`` before dispatch.

   THAT GATE CANNOT CURRENTLY DENY ANYTHING — it is DEFENCE-IN-DEPTH
   for a future tool mapping, not a live control. Driven at the ROUTE
   (not by calling the predicate inline, which is what produced the
   earlier wrong claim here): ``_TOOL_TO_OP`` maps exactly seven tools
   to read / search / list / context / create / edit / move, and ALL
   SEVEN are ``True`` in ``SCOPE_RULES["instructor"]``, so no reachable
   op can be refused. ``delete`` IS ``False`` in that scope entry, but
   it is UNREACHABLE: there is no ``vault_delete`` key in
   ``_TOOL_TO_OP``, so ``_dispatch_tool("vault_delete", ...)`` returns
   ``{"error": "Unknown tool: vault_delete"}`` from the LOOKUP above,
   before the gate is ever consulted. A jailbreak asking for deletion
   is stopped by the tool surface, NOT by the scope gate — attributing
   it to the gate misplaces the protection.

   Measured: neutering this gate entirely reds NOTHING — 0 of the 55
   pins in ``tests/test_instructor_scope_truth.py``, and 1168 passed /
   0 failed across every instructor-touching test file. It is live but
   unpinned code, which is exactly why the two pins named below exist:
   the day someone maps a denied op into ``_TOOL_TO_OP``, the all-allow
   pin turns red and this paragraph stops being true.

   SCOPE OF THAT GATE — it is an OP-LEVEL gate only. The pre-dispatch
   call is the ONLY ``check_scope`` on this path: ``ops.vault_edit`` /
   ``vault_create`` / ``vault_move`` are called WITHOUT ``scope=``, and
   every in-``ops`` scope check sits behind ``if scope is not None``, so
   none of them run. Three enforcement legs are therefore inactive here
   that ARE active on the talker (``conversation.py``, ``scope=
   active_scope``) and batch (``worker.py``, ``scope=BATCH_SCOPE``)
   paths — the instructor is the only one of the three that omits it:

     * per-TYPE rules — the pre-dispatch call passes
       ``record_type=tool_input.get("type", "")`` and the ``vault_edit``
       schema has no ``type`` property, so record_type is ALWAYS ``""``
       for an edit. ``ops.vault_edit`` reads the real type off disk.
     * the body-mutation matrix — ``_BODY_MUTATE_DENIED_TYPES`` plus the
       ``allow_body_insert_at`` / ``allow_body_replace`` allowlists are
       reached only via ``check_scope(scope, "body_insert_at"/
       "body_replace", ...)`` inside ``vault_edit``. Not run here.
     * body-write accounting — the pre-dispatch ``body_write`` flag is
       ``bool(body) or bool(body_append)``, so a ``body_replace`` or
       ``body_insert_at`` edit is seen as body_write=False, and
       ``append_fields`` keys are invisible to the ``fields`` list.

   What DOES still constrain body mutations here is the UNCONDITIONAL
   part of ``vault_edit``: the mutual-exclusion gate (at most one body
   kwarg per call) and the no-op gate both run before any scope check
   and so apply to this path — see ``tests/test_vault_edit_body_
   mutation.py``. That is a shape gate, not a policy gate: it constrains
   HOW MANY body surfaces one call may use, never WHICH RECORD may be
   rewritten. Threading ``scope="instructor"`` is proposed but NOT
   ratified; see ``tests/test_instructor_scope_truth.py`` for the pinned
   enumeration of what it would change.

   Note when weighing that proposal: ``body_replace`` and
   ``body_insert_at`` are the two surfaces the deny set would refuse,
   and while ``vault-instructor/SKILL.md`` does not list them, the
   ``vault_edit`` TOOL DESCRIPTION below DOES ("body_replace (full
   rewrite)"). The model reads that description, so those surfaces are
   advertised and genuinely reachable — the SKILL's silence is not a
   reason to treat them as unused.

3. On success, appends a single-line audit comment to the record body
   (``<!-- ALFRED:INSTRUCTION ... -->``) and prunes older blocks beyond
   ``audit_window_size``. Clears the queue entry and prepends a
   ``{text, executed_at, result}`` dict to ``alfred_instructions_last``.

4. On failure, bumps the state's retry counter. At ``max_retries``, the
   queue entry is dropped and the error surfaces to a visible
   ``alfred_instructions_error`` frontmatter field.

Mutation logging — READ THIS BEFORE TRUSTING THE AUDIT TRAIL.

``_dispatch_tool`` calls ``mutation_log.log_mutation(session_path, ...,
scope="instructor")`` after every create / edit / move. Those calls are
INERT ON THE PRODUCTION PATH: ``log_mutation`` returns immediately when
``session_path`` is falsy, ``session_path`` defaults to ``None`` on both
``execute`` and ``execute_and_record``, and the daemon's only call site
(``daemon.py``, the sole caller in ``src/``; ``instructor/cli.py::cmd_run``
delegates to the same ``run_daemon``, so the CLI-foreground path goes
through that one site too) does not pass it. So a
daemon-executed directive writes NO mutation-log row at all — instructor
vault mutations are absent from the trail rather than mislabelled in it.
Tests that pass ``session_path`` explicitly DO produce rows, which is why
this reads as working when driven directly.

Two further limits on what a row would mean if one were written:

  * ``scope="instructor"`` rides in ``**extra`` into the SESSION JSONL.
    It is PROVENANCE (which subsystem acted), not proof that the scope
    matrix was enforced — and note the op-level gate genuinely does run
    (see above), so the label is not a false claim about a gate that
    never fired; it is simply narrower than "the instructor scope was
    applied to this write".
  * ``data/vault_audit.log`` never carries the scope at all.
    ``append_to_audit_log`` writes ``{ts, tool, op, path, detail}`` — no
    scope field exists in that row shape. An operator asking "what was
    allowed to write this record?" cannot answer it from the audit log
    for ANY tool, and for the instructor there is no row to read.

Wiring ``session_path`` through is a live gap, not a design choice; it
needs a session-file lifecycle (create → flush via ``read_mutations`` →
``append_to_audit_log`` → cleanup) that no instructor caller owns yet.
Pinned in ``tests/test_instructor_scope_truth.py`` so the day someone
threads it, the pin turns red and this paragraph gets rewritten.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import frontmatter

from alfred._anthropic_compat import messages_create_kwargs
from alfred.vault import ops, scope
from alfred.vault.mutation_log import log_mutation
from alfred.vault.paths import VaultContainmentError, resolve_in_vault

from .config import InstructorConfig
from .state import InstructorState
from .utils import get_logger

log = get_logger(__name__)


# --- Constants --------------------------------------------------------------

# Safety cap on the tool-use loop. Matches the talker's
# ``MAX_TOOL_ITERATIONS`` for the same reason — a runaway tool loop is
# the one failure mode ``tool_use`` makes cheap to hit, so gate it
# hard. Ten iterations is well beyond anything a single directive
# should need (the directives are single natural-language tasks, not
# open-ended conversations).
MAX_TOOL_ITERATIONS = 10

# Audit-block marker. Every executed directive leaves exactly one of
# these lines at the bottom of the target record. The regex strips
# them when we prune, so this string is load-bearing for both the
# append and the prune paths.
_AUDIT_MARKER_PREFIX = "<!-- ALFRED:INSTRUCTION"
_AUDIT_MARKER_SUFFIX = "-->"
_AUDIT_BLOCK_RE = re.compile(
    r"^<!-- ALFRED:INSTRUCTION .*? -->\s*$",
    re.MULTILINE,
)


# --- Types ------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Outcome of a single directive execution.

    ``status`` values:
      - ``"done"`` — executed successfully.
      - ``"dry_run"`` — destructive-keyword gate fired, plan written.
      - ``"ambiguous"`` — agent declined, more info needed from operator.
      - ``"refused"`` — agent explicitly refused (scope, policy).
      - ``"error"`` — SDK or tool-execution exception.

    ``summary`` is a 1-line string safe to display in the
    ``alfred_instructions_last[].result`` field or a Telegram reply.

    ``mutated_paths`` is the list of vault-relative paths the executor
    touched. Used by the audit-comment appender and by tests.
    """

    status: str
    summary: str
    mutated_paths: list[str] = field(default_factory=list)
    tool_iterations: int = 0
    dry_run: bool = False


# --- Destructive-keyword gate ----------------------------------------------


def is_destructive(directive: str, keywords: tuple[str, ...]) -> bool:
    """Return True if ``directive`` contains any destructive-keyword substring.

    Case-insensitive substring match. Matches the plan literally.
    """
    lower = directive.lower()
    return any(kw.lower() in lower for kw in keywords)


# --- Tool surface -----------------------------------------------------------

# Seven vault operations — broader than the talker's four because the
# instructor scope permits create/move and has no field allowlist. The
# per-op "would do X" dry-run message in ``_dispatch_tool`` is what
# keeps the destructive path safe.
VAULT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "vault_read",
        "description": "Read a vault record. Input: {path}. Returns {path, frontmatter, body}.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "vault_search",
        "description": "Search the vault. Input: {glob?, grep?}. Returns list of {path, name, type, status}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {"type": "string"},
                "grep": {"type": "string"},
            },
        },
    },
    {
        "name": "vault_list",
        "description": "List all records of a given type. Input: {type}. Returns list of {path, name, status}.",
        "input_schema": {
            "type": "object",
            "properties": {"type": {"type": "string"}},
            "required": ["type"],
        },
    },
    {
        "name": "vault_context",
        "description": "Return a compact summary of the vault grouped by type. No input.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "vault_create",
        "description": (
            "Create a new vault record. Input: {type, name, set_fields?, "
            "body?}. NOTE: ``body`` is a TOP-LEVEL parameter, NOT a "
            "frontmatter field — never put ``body`` in ``set_fields``."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "name": {"type": "string"},
                "set_fields": {
                    "type": "object",
                    "not": {"required": ["body"]},
                },
                "body": {"type": "string"},
            },
            "required": ["type", "name"],
        },
    },
    {
        "name": "vault_edit",
        "description": (
            "Edit an existing vault record. Input: {path, set_fields?, "
            "append_fields?, body_append?, body_insert_at?, body_replace?}. "
            "Body content uses exactly ONE of body_append (end-of-doc), "
            "body_insert_at (anchored mid-doc; {marker, position, content} "
            "with line-exact marker), or body_replace (full rewrite). "
            "Mutually exclusive — at most one body kwarg per call. "
            "NOTE: body content NEVER goes in set_fields (that's a "
            "frontmatter-only field)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "set_fields": {
                    "type": "object",
                    "not": {"required": ["body"]},
                },
                "append_fields": {"type": "object"},
                "body_append": {"type": "string"},
                "body_insert_at": {
                    "type": "object",
                    "properties": {
                        "marker": {"type": "string"},
                        "position": {
                            "type": "string",
                            "enum": ["before", "after"],
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["marker", "position", "content"],
                },
                "body_replace": {"type": "string"},
            },
            "required": ["path"],
            # Mutual exclusion of the body-mutation kwargs is enforced
            # at the RUNTIME GATE in ``vault.ops.vault_edit``, NOT at
            # the JSON-schema layer. Earlier ship (commit ``0d7e7a6``)
            # included a top-level ``oneOf`` here; Anthropic's
            # Messages API request validator rejects oneOf / allOf /
            # anyOf at the top level of any tool's ``input_schema``
            # with HTTP 400 before the model runs. Removed 2026-05-06
            # to unblock production. Runtime gate in vault_edit is
            # the load-bearing protection (covered by
            # ``tests/test_vault_edit_body_mutation.py``).
        },
    },
    {
        "name": "vault_move",
        "description": "Move a vault record. Input: {from, to}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["from", "to"],
        },
    },
]


# Tool name -> scope operation name. Matches ``SCOPE_RULES['instructor']``.
_TOOL_TO_OP = {
    "vault_read": "read",
    "vault_search": "search",
    "vault_list": "list",
    "vault_context": "context",
    "vault_create": "create",
    "vault_edit": "edit",
    "vault_move": "move",
}


# --- JSON helpers -----------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """Fallback for ``json.dumps`` — frontmatter can hold date / datetime.

    Mirrors the talker's ``_json_default`` so tool_result payloads
    serialize consistently across tools.
    """
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


# --- Tool dispatch ----------------------------------------------------------


def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    vault_path: Path,
    dry_run: bool,
    session_path: str | None,
    mutated_paths: list[str],
) -> str:
    """Execute one ``tool_use`` block and return a JSON string.

    ``mutated_paths`` is mutated in place (append) when the tool lands
    a mutation, so the caller knows which paths to include in the
    audit comment.

    Errors are caught and returned as JSON ``{"error": "..."}`` so the
    model can recover rather than seeing an exception. Matches the
    talker's ``_execute_tool`` contract.
    """
    op_name = _TOOL_TO_OP.get(tool_name)
    if op_name is None:
        return _dumps({"error": f"Unknown tool: {tool_name}"})

    if not isinstance(tool_input, dict):
        tool_input = {}

    rel_path = tool_input.get("path", "") or ""
    record_type = tool_input.get("type", "") or ""
    set_fields = tool_input.get("set_fields") if isinstance(
        tool_input.get("set_fields"), dict
    ) else None
    body = tool_input.get("body")
    body_append = tool_input.get("body_append")
    fields_list = list(set_fields.keys()) if set_fields else None
    body_write = bool(body) or bool(body_append)

    # ---- Scope gate — OP-LEVEL, and the ONLY check_scope on this path.
    # It RUNS on every dispatch but CANNOT CURRENTLY REFUSE ANYTHING:
    # every op ``_TOOL_TO_OP`` can produce is True for instructor. It is
    # defence-in-depth for a future tool mapping, so that adding a
    # denied op to the map cannot silently grant it. Do NOT read the
    # ``delete: False`` in the scope entry as protection for this path —
    # ``vault_delete`` is refused by the ``_TOOL_TO_OP`` lookup ABOVE
    # (``Unknown tool``), before this gate is reached.
    #
    # What it does NOT reach, because ``ops.vault_*`` below are called
    # without ``scope=`` and every in-ops check sits behind ``if scope is
    # not None``: the per-type rules (``record_type`` is always "" for an
    # edit — the vault_edit schema has no ``type`` property), the
    # body-mutation matrix (``_BODY_MUTATE_DENIED_TYPES`` +
    # allow_body_* allowlists), and accurate body-write accounting
    # (``body_write`` below ignores body_replace / body_insert_at, and
    # ``fields`` ignores append_fields keys). Full reasoning + the
    # measured enumeration: module docstring and
    # ``tests/test_instructor_scope_truth.py``. -----------------------
    try:
        scope.check_scope(
            "instructor",
            op_name,
            rel_path=rel_path,
            record_type=record_type,
            fields=fields_list,
            body_write=body_write,
        )
    except scope.ScopeError as exc:
        log.info(
            "instructor.tool.scope_denied",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({"error": f"scope denied: {exc}"})

    # ---- Dry-run short-circuit: read-only ops still execute normally so
    # the model can reason about the plan; write ops return a
    # "would do X" descriptor instead of mutating. Mirrors the plan. ---
    if dry_run and op_name in ("create", "edit", "move"):
        return _dumps({
            "dry_run": True,
            "would": {
                "op": op_name,
                "tool": tool_name,
                "input": tool_input,
            },
        })

    try:
        if tool_name == "vault_read":
            result = ops.vault_read(vault_path, rel_path)
            return _dumps(result)

        if tool_name == "vault_search":
            result = ops.vault_search(
                vault_path,
                glob_pattern=tool_input.get("glob") or None,
                grep_pattern=tool_input.get("grep") or None,
            )
            return _dumps({"results": result})

        if tool_name == "vault_list":
            result = ops.vault_list(vault_path, record_type)
            return _dumps({"results": result})

        if tool_name == "vault_context":
            result = ops.vault_context(vault_path)
            return _dumps(result)

        if tool_name == "vault_create":
            name = tool_input.get("name", "") or ""
            # Calibration audit gap (c4): when the instructor agent
            # composes a body for the new record, wrap it in
            # BEGIN_INFERRED markers + append an attribution_audit
            # entry to frontmatter. ``body=None`` (template-default)
            # skips wrapping — there's no inferred prose, just
            # scaffold. Mirrors the talker's vault_create wiring.
            from alfred.vault import attribution
            sf_create = dict(set_fields) if isinstance(set_fields, dict) else {}
            wrapped_body = body
            if body and str(body).strip():
                src = session_path or "(no session)"
                wrapped_body, audit_entry = attribution.with_inferred_marker(
                    str(body),
                    section_title=name or rel_path or "instructor-write",
                    agent="instructor",
                    reason=f"instructor directive (source={src})",
                )
                attribution.append_audit_entry(sf_create, audit_entry)

            result = ops.vault_create(
                vault_path,
                record_type,
                name,
                set_fields=sf_create or None,
                body=wrapped_body,
            )
            mutated_paths.append(result["path"])
            log_mutation(
                session_path,
                "create",
                result["path"],
                scope="instructor",
            )
            return _dumps(result)

        if tool_name == "vault_edit":
            append_fields = tool_input.get("append_fields") if isinstance(
                tool_input.get("append_fields"), dict
            ) else None
            body_insert_at_raw = tool_input.get("body_insert_at")
            body_replace_raw = tool_input.get("body_replace")
            body_insert_at: dict | None = (
                dict(body_insert_at_raw)
                if isinstance(body_insert_at_raw, dict) else None
            )
            body_replace: str | None = (
                str(body_replace_raw)
                if body_replace_raw else None
            )
            # Calibration audit gap (c4): wrap a body_append fragment
            # in inferred markers + carry forward any existing
            # attribution_audit entries on the target record.
            #
            # body_insert_at + body_replace (P1 c3 from QA 2026-05-04):
            # the same attribution wrapping applies. body_insert_at
            # wraps the ``content`` fragment; body_replace wraps the
            # entire replacement.
            from alfred.vault import attribution
            sf_edit = dict(set_fields) if isinstance(set_fields, dict) else {}
            wrapped_append = body_append
            needs_attribution = bool(
                (body_append and str(body_append).strip())
                or body_replace
                or (body_insert_at and body_insert_at.get("content"))
            )
            if needs_attribution:
                # Read existing frontmatter to merge with prior audit
                # entries — mirrors talker._execute_tool.
                merged_fm: dict = {}
                try:
                    existing = ops.vault_read(vault_path, rel_path)
                    existing_fm = existing.get("frontmatter") or {}
                    if isinstance(existing_fm.get("attribution_audit"), list):
                        merged_fm["attribution_audit"] = list(
                            existing_fm["attribution_audit"]
                        )
                except Exception:  # noqa: BLE001 — read failure shouldn't mask the underlying VaultError
                    pass
                merged_fm.update(sf_edit)
                src = session_path or "(no session)"

                def _section_title_from_fragment(fragment: str) -> str:
                    section_title = "instructor-write"
                    for line in fragment.splitlines():
                        s = line.strip()
                        if s.startswith("#"):
                            section_title = (
                                s.lstrip("#").strip() or section_title
                            )
                            break
                    if section_title == "instructor-write" and rel_path:
                        section_title = (
                            Path(rel_path).stem or section_title
                        )
                    return section_title

                if body_append and str(body_append).strip():
                    section_title = _section_title_from_fragment(
                        str(body_append),
                    )
                    wrapped_append, audit_entry = attribution.with_inferred_marker(
                        str(body_append),
                        section_title=section_title,
                        agent="instructor",
                        reason=f"instructor directive (source={src})",
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                elif body_replace:
                    section_title = (
                        Path(rel_path).stem or "instructor-write"
                    )
                    wrapped_replace, audit_entry = attribution.with_inferred_marker(
                        body_replace,
                        section_title=section_title,
                        agent="instructor",
                        reason=(
                            f"instructor directive body_replace "
                            f"(source={src})"
                        ),
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                    body_replace = wrapped_replace
                elif body_insert_at and body_insert_at.get("content"):
                    insert_content = str(body_insert_at["content"])
                    marker_for_title = str(
                        body_insert_at.get("marker", "")
                    )
                    section_title = (
                        _section_title_from_fragment(insert_content)
                        if insert_content.strip().startswith("#")
                        else (
                            marker_for_title
                            or Path(rel_path).stem
                            or "instructor-write"
                        )
                    )
                    wrapped_insert, audit_entry = attribution.with_inferred_marker(
                        insert_content,
                        section_title=section_title,
                        agent="instructor",
                        reason=(
                            f"instructor directive body_insert_at "
                            f"(source={src})"
                        ),
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                    body_insert_at["content"] = wrapped_insert
                sf_edit = merged_fm

            result = ops.vault_edit(
                vault_path,
                rel_path,
                set_fields=sf_edit or None,
                append_fields=append_fields,
                body_append=wrapped_append,
                body_insert_at=body_insert_at,
                body_replace=body_replace,
            )
            mutated_paths.append(result["path"])
            log_mutation(
                session_path,
                "edit",
                result["path"],
                scope="instructor",
            )
            return _dumps(result)

        if tool_name == "vault_move":
            from_path = tool_input.get("from", "") or ""
            to_path = tool_input.get("to", "") or ""
            result = ops.vault_move(vault_path, from_path, to_path)
            mutated_paths.append(result["to"])
            log_mutation(
                session_path,
                "move",
                from_path,
                to=to_path,
                scope="instructor",
            )
            return _dumps(result)

        return _dumps({"error": f"unhandled tool: {tool_name}"})

    except ops.VaultError as exc:
        log.info(
            "instructor.tool.vault_error",
            tool=tool_name,
            error=str(exc),
        )
        payload: dict[str, Any] = {"error": str(exc)}
        details = getattr(exc, "details", None)
        if details:
            payload["details"] = details
        return _dumps(payload)
    except Exception as exc:  # noqa: BLE001 — tool errors must reach the model
        log.warning(
            "instructor.tool.unexpected_error",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({"error": f"unexpected error: {exc}"})


# --- Audit comment + alfred_instructions_last archiving ---------------------


def _append_audit_comment(
    md_path: Path,
    directive: str,
    summary: str,
    audit_window_size: int,
) -> None:
    """Append a 1-line audit block to the record body and prune older ones.

    Format: ``<!-- ALFRED:INSTRUCTION <iso> "<directive>" → <summary> -->``.

    ``audit_window_size`` caps the number of audit blocks kept; older
    blocks are stripped via ``_AUDIT_BLOCK_RE`` so the body doesn't
    grow unbounded.
    """
    if not md_path.exists():
        return
    post = frontmatter.load(str(md_path))
    body = post.content

    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    # Collapse quotes inside the directive text so the one-line block
    # stays parseable. Newlines also get squashed for the same reason.
    flat_directive = directive.replace('"', "'").replace("\n", " ").strip()
    flat_summary = summary.replace("\n", " ").strip()
    new_block = (
        f'{_AUDIT_MARKER_PREFIX} {now_iso} "{flat_directive}" → '
        f"{flat_summary} {_AUDIT_MARKER_SUFFIX}"
    )

    # Collect existing audit blocks (in order), then strip them from
    # the body so we can re-append a pruned set.
    existing = _AUDIT_BLOCK_RE.findall(body)
    stripped = _AUDIT_BLOCK_RE.sub("", body).rstrip() + "\n"

    # New block goes at the end; prune to audit_window_size keeping the
    # most recent (which is the new one + the tail of the old).
    combined = existing + [new_block]
    if audit_window_size > 0:
        combined = combined[-audit_window_size:]

    body = stripped.rstrip("\n") + "\n\n" + "\n".join(combined) + "\n"
    post.content = body
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _archive_directive(
    md_path: Path,
    directive: str,
    result: ExecutionResult,
    archive_field: str = "alfred_instructions_last",
    pending_field: str = "alfred_instructions",
) -> None:
    """Clear the directive from the pending queue and prepend to the archive.

    The archive entry shape is ``{text, executed_at, result}`` per the
    plan. ``result`` combines the status and the 1-line summary into a
    single string so the frontmatter stays simple YAML.
    """
    if not md_path.exists():
        return
    post = frontmatter.load(str(md_path))

    pending = post.metadata.get(pending_field, []) or []
    if isinstance(pending, str):
        pending = [pending]
    if isinstance(pending, list):
        pending = [p for p in pending if p != directive]
    post.metadata[pending_field] = pending

    archive = post.metadata.get(archive_field, []) or []
    if isinstance(archive, str):
        archive = [archive]
    if not isinstance(archive, list):
        archive = []
    entry = {
        "text": directive,
        "executed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "result": f"{result.status}: {result.summary}",
    }
    # Prepend so the newest entry is at index 0.
    archive.insert(0, entry)
    post.metadata[archive_field] = archive

    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _surface_error(
    md_path: Path,
    directive: str,
    error_summary: str,
    pending_field: str = "alfred_instructions",
    error_field: str = "alfred_instructions_error",
) -> None:
    """On max-retries exceeded, drop the directive and flag the error.

    The error field is a plain string (not a list) — operator sees one
    most-recent failure per record. Retries on a single directive rarely
    exceed max, so per-directive history isn't worth the schema cost.
    """
    if not md_path.exists():
        return
    post = frontmatter.load(str(md_path))

    pending = post.metadata.get(pending_field, []) or []
    if isinstance(pending, str):
        pending = [pending]
    if isinstance(pending, list):
        pending = [p for p in pending if p != directive]
    post.metadata[pending_field] = pending

    post.metadata[error_field] = (
        f"Failed after max retries: {directive!r} — {error_summary}"
    )

    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


# --- Skill loader -----------------------------------------------------------


def _load_skill(skills_dir: Path, config: InstructorConfig | None = None) -> str:
    """Load ``vault-instructor/SKILL.md`` and apply instance templating.

    ``{{instance_name}}`` and ``{{instance_canonical}}`` placeholders
    are substituted from ``config.instance`` — same mechanism the
    talker uses (see ``alfred.telegram.daemon._apply_instance_templating``).
    Plain ``str.replace``, zero extra deps.

    Raises ``FileNotFoundError`` if the skill is missing so a
    misconfigured ``skills_dir`` surfaces at the first directive
    dispatch rather than silently succeeding with an empty prompt.
    """
    skill_path = skills_dir / "vault-instructor" / "SKILL.md"
    if not skill_path.exists():
        raise FileNotFoundError(
            f"vault-instructor SKILL.md not found at {skill_path}. "
            f"Ensure the skills_dir is correct — commit 5 ships the "
            f"default skill file."
        )
    prompt = skill_path.read_text(encoding="utf-8")
    if config is not None:
        prompt = (
            prompt
            .replace("{{instance_name}}", config.instance.name)
            .replace("{{instance_canonical}}", config.instance.canonical)
        )
    return prompt


# --- Main executor ----------------------------------------------------------


async def execute(
    client: Any,
    directive: str,
    record_path: str,
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    session_path: str | None = None,
) -> ExecutionResult:
    """Execute one directive against the vault via the Anthropic SDK.

    ``client`` is an ``AsyncAnthropic`` (or a fake with the same
    ``messages.create(**kwargs)`` surface) so tests can inject a
    deterministic tool-use script without touching the real API.

    ``record_path`` is the vault-relative path of the target record —
    the directive's "home." Cross-record mutations are allowed (the
    scope permits it), but the record_path is passed in the user
    message so the model knows the default target.

    ``skills_dir`` lands the SKILL.md for the system prompt. Raises
    FileNotFoundError if the bundle isn't installed — callers must
    provide a valid skills_dir.
    """
    dry_run = is_destructive(directive, config.destructive_keywords)
    system_prompt = _load_skill(skills_dir, config)

    # ``vault_path`` is still needed below (the tool-dispatch loop threads it
    # into the vault ops layer). Arc #18 M6 removed the
    # ``md_path = vault_path / record_path`` that used to sit here and was
    # never read — ``execute`` addresses records through vault ops, not through
    # this path. Deleted rather than gated: an ungated composition nothing
    # dereferences is not a containment hole, and gating it would have added a
    # refusal branch that can never fire plus a pin asserting nothing about
    # production. Removing the site is the stronger containment. The LIVE
    # composition is in ``execute_and_record`` below.
    vault_path = config.vault.vault_path

    # Build the initial user message. We include the directive, the
    # target record path, and the dry-run flag so the model can reason
    # about it — the SKILL covers the dry-run contract, but restating
    # it in the user turn makes the signal impossible to miss.
    user_turn_text = (
        f"Directive: {directive}\n"
        f"Target record: {record_path}\n"
        f"dry_run: {str(dry_run).lower()}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_turn_text}
    ]

    mutated_paths: list[str] = []
    final_text = ""
    tool_iterations = 0

    for iteration in range(MAX_TOOL_ITERATIONS):
        tool_iterations = iteration + 1
        try:
            # Route create-kwargs through the shared quirk gate so every
            # ``messages.create`` call site stays quirk-safe by construction.
            # The instructor passes no ``temperature`` today (so this is a
            # no-op on the current dict), but folding it here means a future
            # temperature addition can't 400 on an Opus-family model.
            response = await client.messages.create(
                **messages_create_kwargs(
                    model=config.anthropic.model,
                    max_tokens=config.anthropic.max_tokens,
                    system=system_prompt,
                    messages=messages,
                    tools=VAULT_TOOLS,
                )
            )
        except anthropic.APIError as exc:
            log.warning(
                "instructor.executor.api_error",
                iteration=iteration,
                error=str(exc),
            )
            return ExecutionResult(
                status="error",
                summary=f"SDK error: {exc}",
                mutated_paths=mutated_paths,
                tool_iterations=tool_iterations,
                dry_run=dry_run,
            )

        stop_reason = getattr(response, "stop_reason", "end_turn")

        if stop_reason == "tool_use":
            # Record the assistant turn so the next API call sees the
            # tool_use IDs we're about to respond to.
            messages.append({
                "role": "assistant",
                "content": _blocks_to_jsonable(response.content),
            })

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                btype = getattr(block, "type", None)
                if btype != "tool_use":
                    continue
                tool_name = getattr(block, "name", "") or ""
                tool_input = getattr(block, "input", {}) or {}
                tool_use_id = getattr(block, "id", "") or ""

                log.info(
                    "instructor.tool.invoke",
                    iteration=iteration,
                    tool=tool_name,
                    dry_run=dry_run,
                )
                result_str = _dispatch_tool(
                    tool_name,
                    tool_input if isinstance(tool_input, dict) else {},
                    vault_path,
                    dry_run,
                    session_path,
                    mutated_paths,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn — extract the final assistant text and parse the JSON
        # summary the SKILL requires.
        final_text = _extract_text(response.content)
        break

    else:
        # Safety cap hit — record and return.
        log.warning(
            "instructor.executor.iteration_cap",
            directive=directive,
            cap=MAX_TOOL_ITERATIONS,
        )
        return ExecutionResult(
            status="error",
            summary="Tool-use loop exceeded safety cap",
            mutated_paths=mutated_paths,
            tool_iterations=tool_iterations,
            dry_run=dry_run,
        )

    # Parse the JSON summary the SKILL asks for:
    #   {"status": "done|ambiguous|refused", "summary": "..."}
    # Fall back to treating the whole reply as the summary if the model
    # didn't comply.
    status, summary = _parse_agent_summary(final_text)

    # Dry-run overrides status — the plan sets ``status="dry_run"`` for
    # the archive entry regardless of what the model said internally.
    if dry_run:
        status = "dry_run"
        if not summary:
            summary = "destructive keyword detected — plan only"

    return ExecutionResult(
        status=status,
        summary=summary,
        mutated_paths=mutated_paths,
        tool_iterations=tool_iterations,
        dry_run=dry_run,
    )


async def execute_and_record(
    client: Any,
    directive: str,
    record_path: str,
    config: InstructorConfig,
    state: InstructorState,
    skills_dir: Path,
    session_path: str | None = None,
) -> ExecutionResult:
    """Run ``execute`` and then mutate the record according to the outcome.

    - On ``status == "done"`` (or ``"dry_run"``), archive the directive
      to ``alfred_instructions_last``, append an audit comment, clear
      the retry counter.
    - On ``status == "error"``, bump the retry counter. At
      ``max_retries``, surface to ``alfred_instructions_error`` and
      drop the directive.
    - On ``status in {"ambiguous", "refused"}`` the directive still
      gets archived (the operator needs the feedback) but no audit
      comment is appended — the record body wasn't meaningfully
      changed.
    """
    vault_path = config.vault.vault_path
    # Arc #18 M6 containment gate. This is the executor's LIVE composition:
    # ``md_path`` feeds three writers below (``_surface_error``,
    # ``_archive_directive``, and the audit-comment append), each of which does
    # a ``write_text``. ``record_path`` originates from an operator directive
    # in record frontmatter, so it is the least-trusted input of the four
    # writers in this arc.
    #
    # Refusal returns an ``error``-status ExecutionResult rather than raising:
    # the caller's contract is a result object, and the directive that named an
    # unreachable record still needs to surface to the operator. It short-
    # circuits BEFORE ``execute`` runs, so a directive naming an out-of-vault
    # record never reaches the model or spends a token.
    try:
        md_path = resolve_in_vault(
            vault_path, record_path,
            writer="instructor.execute_and_record",
        )
    except VaultContainmentError:
        log.warning(
            "instructor.path_escape_denied",
            record_path=str(record_path)[:200],
        )
        return ExecutionResult(
            status="error",
            summary=f"record path is not inside the vault: {record_path}",
        )

    result = await execute(
        client=client,
        directive=directive,
        record_path=record_path,
        config=config,
        state=state,
        skills_dir=skills_dir,
        session_path=session_path,
    )

    if result.status == "error":
        # Retry path: bump the counter; surface on max.
        new_count = state.bump_retry(record_path)
        if new_count >= config.max_retries:
            _surface_error(md_path, directive, result.summary)
            state.clear_retry(record_path)
        return result

    # Successful / ambiguous / refused / dry_run → archive + clear retry.
    _archive_directive(md_path, directive, result)
    state.clear_retry(record_path)

    if result.status == "done":
        _append_audit_comment(
            md_path,
            directive,
            result.summary,
            config.audit_window_size,
        )

    return result


# --- Helpers ----------------------------------------------------------------


def _extract_text(content: Any) -> str:
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
    """Convert response content to plain JSON-serialisable dicts.

    Mirrors the talker's helper of the same name. Preserves tool_use
    IDs so the matching ``tool_result`` lands on the right call.
    """
    if not content:
        return []
    out: list[dict[str, Any]] = []
    for block in content:
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


_SUMMARY_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_agent_summary(text: str) -> tuple[str, str]:
    """Parse the SKILL-mandated final summary block.

    Expected format:
      ``{"status": "done|ambiguous|refused", "summary": "<1-line>"}``

    Returns ``(status, summary)``. Falls back to
    ``("done", <text truncated>)`` when the JSON can't be parsed —
    better to record a noisy success than lose the agent's output.
    """
    if not text:
        return "done", ""
    match = _SUMMARY_JSON_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                status = str(payload.get("status") or "done")
                summary = str(payload.get("summary") or "").strip()
                if status in {"done", "ambiguous", "refused"} and summary:
                    return status, summary
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback — truncate to a reasonable length so it fits in
    # frontmatter without bloating the file.
    return "done", text[:200].strip()
