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
    # ``input`` is intentionally NOT in TALKER_CREATE_TYPES — those are
    # curator-side records produced from raw inbox material, never by
    # Salem mid-conversation.
    with pytest.raises(ScopeError, match="talker types"):
        check_scope("talker", "create", record_type="input")


def test_talker_create_allows_person():
    # Added 2026-04-21: Salem must be able to create person records when
    # Andrew names a new individual. Previously Salem fell back to ``note``
    # stubs (e.g. for "Alex Newton"); widening the scope closes that gap.
    check_scope("talker", "create", record_type="person")


def test_talker_create_allows_org():
    # Added 2026-04-25: Salem creates org records when Andrew names a
    # new business mid-conversation (e.g. "Re/Generate Spa"). Previously
    # she fell back to ``note`` or hit the scope wall.
    check_scope("talker", "create", record_type="org")


def test_talker_create_allows_location():
    # Added 2026-04-25: Salem creates location records when Andrew
    # names a new address or place (e.g. "8736 Commercial St New
    # Minas"). Previously she fell back to ``note`` or hit the wall.
    check_scope("talker", "create", record_type="location")


def test_talker_create_allows_project():
    # Added 2026-04-25: Andrew often kicks off a new initiative in
    # voice; Salem now creates the ``project`` record rather than
    # parking it as a generic note.
    check_scope("talker", "create", record_type="project")


def test_talker_create_allows_constraint():
    # Added 2026-04-25: ``constraint`` is a learn type Salem may surface
    # during reflection when the distiller hasn't yet caught up.
    check_scope("talker", "create", record_type="constraint")


def test_talker_create_allows_contradiction():
    # Added 2026-04-25: ``contradiction`` is a learn type Salem may
    # surface during reflection when the distiller hasn't yet caught up.
    check_scope("talker", "create", record_type="contradiction")


def test_talker_create_allows_routine():
    # Added 2026-05-30 (Phase 2B B2 — conversational routine creation):
    # Salem creates ``routine`` records when the operator names a new
    # recurring practice mid-conversation. The SKILL's "Creating
    # routines" section documents the operator-language → cadence /
    # due_pattern / target_cadence_days mapping.
    #
    # Per-instance isolation is SINGLE-GATE (review-clarified
    # 2026-05-30): ``routine`` is tagged with ``SCOPE_CANONICAL`` in
    # schema.py, which means the type validator accepts it under
    # EVERY named scope (canonical types union with per-scope
    # extensions for kalle / hypatia / talker). The non-Salem
    # refusal lives ONLY at the scope layer:
    # ``kalle_types_only`` / ``hypatia_types_only`` reject ``routine``
    # because it isn't in ``KALLE_CREATE_TYPES`` /
    # ``HYPATIA_CREATE_TYPES``. This test pins the Salem-path
    # allowlist; the KAL-LE / Hypatia rejection paths are pinned
    # separately in test_kalle_scope.py / test_hypatia_scope.py.
    check_scope("talker", "create", record_type="routine")


def test_talker_create_routine_is_in_TALKER_CREATE_TYPES_constant():
    # Direct constant pin — the ``vault_create`` tool schema enum in
    # ``conversation.py`` mirrors this set. Drift between the two
    # would let Salem name a routine but get refused at the scope
    # gate (or vice versa). Pre-2026-05-30 the enum lagged the
    # constant by several days for ``org`` / ``location`` adds; this
    # pin surfaces that class of drift if the constant grows again
    # without the enum following.
    from alfred.vault.scope import TALKER_CREATE_TYPES
    assert "routine" in TALKER_CREATE_TYPES


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


# ---- instructor scope (alfred_instructions watcher) -------------------------


def test_instructor_allows_read():
    check_scope("instructor", "read", rel_path="note/Some Note.md")


def test_instructor_allows_search():
    check_scope("instructor", "search")


def test_instructor_allows_list():
    check_scope("instructor", "list")


def test_instructor_allows_context():
    check_scope("instructor", "context")


def test_instructor_allows_edit_any_field():
    # Instructor has no field allowlist — directives can touch any
    # frontmatter field. ``description`` is NOT on the janitor allowlist;
    # instructor must still accept it.
    check_scope(
        "instructor", "edit",
        rel_path="person/Someone.md",
        fields=["description", "role", "aliases"],
    )


def test_instructor_allows_create():
    # Instructor can create records of any type — directives may ask for
    # a new task, note, or project. No ``*_types_only`` constraint.
    check_scope("instructor", "create", record_type="project")


def test_instructor_allows_move():
    # Instructor may move records anywhere in the vault (janitor cannot).
    check_scope(
        "instructor", "move",
        rel_path="task/Some Task.md",
    )


def test_instructor_allows_body_writes():
    # Directives can ask for drafting or restructuring body content.
    check_scope(
        "instructor", "edit",
        rel_path="note/Some Note.md",
        body_write=True,
    )


def test_instructor_denies_delete():
    # Deletion is always an explicit operator task — the instructor
    # watcher must never execute a delete on its own, even when a
    # directive asks for it.
    with pytest.raises(ScopeError, match="denied for scope 'instructor'"):
        check_scope(
            "instructor", "delete",
            rel_path="task/Some Task.md",
        )


# ---- canonical-type guard on KAL-LE / Hypatia (Phase A inter-instance) -----


def test_kalle_create_denies_canonical_person_with_propose_hint():
    """KAL-LE may NOT create local person/ — Salem's canonical authority.

    The error message points at ``propose_person`` so the agent prompt
    can route the create through the correct tool.
    """
    with pytest.raises(ScopeError, match="propose_person"):
        check_scope("kalle", "create", record_type="person")


def test_kalle_create_denies_canonical_event_with_propose_hint():
    with pytest.raises(ScopeError, match="propose_event"):
        check_scope("kalle", "create", record_type="event")


def test_kalle_create_denies_canonical_org_with_propose_hint():
    with pytest.raises(ScopeError, match="propose_org"):
        check_scope("kalle", "create", record_type="org")


def test_kalle_create_denies_canonical_location_with_propose_hint():
    with pytest.raises(ScopeError, match="propose_location"):
        check_scope("kalle", "create", record_type="location")


def test_hypatia_create_denies_canonical_person_with_propose_hint():
    with pytest.raises(ScopeError, match="propose_person"):
        check_scope("hypatia", "create", record_type="person")


def test_hypatia_create_denies_canonical_event_with_propose_hint():
    with pytest.raises(ScopeError, match="propose_event"):
        check_scope("hypatia", "create", record_type="event")


def test_kalle_canonical_guard_does_not_break_legal_kalle_create():
    """The canonical guard must not break legal KAL-LE creates."""
    check_scope("kalle", "create", record_type="pattern")
    check_scope("kalle", "create", record_type="note")


def test_hypatia_canonical_guard_does_not_break_legal_hypatia_create():
    """The canonical guard must not break legal Hypatia creates."""
    check_scope("hypatia", "create", record_type="document")
    check_scope("hypatia", "create", record_type="note")


def test_talker_canonical_types_still_allowed_on_salem():
    """Salem (talker scope) IS the canonical owner — must still create directly."""
    check_scope("talker", "create", record_type="person")
    check_scope("talker", "create", record_type="org")
    check_scope("talker", "create", record_type="location")
    check_scope("talker", "create", record_type="event")


# ===========================================================================
# Phase 2B B1 (2026-05-30) — talker_routine_completion narrow scope
# ===========================================================================
#
# The conversational completion path (routine_done tool subprocess
# invocation) routes through this scope. Three checks together:
#   * record_type must be 'routine'
#   * fields must be supplied (fail-closed)
#   * fields must all be in {completion_log}


def test_talker_routine_completion_allows_completion_log_on_routine():
    """Happy path — type=routine + fields=[completion_log] passes."""
    check_scope(
        "talker_routine_completion",
        "edit",
        record_type="routine",
        fields=["completion_log"],
    )


def test_talker_routine_completion_rejects_non_completion_log_field():
    """type=routine but field outside the allowlist → rejected."""
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="routine",
            fields=["items"],
        )
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="routine",
            fields=["cadence"],
        )
    # Even mixed allowed + non-allowed → rejected (subset check).
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="routine",
            fields=["completion_log", "cadence"],
        )


def test_talker_routine_completion_rejects_completion_log_on_non_routine():
    """field=completion_log but type != routine → rejected.

    Defense against the talker pointing at a task or person record
    via the completion-log scope."""
    with pytest.raises(ScopeError, match="record types"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="task",
            fields=["completion_log"],
        )
    with pytest.raises(ScopeError, match="record types"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="person",
            fields=["completion_log"],
        )


def test_talker_routine_completion_fails_closed_on_missing_fields():
    """When fields=None, the scope fails closed (no fields supplied
    means no allowlist check possible)."""
    with pytest.raises(ScopeError, match="did not supply"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="routine",
            fields=None,
        )


def test_talker_routine_completion_denies_create_move_delete():
    """The narrow scope ONLY permits edit (of completion_log on
    routine). Create / move / delete all denied."""
    with pytest.raises(ScopeError, match="denied"):
        check_scope(
            "talker_routine_completion",
            "create",
            record_type="routine",
        )
    with pytest.raises(ScopeError, match="denied"):
        check_scope(
            "talker_routine_completion",
            "move",
            record_type="routine",
        )
    with pytest.raises(ScopeError, match="denied"):
        check_scope(
            "talker_routine_completion",
            "delete",
            record_type="routine",
        )


def test_talker_routine_completion_denies_body_writes():
    """Body writes denied per the allow_body_writes=False setting on
    this scope."""
    with pytest.raises(ScopeError, match="body"):
        check_scope(
            "talker_routine_completion",
            "edit",
            record_type="routine",
            fields=["completion_log"],
            body_write=True,
        )


def test_talker_routine_completion_constants_pinned():
    """Cross-agent contract pin — the constants are exported + carry
    the expected values."""
    from alfred.vault.scope import (
        TALKER_COMPLETION_LOG_FIELDS,
        TALKER_COMPLETION_LOG_TYPES,
    )
    assert TALKER_COMPLETION_LOG_TYPES == {"routine"}
    assert TALKER_COMPLETION_LOG_FIELDS == {"completion_log"}


# ===========================================================================
# Phase 2B B3 (2026-05-30) — talker_routine_item narrow scope
# ===========================================================================
#
# Mirrors B1's talker_routine_completion shape. Broader allowlist:
# items + completion_log (so atomic add/remove/edit can mutate both
# fields in the same write — text-rename migrates completion_log
# keys, remove strips dead entries).


def test_talker_routine_item_allows_items_field_on_routine():
    """Happy path — type=routine + fields=[items] passes."""
    check_scope(
        "talker_routine_item",
        "edit",
        record_type="routine",
        fields=["items"],
    )


def test_talker_routine_item_allows_completion_log_field_on_routine():
    """Happy path — type=routine + fields=[completion_log] passes.

    This overlap with the B1 talker_routine_completion scope is
    intentional: B3's edit path (rename + remove) mutates BOTH
    items AND completion_log atomically. The scope allows
    completion_log too so the same write satisfies the gate."""
    check_scope(
        "talker_routine_item",
        "edit",
        record_type="routine",
        fields=["completion_log"],
    )


def test_talker_routine_item_allows_items_plus_completion_log_atomically():
    """Rename + remove paths mutate items AND completion_log in the
    same write. The scope must accept both in one fields= list."""
    check_scope(
        "talker_routine_item",
        "edit",
        record_type="routine",
        fields=["items", "completion_log"],
    )


def test_talker_routine_item_rejects_non_allowlist_field():
    """Other routine fields (cadence top-level, status, name,
    alfred_tags) remain out of bounds for this narrow scope."""
    for bad_field in ("cadence", "status", "name", "alfred_tags"):
        with pytest.raises(ScopeError, match="allowlist"):
            check_scope(
                "talker_routine_item",
                "edit",
                record_type="routine",
                fields=[bad_field],
            )


def test_talker_routine_item_rejects_mixed_allowed_and_disallowed():
    """Subset check — even one disallowed field in the list rejects
    the whole edit. Mirror of B1's behaviour."""
    with pytest.raises(ScopeError, match="allowlist"):
        check_scope(
            "talker_routine_item",
            "edit",
            record_type="routine",
            fields=["items", "cadence"],
        )


def test_talker_routine_item_rejects_items_on_non_routine_types():
    """The scope is type-narrowed to routine — items field on a task
    or person record is rejected (defends against the talker pointing
    at the wrong record type via this scope)."""
    for bad_type in ("task", "person", "note"):
        with pytest.raises(ScopeError, match="record types"):
            check_scope(
                "talker_routine_item",
                "edit",
                record_type=bad_type,
                fields=["items"],
            )


def test_talker_routine_item_fails_closed_on_missing_fields():
    """When fields=None, the scope fails closed (no fields supplied
    means no allowlist check possible). Mirror of B1."""
    with pytest.raises(ScopeError, match="did not supply"):
        check_scope(
            "talker_routine_item",
            "edit",
            record_type="routine",
            fields=None,
        )


def test_talker_routine_item_denies_create_move_delete():
    """The narrow scope ONLY permits edit (of items + completion_log
    on routine). Create / move / delete all denied."""
    for op in ("create", "move", "delete"):
        with pytest.raises(ScopeError, match="denied"):
            check_scope(
                "talker_routine_item",
                op,
                record_type="routine",
            )


def test_talker_routine_item_denies_body_writes():
    """Body writes denied per the allow_body_writes=False setting."""
    with pytest.raises(ScopeError, match="body"):
        check_scope(
            "talker_routine_item",
            "edit",
            record_type="routine",
            fields=["items"],
            body_write=True,
        )


def test_talker_routine_item_constants_pinned():
    """Cross-agent contract pin — TALKER_ROUTINE_ITEM_TYPES +
    TALKER_ROUTINE_ITEM_FIELDS exported + carry expected values."""
    from alfred.vault.scope import (
        TALKER_ROUTINE_ITEM_FIELDS,
        TALKER_ROUTINE_ITEM_TYPES,
    )
    assert TALKER_ROUTINE_ITEM_TYPES == {"routine"}
    assert TALKER_ROUTINE_ITEM_FIELDS == {"items", "completion_log"}


# ===========================================================================
# c6 (2026-05-31) — talker tier_curation field-allowlist
# ===========================================================================
#
# Operator (Andrew) ratified 2026-06-01: pre-set tomorrow's tier list via
# the standard talker LLM vault_create / vault_edit dispatch, with the
# ``daily`` record type carved out so ONLY the ``tier_curation`` field
# may be pre-set. The aggregator's 05:59 ADT fire preserves the pre-set
# block via ``_load_existing_tier_curation`` (aggregator.py:828).
#
# Three layers tested:
#   * Scope layer — check_talker_tier_curation_fields helper enforces
#     the type + fields allowlist.
#   * TALKER_CREATE_TYPES — ``daily`` is in the set so the standard
#     talker_types_only create gate admits the type.
#   * Constants — TALKER_TIER_CURATION_TYPES + _FIELDS pinned.


def test_talker_scope_allows_tier_curation_on_daily():
    """Happy path — type=daily + fields=[tier_curation] passes the
    field-allowlist helper. The actual write goes through
    talker_types_only on the broader talker scope; this helper is the
    per-type narrow-gate spliced in at the conversation.py dispatch."""
    from alfred.vault.scope import check_talker_tier_curation_fields
    check_talker_tier_curation_fields("daily", ["tier_curation"])


def test_talker_scope_rejects_non_tier_curation_field_on_daily():
    """type=daily but field outside the allowlist → rejected. Defends
    against the LLM trying to pre-set ``routines_contributing`` or
    other aggregator-owned fields via the same write."""
    from alfred.vault.scope import check_talker_tier_curation_fields
    with pytest.raises(ScopeError, match="allowlist"):
        check_talker_tier_curation_fields(
            "daily", ["routines_contributing"],
        )
    with pytest.raises(ScopeError, match="allowlist"):
        check_talker_tier_curation_fields("daily", ["date"])
    # Mixed allowed + non-allowed → rejected (subset check).
    with pytest.raises(ScopeError, match="allowlist"):
        check_talker_tier_curation_fields(
            "daily", ["tier_curation", "critical_pending"],
        )


def test_talker_scope_rejects_tier_curation_on_non_daily_type():
    """field=tier_curation but type != daily → rejected. Defends
    against the talker pointing at a task / note / etc. record via
    the tier_curation helper. (The helper is intentionally only
    called when conversation.py detects type=daily; this is the
    defense-in-depth check.)"""
    from alfred.vault.scope import check_talker_tier_curation_fields
    with pytest.raises(ScopeError, match="record types"):
        check_talker_tier_curation_fields("task", ["tier_curation"])
    with pytest.raises(ScopeError, match="record types"):
        check_talker_tier_curation_fields("note", ["tier_curation"])


def test_talker_scope_fails_closed_on_missing_fields():
    """When fields=None (caller didn't supply set_fields keys), the
    helper fails closed. Mirrors B1/B3 narrow-scope semantics."""
    from alfred.vault.scope import check_talker_tier_curation_fields
    with pytest.raises(ScopeError, match="did not supply"):
        check_talker_tier_curation_fields("daily", None)


def test_talker_create_daily_is_in_TALKER_CREATE_TYPES_constant():
    """``daily`` is in TALKER_CREATE_TYPES so the broad talker scope's
    create: talker_types_only gate admits the type. Field-allowlist
    enforcement is then layered at the conversation.py dispatch."""
    from alfred.vault.scope import TALKER_CREATE_TYPES
    assert "daily" in TALKER_CREATE_TYPES


def test_talker_tier_curation_constants_pinned():
    """Cross-agent contract pin — TALKER_TIER_CURATION_TYPES +
    TALKER_TIER_CURATION_FIELDS exported and carry expected values."""
    from alfred.vault.scope import (
        TALKER_TIER_CURATION_FIELDS,
        TALKER_TIER_CURATION_TYPES,
    )
    assert TALKER_TIER_CURATION_TYPES == {"daily"}
    assert TALKER_TIER_CURATION_FIELDS == {"tier_curation"}
