"""The ``jeeves`` vault scope (task #81, stage 2, design §5.3).

The design calls the scope matrix "the principal artifact", so this file
implements the check side of it cell by cell. Two cells are unusual enough
to deserve their own pins: no READ (narrower than every other ingest-shaped
scope) and no ``task`` (notes-only v1, ruling 4).

Mandatory regression pins — run unconditionally, no ``importorskip``.
"""

from __future__ import annotations

import pytest

from alfred.vault import schema
from alfred.vault.ops import _validate_type
from alfred.vault.scope import (
    JEEVES_CREATE_TYPES,
    JEEVES_SCOPE,
    RRTS_INTAKE_CREATE_TYPES,
    SCOPE_RULES,
    WEB_INGEST_CREATE_TYPES,
    ScopeError,
    check_scope,
)

JEEVES_TYPES = ("note", "source")


# ---------------------------------------------------------------------------
# Contract pins
# ---------------------------------------------------------------------------


def test_jeeves_create_types_matrix_pin():
    """CONTRACT PIN (design §5.3 + ruling 4). {note, source} and nothing
    else. Widening this needs a ratified ruling, not a commit — update the
    pin in the same one."""
    assert JEEVES_CREATE_TYPES == {"note", "source"}


def test_the_scope_name_is_the_one_the_route_and_config_use():
    assert JEEVES_SCOPE == "jeeves"
    assert JEEVES_SCOPE in SCOPE_RULES


def test_the_full_matrix_row_matches_the_design():
    """The §5.3 table, implemented exactly. Every cell asserted, because a
    scope is the sort of thing that acquires a permission by accident."""
    rules = SCOPE_RULES[JEEVES_SCOPE]
    assert rules["read"] is False
    assert rules["search"] is False
    assert rules["list"] is False
    assert rules["context"] is False
    assert rules["create"] == "jeeves_types_only"
    assert rules["edit"] is False
    assert rules["move"] is False
    assert rules["delete"] is False
    assert rules["allow_body_writes"] is True
    assert rules["allow_body_insert_at"] == {}
    assert rules["allow_body_replace"] == {}


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rec_type", JEEVES_TYPES)
def test_create_allows_the_two_capture_types(rec_type: str):
    check_scope(JEEVES_SCOPE, "create", record_type=rec_type)


@pytest.mark.parametrize("rec_type", JEEVES_TYPES)
def test_create_allows_body_writes(rec_type: str):
    """The transcript IS the payload — a create with no body would write an
    empty record."""
    check_scope(JEEVES_SCOPE, "create", record_type=rec_type, body_write=True)


def test_jeeves_may_NOT_mint_tasks():
    """RULING 4, the narrowing worth arguing about. A device that mints tasks
    from ambient speech in a noisy room will mint junk tasks, and tasks are
    the type the operator's day is built from."""
    with pytest.raises(ScopeError) as exc:
        check_scope(JEEVES_SCOPE, "create", record_type="task")
    assert "task" in str(exc.value)


@pytest.mark.parametrize("rec_type", [
    "person", "org", "event", "project", "decision", "document",
    "clinical_note", "ticket", "preference", "routine",
])
def test_create_denies_every_other_type(rec_type: str):
    with pytest.raises(ScopeError):
        check_scope(JEEVES_SCOPE, "create", record_type=rec_type)


def test_jeeves_can_never_create_a_clinical_note():
    """FENCE 5, at the scope layer. Personal / RRTS only, never clinical —
    the separation is structural, not a matter of which token was used."""
    assert "clinical_note" not in JEEVES_CREATE_TYPES
    with pytest.raises(ScopeError):
        check_scope(JEEVES_SCOPE, "create", record_type="clinical_note")


def test_create_fails_CLOSED_on_an_empty_record_type():
    """An empty value is a caller bug, not a licence to create any type."""
    with pytest.raises(ScopeError) as exc:
        check_scope(JEEVES_SCOPE, "create", record_type="")
    assert "failing closed" in str(exc.value)


# ---------------------------------------------------------------------------
# The two deliberate narrowings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["read", "search", "list", "context"])
def test_jeeves_is_WRITE_ONLY(op: str):
    """THE NARROWING. Every other ingest-shaped scope grants these. Jeeves
    does not need them — the device transcribes and posts, it never resolves
    a record first — and an always-on microphone in a shed should not double
    as a vault query surface."""
    with pytest.raises(ScopeError):
        check_scope(JEEVES_SCOPE, op)


def test_jeeves_is_narrower_than_the_ingest_scopes_it_resembles():
    """The comparison that makes the narrowing legible: the two closest
    precedents both grant read, and this one deliberately does not."""
    assert SCOPE_RULES["web_ingest"]["read"] is True
    assert SCOPE_RULES["rrts_intake"]["read"] is True
    assert SCOPE_RULES[JEEVES_SCOPE]["read"] is False


@pytest.mark.parametrize("op", ["edit", "move", "delete"])
def test_create_once_denies_every_mutation(op: str):
    with pytest.raises(ScopeError):
        check_scope(JEEVES_SCOPE, op, record_type="note", rel_path="note/x.md")


@pytest.mark.parametrize("op", ["body_insert_at", "body_replace"])
@pytest.mark.parametrize("rec_type", JEEVES_TYPES)
def test_body_patching_is_deny_all(op: str, rec_type: str):
    """Jeeves creates a new record or 409s; it never patches an existing
    body."""
    with pytest.raises(ScopeError):
        check_scope(
            JEEVES_SCOPE, op, record_type=rec_type,
            rel_path=f"{rec_type}/x.md", body_write=True, body_op=op,
        )


# ---------------------------------------------------------------------------
# Both gates agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rec_type", JEEVES_TYPES)
def test_gate_1_admits_what_gate_2_allows(rec_type: str):
    """THE AUTO-DERIVE CATCH. ``_validate_type`` is gate 1 and runs FIRST;
    if ``source`` were not tagged ``available_in_scopes={... jeeves}`` it
    would be rejected with "Unknown type under scope 'jeeves'" before gate
    2's policy ever ran — a scope that passes every check_scope test and
    still cannot write."""
    _validate_type(rec_type, scope=JEEVES_SCOPE)


def test_the_schema_tag_is_present_on_source():
    definition = schema.TYPE_REGISTRY.get("source")
    assert definition is not None
    assert JEEVES_SCOPE in definition.available_in_scopes


def test_note_rides_the_canonical_tag():
    """``note`` is SCOPE_CANONICAL, so gate 1 admits it under every scope —
    it needs no jeeves tag, and adding one would be noise."""
    definition = schema.TYPE_REGISTRY.get("note")
    assert definition is not None
    assert schema.SCOPE_CANONICAL in definition.available_in_scopes
    # ...and specifically NOT tagged jeeves, which would be dead noise.
    assert JEEVES_SCOPE not in definition.available_in_scopes


def test_gate_1_rejects_a_type_gate_2_would_also_reject():
    """Belt and braces on the denial: neither gate alone is the fence."""
    with pytest.raises(Exception):
        _validate_type("clinical_note", scope=JEEVES_SCOPE)


# ---------------------------------------------------------------------------
# Cross-scope isolation
# ---------------------------------------------------------------------------


def test_the_jeeves_allowlist_is_its_own():
    """A shared set object would mean widening one scope silently widened
    another."""
    assert JEEVES_CREATE_TYPES is not WEB_INGEST_CREATE_TYPES
    assert JEEVES_CREATE_TYPES is not RRTS_INTAKE_CREATE_TYPES
    assert JEEVES_CREATE_TYPES != WEB_INGEST_CREATE_TYPES
