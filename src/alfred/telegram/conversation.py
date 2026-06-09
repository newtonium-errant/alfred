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

import asyncio
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import anthropic

from alfred.audit import agent_slug_for

from ._anthropic_compat import messages_create_kwargs
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
            "person, organisation, location, or project that doesn't yet "
            "have its own record. The record name is the filename stem. "
            "``routine`` is for recurring practices — see the SKILL's "
            "'Creating routines' section for cadence + due_pattern grammar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    # Mirrors ``TALKER_CREATE_TYPES`` in vault/scope.py.
                    # The scope layer is the source of truth; this enum
                    # is a soft rail that keeps the LLM from inventing
                    # types it can't actually create. ``org`` and
                    # ``location`` were added 2026-04-25 after Salem
                    # repeatedly hit the scope wall on new businesses
                    # and addresses; ``project``, ``constraint``, and
                    # ``contradiction`` round out the conversational
                    # surface for kick-off + reflection turns.
                    # ``routine`` added 2026-05-30 (Phase 2B B2) for
                    # conversational routine record creation; the
                    # SKILL's "Creating routines" section documents
                    # the cadence + due_pattern + target_cadence_days
                    # grammar. Routine type is canonical (Salem-
                    # native); per-instance isolation is SINGLE-GATE
                    # via the scope layer's ``kalle_types_only`` /
                    # ``hypatia_types_only`` handlers, which reject
                    # canonical types like ``routine`` that aren't in
                    # ``KALLE_CREATE_TYPES`` / ``HYPATIA_CREATE_TYPES``.
                    # The type validator does NOT refuse non-Salem
                    # creates of ``routine`` — it accepts canonical
                    # types under every named scope. See scope.py's
                    # ``routine`` comment for the per-instance-isolation
                    # walkthrough.
                    "enum": [
                        "task", "note", "decision", "event", "person",
                        "org", "location", "project",
                        "session", "conversation",
                        "assumption", "constraint", "contradiction",
                        "synthesis",
                        # ``preference`` (operator-preference V1,
                        # 2026-05-24) lagged the enum until the
                        # B2 review's tightened lockstep pin
                        # (set-difference vs literal-membership)
                        # surfaced the drift on 2026-05-30. Added
                        # here so the enum mirrors TALKER_CREATE_TYPES.
                        "preference",
                        "routine",
                        # c6 (2026-05-31) — ``daily`` for pre-setting
                        # tomorrow's tier_curation. ``name`` must be
                        # an ISO date >= today; ``set_fields`` must
                        # contain ONLY ``tier_curation`` (other
                        # daily/ fields are aggregator-owned). The
                        # aggregator's 05:59 ADT fire preserves the
                        # pre-set block. See SKILL "Pre-setting
                        # tomorrow's tier list" section.
                        "daily",
                    ],
                    "description": (
                        "Record type. Use ``person`` for individuals, "
                        "``org`` for businesses/groups, ``location`` "
                        "for places/addresses, ``project`` for new "
                        "initiatives Andrew kicks off, ``routine`` "
                        "for recurring practices (daily / weekly / "
                        "monthly cadence; see SKILL's 'Creating "
                        "routines' section), and the learn types "
                        "(``assumption``, ``constraint``, "
                        "``contradiction``, ``synthesis``) when "
                        "surfacing latent knowledge."
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
                        "``{\"status\": \"todo\", \"due\": \"2026-05-01\"}``. "
                        "MUST NOT contain ``body`` — body content goes in "
                        "the top-level ``body`` parameter below, not as a "
                        "frontmatter field."
                    ),
                    "not": {"required": ["body"]},
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
            "Edit an existing vault record. Frontmatter goes via "
            "``set_fields`` (overwrite) or ``append_fields`` (add to "
            "list fields). Body content goes via exactly ONE of: "
            "``body_append`` (most common — add to end), "
            "``body_insert_at`` (anchored mid-document insertion — "
            "specific use case), or ``body_replace`` (full rewrite — "
            "rare; explicit user request only). The four body-mutation "
            "kwargs are MUTUALLY EXCLUSIVE — at most one per call. "
            "``body_insert_at`` and ``body_replace`` are gated per-"
            "instance × per-type (see scope matrix); some types and "
            "scopes refuse them outright."
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
                    "description": (
                        "Frontmatter fields to overwrite. MUST NOT contain "
                        "``body`` — that's a markdown-content concept, not "
                        "a frontmatter field. Use ``body_append`` for body "
                        "edits. The vault-ops gate strips ``body`` keys "
                        "from this dict and emits a warning if present."
                    ),
                    "not": {"required": ["body"]},
                },
                "append_fields": {
                    "type": "object",
                    "description": "Fields to append to (list fields).",
                },
                "body_append": {
                    "type": "string",
                    "description": (
                        "Markdown to append to the END of the body. Most "
                        "common body edit. Mutually exclusive with "
                        "body_insert_at and body_replace."
                    ),
                },
                "body_insert_at": {
                    "type": "object",
                    "description": (
                        "Anchored mid-document insertion. ``marker`` is a "
                        "LINE-EXACT match (the WHOLE line, not substring "
                        "or regex — typically a heading like '## Section "
                        "name'). ``position`` is 'before' or 'after'. "
                        "``content`` is the markdown block to insert. "
                        "Marker not found → clean error. Mutually "
                        "exclusive with body_append and body_replace."
                    ),
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
                "body_replace": {
                    "type": "string",
                    "description": (
                        "Full body rewrite (frontmatter preserved). HIGH-"
                        "RISK — use only when the user explicitly asks for "
                        "a full rewrite. Per-instance × per-type allowlist "
                        "applies. Salem ``event`` records with a synced "
                        "GCal mirror REFUSE body_replace at the scope "
                        "layer; vault_delete the event first to clear "
                        "the GCal mirror, then create a fresh record. "
                        "Mutually exclusive with body_append and "
                        "body_insert_at."
                    ),
                },
            },
            "required": ["path"],
            # Mutual exclusion of the four body-mutation kwargs is
            # enforced at the RUNTIME GATE in ``vault.ops.vault_edit``
            # (raises ``VaultError("at most ONE body-mutation kwarg
            # per call")``), NOT at the JSON-schema layer. Earlier
            # ship of this schema (commit ``0d7e7a6``) included a
            # top-level ``oneOf`` here as defense in depth, but the
            # Anthropic Messages API rejects ``oneOf`` / ``allOf`` /
            # ``anyOf`` at the top level of any tool's
            # ``input_schema`` with HTTP 400 BEFORE the model runs:
            #
            #   tools.N.custom.input_schema: input_schema does not
            #   support oneOf, allOf, or anyOf at the top level
            #
            # Surfaced 2026-05-06 when Salem (+ KAL-LE + Hypatia)
            # restarted into the cherry-picked code; every conversation
            # carrying the tool list 400'd at the request validator
            # before any token cost was incurred. Fix is removal —
            # the runtime gate in vault_edit is the load-bearing
            # protection (covered by ``tests/test_vault_edit_body_mutation.py``).
            # Property-level descriptions on body_append / body_insert_at
            # / body_replace each say "Mutually exclusive with ..."
            # so the LLM still gets the constraint as guidance.
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
                "description": (
                    "Frontmatter fields to set. MUST NOT contain ``body`` — "
                    "body content goes in the top-level ``body`` parameter."
                ),
                "not": {"required": ["body"]},
            },
            "body": {
                "type": "string",
                "description": "Markdown body for the record.",
            },
        },
        "required": ["type", "name"],
    },
}


# Phase 2B B1 (2026-05-30) — ``routine_done`` schema.
#
# Salem-only tool that lets the talker log a routine-item completion in
# response to conversational phrasing ("I walked the dog", "exercise
# done", "I read for 30 min last Tuesday"). The dispatch in
# ``_execute_tool`` invokes ``alfred routine done`` as a subprocess
# (the CLI is the single source of truth for the completion_log mutation
# semantics; reusing it via subprocess avoids duplicating the
# frontmatter round-trip + idempotency logic).
#
# Tight scope: the talker tool can ONLY log completions, NOT mutate
# other routine fields (cadence, items, due_pattern, etc.). The
# subprocess invocation uses ``ALFRED_VAULT_SCOPE=talker_routine_completion``
# (added in scope.py same ship) so even if the CLI grows new mutation
# paths in the future, this surface stays narrow.
#
# Cross-agent contract: the SKILL ``Marking routines done`` section
# recognises the conversational phrasings and calls this tool. The
# canary discriminator (``kind``) in the response shapes Salem's reply
# (success / ambiguous → ask back with candidates / unknown_item /
# idempotent_noop / future_date_rejected).
_ROUTINE_DONE_TOOL_SCHEMA = {
    "name": "routine_done",
    "description": (
        "Log a routine item as completed. Salem-only tool (the routine "
        "subsystem is Salem-only in Phase 1/2). Use this when the "
        "operator says they did something on their list of routines "
        "(e.g. 'I walked the dog', 'exercise done', 'I read for an "
        "hour yesterday', 'finished my meditation'). The tool fuzzy-"
        "matches the item across all active routines — pass just "
        "``item`` for vault-wide match (preferred for most operator "
        "phrasings), or pass ``record`` + ``item`` to target a "
        "specific routine. Returns a structured ``kind`` discriminator "
        "you MUST route on:\n"
        "  * 'success' — completion logged; reply confirming\n"
        "  * 'idempotent_noop' — already logged today; tell the "
        "operator gently\n"
        "  * 'ambiguous_item' — multiple matches; ASK BACK with the "
        "numbered candidate list (do NOT guess)\n"
        "  * 'unknown_item' — no matching item; tell the operator + "
        "list known items if helpful\n"
        "  * 'unknown_record' — explicit record name not found\n"
        "  * 'future_date_rejected' — ``completed_at`` is in the "
        "future; ask the operator to clarify\n"
        "Back-dating supported via ``completed_at`` (YYYY-MM-DD). "
        "Default is today. Use 'yesterday' → today−1, 'two days ago' "
        "→ today−2, 'last Tuesday' → most-recent-past-Tuesday, etc. "
        "DO NOT mutate completion_log via vault_edit directly — this "
        "tool is the only authorised path because it goes through "
        "the talker_routine_completion narrow scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "item": {
                "type": "string",
                "description": (
                    "The item text the operator named. Fuzzy match "
                    "(substring + stem-tolerant) — operator phrasing "
                    "doesn't need to be exact."
                ),
            },
            "record": {
                "type": "string",
                "description": (
                    "OPTIONAL: routine record name (e.g. 'For Self "
                    "Health'). Omit to fuzzy-match the item across "
                    "all active routines vault-wide (preferred)."
                ),
            },
            "completed_at": {
                "type": "string",
                "description": (
                    "OPTIONAL: YYYY-MM-DD back-date for the "
                    "completion. Omit for today. Future dates "
                    "rejected by the CLI."
                ),
            },
        },
        "required": ["item"],
    },
}


# Phase 2B B3 (2026-05-30) — ``routine_item`` schema.
#
# Salem-only tool for item-level CRUD on existing routine records.
# Three actions discriminated by the ``action`` field
# (add / remove / edit). The dispatch in ``_execute_tool`` invokes
# ``alfred routine item <action>`` as a subprocess with the
# ``talker_routine_item`` scope.
#
# Cross-agent contract: the SKILL ``Adjusting routines`` section
# recognises operator phrasing for all three actions + builds the
# ``routine_item`` tool call. The canary discriminator (``kind``) in
# the response shapes Salem's reply (added / removed / edited /
# ambiguous → ask back / cadence_conflict → ask back with explicit
# clear-flag offer / etc).
#
# Mutually-exclusive cadence enforcement at write time, NOT at read
# time — see ``cli_items.py``'s ``_check_cadence_conflict_on_edit``
# for the precedence rule.
_ROUTINE_ITEM_TOOL_SCHEMA = {
    "name": "routine_item",
    "description": (
        "Item-level CRUD on existing routine records. Salem-only "
        "(the routine subsystem is Salem-only in Phase 1/2). Three "
        "actions discriminated by the ``action`` field:\n"
        "  * 'add' — append new item to a routine's items list. "
        "Requires ``record`` (vault-wide fuzzy doesn't apply when "
        "adding a NEW item that doesn't exist anywhere).\n"
        "  * 'remove' — delete one item by text match. Strips "
        "completion_log entries atomically. ``record`` optional "
        "(vault-wide fuzzy applies).\n"
        "  * 'edit' — change fields on one item by text match. "
        "Rename (``fields.text``) migrates completion_log key "
        "atomically. ``record`` optional.\n"
        "\n"
        "Returns a structured ``kind`` discriminator you MUST route "
        "on:\n"
        "  * 'added' / 'removed' / 'edited' — operation succeeded; "
        "reply confirming the change\n"
        "  * 'ambiguous_item' — multiple matches; ASK BACK with the "
        "numbered candidate list (do NOT guess)\n"
        "  * 'unknown_item' / 'unknown_record' — no match; tell the "
        "operator + list alternatives if helpful\n"
        "  * 'cadence_conflict' — operator wants to switch hard ↔ "
        "soft cadence mode without explicit clear flag. ASK BACK "
        "naming the conflict + offer to add the clear flag\n"
        "  * 'duplicate_item' (add only) — text matches existing "
        "item; ask back\n"
        "  * 'invalid_field' — operator-supplied value failed "
        "validation (e.g. negative cadence days); tell the operator "
        "the validation error\n"
        "\n"
        "Mutually-exclusive cadence: each item carries EITHER "
        "``target_cadence_days`` (soft, T3 auto-suggest) OR "
        "``due_pattern`` (hard, T1/T2 auto-surface), never both. "
        "Switching modes on an existing item requires the "
        "``fields.clear_due_pattern`` or "
        "``fields.clear_target_cadence_days`` flag set to true.\n"
        "\n"
        "DO NOT mutate routine items via ``vault_edit`` directly — "
        "this tool is the only authorised path because it goes "
        "through the talker_routine_item narrow scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "edit"],
                "description": (
                    "Which CRUD action. 'add' requires record + "
                    "item (item becomes the new text). 'remove' / "
                    "'edit' require item; record optional (vault-"
                    "wide fuzzy fallback)."
                ),
            },
            "item": {
                "type": "string",
                "description": (
                    "Item text. For 'add', this is the NEW item's "
                    "text. For 'remove' / 'edit', this is the "
                    "EXISTING item's text — fuzzy-matched (substring "
                    "+ stem-tolerant)."
                ),
            },
            "record": {
                "type": "string",
                "description": (
                    "Routine record name (e.g. 'For Self Health'). "
                    "REQUIRED for 'add'. Optional for 'remove' / "
                    "'edit' — omit to fuzzy-match the item across "
                    "all active routines vault-wide."
                ),
            },
            "fields": {
                "type": "object",
                "description": (
                    "Per-action field bundle. For 'add' / 'edit': "
                    "``priority`` ('critical'/'tracked'/'aspirational'), "
                    "``target_cadence_days`` (int > 0; soft cadence), "
                    "``due_pattern`` (dict per DUE_PATTERN_TYPES; "
                    "hard cadence), ``surface_at_days`` (int > 0; "
                    "T2 ramp), ``escalate_at_days`` (int >= 0; T1 "
                    "escalation). 'edit'-only: ``text`` (new item "
                    "text — rename, migrates completion_log), "
                    "``clear_due_pattern`` (bool; switch hard → "
                    "soft), ``clear_target_cadence_days`` (bool; "
                    "switch soft → hard). Omit entirely for 'remove' "
                    "(no fields needed)."
                ),
            },
        },
        "required": ["action", "item"],
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


# Stage 3.5 / inter-instance Phase A: peer instances (KAL-LE, Hypatia)
# need to read + propose canonical records on Salem. These tools live
# alongside the vault-bridge tools and fan out via the transport client
# rather than the local vault layer.
#
# - ``query_canonical(type, name)``: GET /canonical/{type}/{name} on
#   Salem. Returns the peer-visible frontmatter subset, or 404
#   structured response. The Pattern 1 read primitive.
# - ``propose_event(...)``: synchronous /canonical/event/propose-create
#   with conflict-check. Mid-conversation create — Andrew gets the
#   answer inline.
# - ``propose_org(name, fields)``: queued /canonical/org/propose. Andrew
#   confirms in next Daily Sync.
# - ``propose_location(name, fields)``: queued /canonical/location/propose.
#
# Salem (talker scope) does NOT get these tools — Salem is the canonical
# authority and creates these records directly via vault_create.

_QUERY_CANONICAL_TOOL = {
    "name": "query_canonical",
    "description": (
        "Look up a canonical record on Salem (the canonical authority "
        "for person/org/location/event records). Use this to fetch a "
        "person's email/timezone, an event's start/end, an org's "
        "address, etc. before composing a reply. Returns the "
        "peer-visible frontmatter subset on hit, or "
        "``{\"status\": \"not_found\"}`` on miss."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "record_type": {
                "type": "string",
                "enum": ["person", "org", "location", "event", "project"],
                "description": "Canonical record type.",
            },
            "name": {
                "type": "string",
                "description": "Record name (filename stem on Salem).",
            },
        },
        "required": ["record_type", "name"],
    },
}


_PROPOSE_PERSON_TOOL = {
    "name": "propose_person",
    "description": (
        "Propose a new canonical person record on Salem (queued — "
        "Andrew confirms or rejects in the next Daily Sync). Use when "
        "Andrew names a new individual mid-conversation. "
        "Do NOT use for someone who already has a record; query "
        "canonical first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Canonical full name (becomes Salem's filename stem).",
            },
            "fields": {
                "type": "object",
                "description": (
                    "Optional frontmatter fields Salem may set on "
                    "creation (e.g. email, role, description)."
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "Brief origin note: which session / commit / "
                    "context triggered this proposal."
                ),
            },
        },
        "required": ["name"],
    },
}


_PROPOSE_ORG_TOOL = {
    "name": "propose_org",
    "description": (
        "Propose a new canonical org record on Salem (queued — "
        "Andrew confirms in the next Daily Sync). Use when a new "
        "company / NGO / agency surfaces in conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Org canonical name.",
            },
            "fields": {
                "type": "object",
                "description": "Optional frontmatter fields (type, description, ...).",
            },
            "source": {
                "type": "string",
                "description": "Origin context for the proposal.",
            },
        },
        "required": ["name"],
    },
}


_PROPOSE_LOCATION_TOOL = {
    "name": "propose_location",
    "description": (
        "Propose a new canonical location record on Salem (queued — "
        "Andrew confirms in the next Daily Sync). Use for "
        "addresses / venues / places mentioned mid-conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Location canonical name.",
            },
            "fields": {
                "type": "object",
                "description": (
                    "Optional frontmatter (address, description, ...)."
                ),
            },
            "source": {
                "type": "string",
                "description": "Origin context for the proposal.",
            },
        },
        "required": ["name"],
    },
}


_PROPOSE_EVENT_TOOL = {
    "name": "propose_event",
    "description": (
        "Synchronously propose-create a calendar event on Salem. "
        "Salem either creates the event (returns ``{status: created, "
        "path}``) or detects a vault time-conflict and returns "
        "``{status: conflict, conflicts: [...]}``. Use when Andrew "
        "asks to schedule something mid-conversation. If Salem flags "
        "a conflict, surface it inline ('you have an X at 14:00 — "
        "reschedule to 16:00?'). Times must be ISO 8601 with "
        "timezone (e.g. '2026-05-04T14:00:00-03:00'). "
        "PARTIAL-FAILURE SURFACE: a ``status: created`` response MAY "
        "also carry a ``gcal_sync`` field describing whether the "
        "Google Calendar projection succeeded. Three states: "
        "``{status: ok}`` — vault AND calendar both updated; narrate "
        "calendar success freely. ``{status: failed, error_code, "
        "error}`` — vault saved, calendar NOT updated; tell Andrew "
        "explicitly the event is in the vault but Google Calendar "
        "didn't sync, and switch on ``error_code`` (e.g. "
        "``auth_failed`` → 'looks like the GCal token needs a "
        "refresh'). ``gcal_sync`` field ABSENT — Salem doesn't sync "
        "to GCal for this op (no calendar mention needed). DO NOT "
        "narrate calendar success unless ``gcal_sync.status == 'ok'``."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Event title (becomes the filename + title field).",
            },
            "start": {
                "type": "string",
                "description": "ISO 8601 start datetime with timezone offset.",
            },
            "end": {
                "type": "string",
                "description": "ISO 8601 end datetime with timezone offset.",
            },
            "summary": {
                "type": "string",
                "description": "Optional summary / agenda.",
            },
            "origin_context": {
                "type": "string",
                "description": (
                    "Brief context for traceability (which session / "
                    "conversation produced this proposal)."
                ),
            },
        },
        "required": ["title", "start", "end"],
    },
}


# Inter-instance peer tools — added to peer instances' tool sets only.
# Salem (talker scope) does not get these tools because Salem IS the
# canonical authority — it creates these records directly via
# vault_create rather than proposing them to itself.
_PEER_INTER_INSTANCE_TOOLS: list[dict[str, Any]] = [
    _QUERY_CANONICAL_TOOL,
    _PROPOSE_PERSON_TOOL,
    _PROPOSE_ORG_TOOL,
    _PROPOSE_LOCATION_TOOL,
    _PROPOSE_EVENT_TOOL,
]


# GCal read tool. Surfaced ONLY when the active config carries
# ``gcal.enabled: true`` — see ``tools_for_set`` for the runtime gating
# (the schema is dropped from the tool list entirely on instances
# without GCal so the model can't hallucinate a "read calendar"
# capability that isn't wired). Salem is the only current consumer.
#
# Output shape is intentionally chat-friendly (title / start / end /
# location / description-truncated), NOT the full GCal API response.
# The model is answering "do I have anything Tuesday?", not driving
# a UI; pruning at the dispatch layer keeps tokens cheap and the
# response readable in transcript review. ``raw`` debug field
# deliberately omitted — operator who wants full event JSON uses the
# ``alfred gcal`` CLI.
_GCAL_LIST_EVENTS_TOOL = {
    "name": "gcal_list_events",
    # Instance-agnostic wording — per CLAUDE.md "Three Layers" framing,
    # tool descriptions are CODE-layer content shipped to every instance
    # that opts into GCal. Per-instance specifics (e.g. which calendar
    # is "the shared calendar" for Salem vs V.E.R.A.) belong in the
    # PROMPT layer (SKILL.md), not embedded here. Earlier wording named
    # "Salem" + "Andrew's Calendar (S.A.L.E.M.)" — would have leaked
    # to KAL-LE / Hypatia / future instances the moment they enable
    # GCal in their own configs.
    "description": (
        "Read events from a Google Calendar by date range. The instance "
        "has read access to BOTH calendars: the configured shared "
        "calendar AND Andrew's primary personal calendar. Use when the "
        "user asks 'do I have anything on Tuesday?' / 'what's on my "
        "calendar this week?' / 'is there a CannaConnect appointment I "
        "should know about?'. Read-only — use vault_create on `event` "
        "records to add to the shared calendar; cannot write to the "
        "primary calendar. Returns a list of ``{title, start, end, "
        "location, description}`` dicts with start/end in ISO 8601 "
        "format."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "calendar": {
                "type": "string",
                "enum": ["alfred", "primary"],
                "description": (
                    "Which calendar to read. ``alfred`` = the configured "
                    "shared calendar (writable); ``primary`` = Andrew's "
                    "personal calendar (read-only)."
                ),
            },
            "start": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime, inclusive lower bound. Must be "
                    "timezone-aware (e.g. ``2026-05-07T00:00:00-03:00``). "
                    "GCal's events.list ``timeMin``."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime, exclusive upper bound. Must be "
                    "timezone-aware. GCal's events.list ``timeMax``."
                ),
            },
        },
        "required": ["calendar", "start", "end"],
    },
}


KALLE_VAULT_TOOLS: list[dict[str, Any]] = [
    TALKER_VAULT_TOOLS[0],  # vault_search
    TALKER_VAULT_TOOLS[1],  # vault_read
    _KALLE_VAULT_CREATE_TOOL,
    TALKER_VAULT_TOOLS[3],  # vault_edit
    _BASH_EXEC_TOOL_SCHEMA,
    *_PEER_INTER_INSTANCE_TOOLS,
]


# Hypatia gets the talker-style vault tools (vault_search/read/create/edit
# with hypatia create allowlist enforced at scope.py) plus the inter-
# instance peer tools so the scribe can ask Salem to register people,
# orgs, locations, and events mentioned in research/business sessions.
HYPATIA_VAULT_TOOLS: list[dict[str, Any]] = [
    *TALKER_VAULT_TOOLS,
    *_PEER_INTER_INSTANCE_TOOLS,
]


# Tool-set registry — selected by ``telegram.instance.tool_set`` in
# config.yaml (c1 wiring). Default ``"talker"`` preserves Salem's
# existing behaviour; KAL-LE's ``config.kalle.yaml`` sets
# ``tool_set: "kalle"`` to pick up bash_exec + inter-instance tools;
# Hypatia (config.hypatia.yaml ``tool_set: "hypatia"``) gets the
# vault-only set + inter-instance tools.
#
# Salem must NOT get the inter-instance tools — Salem IS the canonical
# authority. Routing Salem through ``propose_*`` would cause every
# vault_create to round-trip through the transport against itself.
# Salem-only tool set — base TALKER_VAULT_TOOLS plus Salem-specific
# tools (currently just ``routine_done``, Phase 2B B1 2026-05-30).
# Hypatia + KAL-LE inherit ``*TALKER_VAULT_TOOLS`` separately without
# the Salem-specific tools — keeping this distinct lets us widen
# Salem's surface without leaking tools into the other instances.
SALEM_VAULT_TOOLS: list[dict[str, Any]] = [
    *TALKER_VAULT_TOOLS,
    _ROUTINE_DONE_TOOL_SCHEMA,
    # Phase 2B B3 (2026-05-30) — item-level CRUD on existing routines.
    _ROUTINE_ITEM_TOOL_SCHEMA,
]


VAULT_TOOLS_BY_SET: dict[str, list[dict[str, Any]]] = {
    "talker": SALEM_VAULT_TOOLS,
    "kalle": KALLE_VAULT_TOOLS,
    "hypatia": HYPATIA_VAULT_TOOLS,
}


def tools_for_set(
    set_name: str,
    *,
    gcal_enabled: bool = False,
) -> list[dict[str, Any]]:
    """Return the tool schema list for ``set_name`` (default ``talker``).

    ``gcal_enabled`` (Phase A capability-audit close 2026-05-06):
    when True, appends ``gcal_list_events`` to the returned list so
    the model can answer "do I have anything Tuesday?" honestly.
    Default False so instances without GCal wired (KAL-LE, Hypatia
    today; any future instance that doesn't opt in) don't surface a
    capability they can't fulfil — matches the honesty contract
    surfaced in conversation ``0e52c745`` ("Salem said 'I have no
    calendar read access at all'" because she literally had no
    tool for it).

    Caller (``run_turn``) reads ``gcal.enabled`` off the loaded
    GCalConfig; the schema is appended only when the operator opted
    in. The dispatch branch in ``_execute_tool`` lazy-loads the
    GCal client on each invocation (mirrors peer-tool pattern).
    """
    base = VAULT_TOOLS_BY_SET.get(set_name) or TALKER_VAULT_TOOLS
    if not gcal_enabled:
        return base
    # Append (don't mutate the registry) so subsequent calls without
    # the flag see the unaugmented list.
    return [*base, _GCAL_LIST_EVENTS_TOOL]


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
    """Strip metadata-only keys + heal dangling tool_use blocks before API send.

    Two responsibilities:

    1. **Metadata strip** (wk2): stamps like ``_ts`` / ``_kind`` are
       persisted to the transcript for session-record rendering but the
       Anthropic Messages API strictly validates message schemas and
       rejects unknown fields (400: ``Extra inputs are not permitted``).
       Keep metadata on the persisted transcript; send only the
       Anthropic-schema fields.

    2. **Dangling-tool_use heal** (race-fix 2026-05-03): if any assistant
       turn contains ``tool_use`` blocks but the immediately following
       turn doesn't carry matching ``tool_result`` blocks for every
       tool_use_id, the API returns
       ``messages.N: tool_use ids were found without tool_result blocks
       immediately after``. The wedge mode that hit Andrew's Hypatia
       session 2026-05-03 18:51-18:52: ``_execute_tool`` raised
       mid-loop, so ``run_turn`` exited without the
       ``append_turn(state, session, "user", tool_results)`` line that
       lands the matching tool_results. The next turn's ``run_turn``
       appended a regular user message, sealing the dangling state.
       The user retried twice → same wedge → same 400.

    The heal injects synthetic ``tool_result`` blocks (``is_error: True``,
    detail "execution interrupted; transcript healed") for any
    tool_use_id missing a matching tool_result. The synthetic results
    are inserted as a NEW user message immediately after the dangling
    assistant turn, ahead of any subsequent real user messages.

    The principal fix is the per-tool-call try/except in ``run_turn``'s
    execution loop (which prevents the wedge from being created in the
    first place). This heal is the seatbelt: covers daemon restarts
    mid-tool-execution, manual transcript edits, or any future code
    path that strands a tool_use block. Per
    ``feedback_intentionally_left_blank.md``: log the heal at warning
    level so an operator can grep ``conversation.transcript_healed``
    to spot the gap-then-recovery pattern.
    """
    stripped = [
        {k: v for k, v in turn.items() if not k.startswith("_")}
        for turn in transcript
    ]
    return _heal_dangling_tool_use(stripped)


def _collect_tool_use_ids(content: Any) -> list[str]:
    """Pull tool_use ids out of an assistant content list. Returns
    empty if content is a plain string or no tool_use blocks present."""
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            tid = block.get("id")
            if isinstance(tid, str) and tid:
                out.append(tid)
    return out


def _collect_tool_result_ids(content: Any) -> set[str]:
    """Pull tool_use_ids out of a user content list (tool_result blocks).
    Returns empty if content is a plain string or no tool_result blocks
    present."""
    if not isinstance(content, list):
        return set()
    out: set[str] = set()
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            tid = block.get("tool_use_id")
            if isinstance(tid, str) and tid:
                out.add(tid)
    return out


def _heal_dangling_tool_use(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk messages; insert synthetic tool_result blocks for any
    assistant tool_use that lacks a matching tool_result in the next
    turn.

    Idempotent: a transcript that already has matching tool_results
    passes through unchanged. The healer only adds; it never removes
    or modifies existing blocks.
    """
    out: list[dict[str, Any]] = []
    healed_count = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        out.append(msg)
        if msg.get("role") != "assistant":
            i += 1
            continue
        tool_use_ids = _collect_tool_use_ids(msg.get("content"))
        if not tool_use_ids:
            i += 1
            continue
        # Look at the NEXT message — must be a user message with
        # tool_result blocks for every id, in any order.
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        existing_result_ids: set[str] = set()
        if next_msg is not None and next_msg.get("role") == "user":
            existing_result_ids = _collect_tool_result_ids(next_msg.get("content"))
        missing = [tid for tid in tool_use_ids if tid not in existing_result_ids]
        if not missing:
            i += 1
            continue
        # Heal: build synthetic tool_result blocks for the missing ids.
        # We always insert a NEW user message immediately after the
        # dangling assistant turn rather than mutating the existing
        # next user message — keeps the "what arrived from the user"
        # transcript faithful + makes the heal visible in any audit.
        synthetic_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": (
                    "Tool execution was interrupted before completing. "
                    "Transcript healed by client to satisfy "
                    "tool_use/tool_result pairing — actual tool output "
                    "is unavailable. Please retry the tool call if "
                    "still needed, or proceed with what you have."
                ),
                "is_error": True,
            }
            for tid in missing
        ]
        synthetic_user_msg = {"role": "user", "content": synthetic_blocks}
        out.append(synthetic_user_msg)
        healed_count += len(missing)
        log.warning(
            "talker.conversation.transcript_healed",
            assistant_turn_index=i,
            dangling_tool_use_ids=missing,
            healed_block_count=len(missing),
            detail=(
                "synthesised tool_result blocks for tool_use ids that "
                "had no matching tool_result in the next turn — "
                "almost always a sign of a prior wedge from a tool-"
                "execution exception or daemon restart mid-loop"
            ),
        )
        i += 1

    if healed_count > 0:
        log.warning(
            "talker.conversation.transcript_heal_summary",
            total_healed=healed_count,
            assistant_turns_with_dangling=sum(
                1 for m in out
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and any(
                    isinstance(b, dict) and b.get("is_error") is True
                    and isinstance(b.get("content"), str)
                    and "Transcript healed" in b.get("content", "")
                    for b in m["content"]
                )
            ),
        )
    return out


# --- Startup dangling-tool_use detector (P2 from QA 2026-05-04) ----------


def detect_dangling_tool_use_at_startup(
    state: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Walk every active session's transcript at daemon boot; emit a
    structured warning per dangling tool_use detected.

    Permanent observability — without this, the only operator-visible
    tell that the talker wedged during a previous shutdown is the
    LLM parroting the heal's "interrupted before completing" wording
    back to the user on the NEXT turn. Detecting at startup lets an
    operator grep for ``talker.conversation.startup_dangling_tool_use``
    immediately after restart, before the user sees anything.

    Per ``feedback_intentionally_left_blank.md``: emits a summary
    info event ``talker.conversation.startup_dangling_tool_use_check_complete``
    after the walk so a clean state is observably distinct from the
    detector not having run.

    Args:
        state: A ``StateManager`` (duck-typed; we only access
            ``state.state["active_sessions"]``).
        now: Override for tests. Defaults to ``datetime.now(UTC)``.
            Used to compute ``time_since_last_message_seconds``.

    Returns:
        Total count of dangling tool_use ids detected across all
        sessions (summed over assistant turns).
    """
    from datetime import datetime as _dt, timezone as _tz

    if now is None:
        now = _dt.now(_tz.utc)

    sessions_state = {}
    try:
        sessions_state = state.state.get("active_sessions", {}) or {}
    except AttributeError:
        # Defensive — duck-typed input. Fall through to summary log.
        sessions_state = {}

    sessions_checked = 0
    sessions_with_dangling = 0
    total_dangling_ids = 0

    for chat_id_str, raw in sessions_state.items():
        sessions_checked += 1
        if not isinstance(raw, dict):
            continue
        transcript = raw.get("transcript")
        if not isinstance(transcript, list) or not transcript:
            continue
        session_id = raw.get("session_id", "")
        last_message_iso = raw.get("last_message_at", "")

        # Compute time-since-last-message for the diagnostic log.
        time_since_seconds: float | None = None
        if isinstance(last_message_iso, str) and last_message_iso:
            try:
                ts_str = (
                    last_message_iso.replace("Z", "+00:00")
                    if last_message_iso.endswith("Z") else last_message_iso
                )
                last_dt = _dt.fromisoformat(ts_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                time_since_seconds = (now - last_dt).total_seconds()
            except (ValueError, TypeError):
                time_since_seconds = None

        # Walk each assistant turn — for any tool_use ids in the
        # message, the IMMEDIATELY-following user turn must contain
        # tool_result blocks for every id. Mirrors the heal's gate.
        session_had_dangling = False
        for i, msg in enumerate(transcript):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            tool_use_ids = _collect_tool_use_ids(msg.get("content"))
            if not tool_use_ids:
                continue
            next_msg = transcript[i + 1] if i + 1 < len(transcript) else None
            existing_result_ids: set[str] = set()
            if (
                next_msg is not None
                and isinstance(next_msg, dict)
                and next_msg.get("role") == "user"
            ):
                existing_result_ids = _collect_tool_result_ids(
                    next_msg.get("content"),
                )
            missing = [
                tid for tid in tool_use_ids
                if tid not in existing_result_ids
            ]
            if not missing:
                continue
            session_had_dangling = True
            total_dangling_ids += len(missing)
            log.warning(
                "talker.conversation.startup_dangling_tool_use",
                chat_id=chat_id_str,
                session_id=session_id,
                assistant_turn_index=i,
                dangling_tool_use_ids=missing,
                count_of_dangling_ids=len(missing),
                time_since_last_message_seconds=time_since_seconds,
                detail=(
                    "Active session's transcript carries a dangling "
                    "tool_use turn at startup — the previous daemon "
                    "shutdown left tool_use ids without matching "
                    "tool_result blocks. The next ``run_turn`` will "
                    "trigger the heal in ``_messages_for_api``; this "
                    "log fires BEFORE that so an operator can grep "
                    "for the wedge without needing the LLM's "
                    "parroting-of-heal as the diagnostic tell."
                ),
            )
        if session_had_dangling:
            sessions_with_dangling += 1

    # Always log a completion summary — per intentionally-left-blank,
    # a clean state needs to be distinguishable from the detector not
    # having run at all.
    log.info(
        "talker.conversation.startup_dangling_tool_use_check_complete",
        sessions_checked=sessions_checked,
        sessions_with_dangling=sessions_with_dangling,
        total_dangling_ids=total_dangling_ids,
    )
    return total_dangling_ids


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


_DEFAULT_INSTANCE_TIMEZONE = "America/Halifax"


# Canonical Salem vault path — the authoritative location for
# universal (Shape B1) operator-preference records. Hypatia + KAL-LE
# read this directory at session start in addition to their own
# local vault (Shape B2 instance-application records). Salem reads
# only her own vault. See project_operator_preferences_v1.md Hard
# Contract #7 + #8 for the cross-instance read pattern.
#
# Hardcoded as an absolute path because the dispatch explicitly pins
# it to ``/home/andrew/alfred/vault/preference/`` — peer-protocol
# routing isn't used for V1 (filesystem read only). A future V2
# multi-operator architecture (V.E.R.A.) would replace this with a
# per-operator path resolver; for the single-operator V1 the
# absolute path keeps the code-layer simple.
_SALEM_CANONICAL_VAULT_PATH = Path("/home/andrew/alfred/vault")


def load_voice_preferences_block(
    vault_path: str | Path,
    instance_name: str,
    *,
    canonical_vault_path: Path | None = None,
) -> str | None:
    """Build the Shape B (voice) preferences system block, or None if empty.

    Loads Shape B preferences from the instance's local vault AND
    (for non-Salem instances) Salem's canonical vault, applies
    conflict resolution (local-wins-over-canonical), and returns the
    concatenated body markdown. Returns None when no active voice
    preferences apply — caller should omit the block entirely per
    ``feedback_intentionally_left_blank.md`` (empty header is worse
    than no header).

    Args:
        vault_path: the calling instance's vault root.
        instance_name: instance identity (Salem / Hypatia / KAL-LE).
            Case-insensitive comparisons used for both the "am I
            Salem" branch and the ``applies_to_instance`` matching.
        canonical_vault_path: optional override for Salem's vault
            path. Defaults to ``_SALEM_CANONICAL_VAULT_PATH``. Tests
            pass a tmp path here; production runs use the default.

    Conflict resolution (Hard Contract #6): when a local record AND a
    canonical record both apply, local wins. Match keys (in order):
    1. ``cites_canonical`` — local's wikilink target equals
       canonical's slug → local supersedes that canonical.
    2. ``name`` slug match — local and canonical have the same
       filename stem (rare but possible — symptomatic of operator
       creating a local override without setting cites_canonical).

    Both records are checked for ``applies_to_instance`` matching
    (universal records pass through to every instance; instance
    records only land in their target instance's block).
    """
    from alfred.preferences.loader import load_active_preferences

    instance_lc = (instance_name or "").lower()
    canonical = canonical_vault_path or _SALEM_CANONICAL_VAULT_PATH

    # Load local preferences (always — Salem reads her own canonical
    # vault here too, which is the same path as the canonical load
    # for Salem — see filter below for the dedup).
    try:
        local_prefs = load_active_preferences(vault_path, shape="voice")
    except Exception as exc:
        log.warning(
            "talker.preferences.local_load_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            vault_path=str(vault_path),
        )
        local_prefs = []

    # Load canonical (Salem's) preferences when this instance is NOT
    # Salem. Salem's local IS canonical — loading both would
    # double-count. Compare via casefolded instance name.
    if instance_lc == "salem":
        canonical_prefs = []
    else:
        try:
            canonical_prefs = load_active_preferences(canonical, shape="voice")
        except Exception as exc:
            log.warning(
                "talker.preferences.canonical_load_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                canonical_path=str(canonical),
            )
            canonical_prefs = []

    def _applies(pref) -> bool:
        """True if this preference applies to the calling instance."""
        if pref.scope == "universal" or pref.applies_to_instance is None:
            return True
        return (pref.applies_to_instance or "").lower() == instance_lc

    local_applicable = [p for p in local_prefs if _applies(p)]
    canonical_applicable = [p for p in canonical_prefs if _applies(p)]

    # Conflict resolution: local wins. Build a set of canonical slugs
    # superseded by any local record's ``cites_canonical`` field OR
    # by a name-slug collision.
    def _cited_slug(wikilink: str | None) -> str | None:
        if not wikilink:
            return None
        s = wikilink.strip().strip("[]").strip()
        if "/" in s:
            s = s.rsplit("/", 1)[-1]
        return s or None

    superseded: set[str] = set()
    local_slugs = {p.slug for p in local_applicable}
    for p in local_applicable:
        cited = _cited_slug(p.cites_canonical)
        if cited:
            superseded.add(cited)
    # Slug collisions count as conflict (local wins).
    for c in canonical_applicable:
        if c.slug in local_slugs:
            superseded.add(c.slug)

    final = list(local_applicable) + [
        c for c in canonical_applicable if c.slug not in superseded
    ]

    if not final:
        log.info(
            "talker.preferences.no_voice_block",
            instance=instance_name,
            local_loaded=len(local_prefs),
            canonical_loaded=len(canonical_prefs),
            detail="no active voice preferences apply — omitting system block",
        )
        return None

    # Build the block body. Each preference contributes its full
    # markdown body (which includes ``## Policy`` and any matcher-
    # rationale section). The block header is the same for all
    # instances; per-preference headers (``### <name>``) disambiguate
    # in the rendered block.
    parts: list[str] = ["## Operator voice preferences", ""]
    for p in final:
        parts.append(f"### {p.name or p.slug}")
        body = (p.body or "").strip()
        if body:
            parts.append(body)
        parts.append("")
    # Drop trailing blank.
    while parts and parts[-1] == "":
        parts.pop()
    text = "\n".join(parts)

    log.info(
        "talker.preferences.voice_block_built",
        instance=instance_name,
        active_count=len(final),
        local_applicable=len(local_applicable),
        canonical_applicable=len(canonical_applicable),
        superseded=len(superseded),
    )
    return text


def _build_today_block_text(
    now: _dt.datetime, instance_timezone: str,
) -> str:
    """Render the per-turn today's-date system-block body.

    Anchors the LLM to the operator's local date + day-of-week so
    relative time phrases ("Thursday", "tomorrow", "next week") resolve
    against a known reference point. Surfaced 2026-05-05 in conversation
    ``716f5b24``: Andrew said "Massage Thursday 10am-12pm." on Tue
    2026-05-05; Salem computed "Thursday is 2026-05-08" (added 3 days,
    got Friday's date). The SKILL's confirm-with-absolute-date
    discipline (commit ``1c56966``) caught the mismatch — the safety
    net held — but the LLM had no anchored knowledge of TODAY. This
    block closes that gap at the system-prompt layer.

    Pure helper (no I/O, no log) so tests can pin the rendered shape
    without monkeypatching wall-clock or filesystem.
    """
    tz = ZoneInfo(instance_timezone)
    local_now = now.astimezone(tz)
    # ``%z`` returns "-0300" / "+0000" / etc. Format as "UTC-03:00".
    raw_offset = local_now.strftime("%z")
    if raw_offset:
        offset_label = f"UTC{raw_offset[:3]}:{raw_offset[3:]}"
    else:
        offset_label = "UTC+00:00"
    tz_short = local_now.strftime("%Z") or instance_timezone
    return (
        "## Today\n\n"
        f"{local_now.strftime('%Y-%m-%d (%A)')}, {instance_timezone}, "
        f"currently {offset_label} ({tz_short}).\n\n"
        "Use this date as the anchor for resolving relative time phrases "
        "('Thursday', 'tomorrow', 'next week', etc.). When confirming "
        "events back to the user, always echo the day-of-week with the "
        "absolute date so they can spot a mismatch."
    )


def _build_sender_identity_text(
    user_name: str | None, user_role: str,
) -> str:
    """Render the per-message sender-identity block text.

    VERA reporter follow-up (2026-06-09). Multi-user instances (VERA)
    surface WHO sent the current message so the agent can attribute
    per-message authorship — e.g. set a ticket's ``reporter`` field from
    the actual sender rather than a hardcoded value.

    The block names the sender (``user_name`` when configured, else the
    role label as a fallback) AND the role, and instructs the agent to
    attribute authorship to this sender for the current message only.
    Sender CAN change per message in a shared chat, so this block is
    rebuilt every turn (uncached tail position) — it is genuinely
    dynamic-per-message, NOT a static SKILL template var.

    Callers gate on ``user_name`` being non-None (see ``run_turn``): the
    block is omitted entirely on single-user instances so their behaviour
    is byte-identical. The role-fallback wording here is for the case
    where a multi-user instance has a roster entry WITHOUT a name set.
    """
    who = user_name if user_name else f"the {user_role} user"
    return (
        "## Current message sender\n\n"
        f"This message was sent by **{who}** (role: {user_role}). "
        "When attributing authorship for THIS message — e.g. setting a "
        "record's reporter/author field — use this sender. The sender "
        "can change between messages in a shared chat, so re-read this "
        "block each turn rather than assuming a fixed author."
    )


def _build_system_blocks(
    system_prompt: str,
    vault_context_str: str,
    calibration_str: str | None = None,
    pushback_level: int | None = None,
    voice_preferences_block: str | None = None,
    *,
    sender_identity_block: str | None = None,
    now: _dt.datetime | None = None,
    instance_timezone: str = _DEFAULT_INSTANCE_TIMEZONE,
) -> list[dict[str, Any]]:
    """Return ``system`` as a list of cacheable text blocks.

    Up to five cache breakpoints (Anthropic-recommended for agents):
        1. The frozen SKILL.md-style system prompt (almost never changes).
        2. The vault context snapshot (changes across sessions but stable
           within one, so turn 2+ hits the cache).
        3. The per-user calibration block (wk3 — Alfred's current model
           of the user; stable within a session, updated at session close).
        4. The per-session pushback directive (wk3 — derived from session
           type's ``pushback_level``, stable within a session).
        5. **The today's-date anchor block** (Phase A 2026-05-06 — closes
           the day-of-week date-math gap surfaced 2026-05-05 in
           conversation ``716f5b24``). Carries today's date +
           day-of-week + tz offset. NO ``cache_control`` because it
           changes every day; the cache TTL is 5min anyway, and putting
           an ephemeral breakpoint on a daily-volatile block would
           churn the cache pointlessly.

    Order matters for caching: the most-stable prefix first, the most-
    volatile last. System prompt > vault context > calibration > pushback
    > today. The today-block now occupies the tail position pushback
    used to hold; this is intentional — today is genuinely the most-
    volatile block (changes at midnight ADT every day), and putting
    cacheable blocks AFTER it would invalidate the cache prefix on
    every date rollover.

    ``now`` defaults to ``datetime.now(timezone.utc)`` — kwarg exists
    for deterministic tests (mirrors the dangling-tool_use detector
    pattern). ``instance_timezone`` defaults to ``America/Halifax``;
    callers from a different operator timezone should pass an explicit
    IANA name.

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
    # Operator-preference V1 (project_operator_preferences_v1) — Shape B
    # voice directives. Inserted AFTER calibration so the calibration
    # cache prefix doesn't churn when voice preferences change, BEFORE
    # pushback (per dispatch). Empty / None block is omitted entirely
    # rather than emitting an empty header — per ``feedback_intentionally_
    # left_blank.md`` the absence here is meaningful: no active voice
    # preferences → no block, and the operator notices the missing
    # block at first observation. The block carries its own ``## Operator
    # voice preferences`` header so callers pass concatenated policy
    # bodies without re-deriving the heading.
    if voice_preferences_block:
        blocks.append({
            "type": "text",
            "text": voice_preferences_block,
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

    # Today's-date anchor block — always present, never cached.
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    blocks.append({
        "type": "text",
        "text": _build_today_block_text(now, instance_timezone),
        # Deliberately NO cache_control — see docstring.
    })

    # Sender-identity block (VERA reporter follow-up 2026-06-09) — tail
    # position, never cached. The sender CAN change per message in a
    # shared multi-user chat, so this block is the MOST volatile of all
    # (more volatile than today, which only rolls at midnight). Placing
    # it AFTER the today-block (already uncached) is cache-neutral — no
    # ephemeral breakpoint sits below it to invalidate. Omitted entirely
    # when ``sender_identity_block`` is None (single-user instances pass
    # None), so Salem / KAL-LE / Hypatia system blocks are byte-identical
    # to before. Per ``feedback_intentionally_left_blank.md`` the absence
    # is meaningful — a single-user instance has no per-message sender
    # ambiguity to resolve.
    if sender_identity_block:
        blocks.append({
            "type": "text",
            "text": sender_identity_block,
            # No cache_control — most-volatile block, see above.
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


# --- routine_done dispatch (Phase 2B B1, 2026-05-30) ---------------------
#
# Conversational completion path. Subprocess-invokes ``python -m alfred
# routine done`` with ``ALFRED_VAULT_SCOPE=talker_routine_completion``
# threaded through the env. The CLI is the single source of truth for
# the completion_log mutation semantics — this dispatcher is just the
# adapter between the Anthropic tool_use schema and the subprocess.
#
# Salem-only: the routine subsystem refuses non-Salem instances at the
# CLI's ``_check_salem_only`` guard. We also tool-set-gate here as a
# second line of defence (mirror of ``_dispatch_bash_exec``'s
# tool-set-gating shape) so a Hypatia or KAL-LE that somehow received
# the tool gets a structured refusal rather than a crash.


async def _dispatch_routine_done(
    *,
    tool_input: dict[str, Any],
    session: Session,
    config: TalkerConfig | None,
) -> str:
    """Dispatch one ``routine_done`` tool_use block.

    Salem-only. Subprocess invokes ``python -m alfred routine done``
    with the ``talker_routine_completion`` scope. The CLI emits
    structured JSON with a canary ``kind`` discriminator
    (success / unknown_record / unknown_item / ambiguous_item /
    idempotent_noop / future_date_rejected) — Salem routes on it.

    Adapter shape (mirrors ``_dispatch_bash_exec``):
      1. Tool-set gating. Only Salem (tool_set == "" → talker default,
         OR explicit "talker"/"salem") may invoke. Other instances
         get a structured refusal.
      2. Argument parsing. Validates ``item`` is present + non-empty.
       ``record`` + ``completed_at`` are optional.
      3. Subprocess call. Uses ``sys.executable -m alfred routine done``
         (canonical __main__.py dispatch — NEVER ``alfred.cli`` per
         the 2026-05-28 silent-no-op-skip incident — same lesson as
         migrate_tier_phase1.py's subprocess wrapper).
      4. JSON parse. The CLI emits one JSON object per invocation;
         reversed-line scan returns the last parseable line (the
         structlog-pollution defense pattern from
         migrate_tier_phase1.py).
      5. Subprocess-failure-contract logging. Non-zero exit with a
         ``kind`` canary is normal (operator-recoverable refusal);
         only non-zero WITHOUT a canary is logged as a real failure
         per builder.md (stdout_tail sentinel + stderr).
    """
    import asyncio
    import json
    import os
    import subprocess
    import sys

    # Phase 2B B1 (2026-05-30) — canary kind constants. Imported here
    # rather than at module level because the CLI module is heavyish;
    # lazy import keeps the conversation-module import-time minimal.
    # Importing the constants (rather than using raw string literals)
    # locks the rename-discipline: a future rename of, say,
    # ``DONE_KIND_SUBPROCESS_ERROR`` to ``DONE_KIND_DISPATCH_FAILURE``
    # will fail-loud at this import line rather than silently
    # producing a different canary value in the talker's response.
    # Per WARN-3 reviewer-flagged 2026-05-30.
    from alfred.routine.cli import (
        DONE_KIND_SUBPROCESS_ERROR,
        DONE_KIND_TIMEOUT,
    )

    # --- Tool-set gating -------------------------------------------------
    # Salem's ``tool_set`` is either unset (legacy default → talker)
    # or explicitly "talker"/"salem". KAL-LE → "kalle", Hypatia →
    # "hypatia". Refuse explicitly on the latter two even though they
    # shouldn't see this tool in their list.
    tool_set = ""
    if config is not None:
        tool_set = (config.instance.tool_set or "").lower()
    if tool_set in {"kalle", "hypatia"}:
        log.warning(
            "talker.routine_done.wrong_tool_set",
            tool_set=tool_set,
            session_id=session.session_id,
        )
        return _dumps({
            "error": (
                "routine_done is Salem-only — routine subsystem refuses "
                "non-Salem instances"
            ),
            "tool_set": tool_set,
        })

    # --- Argument parsing ------------------------------------------------
    if not isinstance(tool_input, dict):
        return _dumps({
            "error": "routine_done requires a dict tool_input",
        })
    item = tool_input.get("item", "")
    record = tool_input.get("record", "") or ""
    completed_at = tool_input.get("completed_at", "") or ""
    if not isinstance(item, str) or not item.strip():
        return _dumps({
            "error": "routine_done requires a non-empty 'item'",
        })

    # --- Build argv ------------------------------------------------------
    # Two forms:
    #   python -m alfred routine done <record> <item> [--completed-at]
    #   python -m alfred routine done <item>          [--completed-at]
    argv: list[str] = [
        sys.executable, "-m", "alfred", "routine", "done",
    ]
    if isinstance(record, str) and record.strip():
        argv.append(record.strip())
    argv.append(item.strip())
    if isinstance(completed_at, str) and completed_at.strip():
        argv.extend(["--completed-at", completed_at.strip()])
    argv.append("--json")

    log.info(
        "talker.routine_done.invoke",
        session_id=session.session_id,
        item=item[:200],
        record=record[:200] if record else "",
        completed_at=completed_at or "(today)",
    )

    # --- Subprocess execution -------------------------------------------
    # asyncio-friendly subprocess.run wrapper — keeps the event loop
    # unblocked. The CLI runs in milliseconds (one frontmatter parse +
    # write); a 30-second timeout is generous.
    def _run() -> subprocess.CompletedProcess:
        # Inherit env + add the narrow scope.
        #
        # NOTE: ``ALFRED_VAULT_SCOPE`` here is forward-compat plumbing
        # — the routine done CLI path bypasses ``vault_edit`` (it
        # rewrites the frontmatter directly via ``frontmatter.dumps``
        # because the per-key completion_log mutation doesn't fit the
        # ``set_fields`` shape), so the env var is NOT consulted by
        # the current CLI invocation. The narrow scope gate
        # (``talker_routine_completion`` in ``SCOPE_RULES``) fires at
        # the table level — its effects materialise when B3 widens
        # the routing through ``vault_edit`` and the scope check
        # actually runs. Plumbing the env var now means B3 is a
        # scope-only change rather than a dispatcher-plumbing change
        # too. WARN-2 reviewer note 2026-05-30.
        env = {**os.environ, "ALFRED_VAULT_SCOPE": "talker_routine_completion"}
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        log.warning(
            "talker.routine_done.timeout",
            session_id=session.session_id,
            argv=argv,
        )
        return _dumps({
            "error": "routine_done timed out (30s)",
            "kind": DONE_KIND_TIMEOUT,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.routine_done.subprocess_crashed",
            session_id=session.session_id,
            error=str(exc),
        )
        return _dumps({
            "error": f"routine_done subprocess crashed: {exc}",
            "kind": DONE_KIND_SUBPROCESS_ERROR,
        })

    raw_stdout = (proc.stdout or "").strip()
    raw_stderr = (proc.stderr or "").strip()

    # --- Parse the canary JSON ------------------------------------------
    # Reversed-line scan returns the LAST parseable JSON line per the
    # structlog-pollution defense (migrate_tier_phase1.py pattern).
    parsed: dict[str, Any] | None = None
    if raw_stdout:
        for line in reversed(raw_stdout.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
                break
            except json.JSONDecodeError:
                continue

    # --- Failure contract: non-zero exit WITHOUT a canary ---------------
    # Canary returns (success / unknown_record / unknown_item /
    # ambiguous_item / idempotent_noop / future_date_rejected) exit
    # with codes 0 or 1, but ALWAYS produce JSON with a ``kind``.
    # A non-zero exit WITHOUT a parseable canary is a real subprocess
    # failure (e.g. import error, Salem-only guard rejection) and
    # gets the builder.md subprocess-failure logging treatment.
    if parsed is None or not isinstance(parsed, dict) or "kind" not in parsed:
        # Real failure — log per the contract.
        log.warning(
            "talker.routine_done.nonzero_exit_no_canary",
            session_id=session.session_id,
            code=proc.returncode,
            stderr=raw_stderr[:500],
            stdout_tail=raw_stdout[-2000:] if raw_stdout else "",
            argv=argv,
        )
        return _dumps({
            "error": (
                f"routine_done failed without canary: exit "
                f"{proc.returncode}; "
                f"stderr={raw_stderr[:300]!r}; "
                f"stdout={raw_stdout[-300:]!r}"
            ),
            "kind": DONE_KIND_SUBPROCESS_ERROR,
        })

    # --- Success/canary path: return parsed JSON verbatim ---------------
    log.info(
        "talker.routine_done.result",
        session_id=session.session_id,
        kind=parsed.get("kind"),
        ok=parsed.get("ok"),
        record=parsed.get("record", ""),
        item=parsed.get("item", "")[:200] if isinstance(parsed.get("item"), str) else "",
        date=parsed.get("date", ""),
    )
    # Note: per-session vault_ops bookkeeping skipped here — the
    # vault audit log + the talker.routine_done.result log line
    # above cover observability. A future ship can widen
    # _dispatch_routine_done's signature to thread the StateManager
    # if session.vault_ops tracking becomes load-bearing.
    return _dumps(parsed)


# --- routine_item dispatch (Phase 2B B3, 2026-05-30) ---------------------
#
# Conversational item-CRUD path. Subprocess-invokes ``python -m alfred
# routine item <action>`` with ``ALFRED_VAULT_SCOPE=talker_routine_item``
# threaded through the env. Same adapter shape as
# ``_dispatch_routine_done`` — tool-set gating up-front, argv build,
# canary parse, failure logging per builder.md.


async def _dispatch_routine_item(
    *,
    tool_input: dict[str, Any],
    session: Session,
    config: TalkerConfig | None,
) -> str:
    """Dispatch one ``routine_item`` tool_use block.

    Salem-only. Subprocess invokes ``python -m alfred routine item
    <action>`` with the ``talker_routine_item`` scope. The CLI emits
    structured JSON with a canary ``kind`` discriminator
    (added / removed / edited / unknown_record / unknown_item /
    ambiguous_item / cadence_conflict / duplicate_item /
    invalid_field) — Salem routes on it.

    Argv build per action:
      * add: ``[..., 'routine', 'item', 'add', record, text, --json,
        + optional --priority / --target-cadence-days /
        --due-pattern JSON / --surface-at-days / --escalate-at-days]``
      * remove: ``[..., 'routine', 'item', 'remove', record, item,
        --json]`` (record may be empty → vault-wide fuzzy via the
        one-positional form ``[..., 'remove', item, --json]``)
      * edit: ``[..., 'routine', 'item', 'edit', record, item, --json,
        + optional --text / --priority / --target-cadence-days /
        --due-pattern JSON / --surface-at-days / --escalate-at-days /
        --clear-due-pattern / --clear-target-cadence-days]``
    """
    import asyncio
    import json
    import os
    import subprocess
    import sys

    # Phase 2B B3 — canary kind constants. Lazy-imported to keep
    # conversation-module import-time minimal. Importing the
    # constants (rather than raw literals) locks rename-discipline.
    from alfred.routine.cli import (
        DONE_KIND_SUBPROCESS_ERROR,
        DONE_KIND_TIMEOUT,
    )

    # --- Tool-set gating -------------------------------------------------
    tool_set = ""
    if config is not None:
        tool_set = (config.instance.tool_set or "").lower()
    if tool_set in {"kalle", "hypatia"}:
        log.warning(
            "talker.routine_item.wrong_tool_set",
            tool_set=tool_set,
            session_id=session.session_id,
        )
        return _dumps({
            "error": (
                "routine_item is Salem-only — routine subsystem refuses "
                "non-Salem instances"
            ),
            "tool_set": tool_set,
        })

    # --- Argument parsing ------------------------------------------------
    if not isinstance(tool_input, dict):
        return _dumps({
            "error": "routine_item requires a dict tool_input",
        })
    action = tool_input.get("action", "")
    item = tool_input.get("item", "")
    record = tool_input.get("record", "") or ""
    fields = tool_input.get("fields") or {}
    if action not in ("add", "remove", "edit"):
        return _dumps({
            "error": (
                f"routine_item action must be add/remove/edit; "
                f"got {action!r}"
            ),
        })
    if not isinstance(item, str) or not item.strip():
        return _dumps({
            "error": "routine_item requires a non-empty 'item'",
        })
    if not isinstance(fields, dict):
        return _dumps({
            "error": (
                f"routine_item 'fields' must be a dict; got "
                f"{type(fields).__name__}"
            ),
        })

    # --- Build argv ------------------------------------------------------
    argv: list[str] = [
        sys.executable, "-m", "alfred", "routine", "item", action,
    ]

    if action == "add":
        # add: record is REQUIRED + text is item.
        if not isinstance(record, str) or not record.strip():
            # The CLI also rejects empty record on add (returns
            # unknown_record canary); we forward the operator-facing
            # error explicitly here so the talker doesn't even spawn
            # the subprocess for an obviously-broken call.
            return _dumps({
                "error": (
                    "routine_item action=add requires 'record' (the "
                    "routine to add the item to). Vault-wide fuzzy "
                    "doesn't apply when adding a NEW item."
                ),
                "kind": "unknown_record",
            })
        argv.extend([record.strip(), item.strip()])
    else:
        # remove / edit: two-positional form when record is supplied,
        # one-positional form (vault-wide fuzzy on item) when not.
        if isinstance(record, str) and record.strip():
            argv.extend([record.strip(), item.strip()])
        else:
            argv.append(item.strip())

    # --- Append --fields-style flags from the fields dict ---------------
    # The CLI accepts each field as its own --flag; we serialise back
    # from the operator's nested ``fields`` dict to the flag form.
    if action in ("add", "edit"):
        priority = fields.get("priority")
        if isinstance(priority, str) and priority.strip():
            argv.extend(["--priority", priority.strip()])
        for flag_name, field_key in (
            ("--target-cadence-days", "target_cadence_days"),
            ("--surface-at-days", "surface_at_days"),
            ("--escalate-at-days", "escalate_at_days"),
        ):
            val = fields.get(field_key)
            if val is not None:
                argv.extend([flag_name, str(val)])
        due_pattern = fields.get("due_pattern")
        if due_pattern is not None:
            # Always JSON-serialise the dict — the CLI's
            # _validate_due_pattern accepts JSON-string OR dict, but
            # the CLI form crosses subprocess boundary so we must
            # encode as string.
            if isinstance(due_pattern, dict):
                argv.extend([
                    "--due-pattern", json.dumps(due_pattern),
                ])
            else:
                # String form pass-through (operator may have already
                # supplied a JSON literal — but typically it's a dict
                # from the model).
                argv.extend(["--due-pattern", str(due_pattern)])

    if action == "edit":
        new_text = fields.get("text")
        if isinstance(new_text, str) and new_text.strip():
            argv.extend(["--text", new_text.strip()])
        if fields.get("clear_due_pattern"):
            argv.append("--clear-due-pattern")
        if fields.get("clear_target_cadence_days"):
            argv.append("--clear-target-cadence-days")

    argv.append("--json")

    log.info(
        "talker.routine_item.invoke",
        session_id=session.session_id,
        action=action,
        item=item[:200],
        record=record[:200] if record else "",
        field_keys=sorted(fields.keys()) if fields else [],
    )

    # --- Subprocess execution -------------------------------------------
    def _run() -> subprocess.CompletedProcess:
        # NOTE: ``ALFRED_VAULT_SCOPE`` here is forward-compat plumbing
        # (mirror of routine_done dispatcher's note). The CLI's
        # item-CRUD path uses frontmatter.dumps + yaml-preserve
        # directly (not vault_edit), so the env var isn't consulted
        # by the current code path. The narrow scope gate fires at
        # the SCOPE_RULES table level; a future ship that routes
        # item-CRUD through vault_edit would have the env var
        # immediately available.
        env = {
            **os.environ,
            "ALFRED_VAULT_SCOPE": "talker_routine_item",
        }
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        log.warning(
            "talker.routine_item.timeout",
            session_id=session.session_id,
            argv=argv,
        )
        return _dumps({
            "error": "routine_item timed out (30s)",
            "kind": DONE_KIND_TIMEOUT,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.routine_item.subprocess_crashed",
            session_id=session.session_id,
            error=str(exc),
        )
        return _dumps({
            "error": f"routine_item subprocess crashed: {exc}",
            "kind": DONE_KIND_SUBPROCESS_ERROR,
        })

    raw_stdout = (proc.stdout or "").strip()
    raw_stderr = (proc.stderr or "").strip()

    # --- Parse the canary JSON ------------------------------------------
    # Reversed-line scan per the structlog-pollution defense pattern.
    parsed: dict[str, Any] | None = None
    if raw_stdout:
        for line in reversed(raw_stdout.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
                break
            except json.JSONDecodeError:
                continue

    # --- Failure contract: non-zero exit WITHOUT a canary ---------------
    if parsed is None or not isinstance(parsed, dict) or "kind" not in parsed:
        log.warning(
            "talker.routine_item.nonzero_exit_no_canary",
            session_id=session.session_id,
            code=proc.returncode,
            stderr=raw_stderr[:500],
            stdout_tail=raw_stdout[-2000:] if raw_stdout else "",
            argv=argv,
        )
        return _dumps({
            "error": (
                f"routine_item failed without canary: exit "
                f"{proc.returncode}; "
                f"stderr={raw_stderr[:300]!r}; "
                f"stdout={raw_stdout[-300:]!r}"
            ),
            "kind": DONE_KIND_SUBPROCESS_ERROR,
        })

    # --- Success/canary path: return parsed JSON verbatim ---------------
    log.info(
        "talker.routine_item.result",
        session_id=session.session_id,
        action=action,
        kind=parsed.get("kind"),
        ok=parsed.get("ok"),
        record=parsed.get("record", ""),
        item=parsed.get("item", "")[:200] if isinstance(parsed.get("item"), str) else "",
    )
    return _dumps(parsed)


# --- Inter-instance peer tool dispatch (Phase A) -------------------------
#
# KAL-LE and Hypatia route ``query_canonical`` + the four ``propose_*``
# tools through the transport's outbound peer client against the
# ``salem`` peer entry. The dispatcher is structured the same way as
# ``_dispatch_bash_exec``: tool-set gating up-front, config plumbing,
# tool-input validation, an awaited transport call, and a JSON-serialised
# return that the conversation loop hands back to the model as a
# ``tool_result``. All transport errors are caught and returned as
# ``{"error": "..."}`` payloads — the model can apologise / retry / pivot
# rather than crash the turn.


_PEER_TARGET = "salem"  # The canonical authority. Hardcoded by design.


async def _dispatch_peer_inter_instance_tool(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    session: Session,
    config: TalkerConfig | None,
) -> str:
    """Dispatch one of {query_canonical, propose_*} → Salem.

    All five tools share the same shape:
      * tool-set gating (only ``kalle`` / ``hypatia`` may invoke).
      * load TransportConfig from raw config.yaml — the talker daemon
        doesn't carry one on TalkerConfig directly, so we re-load lazily.
      * call the matching client helper.
      * return ``_dumps(result)`` for the model.
    """
    # --- Tool-set gating -------------------------------------------------
    tool_set = ""
    if config is not None and config.instance is not None:
        tool_set = config.instance.tool_set or ""
    if tool_set not in {"kalle", "hypatia"}:
        log.warning(
            "talker.peer_tool.wrong_tool_set",
            tool=tool_name,
            tool_set=tool_set or "(none)",
            session_id=session.session_id,
        )
        return _dumps({
            "error": (
                f"{tool_name} not available on this instance — "
                f"only peer instances (kal-le, hypatia) may call it"
            ),
            "tool_set": tool_set or "talker",
        })

    self_name = tool_set  # ``"kalle"`` or ``"hypatia"`` — matches the
    # auth.tokens key on Salem's side and the propose-record correlation
    # id prefix.

    # --- Transport config load ------------------------------------------
    # Lazy import + lazy load so test fixtures that don't ship a config
    # file still let the model see the failure as a tool error rather
    # than a startup crash.
    #
    # P0 fix (2026-05-01): use the path the daemon was started with
    # (``config.config_path``), NOT the default ``config.yaml``. Without
    # this, a Hypatia daemon launched with ``--config config.hypatia.yaml``
    # would silently re-read Salem's config here and report
    # ``transport_error: unknown peer 'salem'`` — Salem doesn't list
    # itself as a peer. See commit message for repro.
    from alfred.transport.exceptions import TransportError
    try:
        from alfred.transport.config import load_config as load_transport_config
        transport_config_path = (
            config.config_path
            if config is not None and config.config_path
            else "config.yaml"
        )
        transport_config = load_transport_config(transport_config_path)
    except FileNotFoundError as exc:
        log.warning(
            "talker.peer_tool.config_missing",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({
            "error": "transport config unavailable for inter-instance call",
            "detail": str(exc),
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.peer_tool.config_error",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({"error": f"transport config load failed: {exc}"})

    # --- Per-tool dispatch -----------------------------------------------
    try:
        if tool_name == "query_canonical":
            return await _peer_tool_query_canonical(
                tool_input, transport_config, self_name,
            )
        if tool_name == "propose_person":
            return await _peer_tool_propose_record(
                "person", tool_input, transport_config, self_name,
            )
        if tool_name == "propose_org":
            return await _peer_tool_propose_record(
                "org", tool_input, transport_config, self_name,
            )
        if tool_name == "propose_location":
            return await _peer_tool_propose_record(
                "location", tool_input, transport_config, self_name,
            )
        if tool_name == "propose_event":
            return await _peer_tool_propose_event(
                tool_input, transport_config, self_name,
            )
        return _dumps({"error": f"unhandled peer tool: {tool_name}"})
    except TransportError as exc:
        log.warning(
            "talker.peer_tool.transport_error",
            tool=tool_name,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return _dumps({
            "error": f"transport error: {exc}",
            "error_type": exc.__class__.__name__,
        })
    except Exception as exc:  # noqa: BLE001 — tool errors must reach the model
        log.warning(
            "talker.peer_tool.unexpected_error",
            tool=tool_name,
            error=str(exc),
        )
        return _dumps({"error": f"unexpected error: {exc}"})


# Field-truncation cap for the GCal description payload. Calendar
# event descriptions can carry meeting agendas, Zoom join blurbs,
# multi-paragraph context. The model only needs enough to answer
# "what's this about?" — 500 chars is well above a typical agenda
# blurb. Operator who needs the full text uses ``alfred gcal``.
_GCAL_DESCRIPTION_TRUNC_CHARS = 500


def _resolve_gcal_enabled_for_run_turn(config: TalkerConfig) -> bool:
    """Lazy-resolve ``gcal.enabled`` from the active config.yaml.

    Used by ``run_turn`` to decide whether to surface
    ``gcal_list_events`` in the per-turn tool list. Lazy because
    :class:`TalkerConfig` doesn't carry the GCal binding directly
    (GCal is a separate optional integration module — see
    ``feedback_intentionally_left_blank.md``-style decoupling: the
    talker doesn't need to know about every integration its
    dispatcher might touch).

    Failure to resolve (config path missing, GCal module missing,
    YAML parse error) is non-fatal — returns False so the talker
    falls back to "no GCal tool surfaced", matching pre-feature
    behaviour. The dispatch helper would also catch the same error
    if a misconfigured tool call somehow slipped through.

    Sub-millisecond per call (one yaml read + one dataclass build).
    Acceptable for the once-per-turn frequency.
    """
    config_path = (
        config.config_path
        if config is not None and config.config_path
        else "config.yaml"
    )
    try:
        from alfred.integrations.gcal_config import (
            load_from_unified as load_gcal,
        )
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return bool(load_gcal(raw).enabled)
    except FileNotFoundError:
        return False
    except ImportError:
        # Test fixtures or trimmed installs without the GCal module.
        return False
    except Exception as exc:  # noqa: BLE001
        log.debug("talker.gcal_resolve_enabled_failed", error=str(exc))
        return False


def _gcal_event_to_chat_dict(event: Any) -> dict[str, Any]:
    """Render a :class:`GCalEvent` into the chat-friendly dispatch shape.

    Pruned vs the full GCal API response: title / start / end /
    location / description-truncated. Drops the kitchen-sink ``raw``
    blob (operator-only via ``alfred gcal``), the ``id``
    (chat doesn't need it; future "delete event" tool can use
    ``vault_delete`` against the matching ``event`` record), and
    the ``calendar_id`` (caller already knows which calendar they
    queried — the dispatch helper doesn't echo it back).

    Location is read from the raw GCal payload because
    :class:`GCalEvent` doesn't lift it into a typed attribute today
    (transport peer-handler conflict-check doesn't need it).
    """
    raw = getattr(event, "raw", {}) or {}
    location = raw.get("location") or ""
    description = getattr(event, "description", "") or ""
    if len(description) > _GCAL_DESCRIPTION_TRUNC_CHARS:
        description = description[:_GCAL_DESCRIPTION_TRUNC_CHARS] + "…"
    return {
        "title": getattr(event, "title", ""),
        "start": getattr(event, "start").isoformat()
            if getattr(event, "start", None) else "",
        "end": getattr(event, "end").isoformat()
            if getattr(event, "end", None) else "",
        "location": location,
        "description": description,
    }


async def _dispatch_gcal_list_events(
    *,
    tool_input: dict[str, Any],
    config: TalkerConfig | None,
) -> str:
    """Dispatch ``gcal_list_events`` → :func:`GCalClient.list_events`.

    Phase A capability-audit close (2026-05-06): exposes Salem's
    existing GCal read capability through the talker tool surface
    so the model can answer "do I have anything Tuesday?" honestly
    instead of hitting the no-tool wall surfaced in conversation
    ``0e52c745`` ("I have no calendar read access at all").

    Lazy-loads :class:`GCalConfig` from the active
    ``config.config_path`` (mirrors the peer-tool dispatch shape at
    ``_dispatch_peer_inter_instance_tool``) so the daemon doesn't
    have to plumb GCal through every callsite. Per-call cost is
    one YAML read + one OAuth-token file read; both are sub-
    millisecond and OS-page-cache hot.

    Failure paths surface as ``{"error": "..."}`` to the model so
    the LLM can apologise / retry / pick a different tool, NOT
    propagate. Same pattern as every other tool in this dispatcher.
    """
    # --- Tool input validation ------------------------------------------
    calendar_alias = (
        tool_input.get("calendar") if isinstance(tool_input, dict) else None
    )
    start_str = tool_input.get("start") if isinstance(tool_input, dict) else None
    end_str = tool_input.get("end") if isinstance(tool_input, dict) else None
    if calendar_alias not in {"alfred", "primary"}:
        return _dumps({
            "error": (
                "gcal_list_events requires 'calendar' to be 'alfred' or "
                f"'primary' (got {calendar_alias!r})"
            ),
        })
    if not isinstance(start_str, str) or not start_str:
        return _dumps({"error": "gcal_list_events requires ISO 8601 'start'"})
    if not isinstance(end_str, str) or not end_str:
        return _dumps({"error": "gcal_list_events requires ISO 8601 'end'"})

    try:
        time_min = _dt.datetime.fromisoformat(start_str)
    except ValueError as exc:
        return _dumps({"error": f"gcal_list_events: invalid 'start' — {exc}"})
    try:
        time_max = _dt.datetime.fromisoformat(end_str)
    except ValueError as exc:
        return _dumps({"error": f"gcal_list_events: invalid 'end' — {exc}"})
    if time_min.tzinfo is None or time_max.tzinfo is None:
        return _dumps({
            "error": (
                "gcal_list_events: 'start' and 'end' must be timezone-aware "
                "(include an offset, e.g. 2026-05-07T00:00:00-03:00)"
            ),
        })

    # --- Lazy GCal config + client -------------------------------------
    # Mirror peer-tool pattern: lazy import + lazy load via
    # ``config.config_path``. Test fixtures that don't ship a config
    # file see the failure as a tool error rather than a startup
    # crash.
    try:
        from alfred.integrations.gcal_config import (
            load_from_unified as load_gcal,
        )
        from alfred.integrations.gcal import (
            GCalClient,
            GCalError,
            GCalNotAuthorized,
            GCalNotInstalled,
        )
    except ImportError as exc:
        log.warning("talker.gcal_list.import_failed", error=str(exc))
        return _dumps({
            "error": "Google Calendar dependencies not installed",
            "detail": str(exc),
        })

    config_path = (
        config.config_path
        if config is not None and config.config_path
        else "config.yaml"
    )
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        gcal_config = load_gcal(raw)
    except FileNotFoundError as exc:
        log.warning(
            "talker.gcal_list.config_missing",
            error=str(exc),
            config_path=config_path,
        )
        return _dumps({
            "error": "GCal config unavailable for read",
            "detail": str(exc),
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.gcal_list.config_error", error=str(exc))
        return _dumps({"error": f"GCal config load failed: {exc}"})

    if not gcal_config.enabled:
        # Defensive: tools_for_set should not have surfaced this tool
        # when ``gcal.enabled: false``, but if a misconfiguration races
        # mid-restart, fail honestly rather than silently no-op.
        log.info("talker.gcal_list.not_enabled")
        return _dumps({
            "error": (
                "GCal not enabled on this instance — operator must set "
                "gcal.enabled: true in config.yaml"
            ),
        })

    # Resolve the calendar alias to a real GCal calendar ID.
    if calendar_alias == "alfred":
        calendar_id = gcal_config.alfred_calendar_id
    else:
        calendar_id = gcal_config.primary_calendar_id
    if not calendar_id:
        return _dumps({
            "error": (
                f"GCal {calendar_alias} calendar ID not configured — operator "
                f"must set gcal.{calendar_alias}_calendar_id in config.yaml"
            ),
        })

    # --- API call --------------------------------------------------------
    try:
        client = GCalClient(
            credentials_path=gcal_config.credentials_path,
            token_path=gcal_config.token_path,
            scopes=gcal_config.scopes,
        )
        events = client.list_events(calendar_id, time_min, time_max)
    except GCalNotInstalled as exc:
        log.warning("talker.gcal_list.not_installed", error=str(exc))
        return _dumps({
            "error": "Google Calendar Python deps not installed",
            "detail": str(exc),
        })
    except GCalNotAuthorized as exc:
        log.warning("talker.gcal_list.not_authorized", error=str(exc))
        return _dumps({
            "error": "GCal not authorized — operator must run `alfred gcal authorize`",
            "detail": str(exc),
        })
    except GCalError as exc:
        log.warning(
            "talker.gcal_list.api_error",
            calendar=calendar_alias,
            error=str(exc),
        )
        return _dumps({
            "error": f"GCal API error: {exc}",
            "calendar": calendar_alias,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.gcal_list.unexpected_error",
            calendar=calendar_alias,
            error=str(exc),
        )
        return _dumps({"error": f"unexpected error: {exc}"})

    # Per ``feedback_intentionally_left_blank.md``: explicit
    # "ran, nothing to do" log so an empty result is distinguishable
    # from a missed call. The model gets ``{"events": []}`` either way.
    if not events:
        log.info(
            "talker.gcal_list.empty",
            calendar=calendar_alias,
            window_start=start_str,
            window_end=end_str,
        )
    else:
        log.info(
            "talker.gcal_list.fired",
            calendar=calendar_alias,
            window_start=start_str,
            window_end=end_str,
            count=len(events),
        )

    return _dumps({
        "calendar": calendar_alias,
        "events": [_gcal_event_to_chat_dict(e) for e in events],
    })


async def _peer_tool_query_canonical(
    tool_input: dict[str, Any],
    transport_config: Any,
    self_name: str,
) -> str:
    from alfred.transport.client import peer_get_canonical_record

    record_type = tool_input.get("record_type") if isinstance(tool_input, dict) else None
    name = tool_input.get("name") if isinstance(tool_input, dict) else None
    if not isinstance(record_type, str) or not record_type:
        return _dumps({"error": "query_canonical requires a 'record_type'"})
    if not isinstance(name, str) or not name:
        return _dumps({"error": "query_canonical requires a 'name'"})

    record = await peer_get_canonical_record(
        _PEER_TARGET,
        record_type,
        name,
        config=transport_config,
        self_name=self_name,
    )
    if record is None:
        return _dumps({"status": "not_found", "record_type": record_type, "name": name})
    return _dumps({"status": "found", **record})


async def _peer_tool_propose_record(
    record_type: str,
    tool_input: dict[str, Any],
    transport_config: Any,
    self_name: str,
) -> str:
    from alfred.transport.client import peer_propose_canonical_record

    name = tool_input.get("name") if isinstance(tool_input, dict) else None
    if not isinstance(name, str) or not name.strip():
        return _dumps({"error": f"propose_{record_type} requires a 'name'"})
    fields_raw = tool_input.get("fields") if isinstance(tool_input, dict) else None
    fields = dict(fields_raw) if isinstance(fields_raw, dict) else None
    source = tool_input.get("source") if isinstance(tool_input, dict) else None
    if source is not None and not isinstance(source, str):
        return _dumps({"error": "source must be a string"})

    response = await peer_propose_canonical_record(
        _PEER_TARGET,
        record_type,
        name.strip(),
        proposed_fields=fields,
        source=source or "",
        config=transport_config,
        self_name=self_name,
    )
    return _dumps(response)


async def _peer_tool_propose_event(
    tool_input: dict[str, Any],
    transport_config: Any,
    self_name: str,
) -> str:
    from alfred.transport.client import peer_propose_event

    title = tool_input.get("title") if isinstance(tool_input, dict) else None
    start = tool_input.get("start") if isinstance(tool_input, dict) else None
    end = tool_input.get("end") if isinstance(tool_input, dict) else None
    summary = tool_input.get("summary") if isinstance(tool_input, dict) else ""
    origin_context = tool_input.get("origin_context") if isinstance(tool_input, dict) else ""

    if not isinstance(title, str) or not title.strip():
        return _dumps({"error": "propose_event requires a 'title'"})
    if not isinstance(start, str) or not start.strip():
        return _dumps({"error": "propose_event requires a 'start' ISO datetime"})
    if not isinstance(end, str) or not end.strip():
        return _dumps({"error": "propose_event requires an 'end' ISO datetime"})
    if not isinstance(summary, str):
        summary = ""
    if not isinstance(origin_context, str):
        origin_context = ""

    response = await peer_propose_event(
        _PEER_TARGET,
        title=title.strip(),
        start=start.strip(),
        end=end.strip(),
        summary=summary,
        origin_context=origin_context,
        config=transport_config,
        self_name=self_name,
    )
    return _dumps(response)


# --- Attribution-marker wiring (calibration audit gap, c2) ---------------
#
# The agent slug used in attribution markers comes from
# :func:`alfred.audit.agent_slug_for` — a single canonical helper used by
# both this module and non-talker writers (audit sweep, capture_batch,
# calibration, Daily Sync). Keeping the slug derivation in one place
# avoids the second-copy drift the canonical-helper pattern exists to
# prevent.


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


# Per-tool "minimal-keys-only" signatures used to detect tool_use inputs
# that were max_tokens-truncated mid-emission. The check fires only when
# stop_reason == "max_tokens" — without that signal the model may have
# legitimately chosen a no-op shape and we don't want to false-positive
# on it. (For vault_edit specifically, the no-op shape itself is denied
# by the runtime gate in ``vault.ops.vault_edit``; the truncation
# detector is a higher-quality SIGNAL — it tells the model the reason
# was likely truncation, not a missing-action bug.)
#
# Each entry maps tool_name → ``{"identifier_keys": {...}, "action_keys":
# {...}}``:
#   * identifier_keys — keys the SDK emits first in JSON order (path,
#     type, etc.) that on their own don't drive any mutation
#   * action_keys — at least one MUST be present for the call to do
#     anything; absence + stop_reason=max_tokens is the truncation
#     signature
#
# Add entries here when a new tool surface exhibits the same failure
# mode (long-body tool_use input mid-stream truncation).
_TRUNCATION_DETECT_SIGNATURES: dict[str, dict[str, set[str]]] = {
    "vault_edit": {
        "identifier_keys": {"path"},
        "action_keys": {
            "set_fields",
            "append_fields",
            "body_append",
            "body_replace",
            "body_insert_at",
        },
    },
}


def _detect_truncated_tool_input(
    tool_name: str,
    tool_input: dict[str, Any],
    stop_reason: str,
) -> dict[str, Any] | None:
    """Detect a tool_use block whose input was likely max_tokens-truncated.

    Returns a diagnostic dict suitable for logging + tool_result error
    surfacing when the signature matches, otherwise ``None``.

    The detection is intentionally conservative: it only fires when
    ``stop_reason == "max_tokens"`` AND the tool_input carries the
    tool's identifier keys (``path`` for vault_edit) without any
    action keys (mutation params). Both conditions together indicate
    the JSON emission ran out of budget after the prefix but before
    the action params — the exact signature of the Hypatia 2026-05-21
    essay-planning failure.

    Per ``feedback_intentionally_left_blank.md``: silence is ambiguous;
    when the SDK delivers a tool_use input that's structurally
    incomplete, we surface that to both the operator (via log) and the
    model (via tool_result.is_error) instead of letting the downstream
    op crash with a confusing "missing required field" or — worse —
    silently no-op.
    """
    if stop_reason != "max_tokens":
        return None
    sig = _TRUNCATION_DETECT_SIGNATURES.get(tool_name)
    if sig is None:
        return None
    if not isinstance(tool_input, dict):
        return None
    keys = set(tool_input.keys())
    action_keys = sig["action_keys"]
    identifier_keys = sig["identifier_keys"]
    has_action = bool(keys & action_keys)
    has_identifier = bool(keys & identifier_keys)
    if has_action:
        return None
    # No action keys present. If we ALSO have no identifier keys it's
    # not really our truncation signature — probably a different
    # malformation that the downstream op will surface clearly. Only
    # fire when the prefix landed but the action tail didn't.
    if not has_identifier:
        return None
    return {
        "tool_name": tool_name,
        "received_keys": sorted(keys),
        "expected_action_keys": sorted(action_keys),
        "stop_reason": stop_reason,
    }


# --- Role-aware scope resolution (VERA MVP, 2026-06-09) --------------------
#
# VERA is the first multi-user instance: Andrew (owner) and Ben (ops) hit
# the same daemon / same ``tool_set: vera`` but must route to DIFFERENT
# vault scopes. Every other instance is single-role — scope is a pure
# function of ``tool_set`` and ``role`` is ignored.
#
# This helper is the ONE cross-cutting keystone change ratified in the P0
# matrix (Decision A). It branches ONLY for ``tool_set == "vera"``; for
# every existing instance it returns ``tool_set`` unchanged, so Salem /
# KAL-LE / Hypatia behaviour is byte-identical to before (the previous
# inline ``active_scope = config.instance.tool_set or "talker"``).
_OWNER_ROLE = "owner"
_OPS_ROLE = "ops"


def resolve_scope(tool_set: str, role: str) -> str:
    """Map ``(tool_set, role)`` → vault scope name.

    VERA-only role split: ``vera`` tool_set routes owner → ``vera`` scope,
    any non-owner role → ``vera_ops`` scope. Every other tool_set is
    role-independent and returns unchanged (single-role instances).

    ``tool_set`` empty / falsy → ``"talker"`` (Salem's legacy default,
    preserved from the pre-VERA inline resolution). ``role`` empty / falsy
    is treated as owner (back-compat: a flat-allowlist instance whose
    users carry no explicit role default to owner — see
    ``config.AllowedUser`` and ``bot._role_for``).
    """
    ts = tool_set or "talker"
    if ts == "vera":
        return "vera" if (role or _OWNER_ROLE) == _OWNER_ROLE else "vera_ops"
    return ts


async def _execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    vault_path: str,
    state: StateManager,
    session: Session,
    config: TalkerConfig | None = None,
    user_role: str = _OWNER_ROLE,
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

    ``user_role`` (VERA MVP, 2026-06-09) is the sending Telegram user's
    role (``"owner"`` / ``"ops"``), plumbed from the bot dispatch via
    ``run_turn``. Combined with the instance ``tool_set`` by
    :func:`resolve_scope` to pick the vault scope. Defaults to ``"owner"``
    so single-role callers (every instance except VERA) and legacy test
    callers route exactly as before.
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

    # ``routine_done`` (Salem, Phase 2B B1 2026-05-30) — conversational
    # completion. Subprocess-invokes ``alfred routine done`` with the
    # ``talker_routine_completion`` scope. The CLI's structured JSON
    # output (canary ``kind`` discriminator) is returned verbatim so
    # Salem can route on it.
    if tool_name == "routine_done":
        return await _dispatch_routine_done(
            tool_input=tool_input,
            session=session,
            config=config,
        )

    # ``routine_item`` (Salem, Phase 2B B3 2026-05-30) — item-level
    # CRUD on existing routines (add / remove / edit). Same dispatch
    # shape as ``routine_done``; routes via subprocess against the
    # ``talker_routine_item`` scope.
    if tool_name == "routine_item":
        return await _dispatch_routine_item(
            tool_input=tool_input,
            session=session,
            config=config,
        )

    # Inter-instance Phase A peer tools (KAL-LE, Hypatia → Salem). Each
    # of these routes via :mod:`alfred.transport.client` against the
    # configured ``salem`` peer. Salem (talker scope) must NOT see these
    # tools in its tool list — verified upstream in ``VAULT_TOOLS_BY_SET``
    # — but we still tool-set-gate inside the dispatcher as a second
    # line of defence (same shape as bash_exec).
    if tool_name in {
        "query_canonical",
        "propose_person",
        "propose_org",
        "propose_location",
        "propose_event",
    }:
        return await _dispatch_peer_inter_instance_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            session=session,
            config=config,
        )

    # GCal read tool — Phase A capability-audit close 2026-05-06. Only
    # surfaced in the tool list when ``gcal.enabled: true``; the
    # dispatch helper does its own config / scope / API-error handling
    # and returns ``{"error": ...}`` shapes that match the rest of the
    # dispatcher.
    if tool_name == "gcal_list_events":
        return await _dispatch_gcal_list_events(
            tool_input=tool_input,
            config=config,
        )

    op = _TOOL_TO_OP.get(tool_name)
    if op is None:
        return _dumps({"error": f"Unknown tool: {tool_name}"})

    rel_path = tool_input.get("path", "") if isinstance(tool_input, dict) else ""
    record_type = tool_input.get("type", "") if isinstance(tool_input, dict) else ""
    set_fields = tool_input.get("set_fields") if isinstance(tool_input, dict) else None

    vault_path_obj = Path(vault_path)

    # Per-instance, per-role scope routing. Without this, every bot
    # (Salem, KAL-LE, Hypatia) routed through ``check_scope("talker", ...)``
    # and Hypatia ``document`` / KAL-LE ``pattern`` creates were rejected
    # at ``talker_types_only`` BEFORE the scope-aware ``_validate_type``
    # gate in ops.py ever engaged. The config's ``instance.tool_set`` is
    # the source of truth — it's already used to pick the tool schema in
    # ``tools_for_set`` (KAL-LE → bash_exec, Salem/Hypatia → vault-only)
    # so reusing it for scope dispatch keeps the contract consistent.
    #
    # VERA MVP (2026-06-09): scope is now a function of (tool_set, role),
    # not tool_set alone — VERA is the first instance where the sending
    # user's role splits the scope (owner → ``vera``, ops → ``vera_ops``).
    # ``resolve_scope`` branches ONLY for ``tool_set == "vera"``; every
    # other instance is role-independent and resolves to ``tool_set``
    # unchanged (``"talker"`` when ``config`` is None — legacy callers,
    # tests that skip the config plumb-through).
    tool_set = (
        config.instance.tool_set
        if config and config.instance and config.instance.tool_set
        else "talker"
    )
    active_scope = resolve_scope(tool_set, user_role)

    # Scope enforcement — the scope check happens BEFORE the op so we never
    # attempt a denied mutation.
    try:
        scope.check_scope(
            active_scope,
            op,
            rel_path=rel_path,
            record_type=record_type,
            frontmatter=set_fields if isinstance(set_fields, dict) else None,
        )
    except scope.ScopeError as exc:
        log.info(
            "talker.tool.scope_denied",
            tool=tool_name,
            scope=active_scope,
            error=str(exc),
        )
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

            # c6 (2026-05-31) — per-type field-allowlist + date-future
            # check for ``daily`` records. The talker scope's standard
            # ``create: talker_types_only`` permits ``daily`` type
            # (added to TALKER_CREATE_TYPES this ship), but the operator
            # contract narrows the write surface to ONLY the
            # ``tier_curation`` field, and ONLY for today-or-future
            # dates. Two checks here, both fail-loud as scope_denied:
            #
            #   1. set_fields keys must be a subset of {tier_curation}
            #      — enforced by check_talker_tier_curation_fields.
            #      Body content + aggregator-owned fields stay rejected.
            #   2. name (which becomes the filename stem + iso date)
            #      must be an ISO YYYY-MM-DD string >= today. Past
            #      dates would be operationally weird (the aggregator
            #      already wrote that day; talker pre-write would
            #      stomp historical context). Today is allowed because
            #      the aggregator may not have fired yet (pre-05:59 ADT).
            if record_type == "daily":
                supplied_fields: list[str] | None = (
                    list(set_fields.keys())
                    if isinstance(set_fields, dict) else None
                )
                try:
                    scope.check_talker_tier_curation_fields(
                        record_type, supplied_fields,
                    )
                except scope.ScopeError as exc:
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="tier_curation_field_allowlist",
                        error=str(exc),
                    )
                    return _dumps({"error": f"scope denied: {exc}"})
                # Body content denied — the aggregator owns the body.
                # Defense-in-depth even though the SKILL will tell the
                # LLM to leave body empty for daily/.
                if body:
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="daily_body_aggregator_owned",
                    )
                    return _dumps({
                        "error": (
                            "scope denied: ``daily`` records have "
                            "aggregator-owned body content. Pre-set "
                            "``tier_curation`` via set_fields only; "
                            "leave body empty (the aggregator's next "
                            "fire will fill it via render_daily_body)."
                        ),
                    })
                # Date-future gate. ``name`` becomes the filename stem
                # AND is treated as the date by the aggregator. Reject
                # past dates fail-loud.
                try:
                    target_date = _dt.date.fromisoformat(name)
                except (TypeError, ValueError):
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="daily_name_not_iso_date",
                        name=name,
                    )
                    return _dumps({
                        "error": (
                            f"scope denied: ``daily`` record name must "
                            f"be ISO YYYY-MM-DD (e.g. ``2026-06-01``). "
                            f"Got: {name!r}."
                        ),
                    })
                today_local = _dt.date.today()
                if target_date < today_local:
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="daily_date_in_past",
                        name=name,
                    )
                    return _dumps({
                        "error": (
                            f"scope denied: ``daily`` pre-set requires "
                            f"today or future date. Got: {name} (today "
                            f"is {today_local.isoformat()}). Use a "
                            f"future ISO date (e.g. tomorrow)."
                        ),
                    })

            # Attribution-marker wiring (calibration audit gap, c2). The
            # talker invokes vault_create as a side-effect of an LLM
            # turn — every body that lands this way is, by definition,
            # agent-inferred prose, not Andrew-typed text. Wrap it so a
            # future Daily Sync confirm/reject flow can surface it for
            # explicit confirmation. No-op when ``body`` is None (the
            # template-default-body path); the model only triggers wrapping
            # when it composed body content itself.
            sf = dict(set_fields) if isinstance(set_fields, dict) else {}

            # c6 (2026-05-31) — auto-populate aggregator-owned ``date``
            # field for daily/ creates. The ``daily`` TypeDefinition
            # requires ``date`` per schema.py, but the operator-facing
            # field-allowlist denies operator from supplying it (only
            # ``tier_curation`` is allowlisted at the dispatch gate).
            # The tool layer already parses ``name`` as ISO YYYY-MM-DD
            # for the future-date check — reuse that as the ``date``
            # value. The aggregator's next fire will overwrite this
            # field with its own ISO render (per render_daily_body /
            # aggregator.py:838), so this is just bridging the gap
            # for the record-validator + janitor first-pass on a
            # talker-pre-set daily file. Defensive guard: only set
            # when not already present so a future refactor that
            # allows operator-supplied ``date`` doesn't get stomped.
            if record_type == "daily" and "date" not in sf:
                sf["date"] = name

            if body:
                wrapped_body, audit_entry = attribution.with_inferred_marker(
                    body,
                    section_title=_section_title_for_create(name, body),
                    agent=agent_slug_for(config),
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
                scope=active_scope,
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
            body_insert_at = tool_input.get("body_insert_at")
            body_replace = tool_input.get("body_replace")

            # c6 (2026-05-31) — per-type field-allowlist for talker
            # edits on existing ``daily/<date>.md`` records. Mirror of
            # the vault_create branch's daily-specific check; path-
            # based detection (rel_path starts with "daily/") since
            # the edit tool doesn't carry ``type=`` in tool_input.
            # Fail-loud as scope_denied on any field outside
            # TALKER_TIER_CURATION_FIELDS, on any body-mutation tool
            # use (body_append / body_insert_at / body_replace —
            # aggregator owns the body), or on any append_fields use
            # (the daily fields supporting append are aggregator-owned).
            if rel_path.startswith("daily/"):
                set_field_keys = (
                    list(set_fields.keys())
                    if isinstance(set_fields, dict) else []
                )
                append_field_keys = (
                    list(append_fields.keys())
                    if isinstance(append_fields, dict) else []
                )
                supplied_fields_edit = set_field_keys + append_field_keys
                # Empty-fields case: fail-loud (no-op edits aren't
                # interesting + would otherwise hit the field-check
                # helper's "did not supply" branch — pass None to
                # surface the actionable error message).
                fields_for_check: list[str] | None = (
                    supplied_fields_edit or None
                )
                try:
                    scope.check_talker_tier_curation_fields(
                        "daily", fields_for_check,
                    )
                except scope.ScopeError as exc:
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="tier_curation_field_allowlist",
                        path=rel_path,
                        error=str(exc),
                    )
                    return _dumps({"error": f"scope denied: {exc}"})
                # Body-mutation tools denied — aggregator owns body.
                if body_append or body_insert_at or body_replace:
                    log.info(
                        "talker.tool.scope_denied",
                        tool=tool_name,
                        scope=active_scope,
                        reason="daily_body_aggregator_owned",
                        path=rel_path,
                    )
                    return _dumps({
                        "error": (
                            "scope denied: ``daily`` records have "
                            "aggregator-owned body content. Edit "
                            "``tier_curation`` via set_fields only; "
                            "body_append / body_insert_at / body_replace "
                            "are denied on daily/ records."
                        ),
                    })

            # Attribution-marker wiring (calibration audit gap, c2). For
            # body_append, wrap ONLY the appended fragment — the existing
            # record body is left as-is (it may contain Andrew-typed prose
            # that already shipped). Merge the new audit entry with any
            # entries already on the record so prior inferred sections
            # aren't lost when this edit lands.
            #
            # body_insert_at + body_replace (P1 from QA 2026-05-04 c3):
            # the same attribution wrapping applies to LLM-generated
            # content from these new tools. body_insert_at wraps the
            # ``content`` field (the inserted block); body_replace wraps
            # the entire replacement body (it IS the inserted content).
            sf = dict(set_fields) if isinstance(set_fields, dict) else {}
            needs_attribution = bool(
                body_append
                or body_replace
                or (
                    isinstance(body_insert_at, dict)
                    and body_insert_at.get("content")
                )
            )
            if needs_attribution:
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

                if body_append:
                    wrapped_append, audit_entry = attribution.with_inferred_marker(
                        body_append,
                        section_title=_section_title_for_edit_append(
                            body_append, rel_path,
                        ),
                        agent=agent_slug_for(config),
                        reason=_attribution_reason(session),
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                    body_append = wrapped_append
                elif body_replace:
                    # Full body rewrite — wrap the entire replacement.
                    # Section title falls back to the file stem because
                    # a full rewrite isn't bound to one section.
                    wrapped_replace, audit_entry = attribution.with_inferred_marker(
                        body_replace,
                        section_title=Path(rel_path).stem or "body_replace",
                        agent=agent_slug_for(config),
                        reason=_attribution_reason(session),
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                    body_replace = wrapped_replace
                elif (
                    isinstance(body_insert_at, dict)
                    and body_insert_at.get("content")
                ):
                    # Mid-doc insertion — wrap the content fragment only.
                    # Section title derived from the marker (typically a
                    # heading) so the audit entry points at the right
                    # location in the doc.
                    insert_content = str(body_insert_at["content"])
                    marker_for_title = str(body_insert_at.get("marker", ""))
                    section_title = (
                        _section_title_for_edit_append(
                            insert_content, rel_path,
                        )
                        if insert_content.strip().startswith("#")
                        else (marker_for_title or Path(rel_path).stem)
                    )
                    wrapped_insert, audit_entry = attribution.with_inferred_marker(
                        insert_content,
                        section_title=section_title,
                        agent=agent_slug_for(config),
                        reason=_attribution_reason(session),
                    )
                    attribution.append_audit_entry(merged_fm, audit_entry)
                    # Rebuild body_insert_at with the wrapped content.
                    body_insert_at = dict(body_insert_at)
                    body_insert_at["content"] = wrapped_insert
                sf = merged_fm

            result = ops.vault_edit(
                vault_path_obj,
                rel_path,
                set_fields=sf or None,
                append_fields=append_fields if isinstance(append_fields, dict) else None,
                body_append=body_append,
                body_insert_at=(
                    body_insert_at if isinstance(body_insert_at, dict) else None
                ),
                body_replace=body_replace if body_replace else None,
                scope=active_scope,
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
    image_blocks: list[dict[str, Any]] | None = None,
    user_role: str = _OWNER_ROLE,
    user_name: str | None = None,
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

    ``image_blocks`` (vision phase 2) is a list of pre-built Anthropic
    image content blocks (``{"type": "image", "source": {...}}``) to
    prepend to the user turn. ``None`` / empty preserves the wk1 plain-
    string transcript shape so existing tests + render paths stay
    byte-identical. When present, the user turn is stored as a content-
    block list (image blocks first, then text) — the SDK accepts either
    shape on subsequent turns so the round-trip stays clean.

    ``user_role`` (VERA MVP) is the sending user's role
    (``"owner"`` / ``"ops"``) — threaded to ``_execute_tool`` →
    ``resolve_scope`` for per-role vault scoping. Defaults to ``"owner"``
    so single-user instances route exactly as before.

    ``user_name`` (VERA reporter follow-up) is the sending user's
    configured display name, used to build a per-message sender-identity
    system block so the agent can attribute authorship (e.g. a ticket
    ``reporter``). ``None`` — the default and the value on every
    single-user / flat-list instance — omits the block entirely, leaving
    the system blocks byte-identical to pre-feature behaviour. Dynamic
    per-message: the block is rebuilt each turn because the sender can
    change between turns in a shared chat.

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
    # Vision: when image blocks are attached we store the user turn as a
    # content-block list (image-then-text per Anthropic best-practice
    # ordering); otherwise the wk1 bare-string shape is preserved.
    from .vision import build_user_content
    user_content = build_user_content(user_message, image_blocks)
    append_turn(state, session, "user", user_content, kind=user_kind)

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

    # Operator-preference V1 (project_operator_preferences_v1) — Shape B
    # voice block. Loaded per-turn rather than per-session because a
    # preference change between turns should take effect on the next
    # turn (cheap re-read; preferences are file-on-disk). Defensive:
    # any load failure returns None and the block is omitted; the
    # talker keeps running.
    try:
        voice_pref_block = load_voice_preferences_block(
            vault_path=config.vault.path,
            instance_name=config.instance.name,
        )
    except Exception as exc:
        log.warning(
            "talker.preferences.block_build_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            detail="continuing without voice preferences block",
        )
        voice_pref_block = None

    # Sender-identity block (VERA reporter follow-up 2026-06-09). Built
    # ONLY when the sending user has a configured display name —
    # i.e. multi-user instances (VERA). Single-user instances pass
    # ``user_name=None`` so this stays None and the block is omitted from
    # the system blocks: byte-identical behaviour for Salem / KAL-LE /
    # Hypatia. The block is dynamic per-message (sender can change between
    # turns in a shared chat); ``_build_system_blocks`` places it in the
    # uncached tail so per-message rebuilds don't churn the cache prefix.
    sender_identity_block = (
        _build_sender_identity_text(user_name, user_role)
        if user_name
        else None
    )

    system_blocks = _build_system_blocks(
        system_prompt,
        vault_context_str,
        calibration_str=calibration_str,
        pushback_level=pushback_level,
        voice_preferences_block=voice_pref_block,
        sender_identity_block=sender_identity_block,
    )
    vault_path = config.vault.path
    # Stage 3.5: pick the tool list per instance tool_set. Salem
    # ("talker") gets vault-only; KAL-LE ("kalle") gets vault + bash_exec.
    # Defaults to the talker set so any misconfigured instance can't
    # accidentally surface bash_exec.
    #
    # GCal capability gating (2026-05-06): we surface the
    # ``gcal_list_events`` tool only when the active config has
    # ``gcal.enabled: true``. Lazy-load mirrors the dispatch path —
    # one yaml read per turn is cheap and keeps the GCal binding out
    # of the talker hot path. Failure to load is non-fatal: we fall
    # back to "no GCal tool" so the model can still answer (just
    # without calendar reads), matching the pre-feature behaviour
    # for any instance that doesn't carry a gcal block.
    gcal_enabled = _resolve_gcal_enabled_for_run_turn(config)
    instance_tools = tools_for_set(
        config.instance.tool_set, gcal_enabled=gcal_enabled,
    )

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            create_kwargs = messages_create_kwargs(
                model=session.model,
                max_tokens=config.anthropic.max_tokens,
                temperature=config.anthropic.temperature,
                system=system_blocks,
                messages=_messages_for_api(session.transcript),
                tools=instance_tools,
            )
            response = await client.messages.create(**create_kwargs)
        except anthropic.APIError:
            # Surface to caller — bot.py translates to a user-facing reply.
            log.warning("talker.api_error", iteration=iteration)
            raise

        stop_reason = getattr(response, "stop_reason", "end_turn")

        # Dispatch is content-based, NOT stop_reason-based (race-fix
        # 2026-05-09 from Hypatia voice-profile rebuild WARN-2). The
        # SDK reports ``stop_reason="tool_use"`` only when the model
        # finished naturally on a tool_use turn. When the response hits
        # ``max_tokens`` mid-stream — which is exactly what happens when
        # the model emits a long announcement-paragraph plus several
        # tool_use blocks each carrying substantial body content — the
        # final tool_use block(s) still come through the SDK as fully-
        # formed blocks, but stop_reason flips to ``"max_tokens"``.
        # Pre-fix the loop's "if stop_reason == 'tool_use'" guard fell
        # through to the end-turn branch, persisted the tool_use blocks
        # in the assistant transcript, and returned the partial text
        # reply — leaving every tool_use id DANGLING with no tool_result.
        # The 2026-05-03 ``_messages_for_api`` heal masked the symptom
        # on the NEXT user turn (synthesised tool_result blocks so the
        # API didn't 400), but the actual tool execution never happened.
        # User experience: 7 minutes of silence between announcement and
        # the operator pinging "Progress?" — the mid-stream truncation
        # was completely invisible.
        #
        # Fix: dispatch on whether the response carries any tool_use
        # blocks, regardless of stop_reason. This lets max_tokens-stop
        # responses with tool_use blocks STILL execute the (well-formed)
        # blocks they emitted, append tool_results, and continue the
        # loop — the next API call will let the model finish whatever
        # work was truncated. A max_tokens-stop with NO tool_use blocks
        # falls through to the existing end-turn path (the model just
        # ran long on text — partial reply is still useful).
        has_tool_use_blocks = any(
            getattr(b, "type", None) == "tool_use"
            for b in (response.content or [])
        )

        if has_tool_use_blocks:
            # Observability: when we entered this branch on a non-
            # ``tool_use`` stop_reason, log it explicitly. The most
            # common case is ``"max_tokens"`` (model ran out of budget
            # mid-stream). Per ``feedback_intentionally_left_blank.md``
            # — silence is ambiguous; emit an explicit signal so the
            # operator can grep for the truncation pattern. Also surfaces
            # any future stop_reason that lands tool_use blocks
            # (``"refusal"`` if a future content-policy stop somehow
            # produces partial tool_use, etc.).
            if stop_reason != "tool_use":
                log.warning(
                    "talker.run_turn.tool_use_with_nonstandard_stop",
                    iteration=iteration,
                    stop_reason=stop_reason,
                    tool_use_count=sum(
                        1 for b in response.content
                        if getattr(b, "type", None) == "tool_use"
                    ),
                    detail=(
                        "Response contained tool_use blocks but "
                        "stop_reason was not 'tool_use'. Most likely "
                        "max_tokens-truncated mid-stream — the tool_use "
                        "blocks the model DID emit will execute "
                        "normally; the next iteration lets the model "
                        "finish whatever was cut off. Pre-2026-05-09 "
                        "fix: this case fell through to end-turn and "
                        "left the tool_use blocks dangling, presenting "
                        "as a multi-minute silent gap to the user."
                    ),
                )
            # Append assistant turn (list of blocks) so the tool_use IDs are
            # preserved for the matching tool_result.
            append_turn(state, session, "assistant", _blocks_to_jsonable(response.content))

            # Execute every tool_use block in order, collect tool_results.
            #
            # Per-tool try/except (race-fix 2026-05-03): the assistant
            # turn above is persisted IMMEDIATELY by ``append_turn``.
            # Any unhandled exception from ``_execute_tool`` here would
            # exit ``run_turn`` with the assistant turn on disk + no
            # matching tool_result user turn — wedge state. The next
            # ``run_turn`` call would append a regular user message,
            # sealing the dangling ``tool_use`` IDs. Subsequent
            # ``client.messages.create`` then 400s with
            # ``tool_use ids were found without tool_result blocks
            # immediately after``. User sees "API error try again";
            # retry hits the same wall.
            #
            # ``_execute_tool`` itself wraps vault ops in try/except
            # and returns ``{"error": ...}`` JSON for known failure
            # modes (line ~1104 docstring). But anything outside its
            # catch — an uncaught import error, a syscall-level
            # failure (vault disk full mid-write), an asyncio.
            # CancelledError from daemon shutdown — would propagate.
            # The per-tool try/except here is the safety net.
            #
            # Synthetic tool_result on failure: ``is_error: True`` +
            # detail naming the exception class so the model can see
            # what went wrong and recover (apologise, pick a
            # different tool, retry with different args). The
            # transcript stays well-formed; the next API call
            # succeeds.
            # Pre-collect every tool_use id in this assistant turn so
            # the cancellation handler can synthesize tool_results for
            # the un-iterated tail (P0 from QA 2026-05-04). Pre-fix:
            # CancelledError flushed only the cancelled tool's
            # synthetic + tools BEFORE it; tools AFTER it never got
            # iterated → tool_use ids dangling on next API call →
            # heal fired and the LLM read the heal's "interrupted
            # before completing" wording back to Andrew as a NEW
            # symptom. Post-fix: complete the partial tool_results
            # set with synthetic-cancelled blocks for every remaining
            # tool_use id before re-raising.
            all_tool_use_ids: list[str] = [
                getattr(b, "id", "")
                for b in response.content
                if getattr(b, "type", None) == "tool_use"
                and getattr(b, "id", "")
            ]
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

                # Truncation pre-check (Layer 2 of the Hypatia 2026-05-21
                # essay-planning fix). When stop_reason=max_tokens AND
                # this tool_use's input matches a known "identifier-only,
                # no action keys" signature, surface the diagnosis BEFORE
                # dispatch so the model and the operator see "tool_use
                # input was truncated" rather than the downstream
                # generic-error surface.
                #
                # Layer 1 (the no-op gate inside vault_edit) catches the
                # same case and produces a usable error on its own — but
                # the truncation-aware error is more actionable because
                # it names the root cause. We synthesize the tool_result
                # here, skip the dispatch entirely, log the diagnosis,
                # and continue the loop. The next iteration lets the
                # model retry with a smaller payload.
                trunc_diag = _detect_truncated_tool_input(
                    tool_name,
                    tool_input if isinstance(tool_input, dict) else {},
                    stop_reason,
                )
                if trunc_diag is not None:
                    log.warning(
                        "talker.tool.input_truncated",
                        iteration=iteration,
                        tool=tool_name,
                        tool_use_id=tool_use_id,
                        received_keys=trunc_diag["received_keys"],
                        expected_action_keys=trunc_diag["expected_action_keys"],
                        stop_reason=stop_reason,
                        detail=(
                            "tool_use input was likely max_tokens-"
                            "truncated mid-emission — arrived with "
                            "identifier keys only and no action params. "
                            "Synthesising an error tool_result so the "
                            "model can retry with a smaller payload. "
                            "Recommend the operator consider raising "
                            "anthropic.max_tokens if this recurs."
                        ),
                    )
                    error_payload = {
                        "error": (
                            f"{tool_name} tool_use input was likely "
                            f"max_tokens-truncated mid-emission — "
                            f"arrived with only "
                            f"{trunc_diag['received_keys']} (no "
                            f"action keys from "
                            f"{trunc_diag['expected_action_keys']}). "
                            f"Retry with a smaller payload or split "
                            f"the operation across multiple calls."
                        ),
                    }
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": _dumps(error_payload),
                        "is_error": True,
                    })
                    continue

                try:
                    result_str = await _execute_tool(
                        tool_name,
                        tool_input if isinstance(tool_input, dict) else {},
                        vault_path,
                        state,
                        session,
                        config=config,
                        user_role=user_role,
                    )
                    is_error = False
                except asyncio.CancelledError:
                    # Re-raise — daemon shutdown / task cancellation
                    # must propagate. Before raising, complete the
                    # tool_results set so the persisted transcript is
                    # well-formed when the daemon comes back up and
                    # rehydrates.
                    #
                    # Append the cancelled tool's synthetic FIRST,
                    # THEN walk the remaining (un-iterated) tool_use
                    # ids and synthesize "cancelled" results for each.
                    # Without the second step, every tool AFTER the
                    # cancelled one in this assistant turn dangles,
                    # the next run_turn's heal fires for them, and
                    # the LLM parrots the heal's "interrupted before
                    # completing" content back to the user as a new
                    # symptom (the operator-confusing recurrence
                    # this commit closes).
                    log.warning(
                        "talker.tool.cancelled",
                        iteration=iteration,
                        tool=tool_name,
                        tool_use_id=tool_use_id,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            "Tool execution was cancelled (daemon "
                            "shutdown or task cancellation). Result "
                            "unavailable."
                        ),
                        "is_error": True,
                    })
                    # Synthesize cancelled-results for every tool_use
                    # id AFTER the cancelled one (anything not yet
                    # in tool_results).
                    already_resulted_ids = {
                        r["tool_use_id"] for r in tool_results
                        if isinstance(r, dict) and r.get("tool_use_id")
                    }
                    unprocessed_ids = [
                        tid for tid in all_tool_use_ids
                        if tid not in already_resulted_ids
                    ]
                    for un_tid in unprocessed_ids:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": un_tid,
                            "content": (
                                "Tool execution was cancelled before "
                                "this tool ran (preceding tool in the "
                                "same turn was cancelled). Result "
                                "unavailable."
                            ),
                            "is_error": True,
                        })
                    log.warning(
                        "talker.tool.cancellation_flushed_full_set",
                        iteration=iteration,
                        cancelled_tool_use_id=tool_use_id,
                        unprocessed_tool_use_ids=unprocessed_ids,
                        unprocessed_count=len(unprocessed_ids),
                        total_tool_use_ids_in_turn=len(all_tool_use_ids),
                        detail=(
                            "Synthesised tool_result blocks for the "
                            "un-iterated tail of the assistant turn so "
                            "no tool_use id dangles after re-raise. "
                            "Closes the heal-firing-on-restart symptom."
                        ),
                    )
                    # Flush the COMPLETE tool_results set so the
                    # transcript is well-formed before re-raising.
                    append_turn(state, session, "user", tool_results)
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Any other exception: synthesize an error
                    # tool_result and continue with the rest of the
                    # tool calls. Per ``feedback_intentionally_left
                    # _blank.md`` — log loudly so the operator knows
                    # what failed.
                    log.warning(
                        "talker.tool.execute_failed",
                        iteration=iteration,
                        tool=tool_name,
                        tool_use_id=tool_use_id,
                        error_class=exc.__class__.__name__,
                        error=str(exc)[:500],
                        detail=(
                            "_execute_tool raised an unhandled "
                            "exception; synthesising an error "
                            "tool_result so the transcript stays "
                            "well-formed (preserves tool_use/"
                            "tool_result pairing)."
                        ),
                    )
                    result_str = _dumps({
                        "error": (
                            f"Tool execution failed with "
                            f"{exc.__class__.__name__}: {str(exc)[:200]}"
                        ),
                    })
                    is_error = True
                tool_result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                }
                if is_error:
                    tool_result_block["is_error"] = True
                tool_results.append(tool_result_block)

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
