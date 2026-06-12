"""Ticket pipeline c2 — KAL-LE ticket scope + vera_forwarder gate.

Ratified VERA→KAL-LE→GitHub ticket pipeline (2026-06-11): VERA pushes
tickets over the peer protocol; KAL-LE (the backlog keeper + single
GitHub-credential holder) records them in aftermath-lab's ``ticket/``
queue; the VERA-side forwarder daemon writes the GitHub issue
link-back onto the originating ticket.

Coverage:
    * ``ticket`` under scope "kalle" passes BOTH gates (gate 1
      ``_validate_type`` via ``available_in_scopes``; gate 2
      ``check_scope`` via ``KALLE_CREATE_TYPES``) — end-to-end
      ``vault_create``.
    * Per-instance isolation preserved: ticket still REJECTED under
      Salem's "talker" scope and "hypatia" (gate 1).
    * ``vera_forwarder`` scope — edit of exactly the four link-back
      fields on ticket records; everything else fails loud.
    * ``KNOWN_TYPES_BY_SCOPE`` auto-population pins — a re-derivation
      from the registry that catches a literal-reversion (the VERA P1
      trap class: comment says auto-populated, dict is a hardcoded
      literal).
"""

from __future__ import annotations

import pytest

from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create, vault_edit, vault_list
from alfred.vault.scope import (
    KALLE_CREATE_TYPES,
    VERA_FORWARDER_EDIT_FIELDS,
    VERA_FORWARDER_EDIT_TYPES,
    ScopeError,
    check_scope,
)


_TICKET_FIELDS = {
    "ticket_type": "bug",
    "reporter": "vera",
    "area": "transport-admin-portal",
}


# ---------------------------------------------------------------------------
# KAL-LE — ticket backlog keeper (gate 1 + gate 2)
# ---------------------------------------------------------------------------


def test_kalle_create_types_includes_ticket():
    # The companion exact-shape pin lives in test_kalle_scope.py
    # (test_kalle_create_types_shape) — updated in the same commit per
    # pre-commit checklist #6.
    assert "ticket" in KALLE_CREATE_TYPES


def test_ticket_visible_under_kalle_scope():
    # Gate 1 surface: the kalle union admits ticket.
    assert "ticket" in schema.TYPE_REGISTRY.known_types("kalle")
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["kalle"]


def test_check_scope_kalle_create_ticket_passes():
    # Gate 2: the kalle create allowlist admits ticket.
    check_scope("kalle", "create", record_type="ticket")


def test_vault_create_ticket_under_kalle_succeeds(tmp_path) -> None:
    """End-to-end two-gate pin (test_hypatia_scope.py template): the
    intake handler's create path passes ``_validate_type`` AND
    ``check_scope`` and writes the file."""
    result = vault_create(
        tmp_path,
        "ticket",
        "Portal 500 on login",
        set_fields=dict(_TICKET_FIELDS),
        scope="kalle",
    )
    assert result["path"] == "ticket/Portal 500 on login.md"
    assert (tmp_path / result["path"]).exists()
    content = (tmp_path / result["path"]).read_text(encoding="utf-8")
    assert "type: ticket" in content
    assert "title: Portal 500 on login" in content


def test_vault_create_ticket_under_talker_scope_fails(tmp_path) -> None:
    """Per-instance isolation: Salem's talker scope can't create
    tickets — gate 1 rejects (ticket not in the talker union)."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "ticket", "Nope",
            set_fields=dict(_TICKET_FIELDS), scope="talker",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "talker" in str(exc_info.value)


def test_vault_create_ticket_under_hypatia_scope_fails(tmp_path) -> None:
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "ticket", "Nope",
            set_fields=dict(_TICKET_FIELDS), scope="hypatia",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "hypatia" in str(exc_info.value)


def test_vault_create_ticket_no_scope_fails(tmp_path) -> None:
    # Canonical (scope-less) callers still can't create tickets.
    with pytest.raises(VaultError, match="Unknown type"):
        vault_create(
            tmp_path, "ticket", "Nope", set_fields=dict(_TICKET_FIELDS),
        )


# ---------------------------------------------------------------------------
# vera_forwarder — link-back-only write authority
# ---------------------------------------------------------------------------


def test_forwarder_field_allowlist_contract_pin():
    """CONTRACT PIN: exactly the four link-back fields, ticket only.

    Widening either set is a deliberate scope change — update this pin
    in the same commit (pre-commit checklist #6).
    """
    assert VERA_FORWARDER_EDIT_TYPES == {"ticket"}
    assert VERA_FORWARDER_EDIT_FIELDS == {
        "ticket_uid", "github_issue", "github_url", "forwarded_at",
    }


def test_forwarder_allows_read_search_list_context():
    for op in ("read", "search", "list", "context"):
        check_scope("vera_forwarder", op)


def test_forwarder_edit_all_four_link_back_fields_allowed():
    check_scope(
        "vera_forwarder", "edit", record_type="ticket",
        fields=["ticket_uid", "github_issue", "github_url", "forwarded_at"],
    )


def test_forwarder_edit_subset_allowed():
    check_scope(
        "vera_forwarder", "edit", record_type="ticket",
        fields=["github_issue", "github_url"],
    )


@pytest.mark.parametrize("bad_field", ["status", "title", "reporter", "tags"])
def test_forwarder_edit_other_fields_denied(bad_field: str):
    with pytest.raises(ScopeError, match=bad_field):
        check_scope(
            "vera_forwarder", "edit", record_type="ticket",
            fields=["github_issue", bad_field],
        )


def test_forwarder_edit_fails_closed_without_field_list():
    with pytest.raises(ScopeError, match="did not supply"):
        check_scope("vera_forwarder", "edit", record_type="ticket")


def test_forwarder_edit_fails_closed_without_record_type():
    """REGRESSION (2026-06-12 review WARN-3): an empty record_type used
    to SKIP the type restriction (fail-open) — the forwarder scope
    could write its 4 fields onto ANY record type when the caller
    omitted the type. Now fails closed."""
    with pytest.raises(ScopeError, match="record type is unavailable"):
        check_scope(
            "vera_forwarder", "edit", record_type="",
            fields=["github_issue"],
        )


def test_forwarder_edit_non_ticket_type_denied():
    with pytest.raises(ScopeError, match="ticket"):
        check_scope(
            "vera_forwarder", "edit", record_type="note",
            fields=["github_issue"],
        )


def test_forwarder_create_move_delete_denied():
    for op in ("create", "move", "delete"):
        with pytest.raises(ScopeError):
            check_scope("vera_forwarder", op, record_type="ticket")


def test_forwarder_body_writes_denied():
    # The body-write gate fires before the edit allowlist — link-back
    # is frontmatter-only; the Claude-Code brief body is interview-owned.
    with pytest.raises(ScopeError, match="body"):
        check_scope(
            "vera_forwarder", "edit", record_type="ticket",
            fields=["github_issue"], body_write=True,
        )


def test_forwarder_body_mutation_tools_denied():
    with pytest.raises(ScopeError):
        check_scope("vera_forwarder", "body_insert_at", record_type="ticket")
    with pytest.raises(ScopeError):
        check_scope("vera_forwarder", "body_replace", record_type="ticket")


def test_forwarder_end_to_end_link_back_edit(tmp_path) -> None:
    """vault_edit under vera_forwarder: link-back fields land; a
    status flip is refused at the scope gate."""
    result = vault_create(
        tmp_path, "ticket", "Portal 500 on login",
        set_fields=dict(_TICKET_FIELDS), scope="kalle",
    )
    rel_path = result["path"]

    edited = vault_edit(
        tmp_path, rel_path,
        set_fields={
            "ticket_uid": "t-20260611-0001",
            "github_issue": 42,
            "github_url": "https://github.com/newtonium-errant/transport-admin-portal/issues/42",
            "forwarded_at": "2026-06-11T18:00:00+00:00",
        },
        scope="vera_forwarder",
    )
    assert set(edited["fields_changed"]) >= {"ticket_uid", "github_issue"}
    content = (tmp_path / rel_path).read_text(encoding="utf-8")
    assert "github_issue: 42" in content

    with pytest.raises(ScopeError):
        vault_edit(
            tmp_path, rel_path,
            set_fields={"status": "closed"},
            scope="vera_forwarder",
        )


def test_forwarder_end_to_end_non_ticket_record_denied(tmp_path) -> None:
    """REGRESSION (2026-06-12 review WARN-3): vault_edit under
    vera_forwarder against a NON-ticket record must be refused at the
    type restriction — before the fix, vault_edit never passed the
    parsed record_type to the edit gate, so this write SUCCEEDED.
    (Positive control: test_forwarder_end_to_end_link_back_edit pins
    the same fields landing on a ticket record.)"""
    note_dir = tmp_path / "note"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "Some note.md").write_text(
        "---\ntype: note\ntitle: Some note\n---\n\nA note body.\n",
        encoding="utf-8",
    )
    with pytest.raises(ScopeError, match="ticket"):
        vault_edit(
            tmp_path, "note/Some note.md",
            set_fields={"github_issue": 99},
            scope="vera_forwarder",
        )
    # The write never landed.
    content = (note_dir / "Some note.md").read_text(encoding="utf-8")
    assert "github_issue" not in content


def test_forwarder_vault_list_tickets_passes_gate_1(tmp_path) -> None:
    """Gate 1 fires on LIST too (``vault_list`` calls
    ``_validate_type``) — the vera_forwarder tag on the ticket
    TypeDefinition is what lets the forwarder enumerate the queue."""
    vault_create(
        tmp_path, "ticket", "Portal 500 on login",
        set_fields=dict(_TICKET_FIELDS), scope="kalle",
    )
    records = vault_list(tmp_path, "ticket", scope="vera_forwarder")
    assert len(records) == 1
    assert records[0]["name"] == "Portal 500 on login"


# ---------------------------------------------------------------------------
# KNOWN_TYPES_BY_SCOPE auto-population (literal-reversion catch)
# ---------------------------------------------------------------------------


def test_known_types_by_scope_keys_derive_from_registry():
    """Re-derive the extension-scope key set from the registry and
    assert the exported dict matches. Catches the VERA P1 trap class:
    a comment claiming auto-population over a hardcoded
    ``{"kalle", "hypatia"}`` literal would FAIL here because the
    registry tags vera / vera_ops / vera_forwarder too."""
    expected_scopes = {
        s
        for d in schema.TYPE_REGISTRY
        for s in d.available_in_scopes
        if s != schema.SCOPE_CANONICAL
    }
    assert set(schema.KNOWN_TYPES_BY_SCOPE.keys()) == expected_scopes
    # The pipeline scopes specifically must be present.
    assert {"kalle", "vera", "vera_ops", "vera_forwarder"} <= expected_scopes


def test_ticket_in_scope_unions_via_registry():
    """Each scope's union value derives from the registry — ticket
    appears under every scope the TypeDefinition tags, and ONLY those."""
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["kalle"]
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["vera"]
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["vera_ops"]
    assert "ticket" in schema.KNOWN_TYPES_BY_SCOPE["vera_forwarder"]
    assert "ticket" not in schema.KNOWN_TYPES_BY_SCOPE["hypatia"]
    assert "ticket" not in schema.KNOWN_TYPES


def test_forwarder_union_is_canonical_plus_ticket_only():
    """vera_forwarder unlocks ticket and nothing else beyond canonical
    — the read-surface widening is exactly one type wide."""
    forwarder_extension = schema.TYPE_REGISTRY.types_in_scope("vera_forwarder")
    assert forwarder_extension == frozenset({"ticket"})
