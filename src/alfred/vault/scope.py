"""Per-agent scope enforcement for vault operations."""

from __future__ import annotations

from .schema import LEARN_TYPES


class ScopeError(Exception):
    """Raised when an operation is denied by scope policy."""


# Operation → {scope: checker_function}
# Checkers receive (operation, rel_path, record_type) and raise ScopeError if denied.

SCOPE_RULES: dict[str, dict[str, bool | str]] = {
    "curator": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": True,
        "edit": True,
        "move": "inbox_only",
        "delete": False,
    },
    "janitor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Janitor may create task records only when they carry the
        # alfred_triage: true frontmatter flag (Layer 3 triage queue).
        "create": "triage_tasks_only",
        "edit": True,
        "move": False,
        "delete": True,
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

    Raises:
        ScopeError: If the operation is denied.
    """
    if not scope:
        return  # No scope set → unrestricted (manual CLI usage)

    rules = SCOPE_RULES.get(scope)
    if rules is None:
        raise ScopeError(f"Unknown scope: '{scope}'")

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
