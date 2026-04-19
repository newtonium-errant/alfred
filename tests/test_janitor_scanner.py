"""Tests for ``alfred.janitor.scanner._build_stem_index``.

The stem index powers wikilink resolution for the broken-link scanner.
Obsidian wikilinks can reference either a bare stem (``[[Eagle Farm]]``)
or a path-qualified stem (``[[project/Eagle Farm]]``); the index must
map BOTH forms to the same underlying file, or the scanner will
hallucinate broken-link issues on perfectly valid links.

Also verifies the ignore-dirs filter actually excludes files under
``.obsidian`` / vault-internal paths so noisy no-ops don't flood the
reported issue list.
"""

from __future__ import annotations

from pathlib import Path

from alfred.janitor.scanner import _build_stem_index


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
