"""Migration script tests — V1 tier-field strip (2026-05-30).

Tests the migration script
``alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields`` (reachable
via the top-level shim ``scripts/migrate_2026_05_30_strip_v1_tier_fields.py``)
against synthesized fixture vaults.

Test surface split:

  * **Plan-build** (no I/O): the discovery function against in-fixture
    vaults. Pin the record-shape buckets (both-dead-fields, base_tier-
    only, none, live-field-only) into the right pending / already-clean
    lists. Post-2026-06-26 the strip set is the two DEAD fields only
    (base_tier / escalate_to); ``escalate_at_days`` is the LIVE V2 knob
    and is NEVER stripped — a live-field-only record lands already-clean.
  * **Plan-rendering**: human-readable dry-run output. Pin section
    header fires unconditionally per ``feedback_intentionally_left_
    blank.md``; sentinel strings on empty bucket; per-record line
    naming exactly which fields will be unset.
  * **CLI dispatch**: ``main(["--dry-run"])`` exit code + no-writes
    invariant; ``main(["--vault", <bad>])`` returns exit 2.
  * **Subprocess command shape** (mocked ``_alfred_vault_cmd``): pin
    that ``_apply_strip`` builds the correct ``alfred vault edit``
    invocation with one ``--unset`` flag per ``fields_present`` entry
    (not for fields absent on the record).
  * **Subprocess wrapper canaries** (mirrored from
    ``test_migrate_tier_phase1.py``): empty-stdout, unparseable JSON,
    module-path regression. Same wrapper shape; same pin shape.

This is a one-shot migration script; the test suite ships to lock the
planning contract + subprocess-command shapes against future edits to
the unset / migration-scope / vault CLI surfaces. Per
``feedback_regression_pin_unconditional.md``: no module-level
``pytest.importorskip``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alfred.scripts import migrate_2026_05_30_strip_v1_tier_fields as mig


# --- Fixture vault builder -------------------------------------------------


def _write_task(
    vault: Path,
    name: str,
    *,
    base_tier: int | None = None,
    escalate_to: int | None = None,
    escalate_at_days: int | None = None,
    status: str = "todo",
    extra_fm: dict | None = None,
    body: str = "",
) -> Path:
    """Write a task record to ``vault/task/<name>.md``.

    All V1 tier fields are optional — omit to test the "field absent"
    discovery path.
    """
    task_dir = vault / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        "---",
        "type: task",
        "created: '2026-05-30'",
        f"name: {name!r}",
        f"status: {status}",
    ]
    if base_tier is not None:
        fm_lines.append(f"base_tier: {base_tier}")
    if escalate_to is not None:
        fm_lines.append(f"escalate_to: {escalate_to}")
    if escalate_at_days is not None:
        fm_lines.append(f"escalate_at_days: {escalate_at_days}")
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
    """Build a fixture vault with the V1-tier-field record shapes the
    live vault carries (per 2026-05-30 census), updated for the
    2026-06-26 narrowing of the strip set to the two DEAD fields
    (``base_tier`` / ``escalate_to``); ``escalate_at_days`` is now a
    LIVE V2 field and is NEVER stripped.

      * 1 record carrying both dead fields + the live field
        (``base_tier`` + ``escalate_to`` + ``escalate_at_days``):
        strip removes the two dead fields, PRESERVES ``escalate_at_days``.
        Mirrors RRTS Invoicing / RRTS Payroll / Pay Clinic Rental.
      * 1 base_tier-only record (mirrors the 25 records carrying just
        base_tier in live data) — strip removes that one dead field.
      * 1 out-of-scope record (no V1 fields) — already-clean bucket.
      * 1 partial-two-dead-fields record (base_tier + escalate_to, no
        escalate_at_days) — strip removes exactly those two.
      * 1 LIVE-FIELD-ONLY record (escalate_at_days, NO dead fields) —
        MUST land in the already-clean bucket: the strip never touches
        the live V2 knob. This is the landmine-guard fixture.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "task").mkdir()

    # Both dead fields + the live field — strip removes the 2 dead
    # fields, PRESERVES escalate_at_days.
    _write_task(
        vault, "Both Dead Plus Live",
        base_tier=2, escalate_to=1, escalate_at_days=3,
        status="cancelled",
    )
    # base_tier only — strip removes just that one dead field.
    _write_task(vault, "Only Base Tier", base_tier=1)
    # Two dead fields — strip removes exactly those two.
    _write_task(
        vault, "Partial Two Fields",
        base_tier=3, escalate_to=2,
    )
    # No V1 fields — already-clean bucket.
    _write_task(vault, "Already Clean")
    # LIVE field ONLY (no dead fields) — the strip must NOT touch it;
    # lands in already-clean. Landmine-guard fixture.
    _write_task(vault, "Live Field Only", escalate_at_days=5)

    return vault


# --- Plan-build -----------------------------------------------------------


class TestStripDiscovery:
    def test_records_with_any_v1_field_in_pending(
        self, tmp_path: Path,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        pending, already_clean = mig.discover_strip_records(vault)
        names = {p.rel_path for p in pending}
        assert "task/Both Dead Plus Live.md" in names
        assert "task/Only Base Tier.md" in names
        assert "task/Partial Two Fields.md" in names
        # Already-clean record is in already_clean bucket, NOT pending.
        assert "task/Already Clean.md" not in names
        assert "task/Already Clean.md" in already_clean
        # LANDMINE GUARD: a record carrying ONLY the live escalate_at_days
        # field has no DEAD field to strip → already-clean, never pending.
        # If this flips to pending, the strip set has re-acquired the
        # live V2 knob and would sever task auto-T1 surfacing.
        assert "task/Live Field Only.md" not in names
        assert "task/Live Field Only.md" in already_clean

    def test_fields_present_lists_match_record_shape(
        self, tmp_path: Path,
    ) -> None:
        """Pin the per-record fields_present list — the dry-run report
        + apply path both consume this. Order matches V1_TIER_FIELDS
        (base_tier, escalate_to) for deterministic output.
        ``escalate_at_days`` is NO LONGER in the strip set (live V2
        field, 2026-06-26) so it never appears in fields_present even
        when present on the record."""
        vault = _build_fixture_vault(tmp_path)
        pending, _ = mig.discover_strip_records(vault)
        by_path = {p.rel_path: p.fields_present for p in pending}
        # Record carrying both dead fields + the live field reports ONLY
        # the two dead fields — escalate_at_days is preserved, so it's
        # not in the strip list.
        assert by_path["task/Both Dead Plus Live.md"] == [
            "base_tier", "escalate_to",
        ]
        # base_tier-only carries just base_tier.
        assert by_path["task/Only Base Tier.md"] == ["base_tier"]
        # Partial-two carries base_tier + escalate_to (in order).
        assert by_path["task/Partial Two Fields.md"] == [
            "base_tier", "escalate_to",
        ]

    def test_no_task_dir_returns_empty(self, tmp_path: Path) -> None:
        """No ``task/`` dir → empty pending + empty already_clean, no
        crash. Per ``feedback_intentionally_left_blank.md``: the
        rendering layer handles the empty case with the unconditional
        sentinel, not by absence."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        pending, already_clean = mig.discover_strip_records(empty)
        assert pending == []
        assert already_clean == []

    def test_records_sorted_alphabetically(self, tmp_path: Path) -> None:
        """Pin deterministic ordering — operator reading the dry-run
        sees a stable record list across runs against the same vault
        state."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        # Write in non-alpha order; discovery should sort.
        _write_task(vault, "Z Last", base_tier=1)
        _write_task(vault, "A First", base_tier=2)
        _write_task(vault, "M Middle", base_tier=3)
        pending, _ = mig.discover_strip_records(vault)
        paths = [p.rel_path for p in pending]
        assert paths == [
            "task/A First.md",
            "task/M Middle.md",
            "task/Z Last.md",
        ]


# --- build_plan integration -----------------------------------------------


class TestBuildPlan:
    def test_full_plan_bucket_counts(self, tmp_path: Path) -> None:
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        # 3 records carry at least one DEAD V1 field (base_tier /
        # escalate_to). The live-field-only record is NOT pending.
        assert len(plan.records_pending) == 3
        # 2 records have no DEAD V1 fields: "Already Clean" (none) +
        # "Live Field Only" (only the live escalate_at_days). Both
        # already-clean, sorted alphabetically.
        assert plan.records_already_clean == [
            "task/Already Clean.md",
            "task/Live Field Only.md",
        ]


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

    def test_section_header_emits_unconditionally(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Per intentionally-left-blank: section header fires regardless
        of whether the bucket is empty. Pin via empty-vault edge case."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        plan = mig.build_plan(empty)
        mig.print_plan(plan, empty, dry_run=True)
        out = capsys.readouterr().out
        assert "Strip V1 tier fields" in out

    def test_empty_pending_bucket_emits_explicit_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Empty pending bucket renders the explicit "(no records
        pending strip)" sentinel — operator must never see a silent
        gap."""
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        plan = mig.build_plan(empty)
        mig.print_plan(plan, empty, dry_run=True)
        out = capsys.readouterr().out
        assert "no records pending strip" in out

    def test_per_record_line_names_fields_to_unset(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Per-record dry-run line carries the explicit list of fields
        that'll be unset on that record — operator can compare against
        the live record's frontmatter BEFORE authorising the live run."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        # The both-dead-plus-live record's line names ONLY the two dead
        # fields — escalate_at_days is preserved, not listed.
        assert "task/Both Dead Plus Live.md" in out
        assert "base_tier, escalate_to" in out
        # And escalate_at_days must NOT appear as a field-to-unset
        # anywhere in the dry-run output (landmine guard at the render
        # layer — the operator reading the plan must never see the live
        # field slated for strip).
        assert "escalate_at_days" not in out
        # base_tier-only record's line names just base_tier.
        # Use a line-scoped match so we don't false-positive on the
        # both-dead-plus-live record's line above.
        lines = out.splitlines()
        only_base_lines = [
            ln for ln in lines if "task/Only Base Tier.md" in ln
        ]
        assert len(only_base_lines) == 1
        assert "(unset: base_tier)" in only_base_lines[0]

    def test_already_clean_idempotency_sentinel_when_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Pin the idempotency-skip sentinel surfaces with the count
        when at least one record is already clean."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "idempotency-skip" in out
        # 2 already-clean records: "Already Clean" (no V1 fields) +
        # "Live Field Only" (only the live escalate_at_days, no dead
        # field to strip).
        assert "2 record(s) already clean" in out

    def test_total_count_emitted_in_pending_bucket(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Pin the TOTAL line — operator scans for the record count
        ratification."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        mig.print_plan(plan, vault, dry_run=True)
        out = capsys.readouterr().out
        assert "TOTAL: 3 record(s) to strip" in out


# --- Subprocess command shape (mocked) ------------------------------------


class TestSubprocessCommandShape:
    """Pin the EXACT command shape ``_apply_strip`` builds. Mocks
    ``_alfred_vault_cmd`` to capture call arguments rather than running
    real subprocesses — the live-run path is operator-authorised via
    dry-run inspection.
    """

    def test_strip_both_dead_fields_emits_two_unset_flags(
        self, tmp_path: Path,
    ) -> None:
        """Both-dead-fields record: ``vault edit <path> --unset base_tier
        --unset escalate_to``. Two --unset flags in a single call so the
        audit log emits TWO unset rows per record (per the
        unset-capability dual-emission contract — verified against
        migrate_tier_phase1.py).

        This is the max real shape post-2026-06-26: ``discover_strip_
        records`` can produce at most these two dead fields in
        ``fields_present`` (``escalate_at_days`` is excluded from
        ``V1_TIER_FIELDS``)."""
        entry = mig.StripRecord(
            rel_path="task/RRTS Payroll.md",
            fields_present=["base_tier", "escalate_to"],
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_strip(entry, env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        assert call_args.args[0] == "edit"
        assert call_args.args[1] == "task/RRTS Payroll.md"
        flat = list(call_args.args[2:])
        # Two --unset flags + the two dead field names.
        assert flat.count("--unset") == 2
        assert "base_tier" in flat
        assert "escalate_to" in flat
        # The live V2 field is never emitted as an unset target.
        assert "escalate_at_days" not in flat

    def test_strip_base_tier_only_emits_one_unset_flag(
        self, tmp_path: Path,
    ) -> None:
        """base_tier-only record: ``vault edit <path> --unset base_tier``.
        Exactly one --unset flag — fields not present on the record
        are NOT included (skipping spurious mutation_log rows)."""
        entry = mig.StripRecord(
            rel_path="task/Only Base Tier.md",
            fields_present=["base_tier"],
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_strip(entry, env)
        mocked.assert_called_once()
        call_args = mocked.call_args
        assert call_args.args[0] == "edit"
        assert call_args.args[1] == "task/Only Base Tier.md"
        flat = list(call_args.args[2:])
        # Exactly one --unset flag.
        assert flat.count("--unset") == 1
        assert "base_tier" in flat
        # Other field names NOT in the flag list — skipped because
        # absent from the record.
        assert "escalate_to" not in flat
        assert "escalate_at_days" not in flat

    def test_strip_two_fields_emits_two_unset_flags(
        self, tmp_path: Path,
    ) -> None:
        """Partial-two-fields record: ``vault edit <path> --unset
        base_tier --unset escalate_to``. Pin that the absent third
        field is NOT in the call."""
        entry = mig.StripRecord(
            rel_path="task/Partial Two Fields.md",
            fields_present=["base_tier", "escalate_to"],
        )
        env: dict = {}
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            mig._apply_strip(entry, env)
        mocked.assert_called_once()
        flat = list(mocked.call_args.args[2:])
        assert flat.count("--unset") == 2
        assert "base_tier" in flat
        assert "escalate_to" in flat
        assert "escalate_at_days" not in flat


# --- Apply orchestrator (counter shape, mocked subprocess) ----------------


class TestApplyPlanCounters:
    def test_counters_match_pending_plan(self, tmp_path: Path) -> None:
        """End-to-end ``apply_plan`` with subprocess mocked — pin the
        counter dict matches the pending plan exactly.

        Fixture has 3 records pending (post-2026-06-26 strip set =
        base_tier / escalate_to only): Both Dead Plus Live (2 dead
        fields; escalate_at_days preserved), Only Base (1 field),
        Partial Two (2 fields). Total: 3 records / 5 fields."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        with patch.object(mig, "_alfred_vault_cmd"):
            counters = mig.apply_plan(plan, vault)
        assert counters["records_stripped"] == 3
        assert counters["fields_unset"] == 2 + 1 + 2  # = 5

    def test_idempotent_rerun_zero_counters(self, tmp_path: Path) -> None:
        """When the plan is fully empty after a prior successful
        migration (all task records carry zero V1 fields), counters
        are all zero — no subprocess invocations fire."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "task").mkdir()
        # Three records all already clean — no V1 fields.
        _write_task(vault, "Cleaned 1")
        _write_task(vault, "Cleaned 2")
        _write_task(vault, "Cleaned 3")

        plan = mig.build_plan(vault)
        assert plan.records_pending == []
        assert len(plan.records_already_clean) == 3

        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            counters = mig.apply_plan(plan, vault)
        assert counters == {
            "records_stripped": 0,
            "fields_unset":     0,
        }
        # No subprocess fired — pending bucket was empty.
        assert mocked.call_count == 0


# --- CLI ------------------------------------------------------------------


class TestMainCLI:
    def test_dry_run_returns_zero_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        vault = _build_fixture_vault(tmp_path)
        before = {
            p.name: p.read_bytes() for p in (vault / "task").glob("*.md")
        }
        rc = mig.main(["--vault", str(vault), "--dry-run"])
        assert rc == 0
        after = {
            p.name: p.read_bytes() for p in (vault / "task").glob("*.md")
        }
        # File bytes unchanged across dry-run.
        assert before == after
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_missing_vault_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = mig.main(["--vault", str(tmp_path / "does-not-exist")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "not a directory" in err

    def test_live_run_invokes_subprocess_per_pending_record(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Live-run mode (with the archived-acknowledgement flag) calls
        ``_alfred_vault_cmd`` once per pending record. Fixture has 3
        pending → 3 subprocess invocations."""
        vault = _build_fixture_vault(tmp_path)
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            rc = mig.main([
                "--vault", str(vault), "--i-understand-this-is-archived",
            ])
        assert rc == 0
        assert mocked.call_count == 3
        out = capsys.readouterr().out
        assert "Migration complete" in out

    def test_live_run_refused_without_archived_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """ARCHIVED-script live-run guard (2026-06-26). Without
        ``--i-understand-this-is-archived`` a live run is refused: the
        plan is printed, exit code 3, NO subprocess writes happen.
        Guards against a stale muscle-memory invocation mutating the
        production vault."""
        vault = _build_fixture_vault(tmp_path)
        before = {
            p.name: p.read_bytes() for p in (vault / "task").glob("*.md")
        }
        with patch.object(mig, "_alfred_vault_cmd") as mocked:
            rc = mig.main(["--vault", str(vault)])
        assert rc == 3
        # No vault writes attempted.
        assert mocked.call_count == 0
        after = {
            p.name: p.read_bytes() for p in (vault / "task").glob("*.md")
        }
        assert before == after
        err = capsys.readouterr().err
        assert "REFUSING LIVE RUN" in err
        assert "--i-understand-this-is-archived" in err

    def test_live_run_summary_line_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """Per migrate_tier_phase1 pattern: final ``Summary:`` line
        carries the counter shape. Pin so a refactor doesn't drop it.
        Post-2026-06-26 strip set (base_tier / escalate_to only):
        3 records / 5 fields."""
        vault = _build_fixture_vault(tmp_path)
        with patch.object(mig, "_alfred_vault_cmd"):
            mig.main([
                "--vault", str(vault), "--i-understand-this-is-archived",
            ])
        out = capsys.readouterr().out
        assert "Summary:" in out
        assert "records_stripped=3" in out
        assert "fields_unset=5" in out


# --- Subprocess env-var threading ------------------------------------------


class TestSubprocessEnv:
    def test_env_carries_migration_scope(self, tmp_path: Path) -> None:
        """Pin that the subprocess env carries
        ``ALFRED_VAULT_SCOPE=migration`` — without this the writes
        would route through the no-scope branch and miss the migration
        scope's audit-trail and unset-capability enforcement."""
        vault = tmp_path / "vault"
        vault.mkdir()
        env = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_SCOPE"] == "migration"
        assert env["ALFRED_VAULT_PATH"] == str(vault)
        assert "ALFRED_VAULT_AUDIT_LOG" in env
        assert "ALFRED_VAULT_SESSION" in env
        # Session file unique per call (UUID-stamped).
        env2 = mig._build_subprocess_env(vault)
        assert env["ALFRED_VAULT_SESSION"] != env2["ALFRED_VAULT_SESSION"]


# --- Module-level constants pinning ----------------------------------------


class TestModuleConstants:
    """Pin the V1 field list — if the field set changes, dispatch
    needs to know."""

    def test_v1_tier_fields_pinned(self) -> None:
        """The ACTIVE strip set: the two DEAD V1 fields in canonical
        order (base_tier, escalate_to). Pin so an accidental re-order
        doesn't break the deterministic dry-run output ordering — and,
        critically, so the live V2 ``escalate_at_days`` knob can't be
        re-added to the strip set without this test firing."""
        assert mig.V1_TIER_FIELDS == (
            "base_tier",
            "escalate_to",
        )

    def test_escalate_at_days_not_in_strip_set(self) -> None:
        """LANDMINE GUARD (routine-systems consolidation, 2026-06-26).

        ``escalate_at_days`` is the LIVE V2 due-window knob consumed by
        ``alfred.tier.compute.compute_auto_t1_candidates``. It was
        REMOVED from the strip set so this archived migration can no
        longer sever task auto-T1 surfacing. Re-adding it would
        reintroduce the landmine — pin the absence explicitly (not just
        implicitly via the tuple-equality pin above) so the intent is
        unmissable to a future editor."""
        assert "escalate_at_days" not in mig.V1_TIER_FIELDS, (
            "escalate_at_days is a LIVE V2 field; it must never be in "
            "the strip set. See the module banner + TIER_FIELDS history "
            "in alfred.vault.schema."
        )

    def test_historical_strip_set_preserved_for_provenance(self) -> None:
        """The original 3-field strip set is preserved (renamed,
        DO-NOT-USE) so a reader can see exactly what the superseded
        2026-05-30 migration would have stripped. Pin it stays a
        3-field record AND stays distinct from the active set (i.e. it
        is NOT what build_plan reads)."""
        assert mig._HISTORICAL_V1_TIER_FIELDS_DO_NOT_USE == (
            "base_tier",
            "escalate_to",
            "escalate_at_days",
        )
        assert (
            mig._HISTORICAL_V1_TIER_FIELDS_DO_NOT_USE != mig.V1_TIER_FIELDS
        )


# --- Partial migration handling -------------------------------------------


class TestPartialMigrationHandling:
    """Mirror of ``test_migrate_tier_phase1.py::TestPartialMigrationHandling``.
    Same wrapper shape — same failure / sentinel / re-raise contract.
    """

    def test_runtime_error_mid_stream_prints_partial_sentinel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """When ``_alfred_vault_cmd`` raises on the Nth record, the
        script prints the partial-migration sentinel naming the count
        of records ALREADY stripped (N-1) + recovery pointer + re-
        raises."""
        vault = _build_fixture_vault(tmp_path)
        plan = mig.build_plan(vault)
        # Fixture pending = 3 records. Fail on 2nd → counter = 1.
        call_count = {"n": 0}

        def _flaky_cmd(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError(
                    "alfred vault edit failed: Exit code 1: "
                    "simulated CLI failure"
                )
            return {}

        with patch.object(mig, "_alfred_vault_cmd", side_effect=_flaky_cmd):
            with pytest.raises(RuntimeError):
                mig.apply_plan(plan, vault)

        out = capsys.readouterr().out
        assert "PARTIAL MIGRATION" in out
        # Counter shows 1 record (1 prior call succeeded) and the
        # field count from that 1 record.
        assert "1 record(s) stripped" in out
        assert "re-run the script" in out
        assert "idempotency will skip" in out

    def test_main_returns_one_on_runtime_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """End-to-end: ``main(["--vault", <vault>])`` returns 1 (not 0)
        when ``apply_plan`` raises. Pin so a refactor that catches
        RuntimeError in the wrong place silently returns 0 + deceives
        a CI / wrapper downstream."""
        vault = _build_fixture_vault(tmp_path)

        def _always_fail(*args, **kwargs):
            raise RuntimeError("simulated subprocess failure")

        with patch.object(mig, "_alfred_vault_cmd", side_effect=_always_fail):
            rc = mig.main([
                "--vault", str(vault), "--i-understand-this-is-archived",
            ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "PARTIAL MIGRATION" in captured.out
        # Failure cause emitted to stderr per main()'s contract.
        assert "Failure cause:" in captured.err


# --- Subprocess module-path + parsing tightness ---------------------------


class TestAlfredVaultCmdSubprocessShape:
    """Mirror of ``test_migrate_tier_phase1.py::TestAlfredVaultCmd
    SubprocessShape``. Same wrapper shape — same regression pins.
    """

    def test_command_uses_alfred_module_not_alfred_cli(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin the module path. ``[sys.executable, '-m', 'alfred', ...]``
        is the canonical form; ``'alfred.cli'`` was the 2026-05-28
        silent no-op bug shape."""
        captured: dict = {}

        class _FakeProc:
            returncode = 0
            stdout = '{"path": "task/X.md", "fields_changed": ["base_tier"]}'
            stderr = ""

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr(mig.subprocess, "run", _fake_run)
        result = mig._alfred_vault_cmd(
            "edit", "task/X.md",
            "--unset", "base_tier",
            env={"ALFRED_VAULT_PATH": "/tmp/vault"},
        )
        assert result == {
            "path": "task/X.md",
            "fields_changed": ["base_tier"],
        }
        cmd = captured["cmd"]
        m_idx = cmd.index("-m")
        module_name = cmd[m_idx + 1]
        assert module_name == "alfred", (
            f"Expected module path 'alfred' (canonical __main__.py "
            f"dispatch); got {module_name!r}. The 'alfred.cli' shape "
            f"silently no-ops because cli.py has no __main__ guard."
        )
        assert cmd[m_idx + 2] == "vault"
        assert cmd[m_idx + 3] == "edit"
        assert cmd[m_idx + 4] == "task/X.md"

    def test_empty_stdout_with_exit_zero_raises_canary(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit-0 + empty stdout MUST raise. The canary phrase
        ``empty stdout`` is the operator-grep target."""

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            mig.subprocess, "run", lambda *a, **kw: _FakeProc(),
        )
        with pytest.raises(RuntimeError) as exc_info:
            mig._alfred_vault_cmd(
                "edit", "task/X.md",
                "--unset", "base_tier",
                env={"ALFRED_VAULT_PATH": "/tmp/vault"},
            )
        msg = str(exc_info.value)
        assert "empty stdout" in msg
        assert "edit" in msg
        assert "task/X.md" in msg

    def test_unparseable_stdout_with_exit_zero_raises_distinct(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-empty stdout with no parseable JSON MUST raise with a
        DISTINCT error message from the empty-stdout canary. Distinct
        shapes let the operator debug the right surface."""

        class _FakeProc:
            returncode = 0
            stdout = "garbage no json here\nstill no json"
            stderr = ""

        monkeypatch.setattr(
            mig.subprocess, "run", lambda *a, **kw: _FakeProc(),
        )
        with pytest.raises(RuntimeError) as exc_info:
            mig._alfred_vault_cmd(
                "edit", "task/X.md",
                env={"ALFRED_VAULT_PATH": "/tmp/vault"},
            )
        msg = str(exc_info.value)
        assert "not parseable as JSON" in msg
        assert "empty stdout" not in msg
        assert "garbage no json here" in msg

    def test_reversed_scan_returns_last_parseable_json_line(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structlog-pollution defense: if an info log line lands above
        the JSON payload, the reversed-line scan should still return
        the JSON payload (which lands last from cmd_vault's response
        shape)."""

        class _FakeProc:
            returncode = 0
            stdout = (
                'INFO some.event field=value\n'
                '{"path": "task/X.md", "fields_changed": ["base_tier"]}\n'
            )
            stderr = ""

        monkeypatch.setattr(
            mig.subprocess, "run", lambda *a, **kw: _FakeProc(),
        )
        result = mig._alfred_vault_cmd(
            "edit", "task/X.md",
            env={"ALFRED_VAULT_PATH": "/tmp/vault"},
        )
        assert result == {
            "path": "task/X.md",
            "fields_changed": ["base_tier"],
        }
