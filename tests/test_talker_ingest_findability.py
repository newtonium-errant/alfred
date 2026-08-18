"""Web-ingested records must be FINDABLE from the talker (2026-08-18).

Operator-hit: a `document` written by the web ingest could be read by path and
matched by grep/glob, but `vault list document` died on gate 1 with "Unknown
type: 'document' under scope 'talker'" — the record existed and could be
opened, but could not be ENUMERATED, so the talker's own natural move for
"what documents do I have" returned an error instead of the file.

The fix tags `talker` onto the `document` / `source` TypeDefinitions'
`available_in_scopes`, which opens GATE 1 only. Gate 1 (`_validate_type`) has
exactly two call sites — `vault_list` and `vault_create` — so these pins come
in matched pairs: the read side must open AND the write side must not move.
An "it lists now" assertion on its own would pass identically against a build
that had made `document` canonical for every scope.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alfred.vault import ops as vault_ops
from alfred.vault import schema
from alfred.vault.ops import VaultError
from alfred.vault.scope import ScopeError, check_scope

TALKER = "talker"


@pytest.fixture()
def ingested_vault(tmp_path: Path) -> Path:
    """A vault holding the shapes the web ingest actually writes."""
    (tmp_path / "document").mkdir()
    (tmp_path / "source").mkdir()
    (tmp_path / "document" / "RBC Statement.md").write_text(
        "---\ntype: document\nname: RBC Statement\nstatus: active\n"
        "ingested_from: web\n---\n\n# RBC Statement\n",
        encoding="utf-8",
    )
    (tmp_path / "source" / "Bank Portal.md").write_text(
        "---\ntype: source\nname: Bank Portal\nstatus: active\n---\n\n# Bank Portal\n",
        encoding="utf-8",
    )
    return tmp_path


# --- the read side: what the operator was denied ----------------------------


def test_talker_lists_the_web_ingested_document(ingested_vault: Path) -> None:
    """THE BUG. Pre-fix this raised VaultError at gate 1."""
    records = vault_ops.vault_list(ingested_vault, "document", scope=TALKER)

    assert [r["path"] for r in records] == ["document/RBC Statement.md"]
    assert records[0]["name"] == "RBC Statement"


def test_talker_lists_the_web_ingested_source(ingested_vault: Path) -> None:
    records = vault_ops.vault_list(ingested_vault, "source", scope=TALKER)

    assert [r["name"] for r in records] == ["Bank Portal"]


# --- the write side: the paired control that makes the above non-vacuous ----


def test_talker_still_cannot_create_document_or_source(ingested_vault: Path) -> None:
    """Gate 2 (`TALKER_CREATE_TYPES`) remains the authorship ceiling.

    This is the half that distinguishes "findability opened" from "the type
    became canonical for everyone". Without it, the list pins above would be
    green against a far broader change than the one intended.

    It asserts WHICH GATE refuses, not merely that something did — and that
    distinction is why this test REDS (correctly) if the `talker` tag is
    removed. Untagged, the create is still refused, but by gate 1 with
    `VaultError: Unknown type` rather than by gate 2's policy. Both refuse;
    only one of them means "authorship is bounded by policy". A pin that
    accepted any refusal could not tell those apart, and would report a
    healthy ceiling on a build where the ceiling was really just the type
    registry not knowing the word.
    """
    for record_type in ("document", "source"):
        with pytest.raises(ScopeError) as excinfo:
            vault_ops.vault_create(
                ingested_vault, record_type, "Should Not Exist",
                set_fields={"name": "Should Not Exist"}, scope=TALKER,
            )
        assert "can only create talker types" in str(excinfo.value)

    # And nothing landed on disk.
    assert not (ingested_vault / "document" / "Should Not Exist.md").exists()
    assert not (ingested_vault / "source" / "Should Not Exist.md").exists()


def test_talker_still_cannot_delete_or_body_mutate_a_document() -> None:
    """Delete and the body-mutation verbs stay refused by their own gate-2
    rules — the tag did not reach them."""
    with pytest.raises(ScopeError):
        check_scope(TALKER, "delete", rel_path="document/RBC Statement.md",
                    record_type="document")

    for body_op in ("insert_at", "replace"):
        with pytest.raises(ScopeError) as excinfo:
            check_scope(
                TALKER, f"body_{body_op}", rel_path="document/RBC Statement.md",
                record_type="document", body_write=True, body_op=body_op,
            )
        assert "document" in str(excinfo.value)


# --- scoping: the fix is per-scope, not a global unlock ---------------------


def test_a_scope_without_the_tag_still_cannot_list_documents(
    ingested_vault: Path,
) -> None:
    """The nearest INADMISSIBLE neighbour. `curator` was never tagged for
    `document`, so it must still be refused at gate 1 — proving the tag is
    scoped rather than having made the type universally valid."""
    with pytest.raises(VaultError) as excinfo:
        vault_ops.vault_list(ingested_vault, "document", scope="curator")

    assert "Unknown type" in str(excinfo.value)


# --- the derived view -------------------------------------------------------


def test_talker_union_auto_derives_and_only_widens() -> None:
    """`KNOWN_TYPES_BY_SCOPE['talker']` is a DERIVED view, and adding the tag
    widens it without dropping anything.

    Before this change `talker` was not a key at all, so `_validate_type` took
    its `.get(scope, KNOWN_TYPES)` fallback to the canonical set. Now that the
    key exists, the fallback no longer applies to talker — so the superset
    assertion is load-bearing rather than decorative: if the union ever failed
    to contain canonical, every previously-valid talker type would break at
    once.
    """
    union = schema.KNOWN_TYPES_BY_SCOPE["talker"]

    assert {"document", "source"} <= union
    assert schema.KNOWN_TYPES <= union  # nothing the talker could name is lost
    assert union == set(schema.TYPE_REGISTRY.known_types("talker"))


def test_note_needed_no_tag_because_it_is_canonical() -> None:
    """Documents the decision not to touch `note`: it rides SCOPE_CANONICAL,
    so gate 1 already admitted it under every scope."""
    definition = next(d for d in schema.TYPE_REGISTRY if d.name == "note")

    assert schema.SCOPE_CANONICAL in definition.available_in_scopes
    assert "note" in schema.KNOWN_TYPES
    assert TALKER not in definition.available_in_scopes  # no tag was added
