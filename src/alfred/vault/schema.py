"""Shared vault schema constants — record types, statuses, field definitions."""

from __future__ import annotations

# --- Known record types and their valid statuses ---

KNOWN_TYPES: set[str] = {
    "project", "task", "session", "input", "person", "org",
    "location", "note", "decision", "process", "run", "event",
    "account", "asset", "conversation", "assumption", "constraint",
    "contradiction", "synthesis",
}

# Stage 3.5: record types KAL-LE uses inside ``~/aftermath-lab/``. Kept
# in a separate set so the canonical ``KNOWN_TYPES`` (Salem's
# operational world) stays focused. The kalle scope check (see
# ``vault/scope.py::KALLE_CREATE_TYPES``) intersects these with its
# own create allowlist. ``pattern`` is a reusable development pattern;
# ``principle`` is a higher-level development principle.
KNOWN_TYPES_KALLE: set[str] = {
    "pattern", "principle",
}

# Hypatia operates inside ``~/library-alexandria/`` (see
# ``library-alexandria/CLAUDE.md`` for directory layout + frontmatter
# shapes). Like the kalle set, kept separate from ``KNOWN_TYPES`` so
# Salem's operational vault doesn't gain Hypatia-only types. The
# ``hypatia`` scope check (see ``vault/scope.py::HYPATIA_CREATE_TYPES``)
# is the authoritative create allowlist.
KNOWN_TYPES_HYPATIA: set[str] = {
    "document", "concept", "source", "citation", "template",
}


# Per-scope union of known record types. ``vault.ops._validate_type``
# uses this to gate ``vault_create`` / ``vault_list`` against the right
# type set: a Hypatia agent legitimately creates ``document`` records
# (canonical-vault-only ``KNOWN_TYPES`` would reject them); a Salem
# agent must NOT be able to create ``pattern`` records (KAL-LE-only).
#
# Scopes not listed here fall back to the canonical ``KNOWN_TYPES``
# only. The dict's purpose is "which extension sets does this scope
# unlock?" — not "what may this scope create?" (that's the create
# allowlists in ``vault.scope`` — ``KALLE_CREATE_TYPES``,
# ``HYPATIA_CREATE_TYPES``, ``TALKER_CREATE_TYPES``). Two-layer check:
# this gate lets the type through ``_validate_type``; the create
# allowlist in ``check_scope`` then enforces the per-scope policy.
#
# Pattern-trigger note: when a future instance (V.E.R.A., STAY-C)
# adds its own ``KNOWN_TYPES_<NAME>`` set, also add an entry here —
# otherwise ``_validate_type`` will silently reject the new types
# before scope enforcement gets a chance to run. See CLAUDE.md
# "Vault Operations Layer" for the rationale.
KNOWN_TYPES_BY_SCOPE: dict[str, set[str]] = {
    "kalle": KNOWN_TYPES | KNOWN_TYPES_KALLE,
    "hypatia": KNOWN_TYPES | KNOWN_TYPES_HYPATIA,
}


LEARN_TYPES: set[str] = {
    "assumption", "decision", "constraint", "contradiction", "synthesis",
}

STATUS_BY_TYPE: dict[str, set[str]] = {
    "project": {"active", "paused", "completed", "abandoned", "proposed"},
    "task": {"todo", "active", "blocked", "done", "cancelled"},
    "session": {"active", "completed"},
    "input": {"unprocessed", "processed", "deferred"},
    "person": {"active", "inactive"},
    "org": {"active", "inactive"},
    "location": {"active", "inactive"},
    "note": {"draft", "active", "review", "final"},
    "decision": {"draft", "final", "superseded", "reversed"},
    "process": {"active", "proposed", "design", "deprecated"},
    "run": {"active", "completed", "blocked", "cancelled"},
    "event": set(),  # no status constraint
    "account": {"active", "suspended", "closed", "pending"},
    "asset": {"active", "retired", "maintenance", "disposed"},
    "conversation": {"active", "waiting", "resolved", "closed", "archived"},
    "assumption": {"active", "challenged", "invalidated", "confirmed"},
    "constraint": {"active", "expired", "waived", "superseded"},
    "contradiction": {"unresolved", "resolved", "accepted"},
    "synthesis": {"draft", "active", "superseded"},
}

# Type → expected top-level directory
TYPE_DIRECTORY: dict[str, str] = {
    "project": "project",
    "task": "task",
    "person": "person",
    "org": "org",
    "location": "location",
    "note": "note",
    "decision": "decision",
    "process": "process",
    "run": "run",
    "event": "event",
    "account": "account",
    "asset": "asset",
    "conversation": "conversation",
    "assumption": "assumption",
    "constraint": "constraint",
    "contradiction": "contradiction",
    "synthesis": "synthesis",
    # session, input have flexible placement
}

# Frontmatter field names that carry instructor directives. Part of the
# alfred_instructions watcher contract:
#   - ``alfred_instructions`` — pending queue. Each entry is a directive
#     string the instructor daemon picks up and executes.
#   - ``alfred_instructions_last`` — completed archive. Each entry is a
#     dict of ``{text, executed_at, result}`` describing the directive
#     and its outcome.
INSTRUCTION_FIELDS: tuple[str, ...] = (
    "alfred_instructions",
    "alfred_instructions_last",
)

# Optional frontmatter fields on ``task`` records that carry reminder
# state. Part of the outbound-push transport contract:
#   - ``remind_at``     — pending reminder timestamp (ISO 8601, UTC).
#     When present and in the past, the transport scheduler fires a
#     reminder via Telegram.
#   - ``reminded_at``   — set by the scheduler on successful dispatch.
#     Clears ``remind_at``. Updating ``remind_at`` to a later value
#     (where ``reminded_at < remind_at``) re-arms the reminder.
#   - ``reminder_text`` — optional verbatim text that overrides the
#     default ``"Reminder: {title} (due {due})"`` template.
#
# Values are date or datetime when written from Python; the scheduler
# tolerates ISO-string, date-only, and tz-aware timestamps. None of
# these fields are required — they are opt-in per task.
REMINDER_FIELDS: tuple[str, ...] = (
    "remind_at",
    "reminded_at",
    "reminder_text",
)

# Fields that should be lists
LIST_FIELDS: set[str] = {
    "tags", "aliases", "related", "relationships", "participants",
    "outputs", "depends_on", "blocked_by", "based_on", "supports",
    "challenged_by", "approved_by", "confirmed_by", "invalidated_by",
    "cluster_sources", "governed_by", "references", "project",
    # Instruction fields — both are lists (pending queue + executed archive).
    "alfred_instructions", "alfred_instructions_last",
}

# Required fields for all records
REQUIRED_FIELDS: list[str] = ["type", "created"]

# Types that use "subject" instead of "name" as their title field
NAME_FIELD_BY_TYPE: dict[str, str] = {
    "conversation": "subject",
    "input": "subject",
}
