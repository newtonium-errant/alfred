"""Tests for the ``rrts_intake`` scope + the held-ticket schema fields.

The VOUCHED RRTS bug-report → VERA lane (2026-06-29). RRTS staff converse
with VERA through the web bug widget → a host-side relay (``rrts_relay``
peer) → VERA's ``/chat/*`` endpoints. Every such request resolves to the
FIXED ``rrts_intake`` scope: create HELD tickets only; edit / move / delete
denied; fail-closed on empty/other type.

Coverage:
    * Create-type matrix pin (RRTS_INTAKE_CREATE_TYPES == {ticket}).
    * ``rrts_intake`` scope — create ticket only; non-ticket denied;
      empty-type fail-closed; edit / move / delete denied; body mutation
      denied; read/search/list/context allowed.
    * Two-gate agreement — ``ticket`` tagged ``rrts_intake`` in
      available_in_scopes (gate 1) AND KNOWN_TYPES_BY_SCOPE auto-derives.
    * End-to-end ``vault_create`` of a held ticket under the scope.
    * The held-state fields (``origin`` / ``de_phi_status``) are
      schema-tolerant optional frontmatter (a ticket WITHOUT them validates
      exactly as before).
"""

from __future__ import annotations

import frontmatter
import pytest

from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create, vault_edit
from alfred.vault.scope import (
    RRTS_INTAKE_CREATE_TYPES,
    RRTS_INTAKE_ROLE,
    RRTS_INTAKE_SCOPE,
    ScopeError,
    check_scope,
)


# Minimal valid ticket frontmatter for end-to-end vault_create (reporter is
# stamped by the create path in production; the schema requires it, so the
# test supplies it directly when calling vault_create).
_TICKET_FIELDS = {
    "ticket_type": "bug",
    "reporter": "Ben",
    "area": "Dashboard",
}


# ---------------------------------------------------------------------------
# Constants — matrix pin
# ---------------------------------------------------------------------------


def test_rrts_intake_create_types_matrix_pin():
    """CONTRACT PIN: the vouched intake scope creates ONLY tickets.
    Widening this set is a deliberate matrix change — update the pin in the
    same commit (pre-commit checklist #6)."""
    assert RRTS_INTAKE_CREATE_TYPES == {"ticket"}


def test_rrts_intake_scope_and_role_constants():
    # The role is an auth-path sentinel; kept identical to the scope string.
    assert RRTS_INTAKE_SCOPE == "rrts_intake"
    assert RRTS_INTAKE_ROLE == "rrts_intake"


def test_rrts_intake_registered_in_scope_rules():
    from alfred.vault.scope import SCOPE_RULES

    assert RRTS_INTAKE_SCOPE in SCOPE_RULES


# ---------------------------------------------------------------------------
# Scope: rrts_intake
# ---------------------------------------------------------------------------


def test_rrts_intake_allows_read_search_list_context():
    check_scope("rrts_intake", "read")
    check_scope("rrts_intake", "search")
    check_scope("rrts_intake", "list")
    check_scope("rrts_intake", "context")


def test_rrts_intake_create_allows_ticket():
    check_scope("rrts_intake", "create", record_type="ticket")


def test_rrts_intake_create_with_body_allowed():
    # The Claude-Code handoff brief body IS the payload.
    check_scope(
        "rrts_intake", "create", record_type="ticket", body_write=True,
    )


def test_rrts_intake_create_denies_non_ticket_types():
    for t in ("note", "task", "decision", "project", "person", "event"):
        with pytest.raises(ScopeError) as exc:
            check_scope("rrts_intake", "create", record_type=t)
        assert "rrts-intake types" in str(exc.value).lower()


def test_rrts_intake_create_empty_type_fails_closed():
    # An empty record_type is a caller bug, not a licence to create any type.
    with pytest.raises(ScopeError) as exc:
        check_scope("rrts_intake", "create", record_type="")
    assert "failing closed" in str(exc.value).lower()


def test_rrts_intake_denies_edit_move_delete():
    for op in ("edit", "move", "delete"):
        with pytest.raises(ScopeError):
            check_scope("rrts_intake", op, record_type="ticket")


def test_rrts_intake_denies_body_mutation_tools():
    with pytest.raises(ScopeError):
        check_scope("rrts_intake", "body_insert_at", record_type="ticket")
    with pytest.raises(ScopeError):
        check_scope("rrts_intake", "body_replace", record_type="ticket")


# ---------------------------------------------------------------------------
# Two-gate contract — ticket visible under rrts_intake (gate 1)
# ---------------------------------------------------------------------------


def test_ticket_tagged_for_rrts_intake_scope():
    """Gate 1: the ticket TypeDefinition's available_in_scopes admits
    rrts_intake (so _validate_type passes on create)."""
    defn = schema.TYPE_REGISTRY.get("ticket")
    assert defn is not None
    assert "rrts_intake" in defn.available_in_scopes


def test_ticket_visible_under_rrts_intake_known_types():
    assert "ticket" in schema.TYPE_REGISTRY.known_types("rrts_intake")


def test_known_types_by_scope_auto_derives_for_rrts_intake():
    """Literal-reversion catch (the VERA-P1 trap class): re-derive the
    rrts_intake scope's valid set from the registry and assert the exported
    dict matches. A reverted hardcoded literal would fail here."""
    expected = set(schema.TYPE_REGISTRY.known_types("rrts_intake"))
    assert schema.KNOWN_TYPES_BY_SCOPE["rrts_intake"] == expected
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["rrts_intake"]


# ---------------------------------------------------------------------------
# End-to-end two-gate — vault_create a held ticket
# ---------------------------------------------------------------------------


def test_e2e_create_held_ticket_under_rrts_intake(tmp_path):
    """Full two-gate coverage: create a ticket under rrts_intake, then
    confirm edit is denied (intake is create-once / held)."""
    result = vault_create(
        tmp_path, "ticket", "Portal 500 on staff login",
        set_fields={
            **_TICKET_FIELDS,
            "origin": "rrts",
            "de_phi_status": "pending",
            "source": "web",
        },
        scope=RRTS_INTAKE_SCOPE,
    )
    rel_path = result["path"]
    assert (tmp_path / rel_path).exists()

    # The held-state fields landed (free-text frontmatter, not stripped).
    post = frontmatter.load(str(tmp_path / rel_path))
    assert post.metadata["origin"] == "rrts"
    assert post.metadata["de_phi_status"] == "pending"
    assert post.metadata["source"] == "web"

    # Edit denied — the reporter cannot mutate a held ticket.
    with pytest.raises((VaultError, ScopeError)):
        vault_edit(
            tmp_path, rel_path,
            set_fields={"status": "in_progress"},
            scope=RRTS_INTAKE_SCOPE,
        )


def test_e2e_non_ticket_create_denied_under_rrts_intake(tmp_path):
    with pytest.raises((VaultError, ScopeError)):
        vault_create(
            tmp_path, "note", "A note",
            set_fields={}, scope=RRTS_INTAKE_SCOPE,
        )


# ---------------------------------------------------------------------------
# Schema-tolerance — held-state fields are optional
# ---------------------------------------------------------------------------


def test_ticket_without_held_fields_still_valid(tmp_path):
    """A ticket WITHOUT origin/de_phi_status validates exactly as before —
    the new fields are optional free-text frontmatter (schema-tolerance)."""
    result = vault_create(
        tmp_path, "ticket", "No-held-fields ticket",
        set_fields=dict(_TICKET_FIELDS), scope=RRTS_INTAKE_SCOPE,
    )
    post = frontmatter.load(str(tmp_path / result["path"]))
    assert "origin" not in post.metadata
    assert "de_phi_status" not in post.metadata


def test_held_fields_not_in_required_fields():
    # Neither field is a creation gate — purely optional provenance.
    req = schema.REQUIRED_FIELDS_BY_TYPE["ticket"]
    assert "origin" not in req
    assert "de_phi_status" not in req
