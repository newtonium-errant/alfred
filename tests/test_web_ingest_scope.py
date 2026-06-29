"""Tests for the ``web_ingest`` scope + schema auto-derive (2026-06-29).

Cross-instance verbatim document ingest (BUILD_DECISIONS.md): the
peer-token-gated ``POST /vault/ingest`` transport route writes {document,
note, source} records into the target instance's vault under a NEW
``web_ingest`` scope.

Coverage (mandatory regression pins, run unconditionally — NO
``pytest.importorskip``):
    * WEB_INGEST_CREATE_TYPES contract pin (the universal matrix).
    * ``web_ingest`` scope allows create of {document, note, source};
      denies edit / move / delete; denies a non-ingest type; fail-closed
      on empty record_type.
    * Both vault gates agree — ``_validate_type`` accepts document / source
      under ``web_ingest`` (the auto-derive literal-reversion catch);
      ``note`` rides SCOPE_CANONICAL.
"""

from __future__ import annotations

import pytest

from alfred.vault import schema
from alfred.vault.ops import _validate_type
from alfred.vault.scope import (
    WEB_INGEST_CREATE_TYPES,
    ScopeError,
    check_scope,
)

WEB_INGEST_TYPES = ("document", "note", "source")


# ---------------------------------------------------------------------------
# Contract pin — the universal create matrix
# ---------------------------------------------------------------------------


def test_web_ingest_create_types_matrix_pin():
    """CONTRACT PIN (BUILD_DECISIONS Decision B): the universal set is
    {document, note, source}. Widening it is a deliberate matrix change —
    update this pin in the same commit (pre-commit checklist #6)."""
    assert WEB_INGEST_CREATE_TYPES == {"document", "note", "source"}


# ---------------------------------------------------------------------------
# Scope gate — create / edit / move / delete
# ---------------------------------------------------------------------------


def test_web_ingest_allows_read_search_list_context():
    check_scope("web_ingest", "read")
    check_scope("web_ingest", "search")
    check_scope("web_ingest", "list")
    check_scope("web_ingest", "context")


@pytest.mark.parametrize("rec_type", WEB_INGEST_TYPES)
def test_web_ingest_create_allows_universal_types(rec_type: str):
    check_scope("web_ingest", "create", record_type=rec_type)


@pytest.mark.parametrize("rec_type", WEB_INGEST_TYPES)
def test_web_ingest_create_allows_body_writes(rec_type: str):
    # The verbatim body IS the payload — body writes must be permitted.
    check_scope("web_ingest", "create", record_type=rec_type, body_write=True)


def test_web_ingest_denies_non_ingest_type():
    for t in ("task", "person", "decision", "pattern", "event"):
        with pytest.raises(ScopeError):
            check_scope("web_ingest", "create", record_type=t)


def test_web_ingest_create_fail_closed_on_empty_type():
    # An empty record_type is a caller bug, not a licence to create any
    # type — fail closed.
    with pytest.raises(ScopeError):
        check_scope("web_ingest", "create", record_type="")


def test_web_ingest_denies_edit_move_delete():
    # Ingest is create-once: mutation / relocation / destruction are OUT.
    for op in ("edit", "move", "delete"):
        with pytest.raises(ScopeError):
            check_scope("web_ingest", op, record_type="document")


def test_web_ingest_denies_body_mutation_tools():
    # body_insert_at + body_replace deny-all under web_ingest.
    for op in ("body_insert_at", "body_replace"):
        with pytest.raises(ScopeError):
            check_scope("web_ingest", op, record_type="document")


# ---------------------------------------------------------------------------
# Gate 1 — _validate_type auto-derives from available_in_scopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rec_type", WEB_INGEST_TYPES)
def test_validate_type_accepts_web_ingest_types(rec_type: str):
    """Gate 1 must admit document / source under web_ingest (auto-derive);
    note rides SCOPE_CANONICAL. A literal-reversion of KNOWN_TYPES_BY_SCOPE
    would reject document/source here even though gate 2 allows them."""
    _validate_type(rec_type, scope="web_ingest")


def test_known_types_by_scope_auto_derives_web_ingest():
    """The KNOWN_TYPES_BY_SCOPE['web_ingest'] view auto-derives from the
    registry tags — not a hardcoded literal (VERA-P1 trap class)."""
    web_ingest_known = schema.KNOWN_TYPES_BY_SCOPE["web_ingest"]
    assert "document" in web_ingest_known
    assert "source" in web_ingest_known
    assert "note" in web_ingest_known  # SCOPE_CANONICAL


def test_document_source_tagged_web_ingest_in_registry():
    """The schema tags are the single source of truth gate 1 derives from."""
    assert "web_ingest" in schema.TYPE_REGISTRY.get("document").available_in_scopes
    assert "web_ingest" in schema.TYPE_REGISTRY.get("source").available_in_scopes
