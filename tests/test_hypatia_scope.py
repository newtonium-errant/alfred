"""Tests for the Hypatia scope + tool-set additions.

Hypatia is the scholar/scribe instance operating on the
``library-alexandria`` vault. This module covers the contract pieces a
later code-reviewer pass might silently regress:

* The ``hypatia`` scope entry in ``vault/scope.py`` mirrors curator's
  "create + edit, never delete" shape and only allows the seven Hypatia
  record types per ``library-alexandria/CLAUDE.md``.
* The ``"hypatia"`` key is registered in ``VAULT_TOOLS_BY_SET`` so a
  ``tool_set: "hypatia"`` config entry doesn't silently fall back to the
  talker set (the unknown-set fallback is a debugging trap, not a
  feature).
* The Hypatia known-types schema constant exists and stays separate from
  Salem's ``KNOWN_TYPES``.
"""

from __future__ import annotations

import pytest

from alfred.telegram.conversation import (
    TALKER_VAULT_TOOLS,
    VAULT_TOOLS_BY_SET,
    tools_for_set,
)
from alfred.vault import schema
from alfred.vault.ops import VaultError, vault_create
from alfred.vault.scope import (
    HYPATIA_CREATE_TYPES,
    ScopeError,
    check_scope,
)


# ---------------------------------------------------------------------------
# Scope: hypatia
# ---------------------------------------------------------------------------


def test_hypatia_scope_allows_read_search_list_context() -> None:
    check_scope("hypatia", "read")
    check_scope("hypatia", "search")
    check_scope("hypatia", "list")
    check_scope("hypatia", "context")


def test_hypatia_scope_denies_delete() -> None:
    """Hypatia mirrors curator's no-delete policy — additive only."""
    with pytest.raises(ScopeError) as exc_info:
        check_scope("hypatia", "delete")
    assert "delete" in str(exc_info.value).lower()


def test_hypatia_scope_denies_move() -> None:
    """Phase 1: move stays denied; Andrew reorganises by hand."""
    with pytest.raises(ScopeError):
        check_scope("hypatia", "move")


def test_hypatia_scope_create_allows_each_hypatia_type() -> None:
    """All seven library-alexandria types pass the create gate."""
    for t in ("document", "session", "concept", "note",
              "source", "citation", "template"):
        check_scope("hypatia", "create", record_type=t)


def test_hypatia_scope_create_denies_salem_types() -> None:
    """Operational types are Salem's territory.

    Phase A inter-instance comms differentiates the rejection messages:
      * ``event`` is canonical → "propose_event" suggestion.
      * ``task`` and ``project`` are non-canonical Salem types → still
        emit the generic "hypatia types" message.
    """
    # Canonical event → propose suggestion.
    with pytest.raises(ScopeError, match="propose_event"):
        check_scope("hypatia", "create", record_type="event")
    # Non-canonical Salem types → generic hypatia-types rejection.
    for t in ("task", "project"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope("hypatia", "create", record_type=t)
        assert "hypatia types" in str(exc_info.value).lower()


def test_hypatia_scope_create_denies_kalle_types() -> None:
    """KAL-LE-only types (pattern, principle) don't leak into Hypatia."""
    for t in ("pattern", "principle"):
        with pytest.raises(ScopeError):
            check_scope("hypatia", "create", record_type=t)


def test_hypatia_scope_still_rejects_org_and_location() -> None:
    """Per-instance leak guard: ``org`` / ``location`` are canonical → propose.

    Phase A inter-instance comms (2026-05-01): both types are now
    canonical-authority records routed through Salem via
    ``propose_org`` / ``propose_location`` tools rather than local
    create. The rejection still fires; the message shape changed.
    """
    with pytest.raises(ScopeError, match="propose_org"):
        check_scope("hypatia", "create", record_type="org")
    with pytest.raises(ScopeError, match="propose_location"):
        check_scope("hypatia", "create", record_type="location")


def test_hypatia_scope_edit_permitted_with_no_fields_check() -> None:
    """``edit: True`` (not a field allowlist) — passes without fields arg."""
    check_scope("hypatia", "edit")


def test_hypatia_scope_body_writes_permitted() -> None:
    """Drafting essays + concept notes is the whole point — bodies must work."""
    check_scope("hypatia", "edit", body_write=True)
    check_scope(
        "hypatia", "create", record_type="document", body_write=True,
    )


def test_hypatia_create_types_shape() -> None:
    """Pin the exact set so a quiet edit can't widen the surface.

    Phase 2.5 fiction posture (``project_hypatia_phase2_followups.md``)
    added six ``fiction-{element}`` types so the natural-language
    scaffolding path can call ``vault_create`` for fiction records —
    matches the schema-side ``KNOWN_TYPES_HYPATIA`` set. Both
    registries must list the same fiction types or the gates will
    disagree.

    2026-05-06 added ``practice-session`` (cross-domain practice
    logging — DJ / fencing / workout / language) to close the gap
    surfaced in Hypatia conversation ``833bec8d``.
    """
    assert HYPATIA_CREATE_TYPES == {
        "document", "session", "concept", "note",
        "source", "citation", "template",
        # Phase 2.5 fiction posture
        "fiction-continuity", "fiction-story", "fiction-structure",
        "fiction-world", "fiction-voice", "fiction-character",
        # 2026-05-06 practice logging
        "practice-session",
        # 2026-05-07 voice/method training (/train + /method-source arc).
        # Four new top-level types: ``essay`` is the raw published-essay
        # leaf, ``voice`` is the structured voice profile, ``voice-cluster``
        # is the cluster-tier aggregate, ``method`` is the structured
        # method/system profile. Per CLAUDE.md scope-first design — pin
        # the matrix here so widening it later is a deliberate edit.
        "essay", "voice", "voice-cluster", "method",
        # 2026-05-16 capture-source-anchor arc — ``author`` type indexes
        # works by author (filename = lastname). Created via the capture-
        # mode opening-pattern resolver ("I'm reading X by Y"); explicit
        # operator workflows can also create them. Hypatia-only.
        "author",
        # 2026-05-16 Zettelkasten schema cutover (Phase 1). Five new
        # Hypatia-only types per project_hypatia_zettelkasten_redesign.md
        # "LOCKED IMPLEMENTATION PLAN": memo (fleeting captures),
        # zettel (atomic Zettelkasten records), MOC (Maps of Content),
        # question (elevated atomic questions), research-pointer
        # (elevated atomic research actions). Capture-mode multi-message
        # extraction now targets zettel/ instead of note/.
        "memo", "zettel", "MOC", "question", "research-pointer",
        # 2026-05-17 operator-template #1 ship — ``article`` records.
        # Distinct from ``essay`` (source essays Andrew reads, routed
        # to ``document/essay/``); ``article`` is essays Andrew WRITES
        # himself (Substack / Andrew Errant / future venues), routed
        # to ``article/``. Hypatia-only. Lifecycle:
        # draft → scheduled → published → archived.
        "article",
    }


# ---------------------------------------------------------------------------
# Schema: KNOWN_TYPES_HYPATIA
# ---------------------------------------------------------------------------


def test_known_types_hypatia_is_separate_set() -> None:
    """Hypatia-only types are NOT in Salem's core KNOWN_TYPES.

    Phase 2.5 fiction posture added six ``fiction-{element}`` types —
    these must live ONLY under Hypatia's set, never in Salem's
    operational KNOWN_TYPES (Salem doesn't write fiction projects).

    2026-05-06: ``practice-session`` added — Hypatia-only for now;
    Salem could conceivably want it for RRTS-related practice
    logging later, but the originating use case is Hypatia's
    skill-building domain.
    """
    assert schema.KNOWN_TYPES_HYPATIA == {
        "document", "concept", "source", "citation", "template",
        # Phase 2.5 fiction posture
        "fiction-continuity", "fiction-story", "fiction-structure",
        "fiction-world", "fiction-voice", "fiction-character",
        # 2026-05-06 practice logging
        "practice-session",
        # 2026-05-07 voice/method training types — keep in sync with
        # HYPATIA_CREATE_TYPES (the matching pinning test above).
        # Drift between the two sets surfaces as "type accepted by
        # validator, rejected by scope" or vice versa.
        "essay", "voice", "voice-cluster", "method",
        # 2026-05-16 capture-source-anchor arc — ``author`` type.
        # Hypatia-only; indexes works by author. Keep in sync with
        # HYPATIA_CREATE_TYPES above.
        "author",
        # 2026-05-16 Zettelkasten schema cutover (Phase 1). Same five
        # types as the HYPATIA_CREATE_TYPES pin above — keep in sync.
        "memo", "zettel", "MOC", "question", "research-pointer",
        # 2026-05-17 operator-template #1 ship — ``article`` (Substack
        # / Andrew Errant). Keep in sync with HYPATIA_CREATE_TYPES.
        "article",
    }
    for t in schema.KNOWN_TYPES_HYPATIA:
        assert t not in schema.KNOWN_TYPES, (
            f"{t!r} leaked into Salem's KNOWN_TYPES — keep Hypatia separate"
        )


# ---------------------------------------------------------------------------
# Tool registry — Fix 1
# ---------------------------------------------------------------------------


def test_hypatia_tool_set_registered_explicitly() -> None:
    """``"hypatia"`` is a real key in ``VAULT_TOOLS_BY_SET``.

    Without this entry, ``tools_for_set("hypatia")`` falls through to the
    talker default — the same answer, but a debugging trap. Future Phase 2
    divergence shows up as an explicit registry change, not a silent
    fall-through.

    Phase A inter-instance comms (2026-05-01) added the 5 peer tools to
    Hypatia's set, so the registry no longer aliases TALKER_VAULT_TOOLS
    by identity — it's now a distinct list ``HYPATIA_VAULT_TOOLS``.
    """
    assert "hypatia" in VAULT_TOOLS_BY_SET
    # The four talker vault tools are still present, plus 5 peer tools.
    talker_names = {t["name"] for t in TALKER_VAULT_TOOLS}
    hypatia_names = {t["name"] for t in VAULT_TOOLS_BY_SET["hypatia"]}
    assert talker_names.issubset(hypatia_names)


def test_tools_for_set_hypatia_returns_four_vault_tools() -> None:
    """The four vault tools — search, read, create, edit — are exposed.

    Phase A inter-instance comms also exposes the 5 peer tools
    (query_canonical + 4 propose_*) on top. Hypatia must NOT have
    bash_exec — that's KAL-LE only.
    """
    tools = tools_for_set("hypatia")
    names = {t["name"] for t in tools}
    assert {"vault_search", "vault_read", "vault_create", "vault_edit"}.issubset(names)
    assert "bash_exec" not in names
    # Phase A peer tools.
    assert {"query_canonical", "propose_person", "propose_org",
            "propose_location", "propose_event"}.issubset(names)


# ---------------------------------------------------------------------------
# vault_create end-to-end — release-blocker regression (P1 #2)
# ---------------------------------------------------------------------------
#
# Before the scope-aware ``_validate_type`` fix, ``vault_create`` rejected
# every Hypatia and KAL-LE extension type because ``_validate_type`` ran
# *before* ``check_scope`` and gated against the canonical ``KNOWN_TYPES``
# only. The brief's smoke-check (``alfred vault create document``,
# ``alfred vault create pattern``) hit "Unknown type: ..." against the
# 20-type Salem set — extension scopes never reached the scope-policy
# check. These tests pin the contract end-to-end so the gate-ordering
# doesn't silently regress when V.E.R.A. / STAY-C add their own
# extension type sets.


def test_vault_create_hypatia_document_succeeds(tmp_path) -> None:
    """Hypatia's ``document`` type passes both gates and writes the file."""
    (tmp_path / "document").mkdir()
    result = vault_create(
        tmp_path, "document", "Test Document", scope="hypatia",
    )
    assert result["path"] == "document/Test Document.md"
    assert (tmp_path / result["path"]).exists()


@pytest.mark.parametrize(
    "record_type",
    ["document", "concept", "source", "citation", "template"],
)
def test_vault_create_each_hypatia_type_succeeds(
    tmp_path, record_type: str,
) -> None:
    """All five Hypatia extension types pass ``_validate_type``."""
    (tmp_path / record_type).mkdir()
    result = vault_create(
        tmp_path, record_type, f"Test {record_type}", scope="hypatia",
    )
    assert (tmp_path / result["path"]).exists()


def test_vault_create_kalle_pattern_succeeds(tmp_path) -> None:
    """KAL-LE's ``pattern`` type passes ``_validate_type`` under scope='kalle'."""
    (tmp_path / "pattern").mkdir()
    result = vault_create(
        tmp_path, "pattern", "Test Pattern", scope="kalle",
    )
    assert (tmp_path / result["path"]).exists()


def test_vault_create_kalle_principle_succeeds(tmp_path) -> None:
    """KAL-LE's ``principle`` type passes ``_validate_type`` under scope='kalle'."""
    (tmp_path / "principle").mkdir()
    result = vault_create(
        tmp_path, "principle", "Test Principle", scope="kalle",
    )
    assert (tmp_path / result["path"]).exists()


def test_vault_create_hypatia_type_under_kalle_scope_fails(tmp_path) -> None:
    """Cross-scope leak: Hypatia's ``document`` is unknown under scope='kalle'.

    Under the kalle scope, ``_validate_type`` allows ``KNOWN_TYPES |
    KNOWN_TYPES_KALLE`` — ``document`` is in neither set. The error
    fires at the type gate, not at ``check_scope``'s allowlist; we
    assert on the type-error message to pin which gate caught it.
    """
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "document", "Test Document", scope="kalle",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "kalle" in str(exc_info.value)


def test_vault_create_kalle_type_under_hypatia_scope_fails(tmp_path) -> None:
    """Cross-scope leak: KAL-LE's ``pattern`` is unknown under scope='hypatia'."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "pattern", "Test Pattern", scope="hypatia",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "hypatia" in str(exc_info.value)


def test_vault_create_canonical_type_under_talker_scope_unaffected(
    tmp_path,
) -> None:
    """Salem regression guard: ``note`` under scope='talker' still works.

    The fix must not narrow the canonical-types behavior — every
    Salem-scope create must still pass ``_validate_type`` exactly as
    before.
    """
    (tmp_path / "note").mkdir()
    result = vault_create(
        tmp_path, "note", "Test Note", scope="talker",
    )
    assert (tmp_path / result["path"]).exists()


def test_vault_create_extension_type_without_scope_still_fails(
    tmp_path,
) -> None:
    """Default scope=None preserves canonical-only validation.

    A caller that doesn't propagate scope (e.g. a manual CLI invocation
    without ALFRED_VAULT_SCOPE set) gets the historical error so the
    extension types stay invisible until a scope opts them in.
    """
    with pytest.raises(VaultError) as exc_info:
        vault_create(tmp_path, "document", "Test Document")
    assert "Unknown type" in str(exc_info.value)
    # No scope hint — the error should look like the pre-fix message.
    assert "under scope" not in str(exc_info.value)


@pytest.mark.parametrize(
    "record_type",
    ["org", "location", "project", "constraint", "contradiction"],
)
def test_vault_create_each_new_talker_type_succeeds(
    tmp_path, record_type: str,
) -> None:
    """Talker-scope widening 2026-04-25: five new types succeed end-to-end.

    Salem repeatedly hit the scope wall on ``org`` and ``location`` when
    Andrew named a new business or address mid-conversation. ``project``,
    ``constraint``, and ``contradiction`` round out the kick-off +
    reflection surface. All five are canonical types — they pass
    ``_validate_type`` (no extension needed) and ``check_scope``'s
    ``talker_types_only`` allowlist (extended for this commit).
    """
    (tmp_path / record_type).mkdir()
    result = vault_create(
        tmp_path, record_type, f"Test {record_type}", scope="talker",
    )
    assert (tmp_path / result["path"]).exists()


def test_vault_create_canonical_type_under_hypatia_scope_works(
    tmp_path,
) -> None:
    """Hypatia may also create canonical types via the union (KNOWN_TYPES).

    ``KNOWN_TYPES_BY_SCOPE['hypatia']`` is a union, so a canonical type
    like ``note`` still validates under scope='hypatia'. Whether
    Hypatia's create allowlist actually permits it is a separate gate
    (``check_scope``'s ``hypatia_types_only``) — and ``note`` happens
    to be in HYPATIA_CREATE_TYPES too, so this case round-trips.
    """
    (tmp_path / "note").mkdir()
    result = vault_create(
        tmp_path, "note", "Test Note", scope="hypatia",
    )
    assert (tmp_path / result["path"]).exists()


# ---------------------------------------------------------------------------
# Phase 2.5 fiction posture — the six fiction-{element} types
# ---------------------------------------------------------------------------
#
# Both registries (KNOWN_TYPES_HYPATIA + HYPATIA_CREATE_TYPES) must
# accept the fiction types under scope='hypatia'. The slash-command
# scaffolding path doesn't go through vault_create — it writes files
# directly via Path.write_text — so the in-process slash command
# wouldn't catch a type-registration gap. The natural-language
# scaffolding path (Hypatia's SKILL → vault_create) requires both
# gates to pass for every element write. These tests pin the contract.


_FICTION_TYPES = [
    "fiction-continuity",
    "fiction-story",
    "fiction-structure",
    "fiction-world",
    "fiction-voice",
    "fiction-character",
]


@pytest.mark.parametrize("record_type", _FICTION_TYPES)
def test_validate_type_accepts_fiction_under_hypatia(record_type: str) -> None:
    """Schema gate: every fiction-{element} type passes under scope='hypatia'."""
    from alfred.vault.ops import _validate_type
    # Should NOT raise.
    _validate_type(record_type, scope="hypatia")


@pytest.mark.parametrize("record_type", _FICTION_TYPES)
def test_check_scope_accepts_fiction_create_under_hypatia(
    record_type: str,
) -> None:
    """Scope gate: ``check_scope("create", ...)`` accepts every
    fiction-{element} type under scope='hypatia'.

    The slash command writes files directly so it doesn't fire
    check_scope, but the SKILL natural-language path goes through
    ``vault_create(..., scope="hypatia")`` which fires both gates.
    """
    # Should NOT raise.
    check_scope("hypatia", "create", record_type=record_type)


@pytest.mark.parametrize("record_type", _FICTION_TYPES)
def test_vault_create_each_fiction_type_succeeds_under_hypatia(
    tmp_path, record_type: str,
) -> None:
    """End-to-end: vault_create lands a fiction-element record under
    scope='hypatia' (both gates pass + file actually writes)."""
    # NAME_FIELD_BY_TYPE doesn't list fiction-* explicitly, so the
    # type goes to TYPE_DIRECTORY's fallback ``record_type`` directory.
    # Pre-create the dir so the write doesn't fail on missing parent.
    (tmp_path / record_type).mkdir(exist_ok=True)
    result = vault_create(
        tmp_path,
        record_type,
        f"Test {record_type}",
        scope="hypatia",
    )
    assert (tmp_path / result["path"]).exists()


@pytest.mark.parametrize("record_type", _FICTION_TYPES)
def test_fiction_type_rejected_under_kalle_scope(
    tmp_path, record_type: str,
) -> None:
    """Cross-scope leak guard: KAL-LE must NOT be able to create
    fiction-element records. KAL-LE's vault is aftermath-lab, not
    library-alexandria — fiction belongs to Hypatia only.
    """
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, record_type, f"Test {record_type}", scope="kalle",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "kalle" in str(exc_info.value)


@pytest.mark.parametrize("record_type", _FICTION_TYPES)
def test_fiction_type_rejected_under_no_scope(
    tmp_path, record_type: str,
) -> None:
    """Default scope (None / Salem-only) must reject fiction-element
    types. Salem has no business writing fiction-{element} records;
    the Phase 2.5 contract puts these strictly under Hypatia.
    """
    with pytest.raises(VaultError) as exc_info:
        # No scope kwarg → falls through to canonical KNOWN_TYPES
        vault_create(tmp_path, record_type, f"Test {record_type}")
    assert "Unknown type" in str(exc_info.value)


# ---------------------------------------------------------------------------
# practice-session — cross-domain practice logging (2026-05-06)
# ---------------------------------------------------------------------------
#
# Hypatia-only record type for DJ / fencing / workout / language /
# other skill-building tracks. Distinct from the canonical ``session``
# type because practice-sessions link to a skill tracker / project +
# carry a domain field so progression aggregates over time.
#
# Filed 2026-05-04 from the DJ skill-building arc; surfaced again in
# Hypatia conversation ``833bec8d`` when Andrew looked for it and the
# type didn't exist yet.
#
# Cross-instance gating:
#   * Hypatia: create + edit allowed (both gates)
#   * Salem (talker scope): create REJECTED — operational types are
#     Salem's territory, but practice-session is a skill-building
#     domain artifact that lives under Hypatia
#   * KAL-LE: create REJECTED — coding instance, not skill-building


def test_practice_session_pinned_in_known_types_hypatia() -> None:
    """Schema gate: practice-session is registered in KNOWN_TYPES_HYPATIA."""
    assert "practice-session" in schema.KNOWN_TYPES_HYPATIA


def test_practice_session_not_in_salem_known_types() -> None:
    """Cross-instance leak guard: practice-session is Hypatia-only.
    Operator can extend later if Salem needs RRTS-related practice
    logging, but the originating use case is Hypatia's domain."""
    assert "practice-session" not in schema.KNOWN_TYPES


def test_practice_session_pinned_in_hypatia_create_types() -> None:
    """Scope gate: practice-session passes the Hypatia create allowlist."""
    assert "practice-session" in HYPATIA_CREATE_TYPES


def test_check_scope_accepts_practice_session_create_under_hypatia() -> None:
    """Scope gate: ``check_scope("create", ...)`` accepts
    practice-session under scope='hypatia'."""
    check_scope("hypatia", "create", record_type="practice-session")


def test_vault_create_practice_session_succeeds_under_hypatia(
    tmp_path,
) -> None:
    """End-to-end: vault_create lands a practice-session record under
    scope='hypatia' (both gates pass + file actually writes)."""
    (tmp_path / "practice-session").mkdir(exist_ok=True)
    result = vault_create(
        tmp_path,
        "practice-session",
        "Test Practice Session",
        scope="hypatia",
    )
    assert (tmp_path / result["path"]).exists()


def test_practice_session_rejected_under_kalle_scope(tmp_path) -> None:
    """KAL-LE must NOT be able to create practice-session records.
    KAL-LE is the coding instance; practice-session is skill-building."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "practice-session", "Test", scope="kalle",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "kalle" in str(exc_info.value)


def test_practice_session_rejected_under_no_scope(tmp_path) -> None:
    """Default scope (None / Salem-only) must reject practice-session.
    Per the matrix: Hypatia-only for now."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(tmp_path, "practice-session", "Test")
    assert "Unknown type" in str(exc_info.value)


def test_practice_session_status_set_pinned() -> None:
    """Pin the exact status set so a quiet edit can't widen / narrow it.
    Workflow: planned (scheduled), in_progress (mid-session, e.g. live
    update), completed (most common), skipped (intended-but-didn't —
    useful signal for the tracker aggregator)."""
    assert schema.STATUS_BY_TYPE["practice-session"] == {
        "planned", "in_progress", "completed", "skipped",
    }


def test_practice_session_in_type_directory() -> None:
    """Type → directory routing is registered."""
    assert schema.TYPE_DIRECTORY["practice-session"] == "practice-session"


def test_skills_practiced_in_list_fields() -> None:
    """Schema list-coercion gate: ``skills_practiced`` is a list-shaped
    field, so vault_create coerces a scalar string to ``[string]`` at
    write time. The other list-shaped fields on practice-session
    records (``related_persons`` / ``related_orgs`` /
    ``related_projects``) are ALREADY established list-shaped fields
    in the wild — every writer in the wild emits them as lists, so
    they don't need coerce-from-scalar handling. ``skills_practiced``
    is genuinely new — operators may type a single skill as a string."""
    assert "skills_practiced" in schema.LIST_FIELDS


# ---------------------------------------------------------------------------
# Template → prose-templates routing (latent orphan-path fix, 2026-05-12)
# ---------------------------------------------------------------------------
#
# Hypatia's ``template`` type is in both ``KNOWN_TYPES_HYPATIA`` (schema
# layer) and ``HYPATIA_CREATE_TYPES`` (scope layer), but for a window
# between 2026-05-12 ``a14e0ab`` (SKILL rename ``template/`` →
# ``prose-templates/``) and the fix below, ``TYPE_DIRECTORY`` had no
# entry — so the ``.get(record_type, record_type)`` fallback in
# ``vault_create`` (ops.py:763) routed writes to the now-empty
# ``template/`` ORPHAN directory instead of the canonical
# ``prose-templates/``. Mitigated socially by the SKILL's "Don't create
# new templates speculatively" line but latent — explicit invocation
# would mis-route. These tests pin the routing contract so a future
# accidental removal of the TYPE_DIRECTORY entry reverts the orphan-
# routing bug noisily instead of silently.


def test_template_type_directory_routes_to_prose_templates() -> None:
    """Direct schema pin — the regression-trigger that catches a removed
    ``TYPE_DIRECTORY`` entry. If this assertion ever fires, the fallback
    is silently routing back to ``template/`` (now an orphan dir post-
    2026-05-12 ``a14e0ab``). Failure here always means orphan routing
    has returned."""
    assert schema.TYPE_DIRECTORY["template"] == "prose-templates"


def test_template_still_in_known_types_hypatia() -> None:
    """Allowlist regression-pin: removing ``template`` from
    ``KNOWN_TYPES_HYPATIA`` would break the SKILL flow that creates
    operator-curated prose scaffolds. The earlier broader assertion
    (``test_known_types_hypatia_is_separate_set``) already covers this,
    but a dedicated test makes the contract explicit so a "trim unused
    types" pass doesn't accidentally yank it."""
    assert "template" in schema.KNOWN_TYPES_HYPATIA


def test_template_still_in_hypatia_create_types() -> None:
    """Scope-layer regression-pin: same shape as the schema-layer pin
    above. Scope and schema must both keep ``template`` registered or
    the gate-ordering will surface the orphan asymmetry as confusing
    error messages."""
    assert "template" in HYPATIA_CREATE_TYPES


def test_vault_create_template_under_hypatia_routes_to_prose_templates(
    tmp_path,
) -> None:
    """End-to-end: ``vault_create type=template scope=hypatia`` lands the
    file at ``prose-templates/<name>.md``, NOT ``template/<name>.md``.

    This is the canonical test for the 2026-05-12 fix — it would have
    failed BEFORE the ``TYPE_DIRECTORY`` entry was added (record would
    have landed at ``template/Test prose form.md`` per the fallback).
    """
    result = vault_create(
        tmp_path, "template", "Test prose form", scope="hypatia",
    )
    assert result["path"] == "prose-templates/Test prose form.md"
    assert (tmp_path / "prose-templates" / "Test prose form.md").exists()
    # The orphan dir MUST NOT have been created — if it has, the
    # fallback routing has returned silently.
    assert not (tmp_path / "template" / "Test prose form.md").exists()


def test_vault_create_template_under_salem_scope_rejected(tmp_path) -> None:
    """Cross-instance correctness: Salem (canonical scope) MUST NOT be
    able to create ``template`` records — the type isn't in
    ``KNOWN_TYPES``. Rejected at ``_validate_type`` (the first gate),
    not at the create allowlist (the second gate). Pinning here so a
    future "merge KNOWN_TYPES sets" refactor doesn't silently widen
    Salem's reach.

    No ``scope`` kwarg → validates against the canonical
    ``KNOWN_TYPES`` set, which does NOT contain ``template``."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(tmp_path, "template", "Should fail")
    assert "Unknown type" in str(exc_info.value)


def test_vault_create_template_under_kalle_scope_rejected(tmp_path) -> None:
    """KAL-LE scope is bounded by ``KNOWN_TYPES | KNOWN_TYPES_KALLE``;
    ``template`` is in neither set. Rejected at ``_validate_type``. Same
    pin shape as the Salem test above — guards the per-instance allowlist
    contract so a refactor that accidentally aliased Hypatia's types
    into KAL-LE's set would surface here."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            tmp_path, "template", "Should fail", scope="kalle",
        )
    assert "Unknown type" in str(exc_info.value)
    assert "kalle" in str(exc_info.value)
