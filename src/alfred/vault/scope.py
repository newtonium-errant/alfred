"""Per-agent scope enforcement for vault operations."""

from __future__ import annotations

from .schema import LEARN_TYPES


class ScopeError(Exception):
    """Raised when an operation is denied by scope policy."""


# Operation → {scope: checker_function}
# Checkers receive (operation, rel_path, record_type) and raise ScopeError if denied.

SCOPE_RULES: dict[str, dict[str, bool | str | set[str]]] = {
    "curator": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": True,
        "edit": True,
        "move": "inbox_only",
        "delete": False,
        # Curator writes full record bodies at creation time and during
        # enrichment. Body writes stay allowed.
        "allow_body_writes": True,
    },
    "janitor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Janitor may create task records only when they carry the
        # alfred_triage: true frontmatter flag (Layer 3 triage queue).
        "create": "triage_tasks_only",
        # Stage 1/2 (autofix + link repair) narrows frontmatter writes to
        # a deliberate allowlist. Any field outside this set is rejected
        # by ``check_scope`` so the janitor can't mutate user-authored
        # content. Stage 3 enrichment runs under the separate
        # ``janitor_enrich`` scope below with its own allowlist.
        "edit": "field_allowlist",
        "edit_fields_allowlist": {
            "janitor_note",
            "type", "status",              # FM002/FM003 autofix
            "name", "subject",             # FM001 title
            "created",                     # FM001 mtime
            "related",                     # LINK002 autofix, DUP001 retargeting
            "tags",                        # FM004 scalar→list coercion
            "alfred_triage", "alfred_triage_kind", "alfred_triage_id",
            "candidates", "priority",      # triage task creation
        },
        "move": False,
        "delete": True,
        # Q3 body-write loophole: the field allowlist above gates
        # set_fields/append_fields on frontmatter, but ``vault edit``
        # also accepts ``--body-append`` / ``--body-stdin`` which would
        # otherwise let a Stage 1/2 agent rewrite entire record bodies
        # and sidestep the allowlist. Janitor's structural fixes never
        # need to write body content — the body is user-authored and
        # stays immutable under this scope.
        "allow_body_writes": False,
    },
    # Stage 3 enrichment writes substantive content (description, role,
    # email, etc.) onto existing stub person/org records. Split out as
    # its own scope so the Stage 1/2 allowlist stays tight. Stage 3 never
    # creates or moves records — it only fills in fields on records the
    # structural scanner flagged as STUB001.
    "janitor_enrich": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "field_allowlist",
        "edit_fields_allowlist": {
            "description", "role", "org", "email", "org_type",
            "website", "phone", "aliases", "related", "tags",
        },
        "move": False,
        "delete": False,
        # Stage 3 may append a brief description paragraph to stub
        # bodies when the frontmatter allowlist isn't enough. Body
        # writes stay allowed under this scope.
        "allow_body_writes": True,
    },
    "distiller": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": "learn_types_only",
        # Distiller writes distiller_signals and distiller_learnings
        # back to source records (see distiller/pipeline.py).
        "edit": "distiller_fields_only",
        "move": False,
        "delete": False,
        "allow_body_writes": True,
    },
    "surveyor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        # Surveyor writes alfred_tags and relationships to frontmatter
        # (see surveyor/writer.py). Content is read-only.
        "edit": "tags_only",
        "move": False,
        "delete": False,
        # Surveyor writes only frontmatter tags; leave the default
        # permissive body-write rule on to avoid surprising a
        # hypothetical future surveyor body-rewrite.
        "allow_body_writes": True,
    },
    "talker": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Talker (Telegram voice bot) may only create a limited set of
        # conversational record types (see ``talker_types_only``).
        "create": "talker_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        # Talker creates notes / sessions / conversations with body
        # content synthesised from the voice turn — body writes stay on.
        "allow_body_writes": True,
    },
    # Instructor executes natural-language directives parked in the
    # ``alfred_instructions`` frontmatter field. Broader than janitor
    # (may create + move + write bodies; no frontmatter allowlist) but
    # narrower than talker — delete is denied because removing a record
    # is always an explicit operator task, never an instruction the
    # watcher should execute on its own. Part of the 6-commit
    # alfred_instructions watcher rollout.
    "instructor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": True,
        "edit": True,
        "move": True,
        "delete": False,
        # Instructor may write full record bodies — directives can ask
        # for drafting, restructuring, or inserting content into an
        # existing record body.
        "allow_body_writes": True,
    },
}


# Record types the talker scope is allowed to create. Kept as a module-level
# constant so the rule handler below and any future callers share one source
# of truth.
TALKER_CREATE_TYPES: set[str] = {
    "task", "note", "decision", "event",
    "session", "conversation", "assumption", "synthesis",
}


def check_scope(
    scope: str | None,
    operation: str,
    rel_path: str = "",
    record_type: str = "",
    frontmatter: dict | None = None,
    fields: list[str] | None = None,
    body_write: bool = False,
) -> None:
    """Check if an operation is allowed under the given scope.

    Args:
        scope: The agent scope (curator, janitor, distiller, surveyor) or None for unrestricted.
        operation: The vault operation (read, search, list, context, create, edit, move, delete).
        rel_path: Relative path of the target file (for path-based checks).
        record_type: Record type (for type-based checks on create).
        frontmatter: Optional frontmatter dict of the record being written
            (used by ``triage_tasks_only`` to enforce ``alfred_triage: true``).
            Defaults to None — rules that require it fail closed when absent.
        fields: Optional list of frontmatter field names being written
            (used by ``field_allowlist`` to constrain which fields a scope
            may mutate). Defaults to None — ``field_allowlist`` fails closed
            when absent so callers must always pass the fields being written.
        body_write: True if the caller is asking to write record body
            content (``--body-append`` / ``--body-stdin`` on ``vault edit``,
            or ``body`` on ``vault create``). Scopes that carry
            ``allow_body_writes: False`` reject the operation when this
            flag is set — closes the Q3 body-write loophole where the
            janitor could bypass the frontmatter allowlist by rewriting
            bodies. Defaults to False — callers that don't supply a body
            are trivially compliant.

    Raises:
        ScopeError: If the operation is denied.
    """
    if not scope:
        return  # No scope set → unrestricted (manual CLI usage)

    rules = SCOPE_RULES.get(scope)
    if rules is None:
        raise ScopeError(f"Unknown scope: '{scope}'")

    # Body-write gate — applied independently of the operation-level
    # permission because ``edit`` may succeed under an allowlist while
    # still needing to refuse body writes. Default True preserves
    # backwards compat for any scope (or explicit True) that hasn't
    # opted out. Checked before the operation permission so a caller
    # attempting a forbidden body write gets a body-specific error
    # rather than a misleading allowlist message.
    if body_write and rules.get("allow_body_writes", True) is False:
        raise ScopeError(
            f"Scope '{scope}' may not write record body content "
            f"(body_append / body_stdin). This scope is restricted "
            f"to frontmatter edits via its field allowlist."
        )

    permission = rules.get(operation)
    if permission is None:
        raise ScopeError(f"Unknown operation: '{operation}'")

    if permission is True:
        return

    if permission is False:
        raise ScopeError(
            f"Operation '{operation}' denied for scope '{scope}'"
        )

    # Special rules
    if permission == "inbox_only":
        norm = rel_path.replace("\\", "/")
        if not norm.startswith("inbox/"):
            raise ScopeError(
                f"Operation '{operation}' only allowed on inbox/ paths for scope '{scope}'. "
                f"Got: {rel_path}"
            )
        return

    if permission == "learn_types_only":
        if record_type not in LEARN_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create learn types "
                f"({', '.join(sorted(LEARN_TYPES))}). Got: '{record_type}'"
            )
        return

    if permission == "talker_types_only":
        if record_type not in TALKER_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create talker types "
                f"({', '.join(sorted(TALKER_CREATE_TYPES))}). Got: '{record_type}'"
            )
        return

    # Distiller may only edit distiller_signals / distiller_learnings fields.
    # Field-level enforcement is the caller's responsibility; this gate
    # permits the edit operation to proceed.
    if permission == "distiller_fields_only":
        return

    # Surveyor may only edit alfred_tags / relationships fields.
    # Field-level enforcement is the caller's responsibility; this gate
    # permits the edit operation to proceed.
    if permission == "tags_only":
        return

    # Generic field-allowlist rule. The allowlist lives at
    # ``rules[f"{operation}_fields_allowlist"]`` as an iterable of field
    # names. ``fields`` is the list of frontmatter field names the caller
    # intends to write; this rule fails closed when ``fields`` is None so
    # callers can't accidentally bypass the check by omitting the argument.
    if permission == "field_allowlist":
        allowlist_key = f"{operation}_fields_allowlist"
        allowlist_raw = rules.get(allowlist_key)
        if allowlist_raw is None:
            raise ScopeError(
                f"Scope '{scope}' configured with field_allowlist for "
                f"'{operation}' but no '{allowlist_key}' set in SCOPE_RULES."
            )
        allowlist = set(allowlist_raw)  # type: ignore[arg-type]
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only {operation} fields in the "
                f"allowlist ({', '.join(sorted(allowlist))}); caller did "
                f"not supply the field list."
            )
        rejected = [f for f in fields if f not in allowlist]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only {operation} fields in the "
                f"allowlist ({', '.join(sorted(allowlist))}). Rejected: "
                f"{', '.join(rejected)}"
            )
        return

    # Janitor may create task records only when they carry the
    # alfred_triage: true frontmatter flag. Fails closed when no
    # frontmatter is passed by the caller.
    if permission == "triage_tasks_only":
        if record_type != "task":
            raise ScopeError(
                f"Scope '{scope}' may only create 'task' records "
                f"(with alfred_triage: true). Got: '{record_type}'"
            )
        if not frontmatter or not frontmatter.get("alfred_triage"):
            raise ScopeError(
                f"Scope '{scope}' may only create task records with "
                f"'alfred_triage: true' set in frontmatter."
            )
        return
