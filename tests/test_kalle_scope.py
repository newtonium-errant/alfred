"""Tests for the c5 KAL-LE scope + schema additions."""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram.conversation import (
    KALLE_VAULT_TOOLS,
    TALKER_VAULT_TOOLS,
    VAULT_TOOLS,
    VAULT_TOOLS_BY_SET,
    tools_for_set,
)
from alfred.vault import schema
from alfred.vault.scope import (
    KALLE_CREATE_TYPES,
    TALKER_CREATE_TYPES,
    ScopeError,
    check_scope,
)


# ---------------------------------------------------------------------------
# Scope: kalle
# ---------------------------------------------------------------------------


def test_kalle_scope_allows_read_search_list_context():
    check_scope("kalle", "read")
    check_scope("kalle", "search")
    check_scope("kalle", "list")
    check_scope("kalle", "context")


def test_kalle_scope_denies_move_and_delete():
    with pytest.raises(ScopeError):
        check_scope("kalle", "move")
    with pytest.raises(ScopeError):
        check_scope("kalle", "delete")


def test_kalle_scope_create_allows_kalle_types():
    for t in ("note", "pattern", "principle", "decision", "synthesis"):
        # Kalle can create each kalle type.
        check_scope("kalle", "create", record_type=t)


def test_kalle_scope_create_denies_non_kalle_types():
    # task + event are operational — Salem's territory.
    with pytest.raises(ScopeError) as exc_info:
        check_scope("kalle", "create", record_type="task")
    assert "kalle types" in str(exc_info.value).lower()

    with pytest.raises(ScopeError):
        check_scope("kalle", "create", record_type="event")

    with pytest.raises(ScopeError):
        check_scope("kalle", "create", record_type="project")


def test_kalle_scope_edit_permitted_with_no_fields_check():
    # kalle.edit = True (not a field allowlist), so it passes even
    # without a fields arg.
    check_scope("kalle", "edit")


def test_kalle_scope_body_writes_permitted():
    # Pattern/principle curation needs body writes.
    check_scope("kalle", "edit", body_write=True)
    check_scope("kalle", "create", record_type="pattern", body_write=True)


# ---------------------------------------------------------------------------
# Talker scope still rejects kalle-only types
# ---------------------------------------------------------------------------


def test_talker_scope_rejects_pattern_type():
    with pytest.raises(ScopeError):
        check_scope("talker", "create", record_type="pattern")


def test_talker_scope_rejects_principle_type():
    with pytest.raises(ScopeError):
        check_scope("talker", "create", record_type="principle")


def test_talker_create_types_shape():
    """Talker's creatable set — refreshed 2026-04-25 with five new types.

    ``person`` was added 2026-04-21 after Salem created a stub ``note`` for
    a new person Andrew named, when the canonical record should have been
    a ``person`` record. ``org``, ``location``, ``project``, ``constraint``,
    and ``contradiction`` were added 2026-04-25 after Salem repeatedly hit
    the scope wall on new businesses and addresses mid-conversation, and
    to round out the kick-off + reflection surface. The two-gate design
    keeps these to canonical types only — the per-instance leak guards
    below confirm KAL-LE / Hypatia stay separate.
    """
    assert TALKER_CREATE_TYPES == {
        "task", "note", "decision", "event",
        "session", "conversation", "assumption", "synthesis",
        "person",
        "org", "location", "project", "constraint", "contradiction",
    }


def test_kalle_scope_still_rejects_org():
    """Per-instance leak guard: 2026-04-25 widened Salem only.

    ``org`` was added to ``TALKER_CREATE_TYPES`` so Salem stops hitting
    the scope wall when Andrew names a new business mid-conversation.
    KAL-LE operates on the aftermath-lab vault and has no use for org
    records — confirm the new type didn't leak across instances.

    Phase A inter-instance comms (2026-05-01) reshapes the rejection
    message: org is now a canonical type whose creation must route
    through Salem's ``propose_org`` tool. The match string was updated
    from "kalle types" to "propose_org" — the rejection still fires,
    just with a more actionable message.
    """
    with pytest.raises(ScopeError, match="propose_org"):
        check_scope("kalle", "create", record_type="org")


def test_kalle_scope_still_rejects_location():
    """Per-instance leak guard: ``location`` is canonical → propose, not local create."""
    with pytest.raises(ScopeError, match="propose_location"):
        check_scope("kalle", "create", record_type="location")


def test_kalle_create_types_shape():
    """Pin: KAL-LE's create allowlist. ``architecture`` added
    2026-05-04 — multi-instance system design records distinct from
    ``pattern``'s reusable how-to. Updating this pin is the deliberate
    surface-widening signal the test exists for."""
    assert KALLE_CREATE_TYPES == {
        "note", "session", "conversation",
        "decision", "assumption", "synthesis",
        "pattern", "principle", "architecture",
    }


# ---------------------------------------------------------------------------
# Schema: KNOWN_TYPES_KALLE
# ---------------------------------------------------------------------------


def test_known_types_kalle_is_separate_set():
    """Pattern + principle + architecture are NOT in the core
    KNOWN_TYPES (KAL-LE-only, per the per-instance principle).
    ``architecture`` added 2026-05-04 — see test_kalle_create_types_shape
    for the rationale."""
    assert schema.KNOWN_TYPES_KALLE == {
        "pattern", "principle", "architecture",
    }
    assert "pattern" not in schema.KNOWN_TYPES
    assert "principle" not in schema.KNOWN_TYPES
    assert "architecture" not in schema.KNOWN_TYPES


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_vault_tools_is_alias_for_talker():
    """Legacy ``VAULT_TOOLS`` still points at the talker set."""
    assert VAULT_TOOLS is TALKER_VAULT_TOOLS


def test_tools_for_set_talker_default():
    assert tools_for_set("talker") is TALKER_VAULT_TOOLS


def test_tools_for_set_kalle():
    kalle_tools = tools_for_set("kalle")
    names = {t["name"] for t in kalle_tools}
    assert "vault_search" in names
    assert "vault_read" in names
    assert "vault_create" in names
    assert "vault_edit" in names
    # The differentiator:
    assert "bash_exec" in names


def test_tools_for_set_unknown_falls_back_to_talker():
    """Unknown set_name → talker (conservative fallback)."""
    assert tools_for_set("nonexistent") is TALKER_VAULT_TOOLS
    assert tools_for_set("") is TALKER_VAULT_TOOLS


def test_kalle_vault_create_tool_enum_includes_kalle_types():
    """The kalle ``vault_create`` tool's type enum covers pattern + principle."""
    kalle_tools = VAULT_TOOLS_BY_SET["kalle"]
    create_tool = next(t for t in kalle_tools if t["name"] == "vault_create")
    enum_list = create_tool["input_schema"]["properties"]["type"]["enum"]
    assert "pattern" in enum_list
    assert "principle" in enum_list
    # Operational types shouldn't be in kalle's create enum.
    assert "task" not in enum_list
    assert "event" not in enum_list
    assert "project" not in enum_list


def test_bash_exec_tool_schema_has_required_fields():
    kalle_tools = VAULT_TOOLS_BY_SET["kalle"]
    bash = next(t for t in kalle_tools if t["name"] == "bash_exec")
    required = bash["input_schema"]["required"]
    assert "command" in required
    assert "cwd" in required


# ---------------------------------------------------------------------------
# SKILL.md loadability
# ---------------------------------------------------------------------------


def test_kalle_skill_file_exists_and_renders():
    from alfred._data import get_skills_dir

    skills_dir = get_skills_dir()
    skill_path = skills_dir / "vault-kalle" / "SKILL.md"
    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")
    # Template placeholders are present — the daemon renders them.
    assert "{{instance_name}}" in content
    assert "{{instance_canonical}}" in content
    # Key capability statements are present.
    assert "bash_exec" in content
    assert "aftermath-lab" in content
    # Denial anchors — the commit/push rules are load-bearing.
    assert "git commit" in content.lower() or "no `git commit`" in content.lower()
    assert "git push" in content.lower() or "no `git push`" in content.lower()


# ---------------------------------------------------------------------------
# ``architecture`` type registration (added 2026-05-04)
# ---------------------------------------------------------------------------
#
# Multi-instance system design records — distinct from ``pattern``
# (reusable how-to extracted FROM the system). Examples:
# architecture/canonical-authority.md, architecture/PHI-firewall-design.md,
# architecture/peer-protocol.md. KAL-LE-only — Salem and Hypatia have
# no use case.


def test_architecture_type_in_kalle_known_types():
    """``architecture`` registers in KNOWN_TYPES_KALLE so
    ``_validate_type`` accepts it under the kalle scope."""
    assert "architecture" in schema.KNOWN_TYPES_KALLE
    # The kalle-extension union (KNOWN_TYPES_BY_SCOPE) must include it.
    assert "architecture" in schema.KNOWN_TYPES_BY_SCOPE["kalle"]


def test_architecture_type_in_kalle_create_types():
    """``architecture`` registers in KALLE_CREATE_TYPES so the
    kalle scope's ``create`` permission allows it."""
    assert "architecture" in KALLE_CREATE_TYPES


def test_architecture_type_NOT_in_canonical_known_types():
    """Per the per-instance principle — Salem (KNOWN_TYPES) has no
    use case for ``architecture`` records. Adding it here would
    leak the type to every scope, breaking the kalle-only contract."""
    assert "architecture" not in schema.KNOWN_TYPES


def test_architecture_type_NOT_in_hypatia_or_talker_create_allowlists():
    """Hypatia and Salem (talker) must not be able to create
    ``architecture`` records. The scope-level create allowlists
    enforce this; the schema-level KNOWN_TYPES_BY_SCOPE for hypatia
    excludes the kalle extension set."""
    from alfred.vault.scope import HYPATIA_CREATE_TYPES, TALKER_CREATE_TYPES
    assert "architecture" not in TALKER_CREATE_TYPES
    assert "architecture" not in HYPATIA_CREATE_TYPES
    # Hypatia's scope-extension union should NOT pick it up.
    assert "architecture" not in schema.KNOWN_TYPES_BY_SCOPE["hypatia"]


def test_architecture_status_validation_pinned():
    """``architecture`` carries the same status set as synthesis
    ({draft, active, superseded}). Strict-but-small; widen via
    deliberate decision."""
    assert schema.STATUS_BY_TYPE["architecture"] == {
        "draft", "active", "superseded",
    }


def test_architecture_type_directory_pinned():
    """``architecture`` records land under ``architecture/`` —
    matches existing aftermath-lab convention. Without an explicit
    TYPE_DIRECTORY entry the fallback is ``record_type``, but the
    explicit entry catches accidental drift."""
    assert schema.TYPE_DIRECTORY["architecture"] == "architecture"


def test_kalle_scope_create_allows_architecture():
    """End-to-end: kalle scope's ``create`` permission accepts
    ``architecture`` records."""
    check_scope("kalle", "create", record_type="architecture")


def test_talker_scope_create_denies_architecture():
    """Salem (talker scope) must REFUSE ``architecture`` create —
    KAL-LE-only per the per-instance principle."""
    with pytest.raises(ScopeError) as exc_info:
        check_scope("talker", "create", record_type="architecture")
    assert "talker types" in str(exc_info.value).lower()


def test_hypatia_scope_create_denies_architecture():
    """Hypatia must REFUSE ``architecture`` create — KAL-LE-only."""
    with pytest.raises(ScopeError) as exc_info:
        check_scope("hypatia", "create", record_type="architecture")
    assert "hypatia types" in str(exc_info.value).lower()


def test_kalle_can_body_insert_at_architecture():
    """``architecture`` is in kalle's body_insert_at allowlist —
    design docs evolve via mid-doc inserted sections (e.g.
    peer-protocol amendments)."""
    check_scope("kalle", "body_insert_at", record_type="architecture")


def test_kalle_can_body_replace_architecture():
    """``architecture`` is in kalle's body_replace allowlist —
    design docs sometimes need wholesale rewrites (e.g.
    canonical-authority's first → second iteration). No gcal carve-
    out applies (architecture has no sync mirror)."""
    check_scope("kalle", "body_replace", record_type="architecture")


def test_universal_deny_set_unchanged_by_architecture_addition():
    """``architecture`` must NOT join _BODY_MUTATE_DENIED_TYPES —
    it's a curation type that benefits from mid-doc and full-rewrite
    tooling. Regression guard against accidental drift if a future
    edit treats architecture like an atomic learning record."""
    from alfred.vault.scope import _BODY_MUTATE_DENIED_TYPES
    assert "architecture" not in _BODY_MUTATE_DENIED_TYPES
    # Sanity-check the deny set still contains the expected types
    # (catches drift in either direction in one assertion).
    assert _BODY_MUTATE_DENIED_TYPES == frozenset({
        "session", "conversation", "capture", "run", "input",
        "assumption", "decision", "constraint", "contradiction",
        "synthesis",
    })
