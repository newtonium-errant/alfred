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
