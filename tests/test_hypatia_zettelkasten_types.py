"""Type registration tests — Hypatia Zettelkasten schema cutover (2026-05-16).

Phase 1 of the redesign per ``project_hypatia_zettelkasten_redesign.md``
"LOCKED IMPLEMENTATION PLAN". Five new Hypatia-only record types:
``memo``, ``zettel``, ``MOC``, ``question``, ``research-pointer``.

This file pins the schema + scope contracts:

    * Each type is registered in ``KNOWN_TYPES_HYPATIA`` AND
      ``HYPATIA_CREATE_TYPES`` (drift between the two would surface
      as "validator accepts, scope rejects" or vice versa).
    * Each type routes to its own top-level directory via
      ``TYPE_DIRECTORY``.
    * Status sets exist for ``zettel`` / ``question`` /
      ``research-pointer`` per the brief; ``memo`` and ``MOC``
      deliberately have NO entry (transient / organizational).
    * Body-mutation matrix: ``zettel`` / ``MOC`` / ``question`` /
      ``research-pointer`` are allowed for both ``body_insert_at`` AND
      ``body_replace`` under the ``hypatia`` scope. ``memo`` is denied
      for both (write-once-by-design).
    * The five types are HYPATIA-ONLY — Salem (talker / curator) and
      KAL-LE scopes still reject them.
    * End-to-end ``vault_create`` smoke test: Hypatia scope can create
      one of each new type; other scopes refuse.

The matrix-pin shape mirrors the existing
``tests/test_canonical_permissions.py`` / scope-rule pin tests so
contract-widening commits get caught at commit-time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.vault import ops, schema, scope


# --- KNOWN_TYPES_HYPATIA + HYPATIA_CREATE_TYPES drift check ---------------


_PHASE_1_NEW_TYPES: tuple[str, ...] = (
    "memo", "zettel", "MOC", "question", "research-pointer",
)


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_type_in_known_types_hypatia(rec_type: str) -> None:
    """Each Phase 1 type appears in the Hypatia validator allowlist."""
    assert rec_type in schema.KNOWN_TYPES_HYPATIA


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_type_in_hypatia_create_types(rec_type: str) -> None:
    """Each Phase 1 type appears in the Hypatia scope-create allowlist."""
    assert rec_type in scope.HYPATIA_CREATE_TYPES


def test_known_types_and_create_types_in_sync_for_phase_1() -> None:
    """The KNOWN_TYPES_HYPATIA ∩ HYPATIA_CREATE_TYPES set covers all five.

    Drift detection: if a future commit adds a type to one but forgets
    the other, this fires.
    """
    in_both = schema.KNOWN_TYPES_HYPATIA & scope.HYPATIA_CREATE_TYPES
    for rec_type in _PHASE_1_NEW_TYPES:
        assert rec_type in in_both, (
            f"Type '{rec_type}' missing from one of "
            f"KNOWN_TYPES_HYPATIA / HYPATIA_CREATE_TYPES — drift between "
            f"the two surfaces as inconsistent behaviour."
        )


# --- TYPE_DIRECTORY routing -----------------------------------------------


@pytest.mark.parametrize("rec_type,expected_dir", [
    ("memo",              "memo"),
    ("zettel",            "zettel"),
    ("MOC",               "MOC"),
    ("question",          "question"),
    ("research-pointer",  "research-pointer"),
])
def test_phase_1_type_directory_routing(
    rec_type: str, expected_dir: str,
) -> None:
    """Each new type routes to a top-level directory of the same name."""
    assert schema.TYPE_DIRECTORY[rec_type] == expected_dir


def test_moc_preserves_mixed_case() -> None:
    """``MOC`` directory is mixed-case per Andrew's existing convention.

    Anti-regression: a "normalize all type names to lowercase" sweep
    would silently break ``MOC/Practical Stoicism MOC.md`` etc.
    """
    assert schema.TYPE_DIRECTORY["MOC"] == "MOC"
    assert "MOC" in schema.KNOWN_TYPES_HYPATIA
    # Lowercase ``moc`` is NOT a registered type.
    assert "moc" not in schema.KNOWN_TYPES_HYPATIA


# --- STATUS_BY_TYPE -------------------------------------------------------


def test_zettel_status_set_is_loose_three_value() -> None:
    """``zettel`` carries open/refined/superseded per the brief."""
    assert schema.STATUS_BY_TYPE["zettel"] == {
        "open", "refined", "superseded",
    }


def test_question_status_set() -> None:
    """``question`` lifecycle: open/refined/answered/superseded."""
    assert schema.STATUS_BY_TYPE["question"] == {
        "open", "refined", "answered", "superseded",
    }


def test_research_pointer_status_set() -> None:
    """``research-pointer`` lifecycle: open/in-progress/completed/dropped."""
    assert schema.STATUS_BY_TYPE["research-pointer"] == {
        "open", "in-progress", "completed", "dropped",
    }


def test_memo_has_no_status_set() -> None:
    """``memo`` has no status entry — transient lifecycle is implicit.

    Per the brief: "memo carries no status frontmatter". The schema
    contract here is that ``_validate_status`` returns silently for
    types absent from STATUS_BY_TYPE, so memo records can omit
    ``status`` from frontmatter entirely.
    """
    assert "memo" not in schema.STATUS_BY_TYPE


def test_moc_has_no_status_set() -> None:
    """``MOC`` has no status entry — organizational artifact only."""
    assert "MOC" not in schema.STATUS_BY_TYPE


# --- KNOWN_TYPES_BY_SCOPE union -------------------------------------------


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_types_admitted_to_hypatia_scope_union(rec_type: str) -> None:
    """The schema's per-scope union admits each new type for Hypatia."""
    hypatia_union = schema.KNOWN_TYPES_BY_SCOPE["hypatia"]
    assert rec_type in hypatia_union


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_types_NOT_in_kalle_scope_union(rec_type: str) -> None:
    """KAL-LE scope union does NOT admit the Phase 1 Hypatia types.

    These five are Hypatia-only. KAL-LE captures don't exist (KAL-LE
    uses surveyor instead); the types are semantically meaningless
    in aftermath-lab.
    """
    kalle_union = schema.KNOWN_TYPES_BY_SCOPE["kalle"]
    assert rec_type not in kalle_union


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_types_NOT_in_canonical_known_types(rec_type: str) -> None:
    """Canonical ``KNOWN_TYPES`` (Salem's operational set) does NOT
    contain these — they're Hypatia-only."""
    assert rec_type not in schema.KNOWN_TYPES


# --- Hypatia create-scope acceptance --------------------------------------


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_hypatia_scope_can_create_phase_1_types(rec_type: str) -> None:
    """The ``hypatia`` scope's ``hypatia_types_only`` check admits each."""
    # No raise = pass. Pre-canonical-guard, the check sees the type
    # is in HYPATIA_CREATE_TYPES and returns.
    scope.check_scope(
        scope="hypatia",
        operation="create",
        record_type=rec_type,
    )


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_talker_scope_refuses_phase_1_types(rec_type: str) -> None:
    """Salem (talker) scope refuses to create Hypatia-only types.

    Today's call: Salem's capture-extract still routes through
    ``talker`` scope to produce ``note/`` records. If a future regression
    accidentally targets ``zettel`` etc. from Salem, this catches it.
    """
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="talker",
            operation="create",
            record_type=rec_type,
        )


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_kalle_scope_refuses_phase_1_types(rec_type: str) -> None:
    """KAL-LE scope refuses these — pattern/principle/architecture
    are KAL-LE's set; the Hypatia Zettelkasten types are out-of-domain."""
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="kalle",
            operation="create",
            record_type=rec_type,
        )


# --- Body-mutation matrix pin (per-instance × per-type) --------------------


_HYPATIA_BODY_MUTATE_NEW_TYPES: tuple[str, ...] = (
    "zettel", "MOC", "question", "research-pointer",
)


@pytest.mark.parametrize("rec_type", _HYPATIA_BODY_MUTATE_NEW_TYPES)
def test_hypatia_body_insert_at_allows_new_types(rec_type: str) -> None:
    """zettel/MOC/question/research-pointer all permit anchored mid-doc
    insertion under hypatia scope.

    The rationale: each type evolves over time (zettel Notes accrue,
    MOC Contents tree grows, question Exploration fills, research-pointer
    Notes accumulate). Anchored insertion is the right surface.
    """
    scope.check_scope(
        scope="hypatia",
        operation="body_insert_at",
        record_type=rec_type,
    )


@pytest.mark.parametrize("rec_type", _HYPATIA_BODY_MUTATE_NEW_TYPES)
def test_hypatia_body_replace_allows_new_types(rec_type: str) -> None:
    """Same four types also permit body_replace under hypatia scope.

    Operator-driven workflows (refining a zettel's Premise + Notes
    together; restructuring a MOC's Contents tree) legitimately need
    full-body rewrites. Curated documents — not history records.
    """
    scope.check_scope(
        scope="hypatia",
        operation="body_replace",
        record_type=rec_type,
    )


def test_hypatia_body_insert_at_refuses_memo() -> None:
    """``memo`` is write-once-by-design — body_insert_at denied.

    Per the brief: "memo records are atomic single-thought captures.
    If a memo needs more substance, promote to a zettel (new record,
    not body rewrite)."
    """
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="hypatia",
            operation="body_insert_at",
            record_type="memo",
        )


def test_hypatia_body_replace_refuses_memo() -> None:
    """Same — ``memo`` body_replace denied."""
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="hypatia",
            operation="body_replace",
            record_type="memo",
        )


# --- Allowlist-shape pin (catches silent matrix widening) -----------------


_EXPECTED_HYPATIA_INSERT_AT_KEYS: set[str] = {
    # Pre-existing (do NOT modify in this commit cycle).
    "note", "concept", "document", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    "practice-session",
    # Phase 1 additions (2026-05-16, Zettelkasten cutover).
    "zettel", "MOC", "question", "research-pointer",
    # Article co-write scope extension (2026-05-17). Andrew ratified
    # Option B: Hypatia is a true co-writer on articles, not append-
    # only. Operator-on-request mid-doc inserts on article records
    # are now scope-allowed.
    "article",
}


def test_hypatia_allow_body_insert_at_matrix_pin() -> None:
    """Pin the Hypatia body_insert_at allowlist exactly.

    Any change here triggers an explicit decision: either the new key
    is intentional (update this set) or it's an accidental widening
    (revert). Mirrors the contract-pin pattern noted in builder.md
    pre-commit checklist item #6.
    """
    rules = scope.SCOPE_RULES["hypatia"]
    actual_keys = set(rules["allow_body_insert_at"].keys())  # type: ignore[union-attr]
    assert actual_keys == _EXPECTED_HYPATIA_INSERT_AT_KEYS


_EXPECTED_HYPATIA_REPLACE_KEYS: set[str] = {
    # Pre-existing (do NOT modify in this commit cycle).
    "note", "concept", "document", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    "voice", "voice-cluster", "method",
    # Phase 1 additions (2026-05-16, Zettelkasten cutover).
    "zettel", "MOC", "question", "research-pointer",
    # Article co-write scope extension (2026-05-17). Mirror of the
    # insert_at entry above — full-Part rewrites on operator request.
    "article",
    # practice-session deliberately OMITTED — history-preservation.
    # memo deliberately OMITTED — write-once-by-design.
    # essay / source / author NOT in replace allowlist — write-once
    # raw records or operator-renames-only.
}


def test_hypatia_allow_body_replace_matrix_pin() -> None:
    """Pin the Hypatia body_replace allowlist exactly."""
    rules = scope.SCOPE_RULES["hypatia"]
    actual_keys = set(rules["allow_body_replace"].keys())  # type: ignore[union-attr]
    assert actual_keys == _EXPECTED_HYPATIA_REPLACE_KEYS


# --- End-to-end vault_create smoke ----------------------------------------
#
# Mirror the existing per-type fixture pattern (see test_capture_source_anchor.py)
# — write a minimal Hypatia-shaped vault, fire vault_create, confirm the
# record lands at the expected path with the right type frontmatter.


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in (
        "memo", "zettel", "MOC", "question", "research-pointer",
        "source", "author", "note",
    ):
        (vault / sub).mkdir(parents=True)
    return vault


@pytest.mark.parametrize("rec_type,name", [
    ("memo",             "Quick Thought About Hypatia Schema"),
    ("zettel",           "Atomic Note On Stoicism Origins"),
    ("MOC",              "Practical Stoicism MOC"),
    ("question",         "What is the relationship between zettel and source"),
    ("research-pointer", "Look up Hadot on Stoic practice"),
])
def test_vault_create_phase_1_type_succeeds_under_hypatia(
    tmp_path: Path, rec_type: str, name: str,
) -> None:
    """End-to-end: vault_create with each new type under hypatia scope
    writes a record to the right directory with the right type field."""
    vault = _make_vault(tmp_path)
    result = ops.vault_create(
        vault, rec_type, name, scope="hypatia",
    )
    # Path lands at ``<type>/<name>.md``.
    expected_path = f"{rec_type}/{name}.md"
    assert result["path"] == expected_path
    assert (vault / expected_path).exists()

    # Frontmatter records the right type.
    rec = ops.vault_read(vault, expected_path)
    assert rec["frontmatter"]["type"] == rec_type
    assert rec["frontmatter"]["name"] == name


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_vault_create_phase_1_type_refused_unscoped(
    tmp_path: Path, rec_type: str,
) -> None:
    """Without scope kwarg, ``_validate_type`` rejects the type because
    the canonical KNOWN_TYPES doesn't contain Hypatia-only types.

    Unscoped CLI calls (no ``--scope`` flag) consult only the canonical
    set; the per-scope unions are gated by passing the scope kwarg.
    """
    vault = _make_vault(tmp_path)
    with pytest.raises(ops.VaultError) as exc_info:
        ops.vault_create(vault, rec_type, "X")
    assert "Unknown type" in str(exc_info.value)


# --- NAME_FIELD_BY_TYPE default behaviour ---------------------------------


@pytest.mark.parametrize("rec_type", _PHASE_1_NEW_TYPES)
def test_phase_1_types_use_default_name_field(rec_type: str) -> None:
    """All five new types use ``name`` as the title field.

    NAME_FIELD_BY_TYPE only carries explicit OVERRIDES (conversation
    → subject, input → subject). Anything absent defaults to ``name``
    via ``NAME_FIELD_BY_TYPE.get(record_type, "name")`` in ops/retype.
    Confirming no entry was accidentally added.
    """
    assert rec_type not in schema.NAME_FIELD_BY_TYPE
