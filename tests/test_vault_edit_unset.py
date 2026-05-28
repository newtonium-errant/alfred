"""Tests for ``vault_edit(unset_fields=...)`` — frontmatter field removal.

Shipped 2026-05-28 to unblock the tier Phase 1 migration script. The
CLI ``vault edit`` surface previously had no field-removal mechanism;
operators wanting to drop a stale key had to bypass the CLI entirely
(direct file rewrite) — losing the scope check and audit log in the
process. ``--unset`` closes that gap.

Coverage matrix:
- Library: unset existing field, unset already-absent (no-op log),
  unset REQUIRED_FIELDS (universal + per-type) → fail-loud, unset
  combined with set on same call, combined with body_append, ops-layer
  no-op detection still fires when only path is supplied (no unset).
- CLI: --unset flag parses, --unset + --set combined, repeatable
  --unset, --unset of REQUIRED_FIELDS fail-loud via ops-layer raise,
  combined operations emit BOTH session-log entries (edit + unset).
- Scope: migration scope permits all unset; field-allowlist scopes
  apply the allowlist to unset targets too; edit:False scopes refuse.
- mutation_log: ``unset`` op routes to files_modified bucket;
  read_mutations recognises the new op-string.
- Audit log integration: cmd_edit emits TWO log entries when both
  set and unset happen; ONE entry (op="unset") when only unset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.vault.mutation_log import build_audit_mutations, read_mutations
from alfred.vault.ops import VaultError, vault_create, vault_edit, vault_read
from alfred.vault.scope import ScopeError, check_scope


# ---------------------------------------------------------------------------
# Library surface — vault_edit(unset_fields=...)
# ---------------------------------------------------------------------------


class TestVaultEditUnsetLibrary:
    def test_unset_removes_existing_field(self, tmp_vault: Path) -> None:
        """Baseline: unsetting a present field drops the key entirely."""
        vault_create(
            tmp_vault, "task", "Unset Baseline",
            set_fields={"status": "todo", "priority": "high"},
        )
        result = vault_edit(
            tmp_vault, "task/Unset Baseline.md",
            unset_fields=["priority"],
        )
        assert "priority" in result["fields_changed"]
        post = vault_read(tmp_vault, "task/Unset Baseline.md")
        assert "priority" not in post["frontmatter"]
        # Other fields preserved.
        assert post["frontmatter"]["status"] == "todo"

    def test_unset_distinct_from_set_null(self, tmp_vault: Path) -> None:
        """``unset_fields=["foo"]`` removes the key entirely;
        ``set_fields={"foo": None}`` keeps the key with null value.
        Pin the distinction so a future refactor doesn't conflate them."""
        vault_create(
            tmp_vault, "task", "Set Null vs Unset A",
            set_fields={"status": "todo", "priority": "high"},
        )
        vault_create(
            tmp_vault, "task", "Set Null vs Unset B",
            set_fields={"status": "todo", "priority": "high"},
        )

        # Path A: --set priority=null → key stays, value is None.
        vault_edit(
            tmp_vault, "task/Set Null vs Unset A.md",
            set_fields={"priority": None},
        )
        a = vault_read(tmp_vault, "task/Set Null vs Unset A.md")
        assert "priority" in a["frontmatter"]
        assert a["frontmatter"]["priority"] is None

        # Path B: --unset priority → key gone.
        vault_edit(
            tmp_vault, "task/Set Null vs Unset B.md",
            unset_fields=["priority"],
        )
        b = vault_read(tmp_vault, "task/Set Null vs Unset B.md")
        assert "priority" not in b["frontmatter"]

    def test_unset_already_absent_emits_no_op_log(
        self, tmp_vault: Path,
    ) -> None:
        """Idempotent re-runs of a migration must not fail when the
        field was already removed by a prior run. Per
        ``feedback_intentionally_left_blank.md`` the no-op emits an
        info log so operators can distinguish "removed by this call"
        from "was already absent." Pin via structlog.testing capture
        per builder.md rule #9."""
        vault_create(
            tmp_vault, "task", "Absent Field Unset",
            set_fields={"status": "todo"},
        )
        with structlog.testing.capture_logs() as captured:
            result = vault_edit(
                tmp_vault, "task/Absent Field Unset.md",
                unset_fields=["nonexistent_field"],
            )
        # Field not in fields_changed — nothing actually mutated.
        assert "nonexistent_field" not in result["fields_changed"]
        # No-op log fires with the discriminating fields.
        no_ops = [
            c for c in captured
            if c.get("event") == "vault.edit.unset_no_op"
        ]
        assert len(no_ops) == 1
        assert no_ops[0]["field"] == "nonexistent_field"
        assert "task/Absent Field Unset.md" in no_ops[0]["path"]

    def test_unset_universal_required_field_fails(
        self, tmp_vault: Path,
    ) -> None:
        """``type`` and ``created`` are universal REQUIRED_FIELDS;
        unsetting either must fail-loud."""
        vault_create(
            tmp_vault, "task", "Required Field Guard",
            set_fields={"status": "todo"},
        )
        for field in ("type", "created"):
            with pytest.raises(VaultError) as exc_info:
                vault_edit(
                    tmp_vault, "task/Required Field Guard.md",
                    unset_fields=[field],
                )
            msg = str(exc_info.value)
            assert "required field" in msg.lower()
            assert field in msg

    def test_unset_type_specific_required_field_fails(
        self, tmp_vault: Path,
    ) -> None:
        """``routine`` records require ``name``, ``cadence``, ``items``
        per REQUIRED_FIELDS_BY_TYPE. Pin that unsetting a type-
        specific required field fails-loud, distinct from the
        universal REQUIRED_FIELDS guard above."""
        (tmp_vault / "routine").mkdir(exist_ok=True)
        vault_create(
            tmp_vault, "routine", "Type Required Guard",
            set_fields={
                "status": "active",
                "cadence": {"type": "daily"},
                "items": [{"text": "Walk dog", "priority": "tracked"}],
            },
        )
        with pytest.raises(VaultError) as exc_info:
            vault_edit(
                tmp_vault, "routine/Type Required Guard.md",
                unset_fields=["cadence"],
            )
        msg = str(exc_info.value)
        assert "required field" in msg.lower()
        assert "cadence" in msg
        # Error message names which protection source fired —
        # type-specific, not universal. Operator can diagnose
        # without rereading the schema.
        assert "REQUIRED_FIELDS_BY_TYPE" in msg

    def test_set_type_then_unset_uses_post_set_type_required_fields(
        self, tmp_vault: Path,
    ) -> None:
        """Pin the post-set-type semantics documented in
        ``pre_unset_type`` (ops.py docstring). When ``set_fields``
        retypes the record AND ``unset_fields`` removes a field that
        was required for the PRE-set type but NOT for the POST-set
        type, the unset succeeds — because the protection set is
        derived from the POST-set type.

        Concrete shape: routine → task retype. Routine requires
        ``name``, ``cadence``, ``items``; task has no per-type
        required fields. After set ``type=task``, unsetting
        ``cadence`` should succeed (task doesn't require it).

        Reverse case (task → routine retype with cadence unset on
        the same call) is NOT pinned here because operators wouldn't
        rationally retype to a more-required-fields type while
        simultaneously removing those fields — that shape would
        fail-loud at the schema validator (FM003 missing required
        field) on the post-edit validation pass, which is the right
        gate for that case.
        """
        (tmp_vault / "routine").mkdir(exist_ok=True)
        vault_create(
            tmp_vault, "routine", "Retype Then Unset",
            set_fields={
                "status": "active",
                "cadence": {"type": "daily"},
                "items": [{"text": "x", "priority": "tracked"}],
            },
        )
        # Single edit: retype to task AND unset cadence.
        # Post-set type = task → no per-type required → unset OK.
        result = vault_edit(
            tmp_vault, "routine/Retype Then Unset.md",
            set_fields={"type": "task"},
            unset_fields=["cadence"],
        )
        assert "cadence" in result["fields_changed"]
        assert "type" in result["fields_changed"]
        post = vault_read(tmp_vault, "routine/Retype Then Unset.md")
        assert post["frontmatter"]["type"] == "task"
        assert "cadence" not in post["frontmatter"]

    def test_unset_combined_with_set_on_same_call(
        self, tmp_vault: Path,
    ) -> None:
        """Combined set + unset in one call: both mutations apply,
        execution order is set-then-unset (unset runs last)."""
        vault_create(
            tmp_vault, "task", "Combined Mutations",
            set_fields={"status": "todo", "priority": "high"},
        )
        result = vault_edit(
            tmp_vault, "task/Combined Mutations.md",
            set_fields={"status": "active"},
            unset_fields=["priority"],
        )
        assert "status" in result["fields_changed"]
        assert "priority" in result["fields_changed"]
        post = vault_read(tmp_vault, "task/Combined Mutations.md")
        assert post["frontmatter"]["status"] == "active"
        assert "priority" not in post["frontmatter"]

    def test_unset_combined_with_body_append(
        self, tmp_vault: Path,
    ) -> None:
        """Unset + body_append in one call: both surfaces apply."""
        vault_create(
            tmp_vault, "task", "Unset Plus Body",
            set_fields={"status": "todo", "priority": "high"},
            body="# Unset Plus Body\n\nOriginal.\n",
        )
        result = vault_edit(
            tmp_vault, "task/Unset Plus Body.md",
            unset_fields=["priority"],
            body_append="Migration note.",
        )
        assert "priority" in result["fields_changed"]
        assert "body" in result["fields_changed"]
        post = vault_read(tmp_vault, "task/Unset Plus Body.md")
        assert "priority" not in post["frontmatter"]
        body_text = (
            tmp_vault / "task/Unset Plus Body.md"
        ).read_text(encoding="utf-8")
        assert "Migration note." in body_text

    def test_unset_same_field_as_set_in_one_call_unset_wins(
        self, tmp_vault: Path,
    ) -> None:
        """``--set foo=x --unset foo`` is a contradiction; the documented
        contract is "unset runs last, so foo ends absent." Pin so a
        refactor that reorders the passes surfaces."""
        vault_create(
            tmp_vault, "task", "Set Then Unset Same Key",
            set_fields={"status": "todo"},
        )
        result = vault_edit(
            tmp_vault, "task/Set Then Unset Same Key.md",
            set_fields={"priority": "urgent"},
            unset_fields=["priority"],
        )
        post = vault_read(tmp_vault, "task/Set Then Unset Same Key.md")
        assert "priority" not in post["frontmatter"]
        # The mutation IS recorded in fields_changed (operator's
        # intent was to mutate priority twice — set then unset).
        assert "priority" in result["fields_changed"]

    def test_unset_empty_list_is_no_op_at_param_level(
        self, tmp_vault: Path,
    ) -> None:
        """An empty ``unset_fields=[]`` should still require ANOTHER
        mutation surface — empty list does not count as a mutation."""
        vault_create(
            tmp_vault, "task", "Empty Unset List",
            set_fields={"status": "todo"},
        )
        with pytest.raises(VaultError) as exc_info:
            vault_edit(
                tmp_vault, "task/Empty Unset List.md",
                unset_fields=[],
            )
        # Should fall into the no-mutation-parameter fail-loud gate.
        msg = str(exc_info.value)
        assert "no mutation parameter" in msg

    def test_unset_only_succeeds_as_sole_mutation(
        self, tmp_vault: Path,
    ) -> None:
        """A non-empty ``unset_fields`` is sufficient to pass the
        no-mutation-parameter gate. Pin so a future no-op-gate refactor
        doesn't accidentally require set/append alongside unset."""
        vault_create(
            tmp_vault, "task", "Unset Only Path",
            set_fields={"status": "todo", "priority": "low"},
        )
        result = vault_edit(
            tmp_vault, "task/Unset Only Path.md",
            unset_fields=["priority"],
        )
        assert "priority" in result["fields_changed"]


# ---------------------------------------------------------------------------
# Scope integration
# ---------------------------------------------------------------------------


class TestUnsetScopeGate:
    def test_migration_scope_permits_unset(
        self, tmp_vault: Path,
    ) -> None:
        """Migration scope has ``edit: True`` → all fields unset-able."""
        vault_create(
            tmp_vault, "task", "Migration Scope Unset",
            set_fields={"status": "todo", "priority": "high"},
        )
        result = vault_edit(
            tmp_vault, "task/Migration Scope Unset.md",
            unset_fields=["priority"],
            scope="migration",
        )
        assert "priority" in result["fields_changed"]

    def test_migration_scope_denies_delete(self) -> None:
        """Migration scope's ``delete: False`` must surface as a
        ScopeError refusal. Pinned per code-reviewer NOTE — the
        positive ``permits_unset`` case alone doesn't prove the
        scope is narrow on destructive ops; we need a deny pin for
        completeness."""
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "migration", "delete",
                rel_path="task/some-record.md",
            )
        msg = str(exc_info.value).lower()
        assert "denied" in msg
        assert "migration" in msg

    def test_migration_scope_denies_move(self) -> None:
        """Migration scope's ``move: False`` must surface as a
        ScopeError refusal. Sister to the delete pin above — together
        they prove the scope is narrow on positional/destructive ops
        regardless of how widely edit/create/body-append are opened."""
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "migration", "move",
                rel_path="task/some-record.md",
            )
        msg = str(exc_info.value).lower()
        assert "denied" in msg
        assert "migration" in msg

    def test_migration_scope_create_allowlist_permits_task(self) -> None:
        """``task`` is in MIGRATION_CREATE_TYPES (the tier Phase 1
        migration creates task records during the standing-practices
        cancellation flow). Pin via direct check_scope so a future
        narrowing of MIGRATION_CREATE_TYPES surfaces here."""
        # Should NOT raise.
        check_scope(
            "migration", "create",
            record_type="task",
        )

    def test_migration_scope_create_allowlist_permits_routine(self) -> None:
        """``routine`` is in MIGRATION_CREATE_TYPES (the tier Phase 1
        migration creates a ``routine/Standing Practices.md``
        aggregator)."""
        check_scope(
            "migration", "create",
            record_type="routine",
        )

    def test_migration_scope_create_allowlist_denies_session(self) -> None:
        """``session`` is NOT in MIGRATION_CREATE_TYPES — auto-generated
        transcript record types are denied at the scope gate to keep
        migrations from accidentally producing transcript-shaped
        records that no operator review path catches. Pin per
        code-reviewer NOTE #1."""
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "migration", "create",
                record_type="session",
            )
        msg = str(exc_info.value).lower()
        assert "migration types" in msg
        assert "session" in msg

    def test_migration_scope_create_allowlist_denies_event(self) -> None:
        """``event`` is NOT in MIGRATION_CREATE_TYPES — operator-
        canonical records (events have GCal sync semantics) need
        explicit operator review, not automation."""
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "migration", "create",
                record_type="event",
            )
        assert "migration types" in str(exc_info.value).lower()

    def test_migration_scope_create_allowlist_denies_preference(self) -> None:
        """``preference`` is NOT in MIGRATION_CREATE_TYPES — operator-
        canonical commitments must be conversationally established,
        not migration-scripted."""
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "migration", "create",
                record_type="preference",
            )
        assert "migration types" in str(exc_info.value).lower()

    def test_field_allowlist_scope_gates_unset(self) -> None:
        """The janitor scope has a field allowlist on ``edit``; calling
        ``check_scope("edit", fields=["name"])`` should succeed
        (``name`` is in the allowlist), but calling with a field
        outside the allowlist must raise — same gate for set + unset.

        Field choice matters: ``priority`` IS in the janitor allowlist
        (added for triage-task creation per scope.py:222-231), so a
        disallowed-field assertion must pick a field genuinely outside
        the set. ``description`` is the canonical "operator-authored
        free-text content" field that janitor structural fixes never
        touch — apt choice for the disallowed-side pin.
        """
        # Allowed field — in janitor's edit_fields_allowlist.
        check_scope(
            "janitor", "edit",
            rel_path="task/example.md",
            fields=["name"],
        )
        # Disallowed field — janitor edit_fields_allowlist does NOT
        # include ``description`` (it's user-authored free text;
        # janitor is structural-fixes-only). The unset code path
        # threads the unset target name into the same fields list,
        # so the allowlist check rejects it just as it would for a
        # set/append on the same field.
        with pytest.raises(ScopeError) as exc_info:
            check_scope(
                "janitor", "edit",
                rel_path="task/example.md",
                fields=["description"],
            )
        assert "allowlist" in str(exc_info.value).lower()

    def test_distiller_scope_unset_via_edit_gate(self) -> None:
        """Distiller scope uses ``distiller_fields_only`` for edit —
        not a field allowlist, so the scope check is just an
        operation-level permit. Verify it doesn't accidentally refuse
        because unset names appear in the fields list."""
        check_scope(
            "distiller", "edit",
            rel_path="assumption/example.md",
            fields=["distiller_signals"],
        )


# ---------------------------------------------------------------------------
# mutation_log integration
# ---------------------------------------------------------------------------


class TestUnsetMutationLog:
    def test_build_audit_mutations_unset_routes_to_modified(self) -> None:
        """The new op-string maps to the files_modified bucket
        (same file-state-change as ``edit``)."""
        result = build_audit_mutations("unset", "task/Example.md")
        assert result["files_modified"] == ["task/Example.md"]
        assert result["files_created"] == []
        assert result["files_deleted"] == []

    def test_read_mutations_recognises_unset_op(
        self, tmp_path: Path,
    ) -> None:
        """A session JSONL file with op="unset" entries should route
        to the files_modified bucket — read_mutations is the agent-
        backend wrap-up path that flushes session logs into audit logs."""
        session = tmp_path / "session.jsonl"
        session.write_text(
            '{"op": "unset", "path": "task/Foo.md", "ts": "2026-05-28T00:00:00Z"}\n'
            '{"op": "edit", "path": "task/Bar.md", "ts": "2026-05-28T00:00:01Z"}\n',
            encoding="utf-8",
        )
        result = read_mutations(str(session))
        assert "task/Foo.md" in result["files_modified"]
        assert "task/Bar.md" in result["files_modified"]
        assert result["files_created"] == []
        assert result["files_deleted"] == []

    def test_unset_distinct_op_in_audit_log_layer(self) -> None:
        """Even though both ``edit`` and ``unset`` map to the same
        bucket, the op-string itself is preserved at the bucket-dict
        layer's caller — operators can grep on op="unset" via the
        audit log's per-line ``op`` field (see mutation_log:204-208).
        Pin that both ops produce identical files_modified shapes
        so a regression in either branch surfaces."""
        edit_result = build_audit_mutations("edit", "task/X.md")
        unset_result = build_audit_mutations("unset", "task/X.md")
        # Same shape (file-state-change is identical) — operator
        # distinguishes intent via the audit-log line-level ``op``
        # field, not via the bucket assignment.
        assert edit_result == unset_result
