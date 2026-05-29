"""Migration script tests — Routine Phase 2A (2026-05-29).

Tests ``alfred.scripts.migrate_routine_recurring_bills`` (reachable
via the top-level shim ``scripts/migrate_routine_recurring_bills.py``)
against synthesized fixture vaults.

Mirrors the tier Phase 1 migration test surface:

  * **Plan-build**: discovery functions per sub-task against in-fixture
    vaults. Pin idempotency-skip detection (already-cancelled tasks,
    pre-existing routine with all items) + missing-task buckets.
  * **Plan-rendering**: human-readable dry-run output. Pin section
    headers fire unconditionally; sentinel strings on empty buckets;
    idempotent-no-op overall sentinel.
  * **CLI dispatch**: ``main(["--dry-run"])`` returns BEFORE apply.
  * **Subprocess command shape** (mocked ``_alfred_vault_cmd``): pin
    each ``_apply_*`` builds the correct ``alfred vault edit/create``
    invocation with the right flags + env vars.

Per ``feedback_regression_pin_unconditional.md``: no module-level
``pytest.importorskip``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from alfred.scripts import migrate_routine_recurring_bills as mig


# --- Fixture vault builder -------------------------------------------------


def _write_task(
    vault: Path,
    name: str,
    *,
    status: str = "todo",
    cancelled_at: str | None = None,
    migrated_to: str | None = None,
    extra_fm: dict | None = None,
    body: str = "",
) -> Path:
    """Write a task record to ``vault/task/<name>.md``."""
    task_dir = vault / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        "type: task",
        "created: '2026-05-28'",
        f"name: {name!r}",
        f"status: {status}",
    ]
    if cancelled_at is not None:
        fm_lines.append(f"cancelled_at: '{cancelled_at}'")
    if migrated_to is not None:
        fm_lines.append(f"migrated_to: {migrated_to!r}")
    if extra_fm:
        for k, v in extra_fm.items():
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(body)
    path = task_dir / f"{name}.md"
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


def _build_fixture_vault_all_pending(tmp_path: Path) -> Path:
    """Build a vault with all 3 target task records present + open
    (full migration pending)."""
    vault = tmp_path / "vault"
    for name in mig.TASKS_TO_CANCEL:
        _write_task(vault, name, status="todo")
    return vault


# ===========================================================================
# Plan discovery — task cancels
# ===========================================================================


class TestTaskCancelDiscovery:
    """Pin sub-task 1 discovery against various vault shapes."""

    def test_all_three_tasks_present_and_open_all_pending(
        self, tmp_path: Path,
    ) -> None:
        """3 task records all open → 3 pending cancellations, 0
        already, 0 missing."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        pending, already, missing = mig.discover_task_cancels(vault)
        assert len(pending) == 3
        assert len(already) == 0
        assert len(missing) == 0
        # All three named tasks surface in the pending list.
        rel_paths = {e.rel_path for e in pending}
        for name in mig.TASKS_TO_CANCEL:
            assert f"task/{name}.md" in rel_paths

    def test_one_task_missing_surfaces_in_missing_bucket(
        self, tmp_path: Path,
    ) -> None:
        """When a named target is absent from vault, surface in
        ``missing`` (the dry-run will WARNING it)."""
        vault = tmp_path / "vault"
        # Only 2 of the 3 target tasks exist.
        _write_task(vault, "RRTS Invoicing")
        _write_task(vault, "RRTS Payroll")
        pending, already, missing = mig.discover_task_cancels(vault)
        assert len(pending) == 2
        assert missing == ["task/Pay Clinic Rental to Hussein Rafih.md"]

    def test_task_already_cancelled_with_migrated_to_skipped(
        self, tmp_path: Path,
    ) -> None:
        """Idempotency: ``status: cancelled`` + ``migrated_to`` set →
        skip (already migrated)."""
        vault = tmp_path / "vault"
        _write_task(
            vault, "RRTS Invoicing",
            status="cancelled",
            cancelled_at="2026-05-29",
            migrated_to=mig.MIGRATED_TO_LINK,
        )
        _write_task(vault, "RRTS Payroll", status="todo")
        _write_task(
            vault, "Pay Clinic Rental to Hussein Rafih", status="todo",
        )
        pending, already, missing = mig.discover_task_cancels(vault)
        # Only the two not-yet-cancelled tasks surface as pending.
        assert len(pending) == 2
        assert already == ["task/RRTS Invoicing.md"]
        assert len(missing) == 0

    def test_task_cancelled_without_migrated_to_still_pending(
        self, tmp_path: Path,
    ) -> None:
        """Partial-state: a task cancelled WITHOUT migrated_to is still
        pending (operator may have cancelled it but the migration
        hasn't run). Live run will set migrated_to + body-append."""
        vault = tmp_path / "vault"
        _write_task(
            vault, "RRTS Invoicing",
            status="cancelled",
            cancelled_at="2026-05-20",
            # NO migrated_to.
        )
        _write_task(vault, "RRTS Payroll", status="todo")
        _write_task(
            vault, "Pay Clinic Rental to Hussein Rafih", status="todo",
        )
        pending, already, _missing = mig.discover_task_cancels(vault)
        # The cancelled-without-migrated_to task IS pending.
        assert any(e.rel_path == "task/RRTS Invoicing.md" for e in pending)
        assert "task/RRTS Invoicing.md" not in already


# ===========================================================================
# Plan discovery — routine create
# ===========================================================================


class TestRoutineCreateDiscovery:
    """Pin sub-task 2 discovery (routine create + missing items)."""

    def test_routine_does_not_exist_to_create(self, tmp_path: Path) -> None:
        """No routine file → routine_to_create=True."""
        vault = tmp_path / "vault"
        vault.mkdir()
        to_create, exists, missing_items = mig.discover_routine_create(vault)
        assert to_create is True
        assert exists is False
        assert missing_items == []

    def test_routine_exists_with_all_items_idempotency_skip(
        self, tmp_path: Path,
    ) -> None:
        """Routine exists with all 4 expected items → idempotency
        skip (routine_to_create=False, no missing items)."""
        vault = tmp_path / "vault"
        routine_dir = vault / "routine"
        routine_dir.mkdir(parents=True)
        # Build a routine file with all 4 expected items by text.
        items_yaml_lines = []
        for text, _, _, _, _ in mig.ROUTINE_ITEMS:
            items_yaml_lines.append(f"  - text: {text!r}")
            items_yaml_lines.append("    priority: tracked")
        fm = (
            "---\n"
            "type: routine\n"
            f"name: '{mig.ROUTINE_RECORD_NAME}'\n"
            "status: active\n"
            "items:\n"
            + "\n".join(items_yaml_lines) + "\n"
            "---\n"
        )
        (routine_dir / f"{mig.ROUTINE_RECORD_NAME}.md").write_text(
            fm, encoding="utf-8",
        )
        to_create, exists, missing_items = mig.discover_routine_create(vault)
        assert to_create is False
        assert exists is True
        assert missing_items == []

    def test_routine_exists_missing_one_item_fail_loud(
        self, tmp_path: Path,
    ) -> None:
        """Routine exists but is missing one expected item →
        FAIL-LOUD: routine_missing_items populated; the live run will
        SKIP rather than touch the operator-edited routine."""
        vault = tmp_path / "vault"
        routine_dir = vault / "routine"
        routine_dir.mkdir(parents=True)
        # Build with only 3 of the 4 expected items.
        items = mig.ROUTINE_ITEMS[:3]  # drop the last (RRTS Payroll)
        items_yaml_lines = []
        for text, _, _, _, _ in items:
            items_yaml_lines.append(f"  - text: {text!r}")
            items_yaml_lines.append("    priority: tracked")
        fm = (
            "---\n"
            "type: routine\n"
            f"name: '{mig.ROUTINE_RECORD_NAME}'\n"
            "items:\n"
            + "\n".join(items_yaml_lines) + "\n"
            "---\n"
        )
        (routine_dir / f"{mig.ROUTINE_RECORD_NAME}.md").write_text(
            fm, encoding="utf-8",
        )
        to_create, exists, missing_items = mig.discover_routine_create(vault)
        assert to_create is False
        assert exists is True
        # The 4th item ("RRTS Payroll") is missing.
        assert missing_items == ["RRTS Payroll"]


# ===========================================================================
# Plan rendering
# ===========================================================================


class TestPrintPlan:
    """Pin section headers + sentinel strings per
    feedback_intentionally_left_blank + feedback_plan_discovery_silent_skips.
    """

    def test_print_plan_dry_run_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Dry-run mode header surfaces clearly."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "Routine Phase 2A" in out
        assert "Recurring Bills + Admin" in out
        assert "DRY-RUN — no changes will be written" in out

    def test_print_plan_live_run_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Live-run mode header makes WILL-write explicit."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=False)
        out = capsys.readouterr().out
        assert "LIVE RUN — writes WILL happen" in out

    def test_print_plan_emits_sub_task_headers_unconditionally(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Even on an empty vault, both sub-task headers emit
        (intentionally-left-blank)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "Sub-task 1: cancel 3 one-shot recurring tasks" in out
        assert "Sub-task 2: create Recurring Bills + Admin routine" in out

    def test_print_plan_surfaces_missing_tasks_as_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing-target WARNING block fires per
        feedback_plan_discovery_silent_skips."""
        vault = tmp_path / "vault"
        # Only 1 of 3 target tasks present.
        _write_task(vault, "RRTS Invoicing")
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "WARNING: dispatch-named tasks missing from vault" in out
        assert "task/RRTS Payroll.md" in out
        assert "task/Pay Clinic Rental to Hussein Rafih.md" in out

    def test_print_plan_routine_missing_items_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Routine exists but missing expected items → WARNING +
        explicit SKIP-required hand-fix instruction."""
        vault = tmp_path / "vault"
        routine_dir = vault / "routine"
        routine_dir.mkdir(parents=True)
        fm = (
            "---\n"
            "type: routine\n"
            f"name: '{mig.ROUTINE_RECORD_NAME}'\n"
            "items:\n"
            "  - text: 'Pay Clinic Rental to Hussein Rafih'\n"
            "    priority: tracked\n"
            "---\n"
        )
        (routine_dir / f"{mig.ROUTINE_RECORD_NAME}.md").write_text(
            fm, encoding="utf-8",
        )
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "WARNING: routine/Recurring Bills + Admin.md exists" in out
        assert "missing" in out
        assert "Garbage Day" in out  # specifically a missing item
        assert "hand-edit" in out

    def test_print_plan_idempotent_no_op_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When all sub-tasks are idempotent no-op (re-run after
        successful migration), explicit sentinel surfaces."""
        vault = tmp_path / "vault"
        # All 3 tasks already cancelled with migrated_to.
        for name in mig.TASKS_TO_CANCEL:
            _write_task(
                vault, name,
                status="cancelled",
                cancelled_at=mig.MIGRATION_DATE,
                migrated_to=mig.MIGRATED_TO_LINK,
            )
        # Routine already exists with all 4 items.
        routine_dir = vault / "routine"
        routine_dir.mkdir(parents=True)
        items_yaml_lines = []
        for text, _, _, _, _ in mig.ROUTINE_ITEMS:
            items_yaml_lines.append(f"  - text: {text!r}")
            items_yaml_lines.append("    priority: tracked")
        fm = (
            "---\n"
            "type: routine\n"
            "items:\n"
            + "\n".join(items_yaml_lines) + "\n"
            "---\n"
        )
        (routine_dir / f"{mig.ROUTINE_RECORD_NAME}.md").write_text(
            fm, encoding="utf-8",
        )
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "All sub-tasks idempotent-no-op" in out


# ===========================================================================
# CLI dispatch — main()
# ===========================================================================


class TestMainCLI:
    """Pin CLI entry-point behaviour."""

    def test_dry_run_returns_zero_without_apply(
        self, tmp_path: Path,
    ) -> None:
        """``--dry-run`` returns 0 BEFORE any apply path runs."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        # Patch apply_plan to FAIL if invoked — proves dry-run returns
        # before any subprocess attempt.
        with patch.object(mig, "apply_plan") as mocked:
            mocked.side_effect = AssertionError(
                "apply_plan must not be called in dry-run mode"
            )
            rc = mig.main(["--dry-run", "--vault", str(vault)])
        assert rc == 0
        assert mocked.called is False

    def test_dry_run_prints_no_changes_written_footer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Dry-run footer makes the no-write contract operator-visible."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        mig.main(["--dry-run", "--vault", str(vault)])
        out = capsys.readouterr().out
        assert "DRY-RUN — no changes written" in out

    def test_invalid_vault_path_returns_exit_2(
        self, tmp_path: Path,
    ) -> None:
        """Non-directory ``--vault`` → exit 2 (mirrors tier Phase 1)."""
        rc = mig.main(["--vault", str(tmp_path / "does_not_exist")])
        assert rc == 2

    def test_live_run_invokes_apply_plan(self, tmp_path: Path) -> None:
        """Live run (no --dry-run) calls apply_plan (mocked here so we
        don't actually shell to subprocess)."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        with patch.object(mig, "apply_plan") as mocked:
            mocked.return_value = {"routine_created": 1, "tasks_cancelled": 3}
            rc = mig.main(["--vault", str(vault)])
        assert rc == 0
        mocked.assert_called_once()


# ===========================================================================
# Subprocess command shape
# ===========================================================================


class TestApplyTaskCancelCommandShape:
    """Pin the EXACT command the script builds for task cancellation."""

    def test_task_cancel_sets_status_cancelled_at_migrated_to_body_append(
        self, tmp_path: Path,
    ) -> None:
        """Task cancel: vault edit with --set status=cancelled +
        --set cancelled_at=<date> + --set migrated_to=<wikilink> +
        --body-append <migration note>."""
        entry = mig.TaskCancel(
            rel_path="task/RRTS Invoicing.md",
            task_name="RRTS Invoicing",
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_task_cancel(entry, env)
        mocked.assert_called_once()
        ca = mocked.call_args
        # Verb + path positional.
        assert ca.args[0] == "edit"
        assert ca.args[1] == "task/RRTS Invoicing.md"
        flat = list(ca.args[2:])
        # All three set fields + body-append.
        assert "--set" in flat
        assert "status=cancelled" in flat
        assert f"cancelled_at={mig.MIGRATION_DATE}" in flat
        assert f"migrated_to={mig.MIGRATED_TO_LINK}" in flat
        assert "--body-append" in flat
        # body-append carries the migration note.
        body_append_value = flat[flat.index("--body-append") + 1]
        assert "Migration note" in body_append_value
        assert "Routine Phase 2A" in body_append_value
        assert f"[[routine/{mig.ROUTINE_RECORD_NAME}]]" in body_append_value


class TestApplyRoutineCreateCommandShape:
    """Pin the EXACT command the script builds for routine creation."""

    def test_routine_create_invokes_vault_create_routine_with_body_stdin(
        self, tmp_path: Path,
    ) -> None:
        """Routine create: vault create routine <name> with --body-stdin
        for prose + --set frontmatter fields."""
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_routine_create(env)
        mocked.assert_called_once()
        ca = mocked.call_args
        # Verb + type + name positional.
        assert ca.args[0] == "create"
        assert ca.args[1] == "routine"
        assert ca.args[2] == mig.ROUTINE_RECORD_NAME
        flat = list(ca.args[3:])
        # --body-stdin present (body via stdin).
        assert "--body-stdin" in flat
        # Body content carries migration history + record name.
        assert ca.kwargs["stdin"] == mig.ROUTINE_BODY
        assert mig.ROUTINE_RECORD_NAME in mig.ROUTINE_BODY
        assert "Migration history" in mig.ROUTINE_BODY
        # status=active set.
        assert "status=active" in flat
        # cadence is daily.
        cadence_value = next(v for v in flat if v.startswith("cadence="))
        assert '{"type": "daily"}' in cadence_value
        # completion_log is empty dict (matches Core Daily.md fixture).
        completion_log_value = next(
            v for v in flat if v.startswith("completion_log=")
        )
        assert completion_log_value == "completion_log={}"

    def test_routine_items_json_carries_all_four_items_with_due_pattern(
        self,
    ) -> None:
        """Items list JSON-encodes 4 items each with text, priority,
        due_pattern dict, and escalate_at_days (surface_at_days only
        on T2-ramp items)."""
        items_json = mig._build_routine_items_json()
        items = json.loads(items_json)
        assert len(items) == 4
        # Pay Clinic Rental — surface_at_days=5, escalate=0.
        clinic = next(i for i in items if i["text"] == "Pay Clinic Rental to Hussein Rafih")
        assert clinic["priority"] == "tracked"
        assert clinic["due_pattern"] == {"type": "monthly", "day": 1}
        assert clinic["surface_at_days"] == 5
        assert clinic["escalate_at_days"] == 0
        # Garbage Day — T1-only (no surface_at_days field).
        garbage = next(i for i in items if i["text"] == "Garbage Day")
        assert garbage["priority"] == "critical"
        assert garbage["due_pattern"] == {
            "type": "biweekly", "day": "thu", "anchor": "2026-05-28",
        }
        assert "surface_at_days" not in garbage
        assert garbage["escalate_at_days"] == 1
        # RRTS Invoicing — weekly Tue, surface=1, escalate=0.
        invoicing = next(i for i in items if i["text"] == "RRTS Invoicing")
        assert invoicing["due_pattern"] == {"type": "weekly", "day": "tue"}
        assert invoicing["surface_at_days"] == 1
        assert invoicing["escalate_at_days"] == 0
        # RRTS Payroll — biweekly Thu, surface=1, escalate=0.
        payroll = next(i for i in items if i["text"] == "RRTS Payroll")
        assert payroll["due_pattern"] == {
            "type": "biweekly", "day": "thu", "anchor": "2026-05-29",
        }
        assert payroll["surface_at_days"] == 1
        assert payroll["escalate_at_days"] == 0


# ===========================================================================
# Apply ordering + counters
# ===========================================================================


class TestApplyPlanOrderingAndCounters:
    """Pin sub-task ordering (routine first, tasks second) + counters."""

    def test_apply_plan_creates_routine_before_cancelling_tasks(
        self, tmp_path: Path,
    ) -> None:
        """Order is load-bearing: the body-append on cancelled tasks
        references [[routine/...]] so the routine must exist first."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        plan = mig.build_plan(vault)
        call_order: list[str] = []
        def _track_call(verb, *args, env=None, stdin=None):
            # First positional after verb is path or type.
            call_order.append(f"{verb}:{args[0] if args else '?'}")
            return {"path": args[0] if args else "?"}
        with patch.object(mig, "_alfred_vault_cmd", side_effect=_track_call):
            mig.apply_plan(plan, vault)
        # First call should be the routine create.
        assert call_order[0].startswith("create:routine")
        # Remaining calls are task cancels.
        for call in call_order[1:]:
            assert call.startswith("edit:task/")

    def test_apply_plan_returns_counters(self, tmp_path: Path) -> None:
        """Counters reflect what landed."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        plan = mig.build_plan(vault)
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mocked.return_value = {"ok": True}
            counters = mig.apply_plan(plan, vault)
        assert counters["routine_created"] == 1
        assert counters["tasks_cancelled"] == 3

    def test_apply_plan_partial_migration_sentinel_on_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Mid-stream RuntimeError surfaces the grep-able sentinel."""
        vault = _build_fixture_vault_all_pending(tmp_path)
        plan = mig.build_plan(vault)
        # Routine creation succeeds; first task cancel fails.
        call_count = [0]
        def _maybe_fail(verb, *args, env=None, stdin=None):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise RuntimeError("mock subprocess failure")
            return {"ok": True}
        with patch.object(mig, "_alfred_vault_cmd", side_effect=_maybe_fail):
            with pytest.raises(RuntimeError):
                mig.apply_plan(plan, vault)
        out = capsys.readouterr().out
        assert "PARTIAL MIGRATION" in out
        assert "Recovery" in out

    def test_apply_plan_skips_routine_create_when_missing_items_fail_loud(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If discovery reports routine_missing_items, the live run
        prints a SKIP message + doesn't invoke routine create. Per
        the FAIL-LOUD design — operator must hand-fix."""
        vault = tmp_path / "vault"
        for name in mig.TASKS_TO_CANCEL:
            _write_task(vault, name, status="todo")
        # Routine exists but is missing one item.
        routine_dir = vault / "routine"
        routine_dir.mkdir(parents=True)
        fm = (
            "---\n"
            "type: routine\n"
            "items:\n"
            "  - text: 'Pay Clinic Rental to Hussein Rafih'\n"
            "    priority: tracked\n"
            "---\n"
        )
        (routine_dir / f"{mig.ROUTINE_RECORD_NAME}.md").write_text(
            fm, encoding="utf-8",
        )
        plan = mig.build_plan(vault)
        # The plan must reflect missing items.
        assert plan.routine_missing_items
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mocked.return_value = {"ok": True}
            mig.apply_plan(plan, vault)
        out = capsys.readouterr().out
        # SKIP message surfaced.
        assert "SKIP routine" in out
        # routine create NOT invoked (mocked only saw task cancels).
        verbs = [c.args[0] for c in mocked.call_args_list]
        assert "create" not in verbs
        assert all(v == "edit" for v in verbs)


# ===========================================================================
# Subprocess env-var bundle
# ===========================================================================


class TestSubprocessEnv:
    """Pin the env-var bundle threaded to ``_alfred_vault_cmd``."""

    def test_build_subprocess_env_sets_migration_scope(
        self, tmp_path: Path,
    ) -> None:
        """ALFRED_VAULT_SCOPE=migration is set per the migration scope's
        canonical name in scope.py SCOPE_RULES."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_SCOPE"] == "migration"

    def test_build_subprocess_env_sets_vault_path(
        self, tmp_path: Path,
    ) -> None:
        """ALFRED_VAULT_PATH points at the supplied vault root."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_PATH"] == str(vault)

    def test_build_subprocess_env_session_unique_per_call(
        self, tmp_path: Path,
    ) -> None:
        """Fresh UUID per call so multiple migration runs don't
        interleave audit-log entries."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env1 = mig._build_subprocess_env(vault)
        env2 = mig._build_subprocess_env(vault)
        assert env1["ALFRED_VAULT_SESSION"] != env2["ALFRED_VAULT_SESSION"]

    def test_build_subprocess_env_audit_log_path(
        self, tmp_path: Path,
    ) -> None:
        """ALFRED_VAULT_AUDIT_LOG points at the standard data dir."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_AUDIT_LOG"].endswith("vault_audit.log")


# ===========================================================================
# _alfred_vault_cmd subprocess shape — pin the module path
# ===========================================================================


class TestAlfredVaultCmdModulePath:
    """Critical pin: subprocess module path is ``python -m alfred``,
    NOT ``python -m alfred.cli``. The latter is the silent-failure
    shape that wasted the tier Phase 1 first-run (see the script's
    long docstring + the post-mortem in feedback_subprocess_module_path_canary).
    """

    def test_subprocess_module_path_is_alfred_not_alfred_cli(
        self, tmp_path: Path,
    ) -> None:
        """Capture the argv handed to subprocess.run to pin the module
        path. The script must invoke ``python -m alfred ...`` NOT
        ``python -m alfred.cli ...``."""
        captured_argv: list[list[str]] = []

        class FakeResult:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        def _fake_run(cmd, *, input=None, text=False, capture_output=False, env=None):
            captured_argv.append(list(cmd))
            return FakeResult()

        env: dict = {"ALFRED_VAULT_PATH": str(tmp_path)}
        with patch.object(mig.subprocess, "run", side_effect=_fake_run):
            mig._alfred_vault_cmd("edit", "task/X.md", "--set", "a=b", env=env)
        assert len(captured_argv) == 1
        argv = captured_argv[0]
        # ``python -m alfred`` shape (NOT alfred.cli).
        assert argv[0] == sys.executable
        assert argv[1] == "-m"
        assert argv[2] == "alfred"
        # NOT alfred.cli.
        assert argv[2] != "alfred.cli"


# ===========================================================================
# Module-level constants
# ===========================================================================


class TestModuleConstants:
    """Pin the module-level constants that callers (test code, dry-run
    output) depend on."""

    def test_migration_date_is_2026_05_29(self) -> None:
        """Migration date is the dispatch-ratified day."""
        assert mig.MIGRATION_DATE == "2026-05-29"

    def test_routine_record_name(self) -> None:
        """Routine record name matches the dispatch verbatim."""
        assert mig.ROUTINE_RECORD_NAME == "Recurring Bills + Admin"

    def test_tasks_to_cancel_are_three_named_records(self) -> None:
        """3 task records to cancel per the dispatch."""
        assert mig.TASKS_TO_CANCEL == (
            "RRTS Invoicing",
            "RRTS Payroll",
            "Pay Clinic Rental to Hussein Rafih",
        )

    def test_routine_items_count_is_four(self) -> None:
        """4 items in the new routine per the dispatch."""
        assert len(mig.ROUTINE_ITEMS) == 4

    def test_migrated_to_link_shape(self) -> None:
        """migrated_to wikilink points at the new routine."""
        assert mig.MIGRATED_TO_LINK == (
            f"[[routine/{mig.ROUTINE_RECORD_NAME}]]"
        )

    def test_routine_body_carries_migration_history_and_backlinks(
        self,
    ) -> None:
        """Routine body explains the migration + carries backlinks to
        the cancelled origin records."""
        assert "Migration history" in mig.ROUTINE_BODY
        assert "[[task/RRTS Invoicing]]" in mig.ROUTINE_BODY
        assert "[[task/RRTS Payroll]]" in mig.ROUTINE_BODY
        assert "[[task/Pay Clinic Rental to Hussein Rafih]]" in mig.ROUTINE_BODY
        # Garbage Day mentioned as fresh-added (not migrated from a task).
        assert "Garbage Day added fresh" in mig.ROUTINE_BODY
