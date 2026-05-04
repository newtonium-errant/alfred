"""Tests for the LINK001 wikilink normalization helper +
its routing through scanner.py.

Pins the three normalization rules a scanner lookup must apply
before testing a wikilink target against ``stem_index``:

    1. YAML apostrophe decode (``''`` → ``'``)
    2. Trailing ``.md`` strip (Obsidian-tolerated suffix)
    3. ``#anchor`` strip (in-document jump destination)

Each rule has its own constraint record under
``vault/constraint/Janitor LINK001 ...`` documenting the bug class
and the false-positive volume it created. Trailing ``.md`` strip is
the load-bearing fix from this commit; the other two are pre-existing
behaviors that the helper now consolidates.

Regression coverage: a vault with a GENUINELY broken wikilink still
fires LINK001 — the normalization can't be so aggressive that real
broken links go silent.
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
from alfred.janitor.scanner import (
    _normalize_wikilink_target_for_lookup,
    run_structural_scan,
)
from alfred.janitor.state import JanitorState


# ---------------------------------------------------------------------------
# Helpers (mirror the existing test_janitor_scanner.py shape)
# ---------------------------------------------------------------------------


def _build_scan_config(
    vault: Path, state_dir: Path,
    *, ignore_dirs: list[str] | None = None,
) -> JanitorConfig:
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=ignore_dirs if ignore_dirs is not None else [
                ".obsidian", "_templates", "_bases",
            ],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _write_record(
    vault: Path, rel: str, frontmatter: str, body: str = "",
) -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _link_issues(issues: list, rel: str) -> list:
    return [
        i for i in issues
        if i.file == rel and i.code == IssueCode.BROKEN_WIKILINK
    ]


# ---------------------------------------------------------------------------
# _normalize_wikilink_target_for_lookup — unit tests for each rule
# ---------------------------------------------------------------------------


class TestNormalizeWikilinkTarget:
    """Per-rule unit coverage for the lookup-side normalization helper."""

    def test_strips_trailing_md(self):
        """``[[session/X.md]]`` is the Obsidian-tolerated suffix form;
        the scanner must look up ``session/X``, not ``session/X.md``,
        because stem_index keys carry the rel-path-without-extension."""
        assert _normalize_wikilink_target_for_lookup(
            "session/Voice Note.md",
        ) == "session/Voice Note"

    def test_does_not_strip_md_in_middle_of_path(self):
        """Only the TRAILING ``.md`` is stripped — ``person/Y.md.smith``
        (hypothetical pathological filename) keeps everything except a
        suffix-position match."""
        assert _normalize_wikilink_target_for_lookup(
            "person/X.md.something",
        ) == "person/X.md.something"

    def test_strips_anchor(self):
        """``[[record#Section]]`` resolves to the file ``record``;
        the anchor is an in-document jump, not part of the file path."""
        assert _normalize_wikilink_target_for_lookup(
            "person/Andrew Newton#Calibration",
        ) == "person/Andrew Newton"

    def test_strips_anchor_before_md_strip(self):
        """Anchor strip happens FIRST so weird shapes like
        ``[[X#Y.md]]`` (no real source carries this, but it's a
        plausible YAML continuation artifact) drop the anchor cleanly
        and leave the path bare."""
        assert _normalize_wikilink_target_for_lookup(
            "X#Y.md",
        ) == "X"

    def test_decodes_yaml_apostrophe(self):
        """YAML's single-quoted scalar form doubles literal apostrophes
        (``''`` → ``'``); the on-disk file uses the single form."""
        assert _normalize_wikilink_target_for_lookup(
            "constraint/Andrew''s Note",
        ) == "constraint/Andrew's Note"

    def test_composes_all_three_rules(self):
        """Apostrophe decode + anchor strip + ``.md`` strip in one
        target — all rules apply in the right order."""
        assert _normalize_wikilink_target_for_lookup(
            "constraint/Andrew''s Note#Section.md",
        ) == "constraint/Andrew's Note"

    def test_empty_target_passes_through(self):
        assert _normalize_wikilink_target_for_lookup("") == ""

    def test_unaffected_target_unchanged(self):
        """A vanilla wikilink — no apostrophes, no .md, no anchor —
        passes through unchanged."""
        assert _normalize_wikilink_target_for_lookup(
            "project/Eagle Farm",
        ) == "project/Eagle Farm"


# ---------------------------------------------------------------------------
# Integration — scanner against a vault carrying each false-positive shape
# ---------------------------------------------------------------------------


class TestScannerMdSuffixStrip:
    """LINK001 must NOT fire when a wikilink target carries the
    trailing ``.md`` suffix. Constraint record:
    ``constraint/Janitor LINK001 Scanner Does Not Strip .md Suffix
    From Wikilinks Before Resolving Targets`` (2026-05-01)."""

    def test_md_suffix_wikilink_resolves(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        # Target file exists at session/Voice Note.md.
        _write_record(
            tmp_vault, "session/Voice Note.md",
            dedent(
                """\
                type: session
                name: Voice Note
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
        )
        # Source record carries the wikilink WITH a trailing .md
        # (the Obsidian-tolerated form). Pre-fix: scanner looked up
        # ``session/Voice Note.md`` against stem_index and missed
        # because stem_index only has ``session/Voice Note``.
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
            body="See [[session/Voice Note.md]] for the recording.",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        src_links = _link_issues(issues, "note/Source.md")
        assert src_links == [], (
            f"Unexpected LINK001 on .md-suffixed wikilink whose "
            f"target exists: {[i.message for i in src_links]}"
        )


class TestScannerAnchorStrip:
    """LINK001 must NOT fire when a wikilink carries a ``#anchor``
    suffix and the underlying file exists. Low-volume on Salem's
    vault but a clean structural fix."""

    def test_anchor_wikilink_resolves(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        _write_record(
            tmp_vault, "person/Andrew Newton.md",
            dedent(
                """\
                type: person
                name: Andrew Newton
                created: '2026-04-15'
                tags: []
                """
            ).rstrip(),
            body="# Andrew Newton\n\n## Calibration\n\nNotes.",
        )
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
            body=(
                "Per [[person/Andrew Newton#Calibration]], "
                "the calibration note applies."
            ),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        src_links = _link_issues(issues, "note/Source.md")
        assert src_links == [], (
            f"Unexpected LINK001 on #anchor wikilink whose target "
            f"exists: {[i.message for i in src_links]}"
        )


class TestScannerMdSuffixCombinedWithAnchor:
    """Edge: ``[[X#Section]]`` and ``[[X.md]]`` separately resolve;
    the unusual combined form ``[[X#Section.md]]`` (rare but plausible
    distiller-emitted shape) must also resolve."""

    def test_combined_anchor_and_md_suffix(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        _write_record(
            tmp_vault, "decision/Some Decision.md",
            dedent(
                """\
                type: decision
                name: Some Decision
                created: '2026-04-15'
                tags: []
                """
            ).rstrip(),
        )
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
            body="See [[decision/Some Decision#Rationale.md]].",
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)
        src_links = _link_issues(issues, "note/Source.md")
        assert src_links == []


# ---------------------------------------------------------------------------
# Inbound-index path — same normalization must apply
# ---------------------------------------------------------------------------


class TestInboundIndexNormalization:
    """The inbound-link index (which feeds ORPHAN001) must use the
    same normalization helper as the LINK001 lookup; otherwise a
    record linked-to via ``[[X.md]]`` registers zero inbound and
    falsely flags as orphaned."""

    def test_inbound_via_md_suffix_link_registers(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        # Target — type: person so it's eligible for ORPHAN001.
        _write_record(
            tmp_vault, "person/Target Person.md",
            dedent(
                """\
                type: person
                name: Target Person
                created: '2026-04-15'
                tags: []
                """
            ).rstrip(),
        )
        # Source links to target via the .md-suffixed form.
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-15'
                tags: []
                related:
                - '[[person/Target Person.md]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)

        # Target record should NOT show up as orphaned — the inbound
        # index must have resolved the .md-suffixed link to the same
        # canonical key the target's own rel_path uses.
        orphan_issues = [
            i for i in issues
            if i.file == "person/Target Person.md"
            and i.code == IssueCode.ORPHANED_RECORD
        ]
        assert orphan_issues == [], (
            "Target should not be orphaned — Source links to it via "
            "the .md-suffixed wikilink form, which the inbound index "
            "must normalize the same way as the LINK001 lookup."
        )


# ---------------------------------------------------------------------------
# Regression — REAL broken wikilink must still fire LINK001
# ---------------------------------------------------------------------------


class TestRealBrokenLinkStillFires:
    """The normalization fix must not be so aggressive it hides real
    broken wikilinks. A wikilink whose target genuinely doesn't exist
    — with or without the .md suffix, with or without an anchor —
    must still raise LINK001."""

    def test_md_suffix_on_nonexistent_target_still_fires(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        # Source record references a target that doesn't exist, with
        # the .md suffix present. Even after stripping, the bare path
        # ``session/Phantom Note`` is not in stem_index → LINK001.
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
            body="See [[session/Phantom Note.md]] for nothing.",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)
        src_links = _link_issues(issues, "note/Source.md")
        assert len(src_links) == 1, (
            f"Expected LINK001 on phantom .md-suffixed wikilink: {src_links}"
        )
        # The user-visible message must preserve the RAW captured target
        # (with the .md suffix) so the operator sees what the file
        # contains, not what the scanner internally normalized to.
        assert "session/Phantom Note.md" in src_links[0].message

    def test_anchor_on_nonexistent_target_still_fires(
        self, tmp_vault: Path, tmp_path: Path,
    ) -> None:
        _write_record(
            tmp_vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                created: '2026-04-26'
                tags: []
                """
            ).rstrip(),
            body="See [[person/Phantom Person#Bio]] for nothing.",
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(tmp_vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        issues = run_structural_scan(config, state)
        src_links = _link_issues(issues, "note/Source.md")
        assert len(src_links) == 1
        # Raw target preserved in the message.
        assert "person/Phantom Person#Bio" in src_links[0].message
