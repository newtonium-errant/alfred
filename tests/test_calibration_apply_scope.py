"""The ``calibration_apply`` scope — the voice-calibration loop's only write door.

Two pin families earn their keep here, and they fail in opposite directions:

  * ``test_premise_*`` assert the facts this gate's ARRANGEMENT depends on,
    against sources the gate cannot touch (``alfred.vault.schema``). A gate
    whose fixtures move with it is cosmetic: derive the target type from the
    registry and the obvious mutation scores RED 0, because the fixture
    follows the bug. These pins read the registry directly.

  * ``TestScopeGate`` drives ``check_scope`` for real. EVERY refusal pin is
    preceded by its nearest admissible neighbour being ACCEPTED, so the class
    fails against a build that refuses everything as loudly as against one
    that admits everything. A refusal pin without that control passes
    identically when the whole gate is broken.

The load-bearing asymmetry this file exists for: a ``body_rewriter`` edit
reaches ``check_scope`` as ``operation="edit"`` and NEVER passes through
``_check_body_mutation_allowed``, so ``_BODY_MUTATE_DENIED_TYPES`` never sees
it. This gate is the ONLY type fence between ``calibration_apply`` and any
record in the vault, which is why the wrong-type body-write pin below is the
sharpest test in the file.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import (
    CALIBRATION_APPLY_FIELDS,
    CALIBRATION_APPLY_TYPES,
    CALIBRATION_DIRECTORY,
    SCOPE_RULES,
    ScopeError,
    check_scope,
)

SCOPE = "calibration_apply"

#: The one field the gate admits. Spelled here so a silent widening of
#: CALIBRATION_APPLY_FIELDS arrives as a diff on this line rather than as a
#: quietly-passing subset check.
AUDIT_FIELD = "attribution_audit"


# --- premise pins: read from the registry, which this gate cannot touch ------


def test_premise_target_type_is_a_real_registry_type() -> None:
    """The gate guards a type the registry actually defines.

    Independent source: ``alfred.vault.schema.KNOWN_TYPES``. If
    CALIBRATION_APPLY_TYPES were minted by hand (the ``MOC_MIRROR_TYPE``
    failure — a lowercase ``"moc"`` that no registry entry matched, green for
    34 pins because every fixture minted the same non-existent spelling), this
    pin reds while the gate's own tests stay green.
    """
    from alfred.vault.schema import KNOWN_TYPES

    assert CALIBRATION_APPLY_TYPES, "the gate guards no type at all"
    for name in CALIBRATION_APPLY_TYPES:
        assert name in KNOWN_TYPES, (
            f"{name!r} is not a canonical record type; the gate guards a "
            f"shape vault_create would reject"
        )


def test_premise_target_type_is_derived_from_the_directory() -> None:
    """The constant follows the registry rather than a remembered spelling.

    Asserts the SAME derivation against ``TYPE_DIRECTORY`` directly. A future
    rename of the type moves both sides together; a hand-edit of one side
    reds here.
    """
    from alfred.vault.schema import TYPE_DIRECTORY

    from_registry = {
        name
        for name, directory in TYPE_DIRECTORY.items()
        if directory == CALIBRATION_DIRECTORY
    }
    assert from_registry == CALIBRATION_APPLY_TYPES
    assert len(from_registry) == 1, (
        "the derivation raises at import on an ambiguous directory; if this "
        "ever holds more than one type the gate cannot know what it guards"
    )


def test_premise_the_audit_field_is_the_only_admitted_field() -> None:
    """The allowlist is exactly the provenance field — nothing else."""
    assert CALIBRATION_APPLY_FIELDS == {AUDIT_FIELD}


def test_premise_the_scope_is_registered_and_body_replace_stays_shut() -> None:
    """``allow_body_replace`` must stay ``{}``.

    Not taste: ``tests/test_scope_gcal_remedy.py`` pins the set of scopes
    reaching the event-body_replace remedy by EXACT equality across all of
    SCOPE_RULES. A non-empty dict here silently joins that set and reds a
    test in a different file for a reason nobody would connect back.
    """
    rules = SCOPE_RULES[SCOPE]
    assert rules["allow_body_replace"] == {}
    assert rules["allow_body_insert_at"] == {}
    # Stated explicitly in the entry rather than omitted, because the key
    # DEFAULTS to True when absent — silence would read as an oversight.
    assert rules["allow_body_writes"] is True


# --- the gate ----------------------------------------------------------------


def _target_type() -> str:
    return sorted(CALIBRATION_APPLY_TYPES)[0]


class TestScopeGate:
    """Every refusal below is preceded by an accepted neighbour."""

    def test_the_admitted_write_is_accepted(self) -> None:
        """POSITIVE CONTROL for the whole class.

        The real shape the writer uses: the marker-fenced block rewrite plus
        its provenance field, in ONE edit. If this raises, every refusal pin
        below is vacuous.
        """
        check_scope(
            SCOPE,
            "edit",
            record_type=_target_type(),
            fields=[AUDIT_FIELD],
            body_write=True,
            body_op="body_rewriter",
        )

    def test_a_pure_body_write_on_the_target_is_accepted(self) -> None:
        """``fields == []`` — an empty LIST, not None. The legitimate shape."""
        check_scope(
            SCOPE,
            "edit",
            record_type=_target_type(),
            fields=[],
            body_write=True,
            body_op="body_rewriter",
        )

    def test_a_body_write_on_a_foreign_type_is_refused(self) -> None:
        """THE SHARPEST PIN IN THE FILE.

        A ``body_rewriter`` edit skips ``_check_body_mutation_allowed``
        entirely, so ``_BODY_MUTATE_DENIED_TYPES`` never sees it. Without the
        type fence in this gate, ``calibration_apply`` could rewrite the body
        of ANY record in the vault.

        ``note`` is deliberately a CANONICAL type: a scope-only type would be
        refused at gate 1 (``_validate_type``) and this pin would pass against
        a build whose gate 2 admitted everything.
        """
        with pytest.raises(ScopeError) as exc:
            check_scope(
                SCOPE,
                "edit",
                record_type="note",
                fields=[],
                body_write=True,
                body_op="body_rewriter",
            )
        # Assert WHICH rule fired. A denial for an unrelated cause reads
        # identically to the guard firing.
        assert "may only edit" in str(exc.value)
        assert _target_type() in str(exc.value)

    def test_a_foreign_field_is_refused_even_on_the_right_type(self) -> None:
        with pytest.raises(ScopeError) as exc:
            check_scope(
                SCOPE,
                "edit",
                record_type=_target_type(),
                fields=[AUDIT_FIELD, "status"],
                body_write=True,
                body_op="body_rewriter",
            )
        assert "Rejected: status" in str(exc.value)

    def test_an_empty_record_type_fails_closed(self) -> None:
        with pytest.raises(ScopeError) as exc:
            check_scope(
                SCOPE,
                "edit",
                record_type="",
                fields=[AUDIT_FIELD],
                body_write=True,
            )
        assert "failing closed" in str(exc.value)

    def test_an_edit_that_changes_nothing_fails_closed(self) -> None:
        """No fields AND no body write authorises nothing."""
        with pytest.raises(ScopeError) as exc:
            check_scope(
                SCOPE,
                "edit",
                record_type=_target_type(),
                fields=[],
                body_write=False,
            )
        assert "nothing to authorise" in str(exc.value)

    def test_create_is_refused(self) -> None:
        """Well-posed because the target type is CANONICAL.

        A scope-only type would be refused at gate 1 regardless, and this pin
        would pass against a build with ``create: True``.
        """
        with pytest.raises(ScopeError):
            check_scope(SCOPE, "create", record_type=_target_type())

    def test_delete_and_move_are_refused(self) -> None:
        for op in ("delete", "move"):
            with pytest.raises(ScopeError):
                check_scope(SCOPE, op, record_type=_target_type())

    def test_reads_are_allowed(self) -> None:
        """The neighbour that proves the refusals above are not blanket."""
        for op in ("read", "search", "list", "context"):
            check_scope(SCOPE, op, record_type=_target_type())
