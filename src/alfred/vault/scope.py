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
        "create": False,
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
        "edit": False,
        "move": False,
        "delete": False,
    },
}


def check_scope(
    scope: str | None,
    operation: str,
    rel_path: str = "",
    record_type: str = "",
) -> None:
    """Check if an operation is allowed under the given scope.

    Args:
        scope: The agent scope (curator, janitor, distiller) or None for unrestricted.
        operation: The vault operation (read, search, list, context, create, edit, move, delete).
        rel_path: Relative path of the target file (for path-based checks).
        record_type: Record type (for type-based checks on create).

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
