"""Scope tests — pin the Salem-only contract for routine records.

Per dispatch + CLAUDE.md "Scope-first design for new vault capabilities":
the per-instance × per-type allowlist is THE principal contract. These
tests pin:

  - curator scope can create routine (canonical scope, universal-create).
  - talker scope can NOT create routine in Phase 1 (the talker allowlist
    explicitly omits it — Phase 2 will widen).
  - hypatia scope rejects routine create with the canonical error pointing
    at the propose path or the type-allowlist mismatch.
  - kalle scope rejects routine create.
  - body_insert_at + body_replace on routine are universally denied
    (any scope, any instance).

Why pin all four scopes: a future Phase 2 change that adds routine to
TALKER_CREATE_TYPES needs to update THIS file in lockstep (per
``feedback_intentionally_left_blank.md`` + the contract-pin sweep
discipline). A reviewer reading these tests should immediately see
which scopes are intentionally permissive vs intentionally hostile.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import (
    HYPATIA_CREATE_TYPES,
    KALLE_CREATE_TYPES,
    SCOPE_RULES,
    ScopeError,
    TALKER_CREATE_TYPES,
    check_scope,
)
from alfred.vault.schema import KNOWN_TYPES, TYPE_REGISTRY


# ---------------------------------------------------------------------------
# Schema / scope registration sanity
# ---------------------------------------------------------------------------


def test_routine_is_in_canonical_known_types() -> None:
    """routine appears in the canonical KNOWN_TYPES set."""
    assert "routine" in KNOWN_TYPES


def test_routine_type_definition_present_in_registry() -> None:
    """The TypeDefinition exists with the ratified shape."""
    type_def = TYPE_REGISTRY.get("routine")
    assert type_def is not None
    assert type_def.directory == "routine"
    assert type_def.statuses == frozenset({"active", "archived"})
    assert type_def.required_fields == ("name", "cadence", "items")


def test_routine_not_in_hypatia_create_types() -> None:
    """Hypatia must not be able to create routine records (Salem-only)."""
    assert "routine" not in HYPATIA_CREATE_TYPES


def test_routine_not_in_kalle_create_types() -> None:
    """KAL-LE must not be able to create routine records (Salem-only)."""
    assert "routine" not in KALLE_CREATE_TYPES


def test_routine_in_talker_create_types_phase_2() -> None:
    """Talker (Salem-Telegram) CAN create routine as of Phase 2B B2
    (commit `792e1cd`, 2026-05-30). Conversational routine creation
    grammar shipped + ratified by operator. The prior Phase 1 pin
    (routine NOT in TALKER_CREATE_TYPES) was invalidated by that
    ratification."""
    assert "routine" in TALKER_CREATE_TYPES


# ---------------------------------------------------------------------------
# Create gates per scope
# ---------------------------------------------------------------------------


def test_curator_can_create_routine() -> None:
    """Curator scope has ``create: True`` — universal-create allows
    routine since it's a canonical KNOWN_TYPES entry."""
    # Should not raise.
    check_scope(scope="curator", operation="create", record_type="routine")


def test_talker_can_create_routine_phase_2() -> None:
    """Talker scope permits routine create as of Phase 2B B2
    (commit `792e1cd`, 2026-05-30). Conversational routine creation
    via vault_create type=routine."""
    # Should not raise.
    check_scope(scope="talker", operation="create", record_type="routine")


def test_hypatia_cannot_create_routine() -> None:
    with pytest.raises(ScopeError) as exc_info:
        check_scope(scope="hypatia", operation="create", record_type="routine")
    msg = str(exc_info.value)
    # Either the canonical-type-guard message OR the type-allowlist
    # message — both are acceptable refusals. routine is NOT in
    # CANONICAL_RECORD_TYPES, so the path is the allowlist branch.
    assert "routine" in msg


def test_kalle_cannot_create_routine() -> None:
    with pytest.raises(ScopeError) as exc_info:
        check_scope(scope="kalle", operation="create", record_type="routine")
    assert "routine" in str(exc_info.value)


def test_distiller_cannot_create_routine() -> None:
    """Distiller is restricted to ``learn_types_only``."""
    with pytest.raises(ScopeError):
        check_scope(scope="distiller", operation="create", record_type="routine")


def test_surveyor_cannot_create_routine() -> None:
    """Surveyor has create: False."""
    with pytest.raises(ScopeError):
        check_scope(scope="surveyor", operation="create", record_type="routine")


# ---------------------------------------------------------------------------
# Body-mutation universal denials
# ---------------------------------------------------------------------------


def test_body_insert_at_on_routine_denied_for_all_scopes() -> None:
    """Body is auto-rendered from the template — insert_at is universally
    denied regardless of which scope tries."""
    # Scopes that have a body_insert_at allowlist of some sort.
    for scope in ("talker", "kalle", "hypatia", "janitor", "instructor"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                scope=scope,
                operation="body_insert_at",
                record_type="routine",
            )
        # The universal-deny message is distinctive.
        msg = str(exc_info.value).lower()
        assert "routine" in msg
        assert "universally denied" in msg or "auto-generated or atomic" in msg


def test_body_replace_on_routine_denied_for_all_scopes() -> None:
    for scope in ("talker", "kalle", "hypatia", "instructor"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                scope=scope,
                operation="body_replace",
                record_type="routine",
            )
        msg = str(exc_info.value).lower()
        assert "routine" in msg
        assert "universally denied" in msg or "auto-generated or atomic" in msg


def test_routine_in_body_mutate_denied_set() -> None:
    """Pin the membership directly — catches accidental removal."""
    from alfred.vault.scope import _BODY_MUTATE_DENIED_TYPES
    assert "routine" in _BODY_MUTATE_DENIED_TYPES
