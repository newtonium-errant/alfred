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
    """Operational types (task, event, project) are Salem's territory."""
    for t in ("task", "event", "project"):
        with pytest.raises(ScopeError) as exc_info:
            check_scope("hypatia", "create", record_type=t)
        assert "hypatia types" in str(exc_info.value).lower()


def test_hypatia_scope_create_denies_kalle_types() -> None:
    """KAL-LE-only types (pattern, principle) don't leak into Hypatia."""
    for t in ("pattern", "principle"):
        with pytest.raises(ScopeError):
            check_scope("hypatia", "create", record_type=t)


def test_hypatia_scope_still_rejects_org_and_location() -> None:
    """Per-instance leak guard: 2026-04-25 widened Salem only.

    ``org`` and ``location`` were added to ``TALKER_CREATE_TYPES`` so
    Salem stops hitting the scope wall when Andrew names a new
    business or address mid-conversation. Hypatia operates on the
    library-alexandria vault — entity records aren't her territory.
    Confirm the new types didn't leak across instances.
    """
    for t in ("org", "location"):
        with pytest.raises(ScopeError, match="hypatia types"):
            check_scope("hypatia", "create", record_type=t)


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
    """Pin the exact set so a quiet edit can't widen the surface."""
    assert HYPATIA_CREATE_TYPES == {
        "document", "session", "concept", "note",
        "source", "citation", "template",
    }


# ---------------------------------------------------------------------------
# Schema: KNOWN_TYPES_HYPATIA
# ---------------------------------------------------------------------------


def test_known_types_hypatia_is_separate_set() -> None:
    """Hypatia-only types are NOT in Salem's core KNOWN_TYPES."""
    assert schema.KNOWN_TYPES_HYPATIA == {
        "document", "concept", "source", "citation", "template",
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
    """
    assert "hypatia" in VAULT_TOOLS_BY_SET
    # Phase 1: identical to talker's tool set.
    assert VAULT_TOOLS_BY_SET["hypatia"] is TALKER_VAULT_TOOLS


def test_tools_for_set_hypatia_returns_four_vault_tools() -> None:
    """The four vault tools — search, read, create, edit — are exposed."""
    tools = tools_for_set("hypatia")
    names = {t["name"] for t in tools}
    assert names == {"vault_search", "vault_read", "vault_create", "vault_edit"}
    # Hypatia must NOT have bash_exec — that's KAL-LE only.
    assert "bash_exec" not in names


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
