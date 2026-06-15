"""Tests for the VERA scope + ticket-type schema additions.

VERA (project_vera_ops_assistant.md) is the first multi-user instance:
owner (Andrew) → ``vera`` scope, ops (Ben) → ``vera_ops`` scope.

**Capability expansion 2026-06-15 (vera-assistant arc).** VERA grew from
a ticket bot into a general PHI-free business assistant for the whole
RRTS team. BOTH roles now create+edit the SAME five record types:
``ticket`` (intake) + the four business types ``note`` / ``task`` /
``decision`` / ``project``. ``decision`` is the ONE dual-nature
(canonical + learn) type granted — operational business decisions, not
epistemic distiller extractions; granting it does NOT leak the other
four learn types. Learn types (except ``decision``), canonical/PHI
types, move, and delete all stay DENIED.

Coverage:
    * Create-type contract pins (both sets == the five-type matrix).
    * ``vera_ops`` + ``vera`` scopes — create+edit each of the five
      types (two-gate end-to-end); learn-type create denied (proves
      ``decision`` didn't leak ``assumption``/``synthesis``/...);
      canonical/PHI create denied; move + delete denied.
    * Both gates agree — the four business types tagged
      ``available_in_scopes`` with the VERA scopes; KNOWN_TYPES_BY_SCOPE
      auto-derives (literal-reversion catch).
    * ``ticket`` TypeDefinition — registered with the right statuses,
      required fields, directory, name_field, leaf-ness.
    * Derived globals auto-populate; ``screenshots`` in LIST_FIELDS.
"""

from __future__ import annotations

import pytest

from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create, vault_edit
from alfred.vault.scope import (
    VERA_CREATE_TYPES,
    VERA_OPS_CREATE_TYPES,
    ScopeError,
    check_scope,
)


# The five record types VERA grants both roles (the operator-confirmed
# capability matrix). ``decision`` is the dual-nature one; the four
# business types are note/task/decision/project; ticket is the intake.
VERA_BUSINESS_TYPES = ("note", "task", "decision", "project")
VERA_ALL_CREATE_TYPES = {"ticket", "note", "task", "decision", "project"}

# Per-type minimal valid frontmatter for end-to-end vault_create.
_FIELDS_BY_TYPE: dict[str, dict] = {
    "note": {},
    "task": {"status": "todo"},
    "decision": {"status": "draft"},
    "project": {"status": "active"},
    "ticket": {
        "ticket_type": "bug", "reporter": "ben",
        "area": "transport-admin-portal",
    },
}


# ---------------------------------------------------------------------------
# Create-type constants — the matrix contract pin
# ---------------------------------------------------------------------------


def test_vera_ops_create_types_matrix_pin():
    """CONTRACT PIN (vera-assistant arc): both roles get the same five
    types. Widening either set is a deliberate matrix change — update
    this pin in the same commit (pre-commit checklist #6)."""
    assert VERA_OPS_CREATE_TYPES == VERA_ALL_CREATE_TYPES


def test_vera_owner_create_types_matrix_pin():
    assert VERA_CREATE_TYPES == VERA_ALL_CREATE_TYPES


def test_vera_both_roles_identical_create_set():
    """The two roles are operationally identical today (separate constants
    for future divergence). If they diverge, this pin surfaces it."""
    assert VERA_CREATE_TYPES == VERA_OPS_CREATE_TYPES


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


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_vera_ops_create_allows_business_types(rec_type: str):
    """Ops now creates note/task/decision/project (capability expansion)."""
    check_scope("vera_ops", "create", record_type=rec_type)


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_vera_ops_edit_allows_business_types(rec_type: str):
    """Ops edits the same types (edit: True, gate 1 admits via tag)."""
    check_scope("vera_ops", "edit", record_type=rec_type)


def test_vera_ops_create_denies_learn_types():
    # decision is granted; the OTHER four learn types are NOT — proves
    # the decision grant didn't leak the whole learn set.
    for t in ("assumption", "constraint", "contradiction", "synthesis"):
        with pytest.raises(ScopeError):
            check_scope("vera_ops", "create", record_type=t)


def test_vera_ops_create_denies_canonical_and_phi():
    for t in ("person", "org", "location", "event", "pattern"):
        with pytest.raises(ScopeError):
            check_scope("vera_ops", "create", record_type=t)


def test_vera_ops_edit_permitted_no_field_check():
    # resolve/close = status edit; edit: True (no field allowlist).
    check_scope("vera_ops", "edit")


def test_vera_ops_denies_move_and_delete():
    # Decision B (ratified): both False — UNCHANGED by the expansion.
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "move")
    with pytest.raises(ScopeError):
        check_scope("vera_ops", "delete")


def test_vera_ops_body_writes_permitted():
    # VERA writes record bodies at create time.
    check_scope("vera_ops", "create", record_type="ticket", body_write=True)


def test_vera_ops_body_insert_and_replace_denied():
    # Ops doesn't patch / rewrite bodies (owner-only) — UNCHANGED.
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


def test_vera_create_allows_ticket():
    check_scope("vera", "create", record_type="ticket")


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_vera_create_allows_business_types(rec_type: str):
    """Owner creates note/task/decision/project (capability expansion)."""
    check_scope("vera", "create", record_type=rec_type)


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_vera_edit_allows_business_types(rec_type: str):
    check_scope("vera", "edit", record_type=rec_type)


def test_vera_create_denies_learn_types():
    # decision granted; the other four learn types denied (no leak).
    for t in ("assumption", "constraint", "contradiction", "synthesis"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope("vera", "create", record_type=t)
        assert "vera types" in str(exc_info.value).lower()


def test_vera_create_denies_canonical_and_phi():
    for t in ("person", "org", "location", "event", "pattern"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope("vera", "create", record_type=t)
        assert "vera types" in str(exc_info.value).lower()


def test_vera_denies_move_and_delete():
    with pytest.raises(ScopeError):
        check_scope("vera", "move")
    with pytest.raises(ScopeError):
        check_scope("vera", "delete")


def test_vera_body_insert_and_replace_allowed_for_business_types():
    # Owner may patch + rewrite ticket / note / task / project bodies.
    # ``decision`` is DELIBERATELY excluded (it's in
    # _BODY_MUTATE_DENIED_TYPES — supersede-with-new-record is the
    # change path) — see test_vera_body_mutation_denied_for_decision.
    for t in ("ticket", "note", "task", "project"):
        check_scope("vera", "body_insert_at", record_type=t)
        check_scope("vera", "body_replace", record_type=t)


def test_vera_body_mutation_denied_for_decision():
    """``decision`` is a granted CREATE type but body mutation stays
    denied (universal _BODY_MUTATE_DENIED_TYPES wins over the per-scope
    allowlist — same as kalle's decision handling). body_append (the
    separate allow_body_writes gate) is the append path."""
    with pytest.raises(ScopeError):
        check_scope("vera", "body_insert_at", record_type="decision")
    with pytest.raises(ScopeError):
        check_scope("vera", "body_replace", record_type="decision")


def test_vera_body_replace_denied_for_unlisted_type():
    # ``event`` is not in the vera body-replace allowlist → denied.
    with pytest.raises(ScopeError):
        check_scope("vera", "body_replace", record_type="event")


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


# ---------------------------------------------------------------------------
# Capability expansion (2026-06-15) — gate 1 + gate 2 BOTH agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_business_types_tagged_for_both_vera_scopes(rec_type: str):
    """Gate 1: each business type's available_in_scopes admits both VERA
    scopes (so _validate_type passes on create AND edit/list)."""
    defn = schema.TYPE_REGISTRY.get(rec_type)
    assert defn is not None
    assert "vera" in defn.available_in_scopes
    assert "vera_ops" in defn.available_in_scopes


@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_business_types_in_known_types_by_scope(rec_type: str):
    """KNOWN_TYPES_BY_SCOPE (gate-1's lookup) admits each business type
    under both VERA scopes. Also true via canonical membership, but the
    explicit tags must not have broken the auto-derivation."""
    assert rec_type in schema.KNOWN_TYPES_BY_SCOPE["vera"]
    assert rec_type in schema.KNOWN_TYPES_BY_SCOPE["vera_ops"]


def test_known_types_by_scope_still_auto_derives_for_vera():
    """Literal-reversion catch (the VERA-P1 trap class): re-derive the
    vera scope's valid set from the registry and assert the exported
    dict matches. A reverted hardcoded literal would fail here."""
    for scope_name in ("vera", "vera_ops"):
        expected = set(schema.TYPE_REGISTRY.known_types(scope_name))
        assert schema.KNOWN_TYPES_BY_SCOPE[scope_name] == expected
        # The five granted types are all present in the derived set.
        assert VERA_ALL_CREATE_TYPES <= schema.KNOWN_TYPES_BY_SCOPE[scope_name]


def test_decision_stays_a_learn_type():
    """Granting VERA create on ``decision`` must NOT remove it from
    LEARN_TYPES — the distiller's learn_types_only gate still relies on
    it. The two gates are orthogonal."""
    assert "decision" in schema.LEARN_TYPES
    # The other four learn types are likewise untouched.
    for t in ("assumption", "constraint", "contradiction", "synthesis"):
        assert t in schema.LEARN_TYPES


# ---------------------------------------------------------------------------
# End-to-end two-gate — vault_create + vault_edit under each VERA scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope_name", ("vera", "vera_ops"))
@pytest.mark.parametrize("rec_type", VERA_BUSINESS_TYPES)
def test_e2e_create_then_edit_each_business_type(
    tmp_path, scope_name: str, rec_type: str,
) -> None:
    """Full two-gate coverage (the hypatia/kalle template): create a
    record of each business type under each VERA scope, then edit a
    frontmatter field — both gates pass end-to-end."""
    result = vault_create(
        tmp_path, rec_type, f"VERA {rec_type} record",
        set_fields=dict(_FIELDS_BY_TYPE[rec_type]), scope=scope_name,
    )
    rel_path = result["path"]
    assert (tmp_path / rel_path).exists()

    edited = vault_edit(
        tmp_path, rel_path,
        set_fields={"tags": ["vera-test"]},
        scope=scope_name,
    )
    assert "tags" in edited["fields_changed"]


@pytest.mark.parametrize("scope_name", ("vera", "vera_ops"))
def test_e2e_ticket_still_creates(tmp_path, scope_name: str) -> None:
    """Both roles still file tickets (the original intake capability)."""
    result = vault_create(
        tmp_path, "ticket", "Portal 500 on login",
        set_fields=dict(_FIELDS_BY_TYPE["ticket"]), scope=scope_name,
    )
    assert (tmp_path / result["path"]).exists()


@pytest.mark.parametrize("scope_name", ("vera", "vera_ops"))
def test_e2e_learn_type_create_denied(tmp_path, scope_name: str) -> None:
    """A learn-type create (assumption) is denied end-to-end under both
    VERA scopes — proves the decision grant didn't open the learn set."""
    with pytest.raises((VaultError, ScopeError)):
        vault_create(
            tmp_path, "assumption", "Some assumption",
            set_fields={"status": "active"}, scope=scope_name,
        )


@pytest.mark.parametrize("scope_name", ("vera", "vera_ops"))
def test_e2e_canonical_create_denied(tmp_path, scope_name: str) -> None:
    """A canonical/PHI type create (person) is denied end-to-end."""
    with pytest.raises((VaultError, ScopeError)):
        vault_create(
            tmp_path, "person", "Some Person",
            set_fields={"status": "active"}, scope=scope_name,
        )


@pytest.mark.parametrize("scope_name", ("vera", "vera_ops"))
def test_e2e_delete_denied(tmp_path, scope_name: str) -> None:
    """Delete stays denied under both VERA scopes (unchanged by the
    expansion). Create a note, then a delete attempt fails at the gate."""
    from alfred.vault.ops import vault_delete

    result = vault_create(
        tmp_path, "note", "Throwaway note",
        set_fields={}, scope=scope_name,
    )
    with pytest.raises((VaultError, ScopeError)):
        vault_delete(tmp_path, result["path"], scope=scope_name)
