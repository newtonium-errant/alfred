"""Tests for the VERA MVP scope + ticket-type schema additions (2026-06-09).

VERA (project_vera_ops_assistant.md) is the first multi-user instance:
owner (Andrew) → ``vera`` scope, ops (Ben) → ``vera_ops`` scope. The two
scopes + the ``ticket`` record type are the P0 scope-first contract.

Coverage:
    * ``vera_ops`` scope — ticket create OK; non-ticket create denied;
      move + delete denied (Decision B); edit OK (resolve/close).
    * ``vera`` scope — ticket + note create OK; canonical type denied;
      move + delete denied; body insert/replace allowed for ticket+note.
    * ``ticket`` TypeDefinition — registered with the right statuses,
      required fields, directory, name_field, leaf-ness, and NOT
      canonical (rejected under talker/hypatia; the kalle + the
      vera_forwarder tags were added 2026-06-11 for the ticket
      pipeline — positive pins live in test_ticket_pipeline_scope.py).
    * Derived globals (STATUS_BY_TYPE / TYPE_DIRECTORY / REQUIRED_FIELDS_
      BY_TYPE / NAME_FIELD_BY_TYPE / LEAF_TYPES) auto-populate.
    * ``screenshots`` in LIST_FIELDS.
"""

from __future__ import annotations

import pytest

from alfred.vault import schema
from alfred.vault.scope import (
    VERA_CREATE_TYPES,
    VERA_OPS_CREATE_TYPES,
    ScopeError,
    check_scope,
)


# ---------------------------------------------------------------------------
# Create-type constants
# ---------------------------------------------------------------------------


def test_vera_ops_create_types_is_ticket_only():
    assert VERA_OPS_CREATE_TYPES == {"ticket"}


def test_vera_owner_create_types_is_ticket_and_note():
    assert VERA_CREATE_TYPES == {"ticket", "note"}


# ---------------------------------------------------------------------------
# Scope: vera_ops (Ben)
# ---------------------------------------------------------------------------


def test_vera_ops_allows_read_search_list_context():
    check_scope("vera_ops", "read")
    check_scope("vera_ops", "search")
    check_scope("vera_ops", "list")
    check_scope("vera_ops", "context")


def test_vera_ops_create_allows_ticket():
    check_scope("vera_ops", "create", record_type="ticket")


def test_vera_ops_create_denies_note():
    # note is owner-only (vera scope); ops is ticket-only.
    with pytest.raises(ScopeError) as exc_info:
        check_scope("vera_ops", "create", record_type="note")
    assert "vera-ops types" in str(exc_info.value).lower()


def test_vera_ops_create_denies_arbitrary_type():
    for t in ("task", "person", "event", "pattern", "decision"):
        with pytest.raises(ScopeError):
            check_scope("vera_ops", "create", record_type=t)


def test_vera_ops_edit_permitted_no_field_check():
    # resolve/close = status edit; edit: True (no field allowlist).
    check_scope("vera_ops", "edit")


def test_vera_ops_denies_move_and_delete():
    # Decision B (ratified): both False. Resolution is a status flip.
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "move")
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "delete")


def test_vera_ops_body_writes_permitted():
    # VERA writes the Claude-Code brief body at ticket-create time.
    check_scope("vera_ops", "create", record_type="ticket", body_write=True)


def test_vera_ops_body_insert_and_replace_denied():
    # Ops doesn't patch / rewrite briefs in MVP (owner-only).
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "body_insert_at", record_type="ticket")
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "body_replace", record_type="ticket")


# ---------------------------------------------------------------------------
# Scope: vera (Andrew / owner)
# ---------------------------------------------------------------------------


def test_vera_allows_read_search_list_context():
    check_scope("vera", "read")
    check_scope("vera", "search")
    check_scope("vera", "list")
    check_scope("vera", "context")


def test_vera_create_allows_ticket_and_note():
    check_scope("vera", "create", record_type="ticket")
    check_scope("vera", "create", record_type="note")


def test_vera_create_denies_canonical_and_arbitrary_types():
    for t in ("person", "org", "event", "task", "pattern"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope("vera", "create", record_type=t)
        assert "vera types" in str(exc_info.value).lower()


def test_vera_denies_move_and_delete():
    with pytest.raises(ScopeError):
        check_scope("vera", "move")
    with pytest.raises(ScopeError):
        check_scope("vera", "delete")


def test_vera_body_insert_and_replace_allowed_for_ticket_and_note():
    # Owner may patch + rewrite ticket + note bodies.
    check_scope("vera", "body_insert_at", record_type="ticket")
    check_scope("vera", "body_insert_at", record_type="note")
    check_scope("vera", "body_replace", record_type="ticket")
    check_scope("vera", "body_replace", record_type="note")


def test_vera_body_replace_denied_for_unlisted_type():
    # task isn't in the vera body-replace allowlist → denied.
    with pytest.raises(ScopeError):
        check_scope("vera", "body_replace", record_type="task")


# ---------------------------------------------------------------------------
# ticket TypeDefinition + derived globals
# ---------------------------------------------------------------------------


def test_ticket_type_registered():
    assert "ticket" in schema.TYPE_REGISTRY


def test_ticket_statuses():
    assert schema.STATUS_BY_TYPE["ticket"] == {
        "open", "in_progress", "resolved", "closed", "wont_fix",
    }


def test_ticket_directory():
    assert schema.TYPE_DIRECTORY["ticket"] == "ticket"


def test_ticket_required_fields():
    assert schema.REQUIRED_FIELDS_BY_TYPE["ticket"] == [
        "title", "ticket_type", "reporter", "area",
    ]


def test_ticket_name_field_is_title():
    assert schema.NAME_FIELD_BY_TYPE["ticket"] == "title"


def test_ticket_is_leaf():
    assert "ticket" in schema.LEAF_TYPES


def test_screenshots_in_list_fields():
    assert "screenshots" in schema.LIST_FIELDS


# ---------------------------------------------------------------------------
# Two-gate contract — ticket visible ONLY under VERA scopes
# ---------------------------------------------------------------------------


def test_ticket_visible_under_vera_scopes():
    assert "ticket" in schema.TYPE_REGISTRY.known_types("vera")
    assert "ticket" in schema.TYPE_REGISTRY.known_types("vera_ops")


def test_ticket_not_in_canonical_known_types():
    # Salem / Hypatia must not see the ticket type — it's not
    # canonical, so it stays out of the default + their scope sets.
    # KAL-LE moved OUT of this rejection list 2026-06-11 (pipeline c2):
    # KAL-LE is the ticket backlog keeper of the ratified
    # VERA→KAL-LE→GitHub pipeline — see
    # tests/test_ticket_pipeline_scope.py for its positive pins.
    assert "ticket" not in schema.TYPE_REGISTRY.known_types()
    assert "ticket" not in schema.TYPE_REGISTRY.known_types("talker")
    assert "ticket" not in schema.TYPE_REGISTRY.known_types("hypatia")


def test_ticket_not_canonical_record_type():
    # No propose-flow guard fires on ticket — VERA owns its own tickets.
    from alfred.vault.scope import CANONICAL_RECORD_TYPES

    assert "ticket" not in CANONICAL_RECORD_TYPES
