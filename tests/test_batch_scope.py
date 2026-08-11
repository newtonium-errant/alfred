"""``vera_batch`` scope contract (#83) — the bulk-image batch write surface.

The batch worker is the narrowest write scope in ``scope.py`` that can
still ``body_replace``, so these pins are deliberately paranoid. Two
properties carry the whole design:

  1. **Ownership.** A record is editable/replaceable only when its own
     on-disk frontmatter carries ``BATCH_OWNER_FIELD``. Without this the
     grant "may body_replace a note" means "may flatten ANY note," which
     is unrecoverable from the vault alone.
  2. **No discovery.** search / list / context are denied, which is how
     "no reads beyond the carried record" is enforced rather than merely
     intended.

Every refusal pin below asserts on the REASON, not just that a
``ScopeError`` was raised. A denial for an unrelated cause (missing
type, wrong type, unsupplied fields) renders identically to the
ownership guard firing if you only assert ``pytest.raises`` — so a pin
that checks nothing but the exception class stays green against a build
with no ownership guard at all.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import (
    BATCH_OWNER_FIELD,
    SCOPE_RULES,
    VERA_BATCH_CREATE_TYPES,
    VERA_BATCH_EDIT_FIELDS,
    ScopeError,
    check_scope,
)


# The batch id value used throughout; any non-empty string marks ownership.
_OWNED = {BATCH_OWNER_FIELD: "batch-20260811-abc123"}


# ---------------------------------------------------------------------------
# Contract pins — widening any of these must update this test in the same
# commit (the point of the pin is that a silent widen turns this RED).
# ---------------------------------------------------------------------------


def test_create_types_pinned() -> None:
    assert VERA_BATCH_CREATE_TYPES == {"note"}


def test_edit_fields_pinned() -> None:
    assert VERA_BATCH_EDIT_FIELDS == {
        "batch_items_done",
        "batch_items_total",
        "batch_items_failed",
        "batch_updated_at",
        "batch_last_error",
    }


def test_owner_field_pinned() -> None:
    assert BATCH_OWNER_FIELD == "batch_id"


def test_scope_rules_shape() -> None:
    """The matrix itself — the artifact the feature was designed from."""
    rules = SCOPE_RULES["vera_batch"]
    assert rules["read"] is True
    # No discovery: this is what enforces "no reads beyond the carried record".
    assert rules["search"] is False
    assert rules["list"] is False
    assert rules["context"] is False
    assert rules["create"] == "vera_batch_types_only"
    assert rules["edit"] == "vera_batch_own_records_only"
    # A bulk processor must never relocate or destroy operator records.
    assert rules["move"] is False
    assert rules["delete"] is False
    assert rules["allow_body_writes"] is True
    # Wholesale render only — never mid-document patching.
    assert rules["allow_body_insert_at"] == {}
    assert rules["allow_body_replace"] == {"note": True}


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_note_allowed() -> None:
    check_scope("vera_batch", "create", record_type="note")


@pytest.mark.parametrize(
    "record_type", ["task", "project", "ticket", "decision", "person"],
)
def test_create_other_types_refused(record_type: str) -> None:
    with pytest.raises(ScopeError, match="can only create"):
        check_scope("vera_batch", "create", record_type=record_type)


def test_create_missing_type_fails_closed() -> None:
    with pytest.raises(ScopeError, match="failing closed"):
        check_scope("vera_batch", "create", record_type="")


# ---------------------------------------------------------------------------
# edit — type, ownership, fields
# ---------------------------------------------------------------------------


def test_edit_owned_record_allowed() -> None:
    check_scope(
        "vera_batch",
        "edit",
        record_type="note",
        fields=["batch_items_done", "batch_updated_at"],
        existing_frontmatter=_OWNED,
    )


def test_edit_unowned_record_refused_for_ownership() -> None:
    """The guard under test — NOT a type or field refusal.

    An operator-authored note has every other property the gate wants
    (right type, allowlisted fields); the ONLY thing missing is the
    ownership marker. Asserting on that specific wording is what makes
    this pin fail against a build with no ownership check.
    """
    with pytest.raises(ScopeError, match="carries no 'batch_id'"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="note",
            fields=["batch_items_done"],
            existing_frontmatter={"title": "Operator's meeting notes"},
        )


def test_edit_empty_owner_value_refused() -> None:
    """Present-but-blank is not ownership."""
    with pytest.raises(ScopeError, match="carries no 'batch_id'"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="note",
            fields=["batch_items_done"],
            existing_frontmatter={BATCH_OWNER_FIELD: "   "},
        )


def test_edit_missing_frontmatter_fails_closed() -> None:
    """Unproven ownership is refused, not assumed."""
    with pytest.raises(ScopeError, match="was not supplied"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="note",
            fields=["batch_items_done"],
            existing_frontmatter=None,
        )


def test_edit_wrong_type_refused() -> None:
    with pytest.raises(ScopeError, match="may only edit record types"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="task",
            fields=["batch_items_done"],
            existing_frontmatter=_OWNED,
        )


def test_edit_missing_type_fails_closed() -> None:
    with pytest.raises(ScopeError, match="failing closed"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="",
            fields=["batch_items_done"],
            existing_frontmatter=_OWNED,
        )


def test_edit_missing_fields_fails_closed() -> None:
    with pytest.raises(ScopeError, match="did not supply the field list"):
        check_scope(
            "vera_batch",
            "edit",
            record_type="note",
            fields=None,
            existing_frontmatter=_OWNED,
        )


@pytest.mark.parametrize("field", ["title", "type", "status", "tags"])
def test_edit_disallowed_field_refused(field: str) -> None:
    """The operator owns the record's identity and lifecycle."""
    with pytest.raises(ScopeError, match="Rejected: " + field):
        check_scope(
            "vera_batch",
            "edit",
            record_type="note",
            fields=[field],
            existing_frontmatter=_OWNED,
        )


# ---------------------------------------------------------------------------
# body_replace — the destructive path, gated on ownership
# ---------------------------------------------------------------------------


def test_body_replace_owned_note_allowed() -> None:
    check_scope(
        "vera_batch",
        "body_replace",
        record_type="note",
        existing_frontmatter=_OWNED,
    )


def test_body_replace_unowned_note_refused_for_ownership() -> None:
    """The single most load-bearing pin in this file.

    ``allow_body_replace`` is ``{"note": True}``, so the per-type
    allowlist ADMITS this record. Only the ownership carve-out refuses
    it. If that carve-out is deleted, this test goes green-to-red while
    every type-level pin above stays green — which is exactly the
    separation the two checks are supposed to have.
    """
    with pytest.raises(ScopeError, match="carries no 'batch_id'"):
        check_scope(
            "vera_batch",
            "body_replace",
            record_type="note",
            existing_frontmatter={"title": "Operator's meeting notes"},
        )


def test_body_replace_missing_frontmatter_refused() -> None:
    with pytest.raises(ScopeError, match="carries no 'batch_id'"):
        check_scope(
            "vera_batch", "body_replace", record_type="note",
            existing_frontmatter=None,
        )


def test_body_replace_wrong_type_refused() -> None:
    """Type refusal fires before ownership — distinct reason, distinct pin."""
    with pytest.raises(ScopeError, match="may not 'body_replace' on type"):
        check_scope(
            "vera_batch",
            "body_replace",
            record_type="task",
            existing_frontmatter=_OWNED,
        )


def test_body_insert_at_denied_even_when_owned() -> None:
    """Wholesale render only — an owned record is still not patchable."""
    with pytest.raises(ScopeError, match="no allowlist configured"):
        check_scope(
            "vera_batch",
            "body_insert_at",
            record_type="note",
            existing_frontmatter=_OWNED,
        )


# ---------------------------------------------------------------------------
# Denied operations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["search", "list", "context"])
def test_discovery_denied(operation: str) -> None:
    """No discovery surface — the batch id names its record directly."""
    with pytest.raises(ScopeError):
        check_scope("vera_batch", operation, record_type="note")


@pytest.mark.parametrize("operation", ["move", "delete"])
def test_move_and_delete_denied(operation: str) -> None:
    with pytest.raises(ScopeError):
        check_scope(
            "vera_batch",
            operation,
            record_type="note",
            existing_frontmatter=_OWNED,
        )


def test_read_allowed() -> None:
    """The regenerate path must re-read its record to evaluate the seal."""
    check_scope("vera_batch", "read", record_type="note")


# ---------------------------------------------------------------------------
# Gate 1 / gate 2 agreement (the VERA P1 trap, CLAUDE.md)
# ---------------------------------------------------------------------------


def test_gate1_admits_note_under_vera_batch() -> None:
    """``_validate_type`` and ``check_scope`` must AGREE on ``note``.

    The 2026-06-09 VERA P1 trap was a type that gate 2 allowed and gate 1
    rejected. ``note`` is SCOPE_CANONICAL so it would pass regardless,
    but pinning it here means a future narrowing of the canonical set
    cannot silently break the batch create path.
    """
    from alfred.vault.schema import KNOWN_TYPES_BY_SCOPE

    assert "note" in KNOWN_TYPES_BY_SCOPE["vera_batch"]
