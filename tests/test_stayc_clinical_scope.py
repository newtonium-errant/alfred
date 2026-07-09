"""Tests for the ``stayc_clinical`` scope + the ``clinical_note`` type (scribe P1-b).

The sovereign ambient-scribe scope-first matrix. ``clinical_note`` holds PHI;
it is the record the whole no-egress boundary (scribe P1-a) exists to protect,
so it is scoped, gated, and denied-from-egress at every layer.

Coverage (contract pins, mirroring test_vera_scope / test_rrts_intake_scope):
    * Matrix pins — STAYC_CLINICAL_CREATE_TYPES / ATTEST_TYPES / ATTEST_FIELDS,
      _NEVER_PUSH_TYPES. Widening any is a deliberate matrix change (update
      the pin in the same commit, pre-commit checklist #6).
    * Gate-1 AUTO-DERIVE — clinical_note in known_types("stayc_clinical") +
      KNOWN_TYPES_BY_SCOPE["stayc_clinical"], NOT in canonical / other scopes.
    * create gate — clinical_note only; non-clinical + empty type fail-closed.
    * attest edit gate — status/attested_by/attested_at allowed; other fields,
      missing fields, wrong/empty type denied; body writes REFUSED (content
      frozen — the security-critical pin).
    * ANTI-SPOLIATION — delete/move denied; body_insert_at/body_replace denied;
      the three denysets + their PRECEDENCE over a delete:True / "*" scope.
    * _NEVER_PUSH guard — is_never_push + the peer-propose refusal.
    * End-to-end vault_create draft + attest flip + frozen-body/anti-delete.
"""

from __future__ import annotations

import asyncio

import frontmatter
import pytest

from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create, vault_delete, vault_edit
from alfred.vault.scope import (
    CANONICAL_RECORD_TYPES,
    STAYC_CLINICAL_ATTEST_FIELDS,
    STAYC_CLINICAL_ATTEST_TYPES,
    STAYC_CLINICAL_CREATE_TYPES,
    ScopeError,
    _BODY_MUTATE_DENIED_TYPES,
    _DELETE_DENIED_TYPES,
    check_scope,
)


_SCOPE = "stayc_clinical"


# ---------------------------------------------------------------------------
# Matrix pins (the principal artifact)
# ---------------------------------------------------------------------------

def test_create_types_matrix_pin():
    assert STAYC_CLINICAL_CREATE_TYPES == {"clinical_note"}


def test_attest_types_and_fields_matrix_pin():
    assert STAYC_CLINICAL_ATTEST_TYPES == {"clinical_note"}
    assert STAYC_CLINICAL_ATTEST_FIELDS == {"attested_by", "attested_at", "status"}


def test_never_push_types_pin():
    assert schema._NEVER_PUSH_TYPES == frozenset({"clinical_note"})


def test_clinical_note_not_canonical():
    # NOT a canonical type => the create gate skips the propose-hint guard
    # (same check the coordinator did for `task`).
    assert "clinical_note" not in CANONICAL_RECORD_TYPES


# ---------------------------------------------------------------------------
# Gate 1 — AUTO-DERIVE (no KNOWN_TYPES_BY_SCOPE literal edit)
# ---------------------------------------------------------------------------

def test_gate1_autoderives_for_stayc_clinical():
    # known_types(scope) unions canonical with the scope's tagged types.
    assert "clinical_note" in schema.TYPE_REGISTRY.known_types("stayc_clinical")
    # The derived-view dict auto-populates from the registry comprehension.
    assert "clinical_note" in schema.KNOWN_TYPES_BY_SCOPE["stayc_clinical"]


def test_gate1_isolation_clinical_note_not_canonical_or_other_scope():
    # Per-instance isolation: NOT in the canonical set, NOT visible to any
    # other scope (Salem/VERA-ops/KAL-LE/Hypatia cannot even validate it).
    assert "clinical_note" not in schema.KNOWN_TYPES
    assert "clinical_note" not in schema.TYPE_REGISTRY.known_types()  # canonical
    for other in ("vera", "vera_ops", "talker", "kalle", "hypatia"):
        assert "clinical_note" not in schema.TYPE_REGISTRY.known_types(other)


def test_clinical_note_typedefinition_shape():
    d = schema.TYPE_REGISTRY.get("clinical_note")
    assert d is not None
    assert d.directory == "clinical_note"
    assert d.name_field == "title"
    assert d.required_fields == ("title",)
    assert d.statuses == frozenset({"ai_draft", "attested", "amended"})
    assert d.available_in_scopes == frozenset({"stayc_clinical"})
    assert d.is_leaf is True
    assert d.is_learn_type is False


# ---------------------------------------------------------------------------
# create gate — stayc_clinical_types_only
# ---------------------------------------------------------------------------

def test_create_clinical_note_allowed():
    check_scope(_SCOPE, "create", record_type="clinical_note", body_write=True)


@pytest.mark.parametrize("rtype", ["note", "ticket", "task", "person", "session"])
def test_create_non_clinical_denied(rtype):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type=rtype)


def test_create_empty_type_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="")


# ---------------------------------------------------------------------------
# read/search/list/context = True
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["read", "search", "list", "context"])
def test_read_family_allowed(op):
    check_scope(_SCOPE, op, record_type="clinical_note")  # no raise


# ---------------------------------------------------------------------------
# attest edit gate — stayc_clinical_attest_only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields",
    [["status"], ["attested_by"], ["attested_at"],
     ["status", "attested_by", "attested_at"]],
)
def test_attest_edit_allowed_fields(fields):
    check_scope(_SCOPE, "edit", record_type="clinical_note", fields=fields)


@pytest.mark.parametrize("field", ["title", "assessment", "ai_draft", "synthetic", "draft_original", "body"])
def test_attest_edit_non_allowlisted_field_denied(field):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="clinical_note", fields=[field])


def test_attest_edit_missing_fields_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="clinical_note", fields=None)


def test_attest_edit_empty_type_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="", fields=["status"])


def test_attest_edit_wrong_type_denied():
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="note", fields=["status"])


def test_attest_edit_refuses_body_write_SECURITY_PIN():
    # THE security-critical pin. A pure body_append arrives at check_scope
    # with fields=[] (EMPTY list, not None) + body_write=True — the exact
    # shape vault_edit builds. The field-allowlist alone would let it pass
    # (empty rejected list); the explicit body_write refusal in the attest
    # gate is what freezes clinical content after draft.
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="clinical_note",
                    fields=[], body_write=True)


def test_attest_edit_status_only_no_body_passes():
    # The real attest-flip shape: set status (+ attest metadata), no body.
    check_scope(_SCOPE, "edit", record_type="clinical_note",
                fields=["status", "attested_by", "attested_at"], body_write=False)


# ---------------------------------------------------------------------------
# ANTI-SPOLIATION — move/delete/body-mutation denied + denyset membership
# ---------------------------------------------------------------------------

def test_clinical_note_in_denysets():
    assert "clinical_note" in _DELETE_DENIED_TYPES
    assert "clinical_note" in _BODY_MUTATE_DENIED_TYPES


@pytest.mark.parametrize("op", ["move", "delete"])
def test_move_delete_denied(op):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, op, record_type="clinical_note")


@pytest.mark.parametrize("op", ["body_insert_at", "body_replace"])
def test_body_mutation_denied(op):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, op, record_type="clinical_note")


def test_delete_deny_precedence_over_delete_true_scope():
    # janitor carries delete:True, but the universal _DELETE_DENIED_TYPES
    # takes precedence — a clinical note can never be agent-deleted.
    with pytest.raises(ScopeError):
        check_scope("janitor", "delete", record_type="clinical_note")


def test_body_mutate_deny_precedence_over_wildcard_scope():
    # instructor carries allow_body_replace={"*": True}, but the universal
    # _BODY_MUTATE_DENIED_TYPES takes precedence.
    with pytest.raises(ScopeError):
        check_scope("instructor", "body_replace", record_type="clinical_note")


# ---------------------------------------------------------------------------
# _NEVER_PUSH guard
# ---------------------------------------------------------------------------

def test_is_never_push():
    assert schema.is_never_push("clinical_note") is True
    assert schema.is_never_push("ticket") is False
    assert schema.is_never_push("note") is False


def test_peer_propose_refuses_never_push_type():
    from alfred.transport.client import peer_propose_canonical_record
    with pytest.raises(ValueError):
        asyncio.run(peer_propose_canonical_record(
            "salem", "clinical_note", "Encounter note",
            self_name="vera-clinical",
        ))


# ---------------------------------------------------------------------------
# End-to-end — draft, attest flip, frozen body, anti-delete
# ---------------------------------------------------------------------------

def test_e2e_draft_then_attest_then_frozen(tmp_path):
    # 1. Create the AI draft (body IS the payload).
    result = vault_create(
        tmp_path, "clinical_note", "Encounter 2026-07-09 chest pain",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft"},
        body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope=_SCOPE,
    )
    rel_path = result["path"]
    assert (tmp_path / rel_path).exists()

    # 2. Attest flip — frontmatter only (status + attest metadata).
    vault_edit(
        tmp_path, rel_path,
        set_fields={
            "status": "attested",
            "attested_by": "Dr Synthetic",
            "attested_at": "2026-07-09T12:00:00Z",
        },
        scope=_SCOPE,
    )
    post = frontmatter.load(str(tmp_path / rel_path))
    assert post["status"] == "attested"
    assert post["attested_by"] == "Dr Synthetic"

    # 3. Body is FROZEN — a body_append is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_append="\nSNEAKY addendum\n",
            scope=_SCOPE,
        )

    # 4. Anti-spoliation — delete is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_delete(tmp_path, rel_path, scope=_SCOPE)


def test_e2e_non_clinical_create_denied(tmp_path):
    with pytest.raises((ScopeError, VaultError)):
        vault_create(
            tmp_path, "note", "A note", set_fields={}, scope=_SCOPE,
        )
