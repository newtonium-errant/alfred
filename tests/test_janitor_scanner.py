"""Tests for ``alfred.janitor.scanner._build_stem_index`` and friends.

The stem index powers wikilink resolution for the broken-link scanner.
Obsidian wikilinks can reference either a bare stem (``[[Eagle Farm]]``)
or a path-qualified stem (``[[project/Eagle Farm]]``); the index must
map BOTH forms to the same underlying file, or the scanner will
hallucinate broken-link issues on perfectly valid links.

Also verifies the ignore-dirs filter actually excludes files under
``.obsidian`` / vault-internal paths so noisy no-ops don't flood the
reported issue list.

End-to-end LINK001 tests live at the bottom, covering the two known
false-positive classes that were swamping the morning brief on
2026-04-20: YAML line-wrapped wikilinks (distiller emits long record
names; PyYAML folds the quoted list item across physical lines) and
``_templates/`` placeholder wikilinks (``[[project/My Project]]``).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import IssueCode
from alfred.janitor.parser import extract_wikilinks
from alfred.janitor.scanner import _build_stem_index, run_structural_scan
from alfred.janitor.state import JanitorState


def _touch(vault: Path, rel: str) -> None:
    """Create ``rel`` (including parents) as an empty markdown file."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


class TestBuildStemIndex:
    def test_maps_both_stem_and_path_forms(self, tmp_vault: Path) -> None:
        # A record at ``project/Eagle Farm.md`` must be reachable by BOTH
        # ``Eagle Farm`` and ``project/Eagle Farm``, matching Obsidian's
        # dual-form wikilink resolution.
        _touch(tmp_vault, "project/Eagle Farm.md")

        index = _build_stem_index(tmp_vault, ignore_dirs=set())

        assert "Eagle Farm" in index
        assert "project/Eagle Farm" in index
        assert index["Eagle Farm"] == {"project/Eagle Farm.md"}
        assert index["project/Eagle Farm"] == {"project/Eagle Farm.md"}

    def test_stem_collision_collects_all_paths(self, tmp_vault: Path) -> None:
        # Two records with the same stem in different directories must
        # both land in the stem bucket, so the scanner can flag the
        # ambiguity instead of silently picking one.
        _touch(tmp_vault, "project/Alpha.md")
        _touch(tmp_vault, "task/Alpha.md")

        index = _build_stem_index(tmp_vault, ignore_dirs=set())

        assert index["Alpha"] == {"project/Alpha.md", "task/Alpha.md"}

    def test_ignore_dirs_excludes_subtree(self, tmp_vault: Path) -> None:
        # Files under ignored directories (e.g., ``.obsidian`` or vault
        # internals) must NOT appear in the index — otherwise every
        # template file under ``_templates/`` would register as a
        # wikilink target.
        _touch(tmp_vault, "_templates/person.md")
        _touch(tmp_vault, "person/Real Person.md")

        index = _build_stem_index(tmp_vault, ignore_dirs={"_templates"})

        assert "Real Person" in index
        assert "person" not in index  # only the ignored-dir stem would add this
        # The ignored template should not show up under either form.
        assert all("_templates" not in v for values in index.values() for v in values)


# --- LINK001 false-positive regression tests ----------------------------
#
# See ``vault/session/Janitor scanner wikilink false positives 2026-04-20.md``.
# These pin the two classes of false positives that inflated the open-issue
# count from ~1500 to 2224 on 2026-04-20: YAML line-wrapped wikilink targets
# and template-placeholder wikilinks under ``_templates/``.


def _build_scan_config(vault: Path, state_dir: Path, *, ignore_dirs: list[str] | None = None) -> JanitorConfig:
    """Minimal JanitorConfig wired to the supplied vault and state file.

    Mirrors ``config.yaml``'s janitor section with just enough shape to
    drive ``run_structural_scan`` — we don't need the agent backend, and
    ``fix_mode`` is never invoked by the scanner itself.
    """
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=ignore_dirs if ignore_dirs is not None else [".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _write_record(vault: Path, rel: str, frontmatter: str, body: str = "") -> None:
    """Write a markdown record with the given raw frontmatter text.

    ``frontmatter`` is injected verbatim between the ``---`` fences so
    tests can seed pathological YAML (e.g. line-wrapped list items) that
    a round-trip through ``yaml.dump`` would rewrite before we saw it.
    """
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _link_issues(issues: list, rel: str) -> list:
    """Filter an issue list down to LINK001 entries for a given file."""
    return [i for i in issues if i.file == rel and i.code == IssueCode.BROKEN_WIKILINK]


class TestExtractWikilinksNormalizesWhitespace:
    """``extract_wikilinks`` must collapse embedded whitespace in captures.

    YAML folds long list items across physical lines. The raw regex
    capture then contains a newline + continuation indent that breaks
    stem_index lookup. The extractor normalizes that to a single space
    so resolution sees the same string as the filename-derived index.
    """

    def test_wrapped_target_collapses_to_single_line(self) -> None:
        text = (
            "- '[[assumption/Law Firm Billing Accepted on Summary Confirmation "
            "Without Line-Item\n  Review]]'"
        )
        assert extract_wikilinks(text) == [
            "assumption/Law Firm Billing Accepted on Summary Confirmation "
            "Without Line-Item Review"
        ]

    def test_regular_wikilink_unchanged(self) -> None:
        text = "See [[project/Eagle Farm]] for details."
        assert extract_wikilinks(text) == ["project/Eagle Farm"]

    def test_bare_stem_wikilink_unchanged(self) -> None:
        assert extract_wikilinks("[[Eagle Farm]]") == ["Eagle Farm"]

    def test_mixed_wrapped_and_unwrapped(self) -> None:
        text = (
            "- '[[project/Eagle Farm]]'\n"
            "- '[[assumption/A Very Long Assumption Name That Exceeds\n"
            "  The Line Width]]'\n"
        )
        assert extract_wikilinks(text) == [
            "project/Eagle Farm",
            "assumption/A Very Long Assumption Name That Exceeds The Line Width",
        ]


class TestScannerYamlWrappedWikilinks:
    """LINK001 must not fire on yaml-wrapped wikilinks whose target exists."""

    def test_scanner_unwraps_yaml_wikilinks(self, tmp_vault: Path, tmp_path: Path) -> None:
        # Target record with a long descriptive name — the kind of record
        # the distiller generates that triggers YAML line-wrapping when
        # another record references it from a list field.
        target_name = (
            "Law Firm Billing Accepted on Summary Confirmation Without "
            "Line-Item Review"
        )
        _write_record(
            tmp_vault,
            f"assumption/{target_name}.md",
            dedent(
                f"""\
                type: assumption
                name: {target_name}
                created: '2026-04-11'
                tags: []
                related: []
                """
            ).rstrip(),
        )

        # Source record whose `related:` list item YAML-wraps the wikilink.
        # This is the exact shape PyYAML emits for a quoted string longer
        # than the default 80-col line width.
        _write_record(
            tmp_vault,
            "assumption/Cox and Palmer Newton.md",
            dedent(
                """\
                type: assumption
                name: Cox and Palmer Newton
                created: '2026-04-11'
                tags: []
                related:
                - '[[assumption/Law Firm Billing Accepted on Summary Confirmation Without Line-Item
                    Review]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        # The wrapped wikilink resolves — no LINK001 on the source record.
        src_links = _link_issues(issues, "assumption/Cox and Palmer Newton.md")
        assert src_links == [], (
            f"Unexpected LINK001 on yaml-wrapped but valid wikilink: {src_links}"
        )

    def test_scanner_still_flags_genuinely_broken_wrapped_wikilink(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Same wrap shape, but target does NOT exist. Scanner must still
        # fire LINK001 — the normalization fix can't be so aggressive
        # that it hides real broken links.
        _write_record(
            tmp_vault,
            "assumption/Source.md",
            dedent(
                """\
                type: assumption
                name: Source
                created: '2026-04-11'
                tags: []
                related:
                - '[[assumption/Nonexistent Record With A Very Long Name That Exceeds
                    Line Width]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        src_links = _link_issues(issues, "assumption/Source.md")
        assert len(src_links) == 1, (
            f"Expected exactly one LINK001 on wrapped-but-broken wikilink, "
            f"got {len(src_links)}: {src_links}"
        )
        # Message includes the normalized (single-line) target so the
        # agent fix prompt sees a clean string, not an embedded newline.
        assert "Nonexistent Record With A Very Long Name That Exceeds Line Width" in (
            src_links[0].message
        )
        assert "\n" not in src_links[0].message.split("[[", 1)[1].split("]]", 1)[0]


class TestScannerRegressionRegularWikilinks:
    """Unwrapped wikilinks keep their existing resolve/flag behavior."""

    def test_regular_valid_wikilink_no_issue(self, tmp_vault: Path, tmp_path: Path) -> None:
        _write_record(
            tmp_vault,
            "project/Eagle Farm.md",
            "type: project\nname: Eagle Farm\ncreated: '2026-04-11'\ntags: []\nrelated: []\nstatus: active",
        )
        _write_record(
            tmp_vault,
            "task/Water Chickens.md",
            dedent(
                """\
                type: task
                name: Water Chickens
                created: '2026-04-11'
                status: todo
                tags: []
                related:
                - '[[project/Eagle Farm]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _link_issues(issues, "task/Water Chickens.md") == []

    def test_regular_broken_wikilink_flagged(self, tmp_vault: Path, tmp_path: Path) -> None:
        _write_record(
            tmp_vault,
            "task/Orphan Link.md",
            dedent(
                """\
                type: task
                name: Orphan Link
                created: '2026-04-11'
                status: todo
                tags: []
                related:
                - '[[project/Nonexistent Project]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        link_issues = _link_issues(issues, "task/Orphan Link.md")
        assert len(link_issues) == 1
        assert "project/Nonexistent Project" in link_issues[0].message


class TestScannerSkipsTemplatesDir:
    """Files under ``_templates/`` (placeholder wikilinks) are not scanned."""

    def test_scanner_skips_templates_dir(self, tmp_vault: Path, tmp_path: Path) -> None:
        # A real template file from the scaffold: placeholder wikilinks
        # like ``[[project/My Project]]`` exist as syntax examples and
        # must never be flagged as broken links.
        template_path = tmp_vault / "_templates" / "task.md"
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(
            dedent(
                """\
                ---
                type: task
                name: {{title}}
                created: {{date}}
                tags: []
                related:
                - '[[project/My Project]]'
                - '[[person/Someone]]'
                ---

                Example body with a broken placeholder: [[wikilinks]].
                """
            ),
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Use the default ignore_dirs (which now includes ``_templates``).
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        # Nothing in ``_templates/`` should appear in any issue.
        template_issues = [i for i in issues if i.file.startswith("_templates/")]
        assert template_issues == [], (
            f"Scanner should skip _templates/ but reported: {template_issues}"
        )

    def test_default_ignore_dirs_includes_templates_and_bases(self) -> None:
        # Pin the dataclass default — fresh installs (no config.yaml)
        # must not scan scaffold placeholder content.
        cfg = VaultConfig()
        assert "_templates" in cfg.ignore_dirs
        assert "_bases" in cfg.ignore_dirs
        assert ".obsidian" in cfg.ignore_dirs


# --- LINK001 false-positive: YAML-doubled apostrophes -------------------
#
# YAML serializes ``Andrew's`` inside a single-quoted scalar as
# ``Andrew''s`` (apostrophe doubling is YAML's escape for that quote
# style). The wikilink regex grabs the literal characters between
# ``[[`` and ``]]``, so a doubled-apostrophe target lands in the
# scanner as ``Andrew''s`` and misses the on-disk file ``Andrew's.md``.
# 252 of 761 LINK001 entries on 2026-04-30 were this exact pattern;
# the constraint record documenting the bug also showed up 20 times as
# its own broken-link target.


class TestScannerYamlDoubledApostrophe:
    """LINK001 must not fire when the target's apostrophe is YAML-escaped."""

    def test_doubled_apostrophe_resolves_to_real_file(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Target file uses a real apostrophe in its on-disk name.
        target_name = "Andrew's Notes on Apostrophes"
        _write_record(
            tmp_vault,
            f"constraint/{target_name}.md",
            dedent(
                f"""\
                type: constraint
                name: {target_name}
                created: '2026-04-30'
                tags: []
                related: []
                """
            ).rstrip(),
        )

        # Source record's frontmatter contains the wikilink inside a
        # single-quoted YAML list item — exactly the shape ``yaml.dump``
        # emits when Python's value is the literal ``[[...Andrew's...]]``.
        # The doubled ``''`` is YAML's escape for a single apostrophe.
        _write_record(
            tmp_vault,
            "decision/Some Decision.md",
            dedent(
                """\
                type: decision
                name: Some Decision
                created: '2026-04-30'
                status: final
                tags: []
                related:
                - '[[constraint/Andrew''s Notes on Apostrophes]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        src_links = _link_issues(issues, "decision/Some Decision.md")
        assert src_links == [], (
            f"YAML-doubled apostrophe should resolve to real file, got: {src_links}"
        )

    def test_genuinely_broken_apostrophe_link_still_flagged(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Same shape, but the target file does NOT exist. The decode
        # cannot be so permissive that a real broken link slips through.
        _write_record(
            tmp_vault,
            "decision/Lonely Decision.md",
            dedent(
                """\
                type: decision
                name: Lonely Decision
                created: '2026-04-30'
                status: final
                tags: []
                related:
                - '[[constraint/Nobody''s Constraint Record]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        src_links = _link_issues(issues, "decision/Lonely Decision.md")
        assert len(src_links) == 1, (
            f"Broken apostrophe-bearing wikilink must still flag, got: {src_links}"
        )
        # The displayed message keeps the raw doubled form so reviewers
        # see exactly what the source file contains — only the lookup
        # is normalized, not the report.
        assert "Nobody''s Constraint Record" in src_links[0].message

    def test_apostrophe_link_registers_inbound_so_target_not_orphan(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Bonus: the inbound index also runs through the decoder, so
        # an apostrophe-bearing target with one inbound linker should
        # NOT fire ORPHAN001 just because the linker's YAML doubled
        # the apostrophe. This was the second-order win of the fix.
        target_name = "Andrew's Other Note"
        _write_record(
            tmp_vault,
            f"constraint/{target_name}.md",
            dedent(
                f"""\
                type: constraint
                name: {target_name}
                created: '2026-04-30'
                tags: []
                related: []
                """
            ).rstrip(),
        )
        _write_record(
            tmp_vault,
            "decision/Linker.md",
            dedent(
                """\
                type: decision
                name: Linker
                created: '2026-04-30'
                status: final
                tags: []
                related:
                - '[[constraint/Andrew''s Other Note]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        target_orphans = [
            i for i in issues
            if i.file == f"constraint/{target_name}.md"
            and i.code == IssueCode.ORPHANED_RECORD
        ]
        assert target_orphans == [], (
            f"Inbound index should resolve apostrophe-decoded target, got: {target_orphans}"
        )


# --- ORPHAN001 leaf-type policy -----------------------------------------
#
# Note and run records are terminal-by-design — nothing in the vault
# is expected to wikilink at a captured email or a Morning Brief, so
# zero inbound is the norm, not a defect. The 2026-04-30 categorization
# showed 258 of 360 ORPHAN001 entries lived under ``note/`` and 15 more
# under ``run/`` — 273 of 360 were correct-by-design noise.


def _orphan_issues(issues: list, rel: str) -> list:
    return [i for i in issues if i.file == rel and i.code == IssueCode.ORPHANED_RECORD]


class TestScannerOrphanLeafTypes:
    """ORPHAN001 must not fire for leaf-by-design types."""

    def test_note_with_no_inbound_skips_orphan(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # A standalone note — nothing links to it, but notes are
        # leaf-by-design so the scanner must not flag it.
        _write_record(
            tmp_vault,
            "note/Standalone Capture.md",
            dedent(
                """\
                type: note
                name: Standalone Capture
                created: '2026-04-30'
                status: active
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _orphan_issues(issues, "note/Standalone Capture.md") == []

    def test_run_with_no_inbound_skips_orphan(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Morning Briefs, daily-output records — nothing links to them.
        _write_record(
            tmp_vault,
            "run/Morning Brief 2026-04-30.md",
            dedent(
                """\
                type: run
                name: Morning Brief 2026-04-30
                created: '2026-04-30'
                status: completed
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _orphan_issues(issues, "run/Morning Brief 2026-04-30.md") == []

    def test_non_leaf_type_still_flagged(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # ``person`` is NOT a leaf type — orphaned persons should still
        # surface as a janitor issue. The leaf-type rule must be a
        # precise exclusion, not a blanket "no inbound is fine".
        _write_record(
            tmp_vault,
            "person/Lonely Person.md",
            dedent(
                """\
                type: person
                name: Lonely Person
                created: '2026-04-30'
                status: active
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert len(_orphan_issues(issues, "person/Lonely Person.md")) == 1

    def test_task_still_flagged_pending_separate_policy(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Pin: ``task`` is NOT in LEAF_TYPES (the 2026-04-30 spec
        # explicitly deferred it). A different policy may eventually
        # cover terminal tasks, but until then they keep firing
        # ORPHAN001 — and this test guards against a sloppy expansion
        # of LEAF_TYPES that quietly drops them.
        _write_record(
            tmp_vault,
            "task/Lonely Task.md",
            dedent(
                """\
                type: task
                name: Lonely Task
                created: '2026-04-30'
                status: todo
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert len(_orphan_issues(issues, "task/Lonely Task.md")) == 1

    def test_leaf_types_set_pinned(self) -> None:
        # Pin the conservative starting set. Expansion requires data
        # (per the schema.py docstring) — bumping this assertion is
        # a deliberate signal, not a slip.
        from alfred.vault.schema import LEAF_TYPES
        assert LEAF_TYPES == {"note", "run"}


# --- Scaffold/docs exclusion (vault-root files) -------------------------
#
# Root-level markdown files in the vault are scaffold/documentation —
# CLAUDE.md, README.md, Start Here.md — not records. They have no
# ``type`` frontmatter and contain illustrative wikilinks like
# ``[[wikilinks]]`` or ``[[person/Your Name]]`` that are syntax
# examples, not real targets. Per-record validation generates noise
# without surfacing real issues.
#
# Convention: a record lives under ``<type>/<name>.md``; anything at
# the vault root with no parent directory is documentation. Root
# files stay in the inbound-link index so hand-curated dashboards
# (Start Here.md) still count toward referenced records' inbound
# visibility — only the record-validation pass skips them.


class TestScannerSkipsRootScaffold:
    """LINK001/ORPHAN001/FM001 don't fire on vault-root scaffold files."""

    def test_root_claude_md_skipped(self, tmp_vault: Path, tmp_path: Path) -> None:
        # CLAUDE.md at vault root contains placeholder wikilinks and
        # has no record frontmatter. Scanner must skip it entirely.
        (tmp_vault / "CLAUDE.md").write_text(
            dedent(
                """\
                # CLAUDE.md

                Placeholder docs. Records use wikilinks like
                [[person/Your Name]] and [[project/Example Project]].
                Body-only example: [[wikilinks]].
                """
            ),
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        root_issues = [i for i in issues if i.file == "CLAUDE.md"]
        assert root_issues == [], (
            f"Vault-root scaffold should not generate issues, got: {root_issues}"
        )

    def test_root_readme_and_start_here_also_skipped(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # README.md and Start Here.md are also root-level scaffold/docs.
        (tmp_vault / "README.md").write_text("# README\n[[broken]]\n", encoding="utf-8")
        (tmp_vault / "Start Here.md").write_text(
            "# Start Here\n[[person/Your Name]] [[project/My Project]]\n",
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert [i for i in issues if i.file == "README.md"] == []
        assert [i for i in issues if i.file == "Start Here.md"] == []

    def test_typed_subdirectory_records_still_validated(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Sanity: the scaffold-skip rule must NOT extend to actual
        # records under typed subdirectories. A broken wikilink in
        # ``project/Example.md`` must still flag.
        _write_record(
            tmp_vault,
            "project/Example.md",
            dedent(
                """\
                type: project
                name: Example
                created: '2026-04-30'
                status: active
                tags: []
                related:
                - '[[project/Nonexistent]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        link_issues = _link_issues(issues, "project/Example.md")
        assert len(link_issues) == 1

    def test_root_scaffold_links_still_count_toward_inbound(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Start Here.md is a hand-curated dashboard. Its wikilinks must
        # still register the linked records as having inbound — that
        # was the explicit design decision (skip validation, keep
        # link-graph participation). A linked record should NOT fire
        # ORPHAN001 just because its only inbound is from a root file.
        _write_record(
            tmp_vault,
            "person/Linked Person.md",
            dedent(
                """\
                type: person
                name: Linked Person
                created: '2026-04-30'
                status: active
                tags: []
                related: []
                """
            ).rstrip(),
        )
        (tmp_vault / "Start Here.md").write_text(
            "Dashboard:\n[[person/Linked Person]] is here.\n",
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        # Record with inbound from scaffold file: NOT orphaned.
        target_orphans = _orphan_issues(issues, "person/Linked Person.md")
        assert target_orphans == [], (
            f"Inbound from scaffold file should still count, got: {target_orphans}"
        )

    def test_stale_state_open_issues_cleared_for_scaffold(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Edge case: pre-fix scans recorded LINK001 / FM001 on root
        # scaffold files in state. After the fix lands, those open
        # issues should be cleared on the first scan so the count
        # converges instead of dragging stale entries forward.
        (tmp_vault / "CLAUDE.md").write_text("# CLAUDE\n[[wikilinks]]\n", encoding="utf-8")

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        # Simulate stale state from a pre-fix scan: file present with
        # open_issues populated, md5 matches current content.
        from alfred.janitor.utils import compute_md5
        stale_md5 = compute_md5(tmp_vault / "CLAUDE.md")
        state.update_file("CLAUDE.md", stale_md5, ["LINK001", "FM001"])
        assert state.files["CLAUDE.md"].open_issues == ["LINK001", "FM001"]

        run_structural_scan(config, state)

        # State for the scaffold file is cleared on first post-fix scan.
        assert state.files["CLAUDE.md"].open_issues == []
