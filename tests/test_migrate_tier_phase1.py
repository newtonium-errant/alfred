"""Migration script tests — tier system Phase 1 (2026-05-28).

Tests the migration script ``alfred.scripts.migrate_tier_phase1``
(reachable via the top-level shim ``scripts/migrate_tier_phase1.py``)
against synthesized fixture vaults.

Test surface split:

  * **Plan-build** (no I/O): the discovery functions for each sub-task
    against in-fixture vaults. Pin idempotency-skip detection (already-
    renamed records / already-cancelled tasks / pre-existing routine)
    and the dispatch's three RRTS escalation values.
  * **Plan-rendering**: human-readable dry-run output. Pin section
    headers fire unconditionally per ``feedback_intentionally_left_
    blank.md``; sentinel strings on empty buckets.
  * **CLI dispatch**: ``main(["--dry-run"])`` exit code + no-writes
    invariant; ``main(["--vault", <bad>])`` returns exit 2.
  * **Subprocess command shape** (mocked ``_alfred_vault_cmd``):
    pin that each ``_apply_*`` helper builds the correct ``alfred
    vault edit/create`` invocation with the right ``--set`` /
    ``--unset`` / ``--body-append`` / ``--body-stdin`` flags. We mock
    the subprocess invocation rather than running it because the
    live-run path is the one the operator authorises in the dry-run
    inspection loop.

This is a one-shot migration script; the test suite ships to lock
the planning contract + subprocess-command shapes against future
edits to the unset/migration-scope surfaces. Per
``feedback_regression_pin_unconditional.md``: no module-level
``pytest.importorskip``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from alfred.scripts import migrate_tier_phase1 as mig


# --- Fixture vault builder -------------------------------------------------


def _write_task(
    vault: Path,
    name: str,
    *,
    tier: int | None = None,
    base_tier: int | None = None,
    status: str = "todo",
    priority: str | None = None,
    due: str | None = None,
    cancelled_at: str | None = None,
    extra_fm: dict | None = None,
    body: str = "",
) -> Path:
    """Write a task record to ``vault/task/<name>.md``.

    ``tier`` and ``base_tier`` are optional — omit to test the
    "neither field set" path.
    """
    task_dir = vault / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        "type: task",
        "created: '2026-05-27'",
        f"name: {name!r}",
        f"status: {status}",
    ]
    if tier is not None:
        fm_lines.append(f"tier: {tier}")
    if base_tier is not None:
        fm_lines.append(f"base_tier: {base_tier}")
    if priority is not None:
        fm_lines.append(f"priority: {priority}")
    if due is not None:
        fm_lines.append(f"due: '{due}'")
    if cancelled_at is not None:
        fm_lines.append(f"cancelled_at: '{cancelled_at}'")
    if extra_fm:
        for k, v in extra_fm.items():
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(body)
    path = task_dir / f"{name}.md"
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


def _build_fixture_vault(tmp_path: Path) -> Path:
    """Build a minimal Salem-like vault for the migration tests.

    Layout:
      * 3 task records with legacy ``tier:`` field (rename-eligible)
      * 1 task record with ``base_tier:`` already set (idempotency-skip)
      * 1 task record with NEITHER (out-of-scope; never touched)
      * ``RRTS Invoicing`` + ``RRTS Payroll`` (escalation targets)
      * 2 of the 5 standing-practice records (Reading + Exercise)
        so we test both "cancellable" and "missing" branches in
        sub-task 3
      * No pre-existing ``Standing Practices`` routine — sub-task
        3 will plan to create it

    Other practices (Writing / Playing Music / Listening to Music)
    will surface in ``tasks_missing`` — pinning the dispatch's
    warning shape.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "task").mkdir()
    (vault / "routine").mkdir()

    # --- Sub-task 1: tier rename candidates --------------------
    _write_task(vault, "T1 RRTS Onboarding",   tier=1)
    _write_task(vault, "T2 Misc Followup",     tier=2)
    _write_task(vault, "T3 Aspirational Goal", tier=3)
    # Already migrated (skip).
    _write_task(vault, "Already Migrated", base_tier=2)
    # Out-of-scope (neither field).
    _write_task(vault, "Out Of Scope")

    # --- Sub-task 2: RRTS escalation targets -------------------
    _write_task(vault, "RRTS Invoicing", priority="urgent", due="2026-05-27")
    _write_task(vault, "RRTS Payroll",   priority="urgent", due="2026-05-28")

    # --- Sub-task 3: 2 of 5 standing practices ---
    _write_task(vault, "Reading",  tier=3, priority="low")
    _write_task(vault, "Exercise", tier=3, priority="low")

    return vault


# --- Plan-build (sub-task 1) ----------------------------------------------


class TestTierRenameDiscovery:
    def test_rename_pending_records_found(self, tmp_path: Path) -> None:
        vault = _build_fixture_vault(tmp_path)
        pending, already, invalid = mig.discover_tier_renames(vault)
        # The 3 T1/T2/T3 records + Reading + Exercise + the
        # RRTS escalation targets (NOT — those don't have tier:).
        # Reading/Exercise carry tier: 3 in the fixture so they ARE
        # in pending.
        names = {p.rel_path for p in pending}
        assert "task/T1 RRTS Onboarding.md" in names
        assert "task/T2 Misc Followup.md" in names
        assert "task/T3 Aspirational Goal.md" in names
        assert "task/Reading.md" in names
        assert "task/Exercise.md" in names
        # Out-of-scope record (no tier, no base_tier) NOT in pending.
        assert "task/Out Of Scope.md" not in names
        # Already-migrated record (has base_tier, no tier) NOT in
        # pending; should be in the idempotency-skip list.
        assert "task/Already Migrated.md" not in names
        assert "task/Already Migrated.md" in already
        # No non-int tier records in the base fixture.
        assert invalid == []

    def test_tier_value_preserved(self, tmp_path: Path) -> None:
        """Pin that the int tier value carries through to base_tier
        unchanged — the rename is purely a key change."""
        vault = _build_fixture_vault(tmp_path)
        pending, _, _ = mig.discover_tier_renames(vault)
        by_path = {p.rel_path: p.tier_value for p in pending}
        assert by_path["task/T1 RRTS Onboarding.md"] == 1
        assert by_path["task/T2 Misc Followup.md"] == 2
        assert by_path["task/T3 Aspirational Goal.md"] == 3

    def test_no_task_dir_returns_empty(self, tmp_path: Path) -> None:
        """No ``task/`` dir → empty pending + empty already + empty
        invalid, no crash.

        Per ``feedback_intentionally_left_blank.md``: empty discovery
        on a missing-dir fixture must return empty buckets cleanly
        rather than raise — the report layer renders the empty buckets
        with the unconditional "nothing pending" sentinel."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        pending, already, invalid = mig.discover_tier_renames(empty)
        assert pending == []
        assert already == []
        assert invalid == []

    def test_non_int_tier_value_surfaces_to_invalid_bucket(
        self, tmp_path: Path,
    ) -> None:
        """Operator hand-edit produced ``tier: high`` (string) — the
        rename refuses to coerce. The record surfaces in the
        ``invalid`` bucket (with the raw value preserved) so the
        operator sees an explicit hand-edit-required flag in the
        dry-run report.

        Per ``feedback_intentionally_left_blank.md``: silent skip
        leaves no ground-truth count; the explicit bucket gives the
        operator a non-zero signal."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        _write_task(vault, "Bad Tier String", extra_fm={"tier": "high"})
        pending, already, invalid = mig.discover_tier_renames(vault)
        assert pending == []
        assert already == []
        # Record surfaces in invalid bucket with the raw value
        # preserved so the operator's dry-run report can name the
        # specific bad value.
        assert len(invalid) == 1
        rel_path, raw_value = invalid[0]
        assert rel_path == "task/Bad Tier String.md"
        assert raw_value == "high"

    def test_multiple_non_int_tier_values_all_surface(
        self, tmp_path: Path,
    ) -> None:
        """Two records with bad tier values → both in ``invalid``.
        Pin the per-record visibility (the WARNING block in
        print_plan iterates over invalid)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        _write_task(vault, "Bad String", extra_fm={"tier": "high"})
        _write_task(vault, "Bad List", extra_fm={"tier": "[1, 2]"})
        _, _, invalid = mig.discover_tier_renames(vault)
        assert len(invalid) == 2
        paths = {p for p, _ in invalid}
        assert "task/Bad String.md" in paths
        assert "task/Bad List.md" in paths


# --- Plan-build (sub-task 2) ----------------------------------------------


class TestEscalationDiscovery:
    def test_both_rrts_targets_found(self, tmp_path: Path) -> None:
        vault = _build_fixture_vault(tmp_path)
        pending, missing = mig.discover_escalation_targets(vault)
        names = {e.rel_path for e in pending}
        assert "task/RRTS Invoicing.md" in names
        assert "task/RRTS Payroll.md" in names
        assert missing == []

    def test_escalation_values_match_dispatch(self, tmp_path: Path) -> None:
        """Pin the dispatch-ratified values: base_tier=2, escalate_to=1,
        escalate_at_days=3 for Invoicing, 1 for Payroll."""
        vault = _build_fixture_vault(tmp_path)
        pending, _ = mig.discover_escalation_targets(vault)
        by_path = {e.rel_path: e for e in pending}
        inv = by_path["task/RRTS Invoicing.md"]
        assert inv.base_tier == 2
        assert inv.escalate_to == 1
        assert inv.escalate_at_days == 3
        pay = by_path["task/RRTS Payroll.md"]
        assert pay.base_tier == 2
        assert pay.escalate_to == 1
        assert pay.escalate_at_days == 1

    def test_missing_record_surfaces_to_warning(
        self, tmp_path: Path,
    ) -> None:
        """RRTS Invoicing exists, RRTS Payroll doesn't → Payroll
        surfaces in ``missing``, Invoicing in ``pending``."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        _write_task(vault, "RRTS Invoicing", priority="urgent")
        pending, missing = mig.discover_escalation_targets(vault)
        assert [e.rel_path for e in pending] == ["task/RRTS Invoicing.md"]
        assert missing == ["task/RRTS Payroll.md"]


# --- Plan-build (sub-task 3) ----------------------------------------------


class TestStandingPracticesDiscovery:
    def test_routine_creation_planned_when_absent(
        self, tmp_path: Path,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        to_create, exists, cancels, already, missing = (
            mig.discover_standing_practices(vault)
        )
        assert to_create is True
        assert exists is False

    def test_routine_skip_when_present(self, tmp_path: Path) -> None:
        """Pre-existing ``Standing Practices.md`` → idempotency-skip."""
        vault = _build_fixture_vault(tmp_path)
        (vault / "routine" / "Standing Practices.md").write_text(
            "---\ntype: routine\nname: Standing Practices\n"
            "created: '2026-05-28'\ncadence: {type: daily}\n"
            "items: []\nstatus: active\n---\n\n# Standing Practices\n",
            encoding="utf-8",
        )
        to_create, exists, cancels, already, missing = (
            mig.discover_standing_practices(vault)
        )
        assert to_create is False
        assert exists is True

    def test_task_cancels_for_existing_uncancelled(
        self, tmp_path: Path,
    ) -> None:
        """Reading + Exercise exist + status=todo → both in cancels.
        Writing/Playing Music/Listening to Music absent → in missing."""
        vault = _build_fixture_vault(tmp_path)
        _, _, cancels, already, missing = (
            mig.discover_standing_practices(vault)
        )
        cancel_paths = {c.rel_path for c in cancels}
        assert "task/Reading.md" in cancel_paths
        assert "task/Exercise.md" in cancel_paths
        assert already == []
        assert "task/Writing.md" in missing
        assert "task/Playing Music.md" in missing
        assert "task/Listening to Music.md" in missing

    def test_already_cancelled_task_is_idempotency_skipped(
        self, tmp_path: Path,
    ) -> None:
        """Re-run case: a task previously cancelled by the migration
        carries status=cancelled + cancelled_at. Should NOT be in
        cancels; should be in already_cancelled."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()
        _write_task(
            vault, "Reading",
            tier=3,
            status="cancelled",
            cancelled_at="2026-05-28",
        )
        _, _, cancels, already, _ = (
            mig.discover_standing_practices(vault)
        )
        assert [c.rel_path for c in cancels] == []
        assert "task/Reading.md" in already

    def test_cancelled_without_cancelled_at_still_cancels(
        self, tmp_path: Path,
    ) -> None:
        """Defensive: status=cancelled but no cancelled_at → treat as
        not-fully-migrated, re-apply the cancel + stamp. Better to
        re-run than to leave a record half-migrated."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()
        _write_task(vault, "Reading", tier=3, status="cancelled")
        _, _, cancels, already, _ = (
            mig.discover_standing_practices(vault)
        )
        # Reading is in cancels (no cancelled_at = treat as not-fully-
        # migrated, re-cancel).
        cancel_paths = {c.rel_path for c in cancels}
        assert "task/Reading.md" in cancel_paths
        assert already == []


# --- build_plan integration -----------------------------------------------


class TestBuildPlan:
    def test_full_plan_assembly(self, tmp_path: Path) -> None:
        """End-to-end plan over the fixture vault. Pin all the bucket
        counts at once so a refactor that breaks one sub-task's
        discovery surfaces here too."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        # Sub-task 1: 5 records (T1/T2/T3 + Reading + Exercise)
        assert len(plan.tier_renames) == 5
        assert plan.tier_already_renamed == ["task/Already Migrated.md"]
        # Sub-task 2: 2 records (both RRTS targets exist)
        assert len(plan.escalation_sets) == 2
        assert plan.escalation_missing_records == []
        # Sub-task 3: routine to create, 2 cancels, 3 missing
        assert plan.routine_to_create is True
        assert plan.routine_already_exists is False
        assert len(plan.task_cancels) == 2
        assert len(plan.tasks_missing) == 3


# --- Plan rendering --------------------------------------------------------


class TestPrintPlan:
    def test_dry_run_marker_appears(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_live_run_marker_appears(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=False)
        out = capsys.readouterr().out
        assert "LIVE RUN" in out

    def test_all_three_section_headers_emit_unconditionally(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Per intentionally-left-blank: every sub-task's section
        header is emitted regardless of whether its bucket is empty.
        Pin via the empty-vault edge case."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        plan = mig.build_plan(empty)
        mig.print_plan(plan, empty, dry_run=True)
        out = capsys.readouterr().out
        assert "Sub-task 1: rename tier: → base_tier:" in out
        assert "Sub-task 2: populate RRTS escalation fields" in out
        assert "Sub-task 3: standing-practices → routine" in out

    def test_empty_buckets_emit_explicit_sentinels(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Empty buckets render an explicit "nothing pending" line —
        operator must never see a silent gap."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        plan = mig.build_plan(empty)
        mig.print_plan(plan, empty, dry_run=True)
        out = capsys.readouterr().out
        assert "no records pending rename" in out
        assert "no records to update" in out
        assert "no task cancellations pending" in out

    def test_missing_rrts_records_emit_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """When dispatch-named records are absent, the dry-run
        emits a WARNING with the missing paths so the operator
        notices BEFORE authorising the live run."""
        # Vault with no RRTS records at all.
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "task/RRTS Invoicing.md" in out
        assert "task/RRTS Payroll.md" in out


# --- Subprocess command shape (mocked) ------------------------------------


class TestSubprocessCommandShape:
    """Pin the EXACT command shape the script builds for each apply
    helper. Mocks ``_alfred_vault_cmd`` to capture the call arguments
    rather than actually running subprocesses — the live-run path is
    operator-authorised via dry-run inspection.
    """

    def test_tier_rename_invokes_set_base_tier_and_unset_tier(
        self, tmp_path: Path,
    ) -> None:
        """Sub-task 1 apply: ``vault edit <path> --set base_tier=<N>
        --unset tier``. Both flags in a single call so the audit log
        emits ONE edit + ONE unset entry per record."""
        vault = tmp_path / "vault"
        vault.mkdir()
        entry = mig.TierRename(
            rel_path="task/T2 Misc Followup.md",
            tier_value=2,
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_tier_rename(entry, env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        # Verb is first positional
        assert call_args.args[0] == "edit"
        # Path is second positional
        assert call_args.args[1] == "task/T2 Misc Followup.md"
        # Flags include --set base_tier=2 AND --unset tier
        flat_args = list(call_args.args[2:])
        assert "--set" in flat_args
        assert "base_tier=2" in flat_args
        assert "--unset" in flat_args
        assert "tier" in flat_args

    def test_escalation_set_invokes_three_set_flags(
        self, tmp_path: Path,
    ) -> None:
        """Sub-task 2 apply: ``vault edit <path> --set base_tier=<N>
        --set escalate_to=<N> --set escalate_at_days=<N>``."""
        vault = tmp_path / "vault"
        vault.mkdir()
        entry = mig.EscalationSet(
            rel_path="task/RRTS Invoicing.md",
            base_tier=2,
            escalate_to=1,
            escalate_at_days=3,
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_escalation_set(entry, env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        assert call_args.args[0] == "edit"
        assert call_args.args[1] == "task/RRTS Invoicing.md"
        flat = list(call_args.args[2:])
        # All three set pairs present.
        assert "base_tier=2" in flat
        assert "escalate_to=1" in flat
        assert "escalate_at_days=3" in flat
        assert flat.count("--set") == 3

    def test_routine_create_invokes_with_body_stdin(
        self, tmp_path: Path,
    ) -> None:
        """Sub-task 3 apply: routine creation passes the routine body
        via stdin (NOT --body-append), uses --body-stdin flag, and
        sets cadence + items + completion_log + status frontmatter."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_routine_create(env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        # First positional is verb, then "routine", then name
        assert call_args.args[0] == "create"
        assert call_args.args[1] == "routine"
        assert call_args.args[2] == "Standing Practices"
        flat = list(call_args.args[3:])
        # --body-stdin present (body on stdin, not as --body-append).
        assert "--body-stdin" in flat
        # status=active is set.
        assert "status=active" in flat
        # cadence is a JSON-encoded dict the CLI's set parser will
        # re-parse via json.loads.
        cadence_value = next(
            v for v in flat if v.startswith("cadence=")
        )
        assert '{"type": "daily"}' in cadence_value
        # The stdin body content carries the migration-history prose.
        assert call_args.kwargs["stdin"] == mig.ROUTINE_BODY
        assert "Standing Practices" in mig.ROUTINE_BODY
        assert "Migration history" in mig.ROUTINE_BODY
        # All five practices appear in the body as backlinks.
        for practice in mig.STANDING_PRACTICE_TASKS:
            assert f"[[task/{practice}]]" in mig.ROUTINE_BODY

    def test_routine_items_carry_aspirational_priority(
        self, tmp_path: Path,
    ) -> None:
        """Pin that the routine's items array uses ``aspirational``
        priority (matches the brief render's bucket name)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_routine_create(env)
        flat = list(mocked.call_args.args[3:])
        items_value = next(v for v in flat if v.startswith("items="))
        # Each of the 5 practices is in an aspirational item.
        for practice in mig.STANDING_PRACTICE_TASKS:
            assert practice in items_value
        # The priority value is literal 'aspirational' (the brief
        # render's tracked/critical/aspirational bucket name).
        assert "aspirational" in items_value

    def test_task_cancel_invokes_status_and_body_append(
        self, tmp_path: Path,
    ) -> None:
        """Sub-task 3 cancel apply: ``vault edit <path>
        --set status=cancelled --set cancelled_at=<date>
        --body-append <migration note>``."""
        vault = tmp_path / "vault"
        vault.mkdir()
        entry = mig.TaskCancel(
            rel_path="task/Reading.md",
            practice_name="Reading",
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_task_cancel(entry, env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        assert call_args.args[0] == "edit"
        assert call_args.args[1] == "task/Reading.md"
        flat = list(call_args.args[2:])
        assert "status=cancelled" in flat
        assert f"cancelled_at={mig.MIGRATION_DATE}" in flat
        # Body-append flag + value.
        idx = flat.index("--body-append")
        body_arg = flat[idx + 1]
        assert "Migration note" in body_arg
        assert f"[[routine/{mig.STANDING_PRACTICES_ROUTINE}]]" in body_arg


# --- Apply orchestrator (counter shape, mocked subprocess) ----------------


class TestApplyPlanCounters:
    def test_counters_match_pending_plan(self, tmp_path: Path) -> None:
        """End-to-end ``apply_plan`` with subprocess mocked — pin the
        counter dict matches the pending plan exactly. Idempotency-
        skip records contribute zero to counters even if they appear
        in the plan."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        with patch.object(mig, "_alfred_vault_cmd"):
            counters = mig.apply_plan(plan, vault)
        # 5 tier renames pending (T1/T2/T3 + Reading + Exercise).
        assert counters["tier_renamed"] == 5
        # 2 RRTS escalation sets pending.
        assert counters["escalation_set"] == 2
        # 1 routine to create (Standing Practices absent from fixture).
        assert counters["routine_created"] == 1
        # 2 task cancels (Reading + Exercise in fixture; 3 missing).
        assert counters["tasks_cancelled"] == 2

    def test_idempotent_rerun_zero_counters(self, tmp_path: Path) -> None:
        """When the plan is fully empty after a prior successful
        migration, counters are all zero.

        Concrete fixture shape for "fully-migrated steady state":
          * Routine ``Standing Practices.md`` already exists
            (idempotency-skip: routine_to_create=False).
          * Task records already carry ``base_tier:`` and NO
            ``tier:`` (idempotency-skip: tier_already_renamed
            populated, tier_renames empty).
          * Standing-practice tasks already carry ``status:
            cancelled`` AND ``cancelled_at:`` (idempotency-skip:
            tasks_already_cancelled populated, task_cancels empty).
          * No RRTS records at all (escalation_sets empty AND
            escalation_missing_records populated — but missing
            records don't contribute to apply counters either way).

        Pin per code-reviewer NOTE 2026-05-28: the original test
        used a totally-empty vault, which had ``routine_to_create=
        True`` because the routine file was absent. The "fully-
        migrated" fixture below is the actual steady state."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()

        # Routine already created — sub-task 3 routine creation skips.
        (vault / "routine" / "Standing Practices.md").write_text(
            "---\ntype: routine\nname: Standing Practices\n"
            "created: '2026-05-28'\ncadence: {type: daily}\n"
            "items: []\nstatus: active\n---\n\n# Standing Practices\n",
            encoding="utf-8",
        )

        # Tasks already cancelled (status + cancelled_at present)
        # — sub-task 3 task-cancel skips.
        for practice in mig.STANDING_PRACTICE_TASKS:
            _write_task(
                vault, practice,
                base_tier=3,
                status="cancelled",
                cancelled_at=mig.MIGRATION_DATE,
            )

        # An extra task already carrying base_tier (no tier:) —
        # sub-task 1 idempotency-skip.
        _write_task(vault, "Already Renamed", base_tier=2)

        plan = mig.build_plan(vault)
        # Sanity: nothing pending across all three sub-tasks.
        assert plan.tier_renames == []
        assert plan.escalation_sets == []
        assert plan.routine_to_create is False
        assert plan.task_cancels == []

        with patch.object(mig, "_alfred_vault_cmd"):
            counters = mig.apply_plan(plan, vault)
        assert counters == {
            "tier_renamed":           0,
            "escalation_set":         0,
            "routine_created":        0,
            "tasks_cancelled":        0,
        }


# --- CLI ------------------------------------------------------------------


class TestMainCLI:
    def test_dry_run_returns_zero_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        # File-count snapshot pre-run.
        before = {p.name: p.read_bytes() for p in (vault / "task").glob("*.md")}
        rc = mig.main(["--vault", str(vault), "--dry-run"])
        assert rc == 0
        # File bytes unchanged.
        after = {p.name: p.read_bytes() for p in (vault / "task").glob("*.md")}
        assert before == after
        # No routine created.
        assert not (vault / "routine" / "Standing Practices.md").exists()
        # Output mentions DRY-RUN.
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_missing_vault_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = mig.main(["--vault", str(tmp_path / "does-not-exist")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "not a directory" in err

    def test_live_run_invokes_subprocess_per_change(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Live-run mode (default — no --dry-run) calls
        ``_alfred_vault_cmd`` once per mutation. Mocked so no real
        subprocess fires; pin the call count matches the plan shape.

        Fixture has 5 tier renames + 2 escalation sets + 1 routine
        create + 2 task cancels = 10 subprocess invocations."""
        vault = _build_fixture_vault(tmp_path)
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            rc = mig.main(["--vault", str(vault)])
        assert rc == 0
        assert mocked.call_count == 10
        out = capsys.readouterr().out
        assert "Migration complete" in out

    def test_live_run_summary_line_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Per dispatch: print a final summary line shaped
        ``Summary: renamed=N escalation_set=N routine_created=N
        tasks_cancelled=N``. Pin so a refactor doesn't drop it."""
        vault = _build_fixture_vault(tmp_path)
        with patch.object(mig, "_alfred_vault_cmd"):
            mig.main(["--vault", str(vault)])
        out = capsys.readouterr().out
        assert "Summary:" in out
        assert "renamed=5" in out
        assert "escalation_set=2" in out
        assert "routine_created=1" in out
        assert "tasks_cancelled=2" in out


# --- Subprocess env-var threading ------------------------------------------


class TestSubprocessEnv:
    def test_env_carries_migration_scope(self, tmp_path: Path) -> None:
        """Pin that the subprocess env carries
        ``ALFRED_VAULT_SCOPE=migration`` — without this the writes
        would route through the no-scope branch and miss the migration
        scope's audit-trail and create-allowlist enforcement."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_SCOPE"] == "migration"
        assert env["ALFRED_VAULT_PATH"] == str(vault)
        # Audit log + session file paths set.
        assert "ALFRED_VAULT_AUDIT_LOG" in env
        assert "ALFRED_VAULT_SESSION" in env
        # Session file is unique per call (UUID-stamped) so concurrent
        # migration runs don't collide.
        env2 = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_SESSION"] != env2["ALFRED_VAULT_SESSION"]


# --- Module-level constants pinning ----------------------------------------


class TestModuleConstants:
    """Pin the dispatch-ratified values. If any change, dispatch
    needs to know."""

    def test_migration_date_pinned(self) -> None:
        assert mig.MIGRATION_DATE == "2026-05-28"

    def test_standing_practice_tasks_pinned(self) -> None:
        assert mig.STANDING_PRACTICE_TASKS == (
            "Reading",
            "Writing",
            "Playing Music",
            "Listening to Music",
            "Exercise",
        )

    def test_rrts_escalation_targets_pinned(self) -> None:
        """Dispatch ratification: 2/1/3 for Invoicing, 2/1/1 for
        Payroll. Pin so a value-drift in the script doesn't slip
        past review."""
        assert mig.RRTS_ESCALATION_TARGETS == (
            ("RRTS Invoicing", 2, 1, 3),
            ("RRTS Payroll",   2, 1, 1),
        )

    def test_routine_body_contains_all_practices(self) -> None:
        """Pin that the routine body backlinks every practice in
        ``STANDING_PRACTICE_TASKS`` — operator opening the routine
        record can trace every cancelled origin."""
        for practice in mig.STANDING_PRACTICE_TASKS:
            assert f"[[task/{practice}]]" in mig.ROUTINE_BODY
        assert "Migration history" in mig.ROUTINE_BODY
        assert mig.MIGRATION_DATE in mig.ROUTINE_BODY

    def test_task_cancel_body_append_carries_routine_backlink(self) -> None:
        """Pin that the cancelled-task body append carries a backlink
        to the new routine — operator opening a cancelled task can
        navigate to the live continuation."""
        assert (
            f"[[routine/{mig.STANDING_PRACTICES_ROUTINE}]]"
            in mig.TASK_CANCEL_BODY_APPEND
        )
        assert "Migration note" in mig.TASK_CANCEL_BODY_APPEND
        assert mig.MIGRATION_DATE in mig.TASK_CANCEL_BODY_APPEND


# --- tier_invalid rendering (reviewer WARN 2a) ----------------------------


class TestTierInvalidRendering:
    """Per reviewer NOTE 2a (2026-05-28): non-int ``tier:`` values
    surface in the dry-run report as a WARNING block with the
    specific records + raw values named. The hand-edit-required
    flag is the operator's explicit signal to fix the data BEFORE
    the live run.
    """

    def test_invalid_warning_block_lists_each_record(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Two records with non-int tier values → WARNING block
        names both with their raw values."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()
        _write_task(vault, "Bad String", extra_fm={"tier": "high"})
        _write_task(vault, "Bad List", extra_fm={"tier": "[1, 2]"})
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out

        # WARNING block fires under sub-task 1.
        assert "WARNING" in out
        assert "non-int tier values" in out
        assert "hand-edit required" in out
        # Both record paths named.
        assert "task/Bad String.md" in out
        assert "task/Bad List.md" in out
        # Raw values rendered via repr() so an operator can spot
        # exactly what's wrong (string vs list vs other type).
        assert "'high'" in out

    def test_no_invalid_no_warning_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """No non-int tier values → no WARNING block (the WARNING is
        the explicit signal, NOT the default state)."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        # No tier-invalid WARNING. (Sub-task 2's WARNING for missing
        # RRTS targets isn't present either because the fixture
        # populates both Invoicing + Payroll.)
        assert "non-int tier values" not in out


# --- Partial migration handling (reviewer WARN 2b) ------------------------


class TestPartialMigrationHandling:
    """Per reviewer NOTE 2b (2026-05-28): mid-stream subprocess
    failure surfaces an operator-facing partial-migration sentinel
    + recovery pointer, then re-raises so the caller can return a
    non-zero exit code. Pin the print path AND the re-raise contract.
    """

    def test_runtime_error_mid_subtask_prints_partial_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """When ``_alfred_vault_cmd`` raises on the Nth record of
        sub-task 1, the script:
          * prints the partial-migration sentinel naming the count
            of records ALREADY written (N-1)
          * prints the recovery pointer
          * re-raises so the caller surfaces non-zero exit
        """
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)

        # Fail on the 3rd subprocess invocation (after 2 records
        # have landed successfully). Counter for tier_renamed will
        # be 2 at the point of failure.
        call_count = {"n": 0}

        def _flaky_cmd(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError(
                    "alfred vault edit failed: Exit code 1: "
                    "simulated CLI failure (stderr='', stdout_tail='', "
                    "cmd=['python', '-m', 'alfred.cli', 'vault', 'edit', "
                    "'task/T3 Aspirational Goal.md'])"
                )
            return {}

        with patch.object(mig, "_alfred_vault_cmd", side_effect=_flaky_cmd):
            with pytest.raises(RuntimeError):
                mig.apply_plan(plan, vault)

        out = capsys.readouterr().out
        # Partial-migration sentinel fires with the count of
        # records-before-failure.
        assert "PARTIAL MIGRATION" in out
        assert "2 record(s) written before failure" in out
        # Recovery pointer present.
        assert "re-run the script" in out
        assert "idempotency will skip" in out

    def test_main_returns_one_on_runtime_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """End-to-end: ``main(["--vault", <vault>])`` returns 1 (not 0)
        when ``apply_plan`` raises. The dry-run path is unaffected
        — only live-run-with-failure surfaces non-zero exit.

        Pin the ``main`` integration so a refactor that catches the
        RuntimeError in the wrong place silently returns 0 and
        deceives a CI / wrapper script downstream."""
        vault = _build_fixture_vault(tmp_path)

        def _always_fail(*args, **kwargs):
            raise RuntimeError("simulated subprocess failure")

        with patch.object(mig, "_alfred_vault_cmd", side_effect=_always_fail):
            rc = mig.main(["--vault", str(vault)])
        assert rc == 1
        # Sentinel + failure-cause line both printed. Consume the
        # buffer ONCE — capsys.readouterr() resets the captures,
        # so calling it twice would lose the second stream.
        captured = capsys.readouterr()
        assert "PARTIAL MIGRATION" in captured.out
        # The "Failure cause:" tail line goes to stderr per main()'s
        # contract — keeps the failure detail in the operator's
        # error-stream view distinct from the progress logs.
        assert "Failure cause:" in captured.err

    def test_rerun_after_partial_converges_via_idempotency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Pin the idempotency contract that makes the partial-
        migration recovery shape work in practice: after a partial
        failure, re-running the script with the same vault state
        SHOULD plan only the unfinished records.

        Concrete shape: simulate that sub-task 1 partially completed
        (2 records renamed, 3 still pending). The re-run's
        ``build_plan`` should show only 3 pending renames + the 2
        already-renamed records in the idempotency-skip list."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        (vault / "routine").mkdir()
        # Two records ALREADY renamed (base_tier set, no tier).
        _write_task(vault, "Already Done 1", base_tier=1)
        _write_task(vault, "Already Done 2", base_tier=2)
        # Three records STILL pending (tier set, no base_tier).
        _write_task(vault, "Still Pending 1", tier=1)
        _write_task(vault, "Still Pending 2", tier=2)
        _write_task(vault, "Still Pending 3", tier=3)

        plan = mig.build_plan(vault)
        # Only the 3 pending records show up in tier_renames.
        pending_paths = {p.rel_path for p in plan.tier_renames}
        assert pending_paths == {
            "task/Still Pending 1.md",
            "task/Still Pending 2.md",
            "task/Still Pending 3.md",
        }
        # The 2 already-renamed records are in the idempotency-skip.
        assert set(plan.tier_already_renamed) == {
            "task/Already Done 1.md",
            "task/Already Done 2.md",
        }


# --- _alfred_vault_cmd signature (reviewer NOTE 3) -------------------------


class TestAlfredVaultCmdSignature:
    """Per reviewer NOTE 3: the ``vault_path`` kwarg was unused
    documentation noise; dropped 2026-05-28. Pin the signature so a
    future re-add surfaces here as a regression."""

    def test_signature_does_not_accept_vault_path(self) -> None:
        """``_alfred_vault_cmd(vault_path=...)`` should raise
        TypeError — the kwarg is no longer part of the signature."""
        import inspect
        sig = inspect.signature(mig._alfred_vault_cmd)
        assert "vault_path" not in sig.parameters
        # Sanity: ``env`` IS still a kwarg.
        assert "env" in sig.parameters
        assert "stdin" in sig.parameters
