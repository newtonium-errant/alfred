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
    CALIBRATION_APPLY_SCOPE,
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


# --- the writer is actually GATED, not merely gate-capable -------------------


def test_premise_the_scope_constant_matches_the_registry_key() -> None:
    """One intent, two spellings — pinned rather than trusted.

    ``SCOPE_RULES`` is built at import, before CALIBRATION_APPLY_SCOPE is
    defined, so the key is a literal and the constant is separate. That is
    the ``jeeves`` arrangement, and "stated in two places that cannot drift
    apart" is a claim about a MECHANISM: either a pin enforces the equality
    or the two WILL drift.
    """
    assert CALIBRATION_APPLY_SCOPE == SCOPE
    assert CALIBRATION_APPLY_SCOPE in SCOPE_RULES


def test_the_writer_defaults_to_the_scope_not_to_none() -> None:
    """THE THREADING PIN — the direction that makes the gate load-bearing.

    ``apply_proposals`` used to write with ``scope=None`` (unrestricted).
    Had the gate been added as an optional parameter defaulting to ``None``,
    this lane would have reproduced the standing trap: tests thread it,
    production never does, every pin stays green, feature accepted-then-
    ignored. Defaulting to the restrictive scope inverts that — an
    unrestricted write now requires a visible opt-out.

    Read from the SIGNATURE rather than by calling, so this pin cannot be
    satisfied by a call site that happens to pass the right value.
    """
    import inspect

    from alfred.telegram.calibration import apply_proposals

    default = inspect.signature(apply_proposals).parameters["scope"].default
    assert default == CALIBRATION_APPLY_SCOPE, (
        "apply_proposals must default to the restrictive scope; a None "
        "default is an ungated production write with green tests"
    )


def test_the_writer_refuses_a_foreign_type_end_to_end(tmp_path) -> None:
    """Driven through the real writer, not through check_scope directly.

    A per-layer unit pin cannot catch an unthreaded gate; only a call
    through the production entry point can. The positive control runs FIRST
    so a failure to write for any unrelated reason cannot masquerade as the
    gate firing.
    """
    from alfred.telegram.calibration import (
        CALIBRATION_MARKER_END,
        CALIBRATION_MARKER_START,
        Proposal,
        apply_proposals,
    )

    block = f"{CALIBRATION_MARKER_START}\n## Communication Style\n\n- existing\n\n{CALIBRATION_MARKER_END}\n"
    proposals = [Proposal(subsection="Communication Style", bullet="prefers terse replies")]

    # POSITIVE CONTROL — the admissible neighbour really writes.
    (tmp_path / "person").mkdir()
    (tmp_path / "person" / "Owner.md").write_text(
        f"---\ntype: {_target_type()}\nname: Owner\n---\n\n{block}", encoding="utf-8",
    )
    ok = apply_proposals(tmp_path, "person/Owner", proposals, "session/S")
    assert ok["written"] is True, ok
    assert "prefers terse replies" in (tmp_path / "person" / "Owner.md").read_text()

    # THE REFUSAL — same call, same fields, only the record TYPE differs.
    (tmp_path / "note").mkdir()
    (tmp_path / "note" / "Decoy.md").write_text(
        f"---\ntype: note\nname: Decoy\n---\n\n{block}", encoding="utf-8",
    )
    before = (tmp_path / "note" / "Decoy.md").read_text()
    refused = apply_proposals(tmp_path, "note/Decoy", proposals, "session/S")

    assert refused["written"] is False
    # Assert WHY it refused. A denial for an unrelated cause (missing record,
    # no marker block) reads identically to the guard firing.
    assert "vault_edit_failed" in refused["reason"]
    assert "may only edit" in refused["reason"]
    # And that it touched NOTHING out there — not merely that the write failed.
    assert (tmp_path / "note" / "Decoy.md").read_text() == before
