"""Tests for ``alfred.scaffold.config`` (Stage 2 follow-up to Build #38).

Per-instance config block + 3-layer override precedence. Closes the
structural gap surfaced 2026-05-12 when KAL-LE + Hypatia apply cycles
revealed that the Salem-shape default-include was wrong for canonical-
curation / knowledge-work instances.

Three test surfaces:

1. **ScaffoldConfig dataclass** — schema-tolerance, default shapes,
   list-coercion, malformed-config handling
2. **3-layer precedence** — `_resolve_filter` resolution: CLI >
   config > module default
3. **End-to-end via cmd_sync** — Salem / KAL-LE / Hypatia config
   shapes resolve to the right scan filter without `--include` flag
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Layer 1 — ScaffoldConfig dataclass
# ---------------------------------------------------------------------------


class TestScaffoldConfigDataclass:
    """``ScaffoldConfig.from_dict`` schema-tolerance + shape contract."""

    def test_empty_dict_yields_none_fields(self) -> None:
        # Missing block → ``include`` / ``exclude`` are ``None``, which
        # signals "fall through to module default" downstream. Distinct
        # from empty-list semantics ("operator zeroed out the filter").
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({})
        assert cfg.include is None
        assert cfg.exclude is None

    def test_explicit_empty_list_preserved(self) -> None:
        # ``include: []`` is honored — operator can opt out of all
        # auto-sync without unsetting the block. Empty list is NOT
        # the same as ``None`` and must survive the from_dict round-trip.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({"include": [], "exclude": []})
        assert cfg.include == []
        assert cfg.exclude == []

    def test_list_of_strings_round_trips(self) -> None:
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({
            "include": ["README.md", "Start Here.md"],
            "exclude": [".obsidian"],
        })
        assert cfg.include == ["README.md", "Start Here.md"]
        assert cfg.exclude == [".obsidian"]

    def test_list_elements_stringified(self) -> None:
        # YAML parses bare strings fine, but a mixed-type list is an
        # operator error. We defensively stringify rather than crash
        # — same fail-soft posture as the other config dataclasses.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({"include": ["README.md", 42]})
        assert cfg.include == ["README.md", "42"]

    def test_scalar_string_coerced_to_single_item_list(self) -> None:
        # Operator-friendly: a single-entry config like
        # ``scaffold.include: "README.md"`` (no list dashes) is the
        # common one-entry case — coerce rather than fail.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({"include": "README.md"})
        assert cfg.include == ["README.md"]

    def test_empty_string_coerced_to_empty_list(self) -> None:
        # Edge case: ``include: ""`` should collapse to empty list,
        # NOT None — caller asked for "no items" explicitly.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({"include": ""})
        assert cfg.include == []

    def test_malformed_type_falls_through_to_none(self) -> None:
        # Unrecognized shape (dict, int) → None, NOT crash. Downstream
        # treats None as "use module default" which is safer than
        # propagating a TypeError up through the CLI.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({"include": {"nope": True}})
        assert cfg.include is None

        cfg = ScaffoldConfig.from_dict({"exclude": 42})
        assert cfg.exclude is None

    def test_unknown_keys_silently_dropped(self) -> None:
        # Schema-tolerance contract: extra fields silently dropped via
        # the ``__dataclass_fields__`` filter on from_dict. Forward
        # compatibility — a future field added by a newer binary can
        # be read by an older binary without crashing.
        from alfred.scaffold.config import ScaffoldConfig

        cfg = ScaffoldConfig.from_dict({
            "include": ["a"],
            "future_field_added_later": "should not crash",
            "another_unknown": 42,
        })
        assert cfg.include == ["a"]
        # The unknown attributes are NOT on the instance — the filter
        # prevented setattr.
        assert not hasattr(cfg, "future_field_added_later")
        assert not hasattr(cfg, "another_unknown")


# ---------------------------------------------------------------------------
# Layer 1.5 — load_from_unified
# ---------------------------------------------------------------------------


class TestLoadFromUnified:
    """The top-level loader entry point."""

    def test_missing_scaffold_block_returns_default(self) -> None:
        # No ``scaffold:`` key → both fields None (module default fallback).
        # Must never return ``None`` (callers always read ``.include``).
        from alfred.scaffold.config import load_from_unified

        cfg = load_from_unified({})
        assert cfg.include is None
        assert cfg.exclude is None

    def test_scaffold_block_with_content_parsed(self) -> None:
        from alfred.scaffold.config import load_from_unified

        raw = {
            "vault": {"path": "/tmp/vault"},
            "scaffold": {
                "include": ["README.md", "Start Here.md", "user-profile.md"],
                "exclude": [".obsidian", ".gitkeep"],
            },
        }
        cfg = load_from_unified(raw)
        assert cfg.include == ["README.md", "Start Here.md", "user-profile.md"]
        assert cfg.exclude == [".obsidian", ".gitkeep"]

    def test_null_scaffold_block_falls_back(self) -> None:
        # ``scaffold: null`` (e.g. operator commented out the entries
        # but left the key) → behaves like missing block.
        from alfred.scaffold.config import load_from_unified

        cfg = load_from_unified({"scaffold": None})
        assert cfg.include is None
        assert cfg.exclude is None

    def test_malformed_scaffold_block_falls_back(self) -> None:
        # ``scaffold: "garbage"`` is malformed — non-dict. Fall back
        # rather than crash. Same fail-soft posture as the other loaders.
        from alfred.scaffold.config import load_from_unified

        cfg = load_from_unified({"scaffold": "not-a-dict"})
        assert cfg.include is None
        assert cfg.exclude is None


# ---------------------------------------------------------------------------
# Layer 2 — _resolve_filter precedence
# ---------------------------------------------------------------------------


class TestResolveFilterPrecedence:
    """Pin the 3-layer override precedence (CLI > config > default)."""

    def test_no_cli_no_config_yields_default(self) -> None:
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value=None,
            config_value=None,
            default=("a", "b", "c"),
        )
        assert result == ("a", "b", "c")

    def test_config_wins_over_default_when_no_cli(self) -> None:
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value=None,
            config_value=["x", "y"],
            default=("a", "b", "c"),
        )
        assert result == ("x", "y")

    def test_cli_wins_over_config(self) -> None:
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value="cli1,cli2",
            config_value=["cfg1", "cfg2"],
            default=("def1",),
        )
        assert result == ("cli1", "cli2")

    def test_cli_wins_over_default_when_no_config(self) -> None:
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value="only,cli",
            config_value=None,
            default=("def1",),
        )
        assert result == ("only", "cli")

    def test_cli_whitespace_stripped(self) -> None:
        # ``--include " a , b , c "`` should normalize.
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value="  a , b , c  ",
            config_value=None,
            default=(),
        )
        assert result == ("a", "b", "c")

    def test_cli_empty_string_falls_through(self) -> None:
        # Edge case: ``--include ""`` is an operator error / no-op.
        # We treat empty-string-CLI as "no flag passed" (falls through
        # to config or default). Cleaner than producing an empty tuple
        # which would silently drop ALL includes.
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value="",
            config_value=["cfg1"],
            default=("def1",),
        )
        assert result == ("cfg1",)

    def test_empty_list_config_wins_over_default(self) -> None:
        # ``scaffold.include: []`` is operator-explicit "zero out" —
        # must NOT fall through to default. The ``no_candidates``
        # empty-state signal fires downstream.
        from alfred.scaffold.cli import _resolve_filter

        result = _resolve_filter(
            cli_value=None,
            config_value=[],
            default=("def1", "def2"),
        )
        assert result == ()


# ---------------------------------------------------------------------------
# Layer 3 — End-to-end per-instance scenarios via cmd_sync
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_scaffold(tmp_path: Path) -> Path:
    """Reproduce the real scaffold's shape so per-instance config can
    select different subsets.

    Mirrors ``tests/test_scaffold_sync.py``'s fixture but kept local
    to avoid cross-test-file fixture dependencies.
    """
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()

    (scaffold / "_templates").mkdir()
    (scaffold / "_templates" / "person.md").write_text("person tpl\n", encoding="utf-8")
    (scaffold / "_templates" / "task.md").write_text("task tpl\n", encoding="utf-8")

    (scaffold / "_bases").mkdir()
    (scaffold / "_bases" / "person.base").write_text("person base\n", encoding="utf-8")

    (scaffold / "view").mkdir()
    (scaffold / "view" / "Home.md").write_text("home view\n", encoding="utf-8")

    (scaffold / "CLAUDE.md").write_text("# CLAUDE\n", encoding="utf-8")
    (scaffold / "README.md").write_text("# README\n", encoding="utf-8")
    (scaffold / "Start Here.md").write_text("# Start\n", encoding="utf-8")
    (scaffold / "user-profile.md").write_text("# Profile\n", encoding="utf-8")

    (scaffold / ".obsidian").mkdir()
    (scaffold / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")

    return scaffold


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "empty_vault"
    vault.mkdir()
    return vault


def _make_sync_args(
    vault_path: Path,
    *,
    include: str | None = None,
    exclude: str | None = None,
) -> argparse.Namespace:
    """Build a sync-args namespace with defaults appropriate for the
    per-instance config tests (no --include / --exclude, dry-run).
    """
    return argparse.Namespace(
        scaffold_cmd="sync",
        apply=False,
        dry_run=False,
        force=False,
        include=include,
        exclude=exclude,
        vault_path=str(vault_path),
        config="config.yaml",
    )


class TestPerInstanceConfigEndToEnd:
    """Salem / KAL-LE / Hypatia config shapes produce the right scan filter."""

    def _patch_scaffold(
        self, monkeypatch: pytest.MonkeyPatch, fake_scaffold: Path
    ) -> None:
        monkeypatch.setattr(
            "alfred.scaffold.cli.get_scaffold_dir", lambda: fake_scaffold
        )

    def test_salem_default_include_full_set(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Salem: no scaffold block OR full Salem-shape include — all
        # 7 default-set files surface in the scan plan.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault)
        # No scaffold block in raw_config → falls back to module DEFAULT_INCLUDE.
        rc = cmd_sync(args, raw_config={})
        assert rc == 0
        out = capsys.readouterr().out
        # 8 candidates: 2 templates, 1 base, 1 view, 4 top-level docs.
        # ``.obsidian`` and content dirs excluded by default.
        assert "8 create" in out

    def test_kalle_trimmed_include_only_top_level_docs(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # KAL-LE: canonical-curation shape; trim to just the top-level
        # docs. The per-record-type templates and bases shouldn't sync
        # because KAL-LE's aftermath-lab vault doesn't use those types.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault)
        raw_config = {
            "scaffold": {
                "include": ["README.md", "Start Here.md", "user-profile.md"],
                "exclude": [".obsidian", ".gitkeep"],
            },
        }
        rc = cmd_sync(args, raw_config=raw_config)
        assert rc == 0
        out = capsys.readouterr().out
        # 3 candidates — only the top-level docs.
        assert "3 create" in out
        # CLAUDE.md is in Salem's default include but NOT in KAL-LE's
        # trimmed set — must NOT surface here.
        assert "CLAUDE.md" not in out

    def test_hypatia_trimmed_include_only_top_level_docs(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Hypatia: knowledge-work shape; same trim as KAL-LE.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault)
        raw_config = {
            "scaffold": {
                "include": ["README.md", "Start Here.md", "user-profile.md"],
                "exclude": [".obsidian", ".gitkeep"],
            },
        }
        rc = cmd_sync(args, raw_config=raw_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "3 create" in out
        assert "_templates" not in out
        assert "_bases" not in out

    def test_cli_flag_overrides_per_instance_config(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Even with a trimmed KAL-LE config, --include forces the
        # operator's choice. Pins layer-1-wins-over-layer-2.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault, include="_templates")
        raw_config = {
            "scaffold": {
                "include": ["README.md"],  # KAL-LE-ish
                "exclude": [".obsidian"],
            },
        }
        rc = cmd_sync(args, raw_config=raw_config)
        assert rc == 0
        out = capsys.readouterr().out
        # 2 templates surface — _templates/person.md and _templates/task.md.
        # README.md does NOT surface (the config was overridden).
        assert "2 create" in out
        assert "_templates/person.md" in out
        assert "README.md" not in out

    def test_empty_list_in_config_zeros_out_sync(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # ``scaffold.include: []`` is an explicit "opt out" — must
        # produce the no_candidates empty-state signal.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)
        args = _make_sync_args(empty_vault)
        raw_config = {"scaffold": {"include": []}}
        rc = cmd_sync(args, raw_config=raw_config)
        assert rc == 0
        out = capsys.readouterr().out
        # The intentionally-left-blank signal fires.
        assert "Nothing to do" in out

    def test_no_scaffold_block_uses_module_defaults(
        self,
        fake_scaffold: Path,
        empty_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Mirror of the Salem test, asserted from the other angle:
        # explicitly check that a config WITHOUT a scaffold block
        # behaves identically to a config with the Salem-shape
        # scaffold block. Pins backward-compatibility — existing
        # Salem configs that haven't grown the new block aren't
        # silently broken by this fix.
        from alfred.scaffold.cli import cmd_sync

        self._patch_scaffold(monkeypatch, fake_scaffold)

        # First run: no scaffold block at all.
        args_a = _make_sync_args(empty_vault)
        rc = cmd_sync(args_a, raw_config={"vault": {"path": "/x"}})
        assert rc == 0
        out_no_block = capsys.readouterr().out
        # 8 candidates per default-include set.
        assert "8 create" in out_no_block
