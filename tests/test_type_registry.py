"""Contract tests for ``alfred.vault.schema`` ``TypeRegistry`` + derived globals.

Pins the registry API surface AND the backward-compatibility contract:
the derived globals (``KNOWN_TYPES``, ``TYPE_DIRECTORY``, etc.) must
produce shapes identical to what the pre-refactor literal declarations
produced. The 10+ files that import these globals continue to work
unchanged.

Most of the per-derived-global behavior (every individual type's
status/directory/etc.) is exercised transitively by the rest of the
test suite — this file pins the REGISTRY-LEVEL contract: lookup
semantics, scope-membership semantics, the ``None`` vs ``frozenset()``
status distinction, the explicit-vs-fallback directory distinction.
"""

from __future__ import annotations

import pytest

from alfred.vault.schema import (
    EVENT_GCAL_FIELDS,
    INSTRUCTION_FIELDS,
    KNOWN_TYPES,
    KNOWN_TYPES_BY_SCOPE,
    KNOWN_TYPES_HYPATIA,
    KNOWN_TYPES_KALLE,
    LEAF_TYPES,
    LEARN_TYPES,
    LIST_FIELDS,
    NAME_FIELD_BY_TYPE,
    REMINDER_FIELDS,
    REQUIRED_FIELDS,
    REQUIRED_FIELDS_BY_TYPE,
    SCOPE_CANONICAL,
    STATUS_BY_TYPE,
    TYPE_DIRECTORY,
    TYPE_REGISTRY,
    TypeDefinition,
    TypeRegistry,
)


# ---------------------------------------------------------------------------
# Registry construction + lookup
# ---------------------------------------------------------------------------


def test_registry_construction_rejects_duplicate_names():
    # The constructor must refuse a duplicate type definition — silent
    # last-wins overwriting would let two callers register conflicting
    # metadata for the same type.
    with pytest.raises(ValueError, match="duplicate definition"):
        TypeRegistry([
            TypeDefinition(name="foo", directory="foo"),
            TypeDefinition(name="foo", directory="bar"),
        ])


def test_registry_get_returns_definition_or_none():
    # Known types resolve to a TypeDefinition; unknown types return None.
    d = TYPE_REGISTRY.get("person")
    assert d is not None
    assert d.name == "person"

    assert TYPE_REGISTRY.get("not-a-real-type") is None


def test_registry_contains_uses_name_string():
    # ``__contains__`` accepts a type name string; non-strings return False.
    assert "person" in TYPE_REGISTRY
    assert "not-a-real-type" not in TYPE_REGISTRY
    assert 42 not in TYPE_REGISTRY  # non-string returns False, doesn't raise


def test_registry_iter_yields_all_definitions():
    # Iteration yields every TypeDefinition exactly once.
    names = [d.name for d in TYPE_REGISTRY]
    assert len(names) == len(set(names))  # no dupes
    assert "person" in names
    assert "zettel" in names  # Hypatia scope
    assert "pattern" in names  # KAL-LE scope


def test_registry_len_matches_definition_count():
    # ``len()`` returns the total number of registered types.
    assert len(TYPE_REGISTRY) == sum(1 for _ in TYPE_REGISTRY)
    assert len(TYPE_REGISTRY) > 30  # sanity check; far above this in practice


# ---------------------------------------------------------------------------
# Scope-membership semantics
# ---------------------------------------------------------------------------


def test_known_types_canonical_matches_legacy_set():
    # ``known_types()`` with no scope = the canonical-only set. This is
    # the BACKWARD-COMPAT pin for the ``KNOWN_TYPES`` global.
    canonical = TYPE_REGISTRY.known_types()
    assert canonical == frozenset(KNOWN_TYPES)
    # Must include the operational types CLAUDE.md documents:
    assert "person" in canonical
    assert "project" in canonical
    assert "task" in canonical
    assert "decision" in canonical
    assert "synthesis" in canonical


def test_known_types_with_scope_unions_canonical_plus_scope():
    # Hypatia scope = canonical + Hypatia-only types. Backward-compat
    # pin for ``KNOWN_TYPES_BY_SCOPE["hypatia"]``.
    hypatia = TYPE_REGISTRY.known_types("hypatia")
    assert hypatia == frozenset(KNOWN_TYPES_BY_SCOPE["hypatia"])
    assert "person" in hypatia  # canonical
    assert "zettel" in hypatia  # hypatia-only
    assert "pattern" not in hypatia  # kalle-only, NOT in hypatia

    kalle = TYPE_REGISTRY.known_types("kalle")
    assert kalle == frozenset(KNOWN_TYPES_BY_SCOPE["kalle"])
    assert "person" in kalle
    assert "pattern" in kalle
    assert "zettel" not in kalle  # hypatia-only


def test_known_types_unknown_scope_falls_back_to_canonical():
    # Unknown scope returns canonical only — same semantics as the old
    # ``KNOWN_TYPES_BY_SCOPE.get(scope, KNOWN_TYPES)`` access pattern.
    unknown = TYPE_REGISTRY.known_types("not-a-real-scope")
    canonical = TYPE_REGISTRY.known_types()
    assert unknown == canonical


def test_types_in_scope_returns_extension_only():
    # ``types_in_scope`` returns ONLY the types tagged with that scope,
    # NOT canonical. Backward-compat pin for ``KNOWN_TYPES_HYPATIA`` /
    # ``KNOWN_TYPES_KALLE`` (which are extension-only).
    assert TYPE_REGISTRY.types_in_scope("hypatia") == frozenset(KNOWN_TYPES_HYPATIA)
    assert TYPE_REGISTRY.types_in_scope("kalle") == frozenset(KNOWN_TYPES_KALLE)

    # No canonical types appear in the extension sets:
    assert "person" not in TYPE_REGISTRY.types_in_scope("hypatia")
    assert "person" not in TYPE_REGISTRY.types_in_scope("kalle")


# ---------------------------------------------------------------------------
# Per-type metadata
# ---------------------------------------------------------------------------


def test_directory_falls_back_to_name_for_missing_entry():
    # Types without an explicit directory fall back to the type name —
    # same semantics as ``TYPE_DIRECTORY.get(t, t)`` used throughout
    # the codebase.
    #
    # ``session`` historically has NO TYPE_DIRECTORY entry; the
    # registry returns ``"session"`` from the fallback.
    assert TYPE_REGISTRY.directory("session") == "session"
    assert "session" not in TYPE_DIRECTORY  # absence is load-bearing

    # ``concept`` is Hypatia-scoped, no explicit directory:
    assert TYPE_REGISTRY.directory("concept") == "concept"
    assert "concept" not in TYPE_DIRECTORY


def test_directory_returns_explicit_entry_when_present():
    # Types with an explicit directory (non-default routing) return
    # that directory, NOT the type name.
    assert TYPE_REGISTRY.directory("essay") == "document/essay"
    assert TYPE_REGISTRY.directory("voice-cluster") == "voice/cluster"
    assert TYPE_REGISTRY.directory("template") == "prose-templates"


def test_directory_for_unknown_type_returns_input_string():
    # Unknown types fall through to the input string. Matches the
    # ``TYPE_DIRECTORY.get(t, t)`` legacy pattern.
    assert TYPE_REGISTRY.directory("not-a-real-type") == "not-a-real-type"


def test_statuses_distinguishes_no_entry_from_empty_entry():
    # ``event`` has an EXPLICIT empty status set — declares "no
    # constraint" by design. This must be distinguishable from "no
    # STATUS_BY_TYPE entry" because ``if rec_type in STATUS_BY_TYPE``
    # is used as a gate in ``janitor/scanner.py`` and ``vault/ops.py``.
    assert TYPE_REGISTRY.statuses("event") == frozenset()
    assert TYPE_REGISTRY.has_status_entry("event") is True
    assert "event" in STATUS_BY_TYPE
    assert STATUS_BY_TYPE["event"] == set()

    # ``concept`` (Hypatia-scoped, no status entry) returns empty
    # frozenset but reports has_status_entry=False:
    assert TYPE_REGISTRY.statuses("concept") == frozenset()
    assert TYPE_REGISTRY.has_status_entry("concept") is False
    assert "concept" not in STATUS_BY_TYPE


def test_statuses_returns_explicit_set():
    # Types with explicit status sets return them verbatim.
    assert TYPE_REGISTRY.statuses("project") == frozenset({
        "active", "paused", "completed", "abandoned", "proposed",
    })
    assert TYPE_REGISTRY.statuses("preference") == frozenset({"active", "revoked"})


def test_statuses_unknown_type_returns_empty():
    # Unknown types return empty frozenset (matches legacy
    # ``STATUS_BY_TYPE.get(t, set())`` semantics).
    assert TYPE_REGISTRY.statuses("not-a-real-type") == frozenset()
    assert TYPE_REGISTRY.has_status_entry("not-a-real-type") is False


def test_required_fields_per_type():
    # ``preference`` is the only type with extra required fields today.
    assert TYPE_REGISTRY.required_fields("preference") == ("name", "shape", "scope")
    # All others return empty tuple:
    assert TYPE_REGISTRY.required_fields("person") == ()
    assert TYPE_REGISTRY.required_fields("not-a-real-type") == ()


def test_name_field_returns_subject_for_special_types():
    # ``conversation`` and ``input`` use ``subject`` instead of ``name``.
    assert TYPE_REGISTRY.name_field("conversation") == "subject"
    assert TYPE_REGISTRY.name_field("input") == "subject"
    # Everything else (including unknowns) defaults to ``name``:
    assert TYPE_REGISTRY.name_field("person") == "name"
    assert TYPE_REGISTRY.name_field("not-a-real-type") == "name"


def test_is_learn_type_matches_legacy_learn_types_set():
    # Every type flagged ``is_learn_type=True`` is in LEARN_TYPES,
    # and vice versa.
    for t in LEARN_TYPES:
        assert TYPE_REGISTRY.is_learn_type(t), f"{t} should be is_learn_type"
    for d in TYPE_REGISTRY:
        if d.is_learn_type:
            assert d.name in LEARN_TYPES
    # ``decision`` is BOTH canonical AND learn — preserved per the
    # pre-refactor LEARN_TYPES literal.
    assert "decision" in LEARN_TYPES
    assert "decision" in KNOWN_TYPES


def test_is_leaf_matches_legacy_leaf_types_set():
    # Every type flagged ``is_leaf=True`` is in LEAF_TYPES, and v.v.
    for t in LEAF_TYPES:
        assert TYPE_REGISTRY.is_leaf(t), f"{t} should be is_leaf"
    for d in TYPE_REGISTRY:
        if d.is_leaf:
            assert d.name in LEAF_TYPES


# ---------------------------------------------------------------------------
# Backward-compatibility — derived globals match expected shapes
# ---------------------------------------------------------------------------
#
# These tests pin the EXACT shape of the historical globals. If a
# future refactor changes the registry shape, these tests must continue
# to pass — the 10+ consumer files depend on the precise types/shapes.


def test_known_types_global_is_set_with_canonical_types():
    # ``KNOWN_TYPES`` must be a regular ``set`` (not frozenset) — some
    # legacy callers may rely on mutability or the ``set`` type.
    assert isinstance(KNOWN_TYPES, set)
    # Spot-check the documented canonical types from CLAUDE.md:
    for t in ["project", "task", "person", "org", "note", "event",
              "decision", "preference", "assumption", "synthesis"]:
        assert t in KNOWN_TYPES, f"{t} must be in canonical KNOWN_TYPES"


def test_known_types_disjoint_from_scope_extensions():
    # The three sets must be pairwise disjoint — that's the underlying
    # invariant the union-based ``KNOWN_TYPES_BY_SCOPE`` relies on.
    assert KNOWN_TYPES.isdisjoint(KNOWN_TYPES_HYPATIA)
    assert KNOWN_TYPES.isdisjoint(KNOWN_TYPES_KALLE)
    assert KNOWN_TYPES_HYPATIA.isdisjoint(KNOWN_TYPES_KALLE)


def test_known_types_by_scope_unions_canonical_plus_extension():
    # Each scope entry = canonical ∪ extension set.
    assert KNOWN_TYPES_BY_SCOPE["kalle"] == KNOWN_TYPES | KNOWN_TYPES_KALLE
    assert KNOWN_TYPES_BY_SCOPE["hypatia"] == KNOWN_TYPES | KNOWN_TYPES_HYPATIA


def test_type_directory_omits_fallback_entries():
    # Types with default-fallback directory routing must NOT appear in
    # the TYPE_DIRECTORY dict — ``set(TYPE_DIRECTORY.values())`` is
    # load-bearing in janitor/scanner.py for body-link entity
    # detection, and silently expanding the values set would change
    # scan behavior.
    assert "session" not in TYPE_DIRECTORY
    assert "concept" not in TYPE_DIRECTORY
    assert "source" not in TYPE_DIRECTORY
    assert "pattern" not in TYPE_DIRECTORY
    # Explicit entries still present:
    assert TYPE_DIRECTORY["essay"] == "document/essay"
    assert TYPE_DIRECTORY["template"] == "prose-templates"


def test_status_by_type_preserves_explicit_empty_event_entry():
    # ``event`` must appear in ``STATUS_BY_TYPE`` with an empty set —
    # the explicit empty-set vs missing-entry distinction is the gate
    # used by ``if rec_type in STATUS_BY_TYPE`` in scanner.py:381.
    assert "event" in STATUS_BY_TYPE
    assert STATUS_BY_TYPE["event"] == set()


def test_required_fields_by_type_omits_empty_entries():
    # Empty-tuple required-fields must NOT appear in the dict — that's
    # the historical semantics: ``REQUIRED_FIELDS_BY_TYPE.get(t, [])``
    # treats absence and empty as equivalent. Only types with non-empty
    # required-fields are listed.
    for t, fields in REQUIRED_FIELDS_BY_TYPE.items():
        assert fields, f"{t} has an empty required_fields entry — should be omitted"


def test_name_field_by_type_omits_default_entries():
    # Types defaulting to ``"name"`` must NOT appear in the dict — same
    # ``.get(t, "name")`` semantics as the historical literal.
    for t, field in NAME_FIELD_BY_TYPE.items():
        assert field != "name", f"{t} has default name_field — should be omitted"
    # The two known special cases stay registered:
    assert NAME_FIELD_BY_TYPE["conversation"] == "subject"
    assert NAME_FIELD_BY_TYPE["input"] == "subject"


def test_learn_types_is_set_of_strings():
    # Backward-compat: LEARN_TYPES is a regular set used in
    # ``rec_type in LEARN_TYPES`` membership checks throughout.
    assert isinstance(LEARN_TYPES, set)
    assert LEARN_TYPES == {
        "assumption", "decision", "constraint", "contradiction", "synthesis",
    }


def test_leaf_types_is_set_of_strings():
    # Backward-compat: LEAF_TYPES is a regular set used in
    # ``rec_type not in LEAF_TYPES`` orphan-skip checks.
    assert isinstance(LEAF_TYPES, set)
    # Pin the 2026-05-06 expansion content — note + run + epistemic.
    assert "note" in LEAF_TYPES
    assert "run" in LEAF_TYPES
    for epistemic in ("synthesis", "contradiction", "decision",
                       "assumption", "constraint"):
        assert epistemic in LEAF_TYPES


# ---------------------------------------------------------------------------
# Non-per-type registries — unchanged by refactor, pin them anyway.
# ---------------------------------------------------------------------------


def test_non_per_type_registries_shape_preserved():
    # These five constants were NOT folded into TypeDefinition — they
    # aren't keyed by record type. Their existing shapes must hold so
    # the refactor doesn't break consumers (instructor/daemon.py,
    # gcal_sync.py, scope.py, etc.).
    assert INSTRUCTION_FIELDS == (
        "alfred_instructions", "alfred_instructions_last",
    )
    assert REMINDER_FIELDS == (
        "remind_at", "reminded_at", "reminder_text",
    )
    assert EVENT_GCAL_FIELDS == (
        "gcal_event_id", "gcal_calendar", "gcal_keep_on_cancel", "gcal_title",
    )
    assert REQUIRED_FIELDS == ["type", "created"]
    assert "tags" in LIST_FIELDS
    # Instruction fields must also be in LIST_FIELDS — pinned in
    # ``tests/test_schema.py`` already, mirrored here so the registry
    # contract is self-contained.
    for f in INSTRUCTION_FIELDS:
        assert f in LIST_FIELDS


# ---------------------------------------------------------------------------
# Sentinel constant
# ---------------------------------------------------------------------------


def test_scope_canonical_sentinel_is_string():
    # ``SCOPE_CANONICAL`` is the sentinel value that marks
    # universally-available types in TypeDefinition.available_in_scopes.
    # Tests reference it; we pin the literal so future refactors don't
    # silently rename it (a rename would silently break every canonical
    # type's scope membership).
    assert SCOPE_CANONICAL == "canonical"
