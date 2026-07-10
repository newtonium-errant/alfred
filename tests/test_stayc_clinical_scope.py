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


_SCOPE = "stayc_clinical"                 # the pipeline/agent scope
_ATTEST_SCOPE = "stayc_clinical_attest"   # the privileged orchestrator scope (P2-a)


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
    # P2-a (#41) tagged the privileged attest scope for gate-1 list-safety.
    assert d.available_in_scopes == frozenset({"stayc_clinical", "stayc_clinical_attest"})
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
# #41 STRUCTURAL ATTESTATION (scribe P2-a) — the agent scope DENIES the triad;
# the privileged stayc_clinical_attest scope allows exactly the triad.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fields",
    [["status"], ["attested_by"], ["attested_at"], ["title"], []],
)
def test_agent_scope_denies_all_clinical_note_edits(fields):
    # THE #41 raw-flip pin (mutation: allow raw triad edit => fails). The
    # pipeline/agent scope may NOT edit clinical_notes at all — the attest
    # triad is orchestrator-only. This is the structural half of #41.
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "edit", record_type="clinical_note", fields=fields)


# The privileged attest scope (reachable ONLY via scribe.attest) allows the
# triad — the P1-b field-allowlist logic now lives here.

@pytest.mark.parametrize(
    "fields",
    [["status"], ["attested_by"], ["attested_at"],
     ["status", "attested_by", "attested_at"]],
)
def test_attest_scope_allowed_triad(fields):
    check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=fields)


@pytest.mark.parametrize("field", ["title", "assessment", "ai_draft", "synthetic", "draft_original", "body"])
def test_attest_scope_non_triad_field_denied(field):
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=[field])


def test_attest_scope_missing_fields_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note", fields=None)


def test_attest_scope_empty_type_fail_closed():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="", fields=["status"])


def test_attest_scope_wrong_type_denied():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="note", fields=["status"])


def test_attest_scope_refuses_body_write_SECURITY_PIN():
    # The body-freeze pin, now on the privileged attest scope. A pure
    # body_append arrives with fields=[] (EMPTY, not None) + body_write=True;
    # the explicit body_write refusal is what freezes clinical content.
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note",
                    fields=[], body_write=True)


def test_attest_scope_status_only_no_body_passes():
    check_scope(_ATTEST_SCOPE, "edit", record_type="clinical_note",
                fields=["status", "attested_by", "attested_at"], body_write=False)


def test_attest_scope_cannot_create_or_delete():
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "create", record_type="clinical_note")
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "delete", record_type="clinical_note")
    with pytest.raises(ScopeError):
        check_scope(_ATTEST_SCOPE, "move", record_type="clinical_note")


def test_gate1_admits_clinical_note_under_attest_scope():
    # The attest scope has list:True; gate 1 (_validate_type) must admit
    # clinical_note under it (VERA-P1 lesson). Auto-derived, no literal edit.
    assert "clinical_note" in schema.TYPE_REGISTRY.known_types("stayc_clinical_attest")


# ---------------------------------------------------------------------------
# #41 CREATE-BYPASS GUARD — a clinical_note is born ai_draft only.
# ---------------------------------------------------------------------------

def test_create_bypass_born_attested_refused():
    # THE born-attested pin (mutation: allow status=attested at create => fails).
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="clinical_note",
                    frontmatter={"status": "attested"})


@pytest.mark.parametrize("bad", [{"attested_by": "np_jamie"}, {"attested_at": "2026-07-09T00:00:00Z"}])
def test_create_bypass_attest_fields_at_create_refused(bad):
    with pytest.raises(ScopeError):
        check_scope(_SCOPE, "create", record_type="clinical_note", frontmatter=bad)


@pytest.mark.parametrize("fm", [{}, {"status": "ai_draft"}, {"status": "ai_draft", "synthetic": True}])
def test_create_ai_draft_or_absent_status_allowed(fm):
    # status absent (schema default ai_draft) or explicitly ai_draft => OK.
    check_scope(_SCOPE, "create", record_type="clinical_note", frontmatter=fm)


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

def test_e2e_draft_then_structural_attest_then_frozen(tmp_path):
    # 1. Create the AI draft (body IS the payload; born ai_draft).
    result = vault_create(
        tmp_path, "clinical_note", "Encounter 2026-07-09 chest pain",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft"},
        body="## Subjective\nSynthetic patient reports chest pain.\n",
        scope=_SCOPE,
    )
    rel_path = result["path"]
    assert (tmp_path / rel_path).exists()

    # 2. RAW attest flip via the agent scope is STRUCTURALLY REFUSED (#41).
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            set_fields={"status": "attested", "attested_by": "stayc_scribe"},
            scope=_SCOPE,
        )
    # Still ai_draft — the raw edit did not land.
    assert frontmatter.load(str(tmp_path / rel_path))["status"] == "ai_draft"

    # 3. Attestation goes ONLY through the scribe.attest orchestrator, by a
    # distinct human clinician.
    from alfred.scribe.attest import attest as scribe_attest
    audit_path = tmp_path / "clinical_attest_audit.jsonl"
    scribe_attest(
        tmp_path, rel_path,
        new_status="attested",
        attester="np_jamie",
        clinician_ids={"np_jamie"},
        audit_path=audit_path,
    )
    post = frontmatter.load(str(tmp_path / rel_path))
    assert post["status"] == "attested"
    assert post["attested_by"] == "np_jamie"
    assert audit_path.exists()

    # 4. Body is FROZEN — a body_append via the agent scope is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_edit(
            tmp_path, rel_path,
            body_append="\nSNEAKY addendum\n",
            scope=_SCOPE,
        )

    # 5. Anti-spoliation — delete is refused.
    with pytest.raises((ScopeError, VaultError)):
        vault_delete(tmp_path, rel_path, scope=_SCOPE)


def test_e2e_non_clinical_create_denied(tmp_path):
    with pytest.raises((ScopeError, VaultError)):
        vault_create(
            tmp_path, "note", "A note", set_fields={}, scope=_SCOPE,
        )
