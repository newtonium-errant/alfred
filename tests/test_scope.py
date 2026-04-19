"""Smoke tests for ``alfred.vault.scope.check_scope``.

Bootstrap-scope: one allow + one deny per rule we care about. Broader
coverage (every scope, every operation, every edge case) lands as we add
behaviour, not here.
"""

from __future__ import annotations

import pytest

from alfred.vault.scope import ScopeError, check_scope


# ---- learn_types_only (distiller create) ------------------------------------


def test_distiller_create_allows_learn_type():
    # ``decision`` is in LEARN_TYPES — distiller may create it.
    check_scope("distiller", "create", record_type="decision")


def test_distiller_create_denies_non_learn_type():
    # ``task`` is an entity type, not a learn type — distiller is blocked.
    with pytest.raises(ScopeError, match="learn types"):
        check_scope("distiller", "create", record_type="task")


# ---- talker_types_only (talker create) --------------------------------------


def test_talker_create_allows_whitelisted_type():
    check_scope("talker", "create", record_type="task")


def test_talker_create_denies_non_whitelisted_type():
    # ``project`` is intentionally NOT in TALKER_CREATE_TYPES.
    with pytest.raises(ScopeError, match="talker types"):
        check_scope("talker", "create", record_type="project")


# ---- field_allowlist (janitor edit, the new Option E rule) ------------------


def test_janitor_edit_allows_fields_in_allowlist():
    # ``status`` and ``related`` are both in the janitor edit allowlist.
    check_scope(
        "janitor",
        "edit",
        rel_path="task/Some Task.md",
        fields=["status", "related"],
    )


def test_janitor_edit_denies_field_outside_allowlist():
    # ``description`` is NOT in the Stage 1/2 janitor allowlist (it lives in
    # the separate ``janitor_enrich`` scope).
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "janitor",
            "edit",
            rel_path="person/Someone.md",
            fields=["description"],
        )


def test_janitor_edit_fails_closed_when_fields_omitted():
    # field_allowlist must fail closed if the caller forgets to pass fields —
    # otherwise the check is trivially bypassable.
    with pytest.raises(ScopeError, match="did not supply the field list"):
        check_scope("janitor", "edit", rel_path="task/X.md")


# ---- Q3: body-write loophole (allow_body_writes) ----------------------------


def test_janitor_scope_denies_body_append():
    # Janitor carries allow_body_writes=False — body writes via edit must
    # raise ScopeError even when the caller passes no frontmatter fields.
    # This closes the Q3 loophole where body_append could rewrite the
    # entire body, bypassing the frontmatter allowlist.
    with pytest.raises(ScopeError, match="may not write record body"):
        check_scope(
            "janitor", "edit",
            rel_path="note/Some Note.md",
            fields=[],
            body_write=True,
        )


def test_janitor_scope_denies_body_replace():
    # Same behaviour when a hypothetical body_replace is requested — the
    # gate is on the body_write flag, not on which body kwarg triggered it.
    # ``fields=["related"]`` is in the allowlist so the frontmatter-level
    # check would otherwise succeed; the body_write gate must fire first.
    with pytest.raises(ScopeError, match="may not write record body"):
        check_scope(
            "janitor", "edit",
            rel_path="note/Some Note.md",
            fields=["related"],
            body_write=True,
        )


def test_janitor_enrich_allows_body_append():
    # Stage 3 enrichment writes substantive content to stub person/org
    # records. It carries its own scope and allow_body_writes=True so
    # description-appending continues to work after Q3.
    check_scope(
        "janitor_enrich", "edit",
        rel_path="person/Jane Doe.md",
        fields=["description"],
        body_write=True,
    )


def test_talker_allows_body_append():
    # Talker creates notes / sessions / conversations with body content
    # synthesised from the voice turn — body writes must still succeed.
    check_scope(
        "talker", "edit",
        rel_path="note/Voice Note.md",
        body_write=True,
    )


def test_curator_allows_body_append():
    # Curator writes full record bodies at creation and during
    # enrichment. Body writes must stay allowed.
    check_scope(
        "curator", "edit",
        rel_path="note/Inbox Capture.md",
        body_write=True,
    )


def test_janitor_frontmatter_only_works():
    # Baseline: janitor set_fields on an allowlisted field (no body write)
    # continues to succeed after the body-write gate is added. This is the
    # Stage 1/2 autofix happy path.
    check_scope(
        "janitor", "edit",
        rel_path="task/Some Task.md",
        fields=["janitor_note"],
        body_write=False,
    )


def test_curator_create_allows_body_write():
    # Curator create-with-body is the core curator flow (email → input
    # record with body). Must continue to pass after the Q3 gate lands.
    check_scope(
        "curator", "create",
        record_type="input",
        body_write=True,
    )


def test_janitor_create_denies_body_write():
    # Janitor triage-task creation never sets a body; a create call that
    # tries to supply one must be rejected before it can reach vault_create.
    with pytest.raises(ScopeError, match="may not write record body"):
        check_scope(
            "janitor", "create",
            record_type="task",
            frontmatter={"alfred_triage": True},
            body_write=True,
        )
