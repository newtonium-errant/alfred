"""Inbox/processed exclusion policy tests.

Contract: ``inbox/processed/`` is the curator's audit trail of consumed raw
inputs. The derived vault records are the canonical artifacts. Janitor and
distiller must not scan `inbox/processed/` — janitor would flag FM001 /
LINK001 on raw email bodies (noise), and distiller would double-extract
from raw emails alongside derived notes.

Surveyor already excludes all of `inbox/` via its own default
``ignore_dirs``. These tests lock the unified policy in place for
janitor and distiller and verify that the shared ``is_ignored_path``
helper supports both legacy single-component entries (``".obsidian"``)
and new nested-path entries (``"inbox/processed"``).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.distiller.candidates import scan_candidates, collect_existing_learns
from alfred.distiller.config import VaultConfig as DistillerVaultConfig
from alfred.janitor.config import VaultConfig as JanitorVaultConfig
from alfred.vault.ops import is_ignored_path


class TestIsIgnoredPathHelper:
    def test_single_component_entry_matches_any_depth(self) -> None:
        # Legacy behavior: bare directory names match any path component.
        assert is_ignored_path("a/.obsidian/b.md", [".obsidian"])
        assert is_ignored_path(".obsidian/b.md", [".obsidian"])
        assert is_ignored_path("deep/nested/.obsidian/b.md", [".obsidian"])

    def test_single_component_entry_does_not_cross_prefix(self) -> None:
        # `.obsidian` as a single component must NOT match the literal
        # file name `obsidian.md` or a directory that merely starts with
        # `.obsidian-`.
        assert not is_ignored_path("notes/obsidian.md", [".obsidian"])
        assert not is_ignored_path(".obsidian-backup/x.md", [".obsidian"])

    def test_nested_path_entry_matches_prefix_only(self) -> None:
        # `inbox/processed` must match its prefix but NOT plain `inbox/`.
        assert is_ignored_path("inbox/processed/foo.md", ["inbox/processed"])
        assert is_ignored_path("inbox/processed/sub/foo.md", ["inbox/processed"])
        assert not is_ignored_path("inbox/foo.md", ["inbox/processed"])
        assert not is_ignored_path("notes/inbox/processed/x.md", ["inbox/processed"])

    def test_nested_path_tolerates_leading_or_trailing_slash(self) -> None:
        # Defensive: users may type `/inbox/processed/` in config.
        assert is_ignored_path("inbox/processed/x.md", ["/inbox/processed/"])

    def test_mixed_entries_work_together(self) -> None:
        # Both shapes in the same set must coexist.
        ignore = [".obsidian", "_templates", "inbox/processed"]
        assert is_ignored_path("inbox/processed/x.md", ignore)
        assert is_ignored_path("a/.obsidian/x.md", ignore)
        assert is_ignored_path("_templates/x.md", ignore)
        assert not is_ignored_path("note/x.md", ignore)
        assert not is_ignored_path("inbox/x.md", ignore)

    def test_pathlib_path_accepted(self) -> None:
        # Callers pass `rel` as a ``Path`` in most sites — the helper
        # must accept it without forcing a str() at every call site.
        assert is_ignored_path(Path("inbox/processed/x.md"), ["inbox/processed"])


class TestJanitorDefaultExcludesInboxProcessed:
    def test_default_ignore_dirs_includes_inbox_processed(self) -> None:
        cfg = JanitorVaultConfig()
        assert "inbox/processed" in cfg.ignore_dirs

    def test_scanner_skips_inbox_processed(self, tmp_vault: Path, tmp_path: Path) -> None:
        # Seed a file in inbox/processed/ that would otherwise trip
        # multiple scanner checks: broken wikilink (LINK001), no name
        # field (FM001). If the scanner respects the default ignore,
        # none of those issues should appear.
        processed_dir = tmp_vault / "inbox" / "processed"
        processed_dir.mkdir(parents=True)
        raw_email = dedent(
            """\
            ---
            type: email
            ---

            Raw email body mentioning [[project/Does Not Exist]].
            """
        )
        (processed_dir / "email-20260420-noise.md").write_text(raw_email, encoding="utf-8")

        from alfred.janitor.config import JanitorConfig
        from alfred.janitor.scanner import run_structural_scan
        from alfred.janitor.state import JanitorState

        config = JanitorConfig()
        config.vault.path = str(tmp_vault)
        # Use the dataclass default (includes inbox/processed).
        state = JanitorState(state_path=str(tmp_path / "janitor_state.json"))

        issues = run_structural_scan(config, state)
        # Any issue whose path starts with inbox/processed/ is a regression.
        flagged_processed = [i for i in issues if i.file.startswith("inbox/processed/")]
        assert flagged_processed == [], (
            f"scanner flagged inbox/processed files: {[i.file for i in flagged_processed]}"
        )


class TestDistillerDefaultExcludesInboxProcessed:
    def test_default_ignore_dirs_includes_inbox_processed(self) -> None:
        cfg = DistillerVaultConfig()
        assert "inbox/processed" in cfg.ignore_dirs

    def test_scan_candidates_skips_inbox_processed(self, tmp_vault: Path) -> None:
        # Seed a juicy extraction target (conversation-type record with
        # decision keywords + substantial body) in inbox/processed/. If
        # the candidate scanner respects the exclusion, it must NOT
        # appear in the output.
        processed_dir = tmp_vault / "inbox" / "processed"
        processed_dir.mkdir(parents=True)
        juicy = dedent(
            """\
            ---
            type: conversation
            name: Old email thread
            created: 2026-04-01
            ---

            We decided to ship the new module and agreed the deadline is
            Friday. The team confirmed the approach, chose the Rust
            backend, and settled on a staged rollout.
            """
        )
        (processed_dir / "email-thread.md").write_text(juicy, encoding="utf-8")

        # Also seed a real candidate so we know the scanner ran.
        real = tmp_vault / "note" / "Real decision.md"
        real.parent.mkdir(exist_ok=True)
        real.write_text(
            dedent(
                """\
                ---
                type: note
                name: Real decision
                created: 2026-04-15
                ---

                We decided to adopt the new standard. Team agreed on
                Friday. Confirmed go-live.
                """
            ),
            encoding="utf-8",
        )

        candidates = scan_candidates(
            tmp_vault,
            ignore_dirs=[".obsidian", "inbox/processed"],
            ignore_files=[".gitkeep"],
            source_types=["conversation", "note"],
            threshold=0.0,  # accept everything that passes filters
        )

        paths = [c.record.rel_path for c in candidates]
        assert all(not p.startswith("inbox/processed/") for p in paths), (
            f"scan_candidates returned inbox/processed entries: {paths}"
        )
        # Confirm the scanner actually found something else — otherwise
        # the assertion above is vacuous.
        assert any(p.startswith("note/") for p in paths)

    def test_collect_existing_learns_skips_inbox_processed(self, tmp_vault: Path) -> None:
        # If someone manually dropped an assumption-typed markdown file
        # into inbox/processed (or curator misrouted one once), the
        # distiller's existing-learns collector must not pick it up as a
        # dedup target.
        processed_dir = tmp_vault / "inbox" / "processed"
        processed_dir.mkdir(parents=True)
        fake_learn = dedent(
            """\
            ---
            type: assumption
            name: stale assumption in processed
            created: 2026-04-01
            ---

            body
            """
        )
        (processed_dir / "stale-learn.md").write_text(fake_learn, encoding="utf-8")

        learns = collect_existing_learns(
            tmp_vault,
            ignore_dirs=[".obsidian", "inbox/processed"],
            learn_types=["assumption", "decision", "constraint"],
        )
        paths = [l.rel_path for l in learns]
        assert all(not p.startswith("inbox/processed/") for p in paths), (
            f"collect_existing_learns returned inbox/processed entries: {paths}"
        )
