"""Tests for ``alfred scaffold sync`` (Build #38).

Three test surfaces, in increasing layer-depth:

1. **sync module unit tests** — :func:`scan_scaffold`, :func:`apply_sync`,
   include/exclude filtering, byte-compare classification.
2. **scaffold CLI integration** — :func:`alfred.scaffold.cli.cmd_sync`
   end-to-end on a fixture scaffold tree, including stdout shape, audit
   log emission, and structlog event emission.
3. **cmd_scaffold dispatcher** — env-var injection contract, gated to
   ``--apply``; mirrors the ``cmd_vault`` (issue #64) and ``cmd_distiller``
   (promote-proposal) dispatchers.

Test-hygiene per CLAUDE.md ``Dispatcher env-var injection`` contract:
every dispatcher-test ``monkeypatch.delenv`` the relevant env vars at
setup — without this, env-var bleed from one test contaminates the
next handler's view of the world.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest
import structlog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_scaffold(tmp_path: Path) -> Path:
    """Build a small scaffold tree mirroring the real one's shape.

    Contains the default-include buckets + ``.obsidian`` (opt-in) +
    empty content dirs (placeholders). Just enough to exercise every
    include/exclude branch without the 30+ files of the real scaffold.
    """
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()

    # _templates/ — two per-type templates
    (scaffold / "_templates").mkdir()
    (scaffold / "_templates" / "person.md").write_text(
        "# {{title}}\n\nperson template body\n", encoding="utf-8"
    )
    (scaffold / "_templates" / "task.md").write_text(
        "# {{title}}\n\ntask template body\n", encoding="utf-8"
    )

    # _bases/ — one .base file
    (scaffold / "_bases").mkdir()
    (scaffold / "_bases" / "person.base").write_text(
        "TABLE FROM #person\n", encoding="utf-8"
    )

    # view/ — one dashboard view
    (scaffold / "view").mkdir()
    (scaffold / "view" / "Home.md").write_text(
        "# Home\n\nDashboard view\n", encoding="utf-8"
    )

    # top-level docs
    (scaffold / "CLAUDE.md").write_text("# Vault CLAUDE\n", encoding="utf-8")
    (scaffold / "README.md").write_text("# Vault README\n", encoding="utf-8")

    # .obsidian/ — opt-in only
    (scaffold / ".obsidian").mkdir()
    (scaffold / ".obsidian" / "app.json").write_text(
        '{"vimMode": false}\n', encoding="utf-8"
    )

    # content dir with .gitkeep — should be excluded by default
    (scaffold / "person").mkdir()
    (scaffold / "person" / ".gitkeep").write_text("", encoding="utf-8")

    return scaffold


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    """Empty vault dir — every scaffold file should classify as CREATE."""
    vault = tmp_path / "empty_vault"
    vault.mkdir()
    return vault


@pytest.fixture
def partially_synced_vault(tmp_path: Path, fake_scaffold: Path) -> Path:
    """Vault with some scaffold files already present.

    Layout:
      - ``_templates/person.md`` — identical to scaffold (NOOP)
      - ``_templates/task.md`` — modified by operator (CONFLICT)
      - ``_bases/person.base`` — absent (CREATE)
      - ``view/Home.md`` — absent (CREATE)
      - ``CLAUDE.md`` — modified by operator (CONFLICT)
      - ``README.md`` — absent (CREATE)
    """
    vault = tmp_path / "partial_vault"
    vault.mkdir()
    (vault / "_templates").mkdir()

    # NOOP — copy scaffold content verbatim
    (vault / "_templates" / "person.md").write_text(
        (fake_scaffold / "_templates" / "person.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # CONFLICT — operator-modified
    (vault / "_templates" / "task.md").write_text(
        "# {{title}}\n\noperator-modified task template\n", encoding="utf-8"
    )
    (vault / "CLAUDE.md").write_text(
        "# Vault CLAUDE — operator edits\n", encoding="utf-8"
    )

    return vault


# ---------------------------------------------------------------------------
# Layer 1 — sync module unit tests (scan_scaffold + apply_sync)
# ---------------------------------------------------------------------------


class TestScanScaffold:
    """``scan_scaffold`` walks the scaffold and classifies each file."""

    def test_empty_vault_classifies_everything_as_create(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import SyncStatus, scan_scaffold

        items = scan_scaffold(fake_scaffold, empty_vault)
        # Default include set excludes .obsidian and content-dirs.
        # Expected: _templates/person.md, _templates/task.md,
        # _bases/person.base, view/Home.md, CLAUDE.md, README.md = 6
        assert len(items) == 6
        for item in items:
            assert item.status == SyncStatus.CREATE, (
                f"{item.relpath} expected CREATE in empty vault, got {item.status}"
            )

    def test_partial_vault_classifies_mixed_statuses(
        self, fake_scaffold: Path, partially_synced_vault: Path
    ) -> None:
        from alfred.scaffold.sync import SyncStatus, scan_scaffold

        items = scan_scaffold(fake_scaffold, partially_synced_vault)
        by_path = {item.relpath: item.status for item in items}

        assert by_path["_templates/person.md"] == SyncStatus.NOOP
        assert by_path["_templates/task.md"] == SyncStatus.CONFLICT
        assert by_path["_bases/person.base"] == SyncStatus.CREATE
        assert by_path["view/Home.md"] == SyncStatus.CREATE
        assert by_path["CLAUDE.md"] == SyncStatus.CONFLICT
        assert by_path["README.md"] == SyncStatus.CREATE

    def test_default_exclude_skips_obsidian(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import scan_scaffold

        items = scan_scaffold(fake_scaffold, empty_vault)
        relpaths = {item.relpath for item in items}
        # .obsidian/app.json must NOT surface with default include set
        assert not any(rp.startswith(".obsidian") for rp in relpaths)

    def test_obsidian_included_when_explicit(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import scan_scaffold

        items = scan_scaffold(
            fake_scaffold,
            empty_vault,
            include=("_templates", ".obsidian"),
            exclude=(".gitkeep",),  # drop the default .obsidian exclude
        )
        relpaths = {item.relpath for item in items}
        assert ".obsidian/app.json" in relpaths

    def test_gitkeep_excluded_by_default(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import scan_scaffold

        # Even when we explicitly include the content dir, .gitkeep
        # files should be excluded by name (so we don't propagate
        # the scaffold's placeholder mechanism into the vault).
        items = scan_scaffold(
            fake_scaffold, empty_vault, include=("person",), exclude=(".gitkeep",)
        )
        assert items == []

    def test_path_prefix_match_not_string_prefix(
        self, tmp_path: Path
    ) -> None:
        # Regression-pin: ``_templates`` include must NOT match
        # ``_templates_old/foo.md``. The bug-shape we're guarding
        # against is naive str.startswith() instead of path-segment.
        from alfred.scaffold.sync import scan_scaffold

        scaffold = tmp_path / "s"
        scaffold.mkdir()
        (scaffold / "_templates").mkdir()
        (scaffold / "_templates" / "ok.md").write_text("ok", encoding="utf-8")
        (scaffold / "_templates_old").mkdir()
        (scaffold / "_templates_old" / "wrong.md").write_text(
            "should not match", encoding="utf-8"
        )

        vault = tmp_path / "v"
        vault.mkdir()

        items = scan_scaffold(scaffold, vault, include=("_templates",), exclude=())
        relpaths = {item.relpath for item in items}
        assert "_templates/ok.md" in relpaths
        assert "_templates_old/wrong.md" not in relpaths

    def test_scaffold_dir_missing_raises(self, tmp_path: Path) -> None:
        from alfred.scaffold.sync import scan_scaffold

        with pytest.raises(FileNotFoundError, match="scaffold_dir"):
            scan_scaffold(tmp_path / "does-not-exist", tmp_path)

    def test_vault_parent_missing_raises(
        self, fake_scaffold: Path, tmp_path: Path
    ) -> None:
        from alfred.scaffold.sync import scan_scaffold

        # vault dir parent is a typo path — refuse to silently create
        with pytest.raises(FileNotFoundError, match="vault_dir parent"):
            scan_scaffold(fake_scaffold, tmp_path / "typo" / "nested" / "vault")


class TestApplySync:
    """``apply_sync`` materializes the plan or dry-runs it."""

    def test_dry_run_writes_nothing(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, empty_vault)
        summary = apply_sync(items, apply=False)

        # Plan categorizes everything correctly
        assert len(summary.created) == 6
        assert summary.dry_run is True
        # But nothing landed on disk
        assert not (empty_vault / "_templates" / "person.md").exists()
        assert not (empty_vault / "CLAUDE.md").exists()

    def test_apply_writes_create_items(
        self, fake_scaffold: Path, empty_vault: Path
    ) -> None:
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, empty_vault)
        summary = apply_sync(items, apply=True)

        assert summary.dry_run is False
        assert len(summary.created) == 6
        # Spot-check two files landed with correct bytes
        assert (empty_vault / "_templates" / "person.md").read_text(
            encoding="utf-8"
        ) == (fake_scaffold / "_templates" / "person.md").read_text(encoding="utf-8")
        assert (empty_vault / "CLAUDE.md").read_text(encoding="utf-8") == (
            fake_scaffold / "CLAUDE.md"
        ).read_text(encoding="utf-8")

    def test_conflict_skipped_without_force(
        self, fake_scaffold: Path, partially_synced_vault: Path
    ) -> None:
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, partially_synced_vault)
        # Pre-state: operator content
        original_task = (partially_synced_vault / "_templates" / "task.md").read_text(
            encoding="utf-8"
        )
        assert "operator-modified" in original_task

        summary = apply_sync(items, apply=True, force=False)

        # CONFLICT files surface in summary but are NOT overwritten
        assert "_templates/task.md" in summary.skipped_conflicts
        assert "CLAUDE.md" in summary.skipped_conflicts
        # Operator content preserved on disk
        assert (
            partially_synced_vault / "_templates" / "task.md"
        ).read_text(encoding="utf-8") == original_task

    def test_force_overwrites_conflicts(
        self, fake_scaffold: Path, partially_synced_vault: Path
    ) -> None:
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, partially_synced_vault)
        summary = apply_sync(items, apply=True, force=True)

        assert "_templates/task.md" in summary.overwritten
        assert "CLAUDE.md" in summary.overwritten
        # Now disk matches scaffold
        assert (
            partially_synced_vault / "_templates" / "task.md"
        ).read_text(encoding="utf-8") == (
            fake_scaffold / "_templates" / "task.md"
        ).read_text(encoding="utf-8")

    def test_noop_preserved(
        self, fake_scaffold: Path, partially_synced_vault: Path
    ) -> None:
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, partially_synced_vault)
        summary = apply_sync(items, apply=True)
        assert "_templates/person.md" in summary.skipped_noops

    def test_to_audit_mutations_shape(
        self, fake_scaffold: Path, partially_synced_vault: Path
    ) -> None:
        # The audit-mutations dict must use the build_audit_mutations
        # shape (``files_created`` / ``files_modified`` / ``files_deleted``)
        # so it can be passed through to append_to_audit_log unmodified.
        from alfred.scaffold.sync import apply_sync, scan_scaffold

        items = scan_scaffold(fake_scaffold, partially_synced_vault)
        summary = apply_sync(items, apply=True, force=True)
        mutations = summary.to_audit_mutations()

        assert set(mutations.keys()) == {
            "files_created",
            "files_modified",
            "files_deleted",
        }
        # creates = the CREATE items, modifies = the force-overwritten
        # CONFLICTS, deletes = empty (sync never deletes)
        assert "_bases/person.base" in mutations["files_created"]
        assert "CLAUDE.md" in mutations["files_modified"]
        assert mutations["files_deleted"] == []


# ---------------------------------------------------------------------------
# Layer 2 — scaffold CLI integration (cmd_sync end-to-end)
# ---------------------------------------------------------------------------


def _make_sync_args(
    vault_path: Path,
    *,
    apply: bool = False,
    dry_run: bool = False,
    force: bool = False,
    include: str | None = None,
    exclude: str | None = None,
    config: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        scaffold_cmd="sync",
        apply=apply,
        dry_run=dry_run,
        force=force,
        include=include,
        exclude=exclude,
        vault_path=str(vault_path),
        config=config or "config.yaml",
    )


class TestCmdSync:
    """End-to-end ``alfred scaffold sync`` via cmd_sync.

    Each test patches ``alfred.scaffold.cli.get_scaffold_dir`` to point
    at the fake scaffold fixture so we exercise the real CLI handler
    without depending on the bundled scaffold's specific contents.
    """

    def _patch_scaffold(
        self, monkeypatch: pytest.MonkeyPatch, fake_scaffold: Path
    ) -> None:
        monkeypatch.setattr(
            "alfred.scaffold.cli.get_scaffold_dir", lambda: fake_scaffold
        )

    def test_dry_run_default_writes_nothing(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault, apply=False)
        rc = cmd_sync(args, raw_config={})
        assert rc == 0

        # Filesystem untouched
        assert not (empty_vault / "_templates" / "person.md").exists()

        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "6 create" in out
        assert "Re-run with --apply" in out

    def test_apply_writes_files(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        # No audit-log path set — cmd_sync should still apply, just
        # surface the skip via structlog (tested separately below).
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        args = _make_sync_args(empty_vault, apply=True)
        rc = cmd_sync(args, raw_config={})
        assert rc == 0

        assert (empty_vault / "_templates" / "person.md").exists()
        assert (empty_vault / "CLAUDE.md").exists()

        out = capsys.readouterr().out
        assert "APPLIED" in out
        assert "6 create" in out

    def test_dry_run_wins_over_apply_when_both_passed(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault, apply=True, dry_run=True)
        cmd_sync(args, raw_config={})

        # Disk untouched
        assert not (empty_vault / "_templates" / "person.md").exists()
        out = capsys.readouterr().out
        assert "DRY-RUN" in out

    def test_no_candidates_emits_left_blank_signal(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Intentionally-left-blank contract: empty scan MUST surface
        # an explicit "no candidates" message so idle is distinguishable
        # from broken. Per feedback_intentionally_left_blank.md.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        # Include set that matches nothing in the scaffold
        args = _make_sync_args(
            empty_vault, apply=True, include="does_not_exist_dir"
        )

        with structlog.testing.capture_logs() as captured:
            rc = cmd_sync(args, raw_config={})

        assert rc == 0
        out = capsys.readouterr().out
        assert "Nothing to do" in out

        # Log emission MUST drive a grep-able event — per CLAUDE.md
        # ``Log-emission tests must drive the production code path``.
        no_cands = [c for c in captured if c.get("event") == "scaffold.sync.no_candidates"]
        assert len(no_cands) == 1
        assert "scaffold_dir" in no_cands[0]
        assert "vault_dir" in no_cands[0]

    def test_apply_appends_to_audit_log(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        audit_log = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_log))

        args = _make_sync_args(empty_vault, apply=True)
        rc = cmd_sync(args, raw_config={})
        assert rc == 0

        assert audit_log.exists()
        rows = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines() if line]
        # All 6 CREATEs land as "create" rows with tool=scaffold
        assert len(rows) == 6
        for row in rows:
            assert row["tool"] == "scaffold"
            assert row["op"] == "create"
            assert "ts" in row
            assert "detail" in row

        # Detail string includes the include/exclude settings (so an
        # operator can reconstruct which sync produced which row).
        assert all("include=" in row["detail"] for row in rows)
        assert all("force=False" in row["detail"] for row in rows)

    def test_dry_run_does_not_append_to_audit_log(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Audit log records what the FILESYSTEM did, not the plan.
        # Dry-runs produce no rows even when the env var is set.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        audit_log = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_log))

        args = _make_sync_args(empty_vault, apply=False)
        cmd_sync(args, raw_config={})

        # Audit log file may or may not exist; if it does, it must be
        # empty.
        if audit_log.exists():
            assert audit_log.read_text(encoding="utf-8") == ""

    def test_force_overwrite_produces_modify_audit_rows(
        self,
        fake_scaffold: Path,
        partially_synced_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        audit_log = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_log))

        args = _make_sync_args(partially_synced_vault, apply=True, force=True)
        cmd_sync(args, raw_config={})

        rows = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines() if line]
        ops_by_path = {row["path"]: row["op"] for row in rows}

        # CONFLICT-overwritten files → "modify" op
        assert ops_by_path["_templates/task.md"] == "modify"
        assert ops_by_path["CLAUDE.md"] == "modify"
        # Files that were CREATE → "create" op
        assert ops_by_path["_bases/person.base"] == "create"

    def test_audit_skip_logged_when_env_unset(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When --apply runs without ALFRED_VAULT_AUDIT_LOG set, the
        # handler MUST surface a structured "audit_skipped_no_env"
        # warning so an operator grep finds the silent-skip.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        args = _make_sync_args(empty_vault, apply=True)
        with structlog.testing.capture_logs() as captured:
            cmd_sync(args, raw_config={})

        skip_events = [
            c for c in captured if c.get("event") == "scaffold.sync.audit_skipped_no_env"
        ]
        assert len(skip_events) == 1

    def test_include_override(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # --include _templates only → 2 files
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault, apply=True, include="_templates")
        cmd_sync(args, raw_config={})

        out = capsys.readouterr().out
        assert "2 create" in out
        assert (empty_vault / "_templates" / "person.md").exists()
        assert not (empty_vault / "CLAUDE.md").exists()

    def test_vault_path_from_config_when_no_cli_override(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)

        # Pass an args namespace with vault_path=None — must fall back
        # to raw_config["vault"]["path"].
        args = argparse.Namespace(
            scaffold_cmd="sync",
            apply=True,
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
            config="config.yaml",
        )
        raw_config = {"vault": {"path": str(empty_vault)}}
        rc = cmd_sync(args, raw_config)
        assert rc == 0
        assert (empty_vault / "_templates" / "person.md").exists()

    def test_no_vault_path_returns_error(
        self,
        fake_scaffold: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = argparse.Namespace(
            scaffold_cmd="sync",
            apply=True,
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
            config="config.yaml",
        )
        rc = cmd_sync(args, raw_config={})
        assert rc == 1
        err = capsys.readouterr().err
        assert "vault.path not set" in err


# ---------------------------------------------------------------------------
# Layer 3 — cmd_scaffold dispatcher env-var injection
# ---------------------------------------------------------------------------


class TestCmdScaffoldDispatcherWiring:
    """Pin the ``cmd_scaffold`` dispatcher's env-var injection contract.

    The dispatcher MUST:
      - inject ALFRED_VAULT_AUDIT_LOG on ``--apply`` invocations
      - NOT inject on ``--dry-run`` invocations (or default dry-run)
      - resolve ``<logging.dir>/vault_audit.log`` from the per-instance
        config, NOT hardcode Salem's ``./data``
      - respect a caller-set override

    Per CLAUDE.md ``Dispatcher env-var injection`` contract: every test
    here ``monkeypatch.delenv`` the relevant env vars at setup.
    """

    def _write_config(
        self,
        path: Path,
        *,
        log_dir: str,
        vault_path: Path,
    ) -> None:
        path.write_text(
            f"vault:\n"
            f"  path: {vault_path}\n"
            f"logging:\n"
            f"  dir: {log_dir}\n",
            encoding="utf-8",
        )

    def _stub_dispatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured: dict,
    ) -> None:
        """Capture ALFRED_VAULT_AUDIT_LOG at dispatch-time."""
        def _stub(_args: argparse.Namespace, _raw: dict) -> int:
            captured["audit_log"] = os.environ.get("ALFRED_VAULT_AUDIT_LOG")
            return 0
        monkeypatch.setattr("alfred.scaffold.cli.dispatch", _stub)

    def test_apply_injects_audit_log_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.cli import cmd_scaffold

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_dispatch(monkeypatch, captured)

        args = argparse.Namespace(
            command="scaffold",
            config=str(config),
            scaffold_cmd="sync",
            apply=True,
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_scaffold(args)
        assert exc.value.code == 0

        assert captured["audit_log"] == str(log_dir / "vault_audit.log")

    def test_dry_run_does_not_inject_audit_log_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.cli import cmd_scaffold

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_dispatch(monkeypatch, captured)

        args = argparse.Namespace(
            command="scaffold",
            config=str(config),
            scaffold_cmd="sync",
            apply=False,  # default dry-run
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
        )
        with pytest.raises(SystemExit):
            cmd_scaffold(args)

        # Env var MUST NOT be set — dry-run is a no-op on the filesystem,
        # injecting would violate the gated-injection contract.
        assert captured["audit_log"] is None

    def test_explicit_dry_run_flag_does_not_inject(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.cli import cmd_scaffold

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_dispatch(monkeypatch, captured)

        # User passed BOTH --apply and --dry-run; --dry-run wins, so
        # injection is suppressed.
        args = argparse.Namespace(
            command="scaffold",
            config=str(config),
            scaffold_cmd="sync",
            apply=True,
            dry_run=True,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
        )
        with pytest.raises(SystemExit):
            cmd_scaffold(args)

        assert captured["audit_log"] is None

    def test_per_instance_log_dir_resolved_correctly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per-instance scope discipline: the dispatcher MUST resolve
        # logging.dir from the config block, NOT hardcode Salem's "./data".
        from alfred.cli import cmd_scaffold

        vault = tmp_path / "kalle-vault"
        vault.mkdir()
        # Simulate KAL-LE's distinct data dir layout
        log_dir = tmp_path / "kalle" / "data"
        log_dir.mkdir(parents=True)
        config = tmp_path / "config.kalle.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_dispatch(monkeypatch, captured)

        args = argparse.Namespace(
            command="scaffold",
            config=str(config),
            scaffold_cmd="sync",
            apply=True,
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
        )
        with pytest.raises(SystemExit):
            cmd_scaffold(args)

        assert captured["audit_log"] == str(log_dir / "vault_audit.log")
        # Sanity: NOT the Salem default
        assert captured["audit_log"] != "./data/vault_audit.log"

    def test_caller_override_respected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the caller has already set ALFRED_VAULT_AUDIT_LOG, the
        # dispatcher MUST NOT overwrite it. Mirrors cmd_vault precedent.
        from alfred.cli import cmd_scaffold

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        override = str(tmp_path / "test-only-audit.log")
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", override)
        captured: dict = {}
        self._stub_dispatch(monkeypatch, captured)

        args = argparse.Namespace(
            command="scaffold",
            config=str(config),
            scaffold_cmd="sync",
            apply=True,
            dry_run=False,
            force=False,
            include=None,
            exclude=None,
            vault_path=None,
        )
        with pytest.raises(SystemExit):
            cmd_scaffold(args)

        assert captured["audit_log"] == override
