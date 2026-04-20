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
