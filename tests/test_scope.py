"""Smoke tests for ``alfred.vault.scope.check_scope``.

Bootstrap-scope: one allow + one deny per rule we care about. Broader
coverage (every scope, every operation, every edge case) lands as we add
behaviour, not here.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import ScopeError, check_scope


# ---- learn_types_only (distiller create) ------------------------------------


def test_distiller_create_allows_learn_type():
    # ``decision`` is in LEARN_TYPES — distiller may create it.
    check_scope("distiller", "create", record_type="decision")


def test_distiller_create_denies_non_learn_type():
    # ``task`` is an entity type, not a learn type — distiller is blocked.
    with pytest.raises(ScopeError, match="learn types"):
        check_scope("distiller", "create", record_type="task")


# ---- talker_types_only (talker create) --------------------------------------


def test_talker_create_allows_whitelisted_type():
    check_scope("talker", "create", record_type="task")


def test_talker_create_denies_non_whitelisted_type():
    # ``project`` is intentionally NOT in TALKER_CREATE_TYPES.
    with pytest.raises(ScopeError, match="talker types"):
        check_scope("talker", "create", record_type="project")


# ---- field_allowlist (janitor edit, the new Option E rule) ------------------


def test_janitor_edit_allows_fields_in_allowlist():
    # ``status`` and ``related`` are both in the janitor edit allowlist.
    check_scope(
        "janitor",
        "edit",
        rel_path="task/Some Task.md",
        fields=["status", "related"],
    )


def test_janitor_edit_denies_field_outside_allowlist():
    # ``description`` is NOT in the Stage 1/2 janitor allowlist (it lives in
    # the separate ``janitor_enrich`` scope).
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "janitor",
            "edit",
            rel_path="person/Someone.md",
            fields=["description"],
        )


def test_janitor_edit_fails_closed_when_fields_omitted():
    # field_allowlist must fail closed if the caller forgets to pass fields —
    # otherwise the check is trivially bypassable.
    with pytest.raises(ScopeError, match="did not supply the field list"):
        check_scope("janitor", "edit", rel_path="task/X.md")
