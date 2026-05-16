"""Migration script tests — synthesized 22-Meditations-note fixture vault.

Phase 1 commit 5/5 of the Hypatia Zettelkasten cutover. Tests the
migration script ``scripts/migrate_2026-05-16_meditations_zettels.py``
against a synthesized fixture vault that simulates the morning's
auto-created Meditations notes + author record + wikilinks.

The script runs against the LIVE Hypatia vault when team-lead executes
it (sandbox blocks cross-vault access from this test suite). These
fixture tests cover:

  * Identification: notes with frontmatter ``source: [[source/Meditations]]``
    are found; notes with other sources are excluded.
  * Author rename: legacy ``author/Aurelius.md`` → canonical
    ``author/Aurelius, Marcus.md`` with aliases populated and
    legacy fields stripped.
  * Note move: ``note/<title>.md`` → ``zettel/<title>.md`` with
    ``type: zettel`` updated.
  * Wikilink updates: ``[[note/<title>]]`` → ``[[zettel/<title>]]``
    and ``[[author/Aurelius]]`` → ``[[author/Aurelius, Marcus]]``
    across the entire vault.
  * Source body update: ``## Permanent Notes spawned`` section's
    ``[[note/X]]`` wikilinks rewritten to ``[[zettel/X]]``.
  * Idempotency: re-running on an already-migrated vault is a no-op.
  * Dry-run vs. apply: dry-run reports the plan but writes nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import frontmatter
import pytest


# --- Loader (script is not a package member, so importlib.util) -----------


def _load_migration_module():
    """Load the migration script as a module for testing."""
    script_path = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "migrate_2026-05-16_meditations_zettels.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_migrate_meditations", script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mig():
    """Module-level fixture: the migration script as an importable module."""
    return _load_migration_module()


# --- Fixture-vault builder ------------------------------------------------


def _build_fixture_vault(
    tmp_path: Path, num_meditations_notes: int = 22,
) -> Path:
    """Build a fixture Hypatia vault simulating the morning's auto-created
    state. Returns the vault root path.

    Layout produced:
      vault/note/<22 Meditations notes>.md  (frontmatter
          ``source: [[source/Meditations]]`` + ``author: [[author/Aurelius]]``)
      vault/note/Unrelated Note.md  (different source — must NOT migrate)
      vault/author/Aurelius.md  (legacy last-name-only, with ``last_name``
          and ``status: active`` fields the migration strips)
      vault/source/Meditations.md  (with a ``## Permanent Notes spawned``
          section listing some of the 22)
      vault/session/capture-A.md  (carries a ``[[note/<title>]]`` reference
          inside a body section — wikilink update target)
    """
    vault = tmp_path / "vault"
    for sub in ("note", "zettel", "author", "source", "session"):
        (vault / sub).mkdir(parents=True)

    # Build the 22 (or N) Meditations-anchored notes.
    note_titles: list[str] = []
    for i in range(num_meditations_notes):
        title = f"Meditations Insight {i:02d}"
        note_titles.append(title)
        (vault / "note" / f"{title}.md").write_text(
            "---\n"
            "type: note\n"
            f"name: {title}\n"
            "created: '2026-05-16'\n"
            "source: \"[[source/Meditations]]\"\n"
            "author: \"[[author/Aurelius]]\"\n"
            "tags: []\n"
            "---\n\n"
            "# Meditations Insight\n\n"
            f"Body text for note {i}.\n",
            encoding="utf-8",
        )

    # Unrelated note with a different source — should NOT migrate.
    (vault / "note" / "Unrelated Note.md").write_text(
        "---\n"
        "type: note\n"
        "name: Unrelated Note\n"
        "created: '2026-05-16'\n"
        "source: \"[[source/Some Other Book]]\"\n"
        "tags: []\n"
        "---\n\n# Unrelated\n",
        encoding="utf-8",
    )

    # A note with NO source field — also should NOT migrate.
    (vault / "note" / "Sourceless Note.md").write_text(
        "---\n"
        "type: note\n"
        "name: Sourceless Note\n"
        "created: '2026-05-16'\n"
        "tags: []\n"
        "---\n\n# Sourceless\n",
        encoding="utf-8",
    )

    # Legacy author record.
    (vault / "author" / "Aurelius.md").write_text(
        "---\n"
        "type: author\n"
        "status: active\n"
        "name: Marcus Aurelius\n"
        "last_name: Aurelius\n"
        "aliases: []\n"
        "era: ''\n"
        "school: ''\n"
        "description: ''\n"
        "created: '2026-05-16'\n"
        "tags: []\n"
        "related: []\n"
        "---\n\n"
        "# Marcus Aurelius\n\n"
        "## Summary\n\n## Works in the library\n\n## Related\n",
        encoding="utf-8",
    )

    # Source record with Permanent Notes spawned section.
    perm_notes_links = "\n".join(
        f"- [[note/{title}]]" for title in note_titles[:5]
    )
    (vault / "source" / "Meditations.md").write_text(
        "---\n"
        "type: source\n"
        "name: Meditations\n"
        "author: \"[[author/Aurelius]]\"\n"
        "created: '2026-05-16'\n"
        "---\n\n"
        "# Meditations\n\n"
        "## Permanent Notes spawned\n"
        f"{perm_notes_links}\n\n"
        "## Notes\n\n"
        "(running notes here)\n",
        encoding="utf-8",
    )

    # Session record that links back to one of the notes + the author.
    if note_titles:
        (vault / "session" / "capture-A.md").write_text(
            "---\n"
            "type: session\n"
            "name: capture-A\n"
            "created: '2026-05-16'\n"
            f"source: \"[[source/Meditations]]\"\n"
            "author: \"[[author/Aurelius]]\"\n"
            "---\n\n"
            "# Transcript\n\n"
            f"Linked content: [[note/{note_titles[0]}]] and "
            f"[[author/Aurelius]] together.\n",
            encoding="utf-8",
        )

    return vault


# --- Plan discovery ------------------------------------------------------


def test_identify_orphan_meditations_notes_finds_22(tmp_path: Path, mig) -> None:
    """The fixture's 22 Meditations notes are all identified."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=22)
    found = mig.identify_orphan_meditations_notes(vault)
    assert len(found) == 22


def test_identify_skips_notes_with_different_source(
    tmp_path: Path, mig,
) -> None:
    """Notes with a different source (or no source) are excluded."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    found = mig.identify_orphan_meditations_notes(vault)
    found_names = {p.name for p in found}
    assert "Unrelated Note.md" not in found_names
    assert "Sourceless Note.md" not in found_names


def test_identify_returns_empty_when_no_note_dir(
    tmp_path: Path, mig,
) -> None:
    """Vault without a note/ directory → empty list, no error."""
    vault = tmp_path / "vault"
    vault.mkdir()
    assert mig.identify_orphan_meditations_notes(vault) == []


def test_identify_author_rename_present(tmp_path: Path, mig) -> None:
    """Legacy ``Aurelius.md`` exists, canonical doesn't → rename pair."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    pair = mig.identify_author_rename(vault)
    assert pair is not None
    legacy, canonical = pair
    assert legacy.name == "Aurelius.md"
    assert canonical.name == "Aurelius, Marcus.md"


def test_identify_author_rename_skips_if_canonical_exists(
    tmp_path: Path, mig,
) -> None:
    """Canonical already present → idempotent skip."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    # Pre-create the canonical record (simulates partial prior run).
    (vault / "author" / "Aurelius, Marcus.md").write_text(
        "---\ntype: author\nname: Marcus Aurelius\n---\n",
        encoding="utf-8",
    )
    assert mig.identify_author_rename(vault) is None


# --- Author rename ---------------------------------------------------------


def test_rename_author_record_strips_legacy_fields(
    tmp_path: Path, mig,
) -> None:
    """After rename: aliases populated, status / last_name / era /
    school / description dropped."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    legacy = vault / "author" / "Aurelius.md"
    canonical = vault / "author" / "Aurelius, Marcus.md"

    mig.rename_author_record(legacy, canonical)

    assert canonical.exists()
    assert not legacy.exists()

    post = frontmatter.load(canonical)
    fm = post.metadata
    assert fm["name"] == "Marcus Aurelius"
    aliases = fm.get("aliases") or []
    assert "Marcus Aurelius" in aliases
    assert "Aurelius, Marcus" in aliases
    # Stripped fields.
    for stripped in ("last_name", "era", "school", "description"):
        assert stripped not in fm, f"{stripped!r} not stripped during rename"
    # Status was the legacy ``active`` default → dropped.
    assert "status" not in fm


# --- Note → zettel move ---------------------------------------------------


def test_move_note_to_zettel_updates_type_field(
    tmp_path: Path, mig,
) -> None:
    """Move sets ``type: zettel`` and lands the file in zettel/."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=2)
    note = vault / "note" / "Meditations Insight 00.md"
    assert note.exists()

    dest = mig.move_note_to_zettel(note, vault)

    assert dest.exists()
    assert not note.exists()
    assert dest.name == "Meditations Insight 00.md"
    assert dest.parent.name == "zettel"

    post = frontmatter.load(dest)
    assert post["type"] == "zettel"
    assert post["name"] == "Meditations Insight 00"
    # Other fields preserved.
    assert post["source"] == "[[source/Meditations]]"


def test_move_note_to_zettel_refuses_overwrite(tmp_path: Path, mig) -> None:
    """If a zettel with the same name already exists, raise rather than
    silently overwrite — idempotency requires explicit handling."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    note = vault / "note" / "Meditations Insight 00.md"
    # Pre-create the destination (simulating partial migration state
    # we should NOT bulldoze).
    (vault / "zettel" / "Meditations Insight 00.md").write_text(
        "---\ntype: zettel\nname: Meditations Insight 00\n---\n# Pre-existing\n",
        encoding="utf-8",
    )
    with pytest.raises(FileExistsError):
        mig.move_note_to_zettel(note, vault)


# --- Wikilink updates -----------------------------------------------------


def test_find_wikilink_update_targets_catches_session_link(
    tmp_path: Path, mig,
) -> None:
    """The session record (with ``[[note/Meditations Insight 00]]`` body
    link + ``[[author/Aurelius]]``) is flagged for update."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=2)
    plan = mig.build_plan(vault)
    target_paths = {p for p, _ in plan.wikilink_update_targets}
    # The session record contains both a note/ link AND an author link.
    assert any("session/capture-A.md" in str(p) for p in target_paths)
    # The notes themselves carry author links — they get rewritten too.
    # (They contain ``author: "[[author/Aurelius]]"`` in frontmatter.)
    note_targets = [p for p in target_paths if "note/" in str(p)]
    assert len(note_targets) > 0


def test_apply_wikilink_updates_rewrites_author_link(
    tmp_path: Path, mig,
) -> None:
    """The ``[[author/Aurelius]]`` body link in the session record gets
    rewritten to ``[[author/Aurelius, Marcus]]``."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    session_path = vault / "session" / "capture-A.md"
    migrated_titles = {"Meditations Insight 00"}

    applied = mig.apply_wikilink_updates(session_path, migrated_titles)
    assert applied >= 2  # at minimum: the body's note link + author link

    text = session_path.read_text(encoding="utf-8")
    assert "[[author/Aurelius, Marcus]]" in text
    assert "[[zettel/Meditations Insight 00]]" in text
    # Legacy forms are gone.
    assert "[[author/Aurelius]]" not in text
    assert "[[note/Meditations Insight 00]]" not in text


# --- Source body update ---------------------------------------------------


def test_update_source_permanent_notes_section(tmp_path: Path, mig) -> None:
    """The source record's ``## Permanent Notes spawned`` section gets
    ``[[note/X]]`` → ``[[zettel/X]]`` rewrites."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=5)
    source_path = vault / "source" / "Meditations.md"
    migrated_titles = {f"Meditations Insight {i:02d}" for i in range(5)}

    count = mig.update_source_permanent_notes(source_path, migrated_titles)
    assert count == 5

    text = source_path.read_text(encoding="utf-8")
    for i in range(5):
        title = f"Meditations Insight {i:02d}"
        assert f"[[zettel/{title}]]" in text
        assert f"[[note/{title}]]" not in text


# --- End-to-end apply -----------------------------------------------------


def test_apply_plan_end_to_end(tmp_path: Path, mig) -> None:
    """Full migration cycle: 22 notes moved, author renamed, wikilinks
    + source section updated."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=22)
    plan = mig.build_plan(vault)

    counters = mig.apply_plan(plan, vault)

    assert counters["notes_moved"] == 22
    assert counters["author_renamed"] == 1
    assert counters["wikilink_files_updated"] >= 1
    assert counters["source_section_updates"] == 5  # 5 in fixture's spawned section

    # Vault post-state:
    # - 22 zettels exist; 0 Meditations notes remain.
    zettel_count = len(list((vault / "zettel").glob("Meditations Insight *.md")))
    assert zettel_count == 22
    note_meditations_count = len(
        list((vault / "note").glob("Meditations Insight *.md"))
    )
    assert note_meditations_count == 0
    # - Unrelated notes still in note/.
    assert (vault / "note" / "Unrelated Note.md").exists()
    assert (vault / "note" / "Sourceless Note.md").exists()
    # - Canonical author exists, legacy doesn't.
    assert (vault / "author" / "Aurelius, Marcus.md").exists()
    assert not (vault / "author" / "Aurelius.md").exists()


# --- Idempotency ----------------------------------------------------------


def test_re_run_is_no_op(tmp_path: Path, mig) -> None:
    """Running the migration a second time finds nothing to do."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)

    # First run.
    plan1 = mig.build_plan(vault)
    mig.apply_plan(plan1, vault)

    # Second run.
    plan2 = mig.build_plan(vault)
    assert plan2.notes_to_move == []
    assert plan2.author_rename is None
    # already_migrated_titles populated from zettel/ scan.
    assert len(plan2.already_migrated_titles) == 3

    counters2 = mig.apply_plan(plan2, vault)
    assert counters2["notes_moved"] == 0
    assert counters2["author_renamed"] == 0


def test_partial_state_idempotent(tmp_path: Path, mig) -> None:
    """Mid-migration crash: some notes moved, some not. Re-run completes."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=5)

    # Simulate partial migration: move 2 of 5 manually + delete from note/.
    for i in range(2):
        title = f"Meditations Insight {i:02d}"
        src = vault / "note" / f"{title}.md"
        dest = vault / "zettel" / f"{title}.md"
        post = frontmatter.load(src)
        post["type"] = "zettel"
        dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
        src.unlink()

    # Now re-run.
    plan = mig.build_plan(vault)
    assert len(plan.notes_to_move) == 3
    assert len(plan.already_migrated_titles) == 2

    counters = mig.apply_plan(plan, vault)
    assert counters["notes_moved"] == 3

    # Final state: 5 zettels, 0 Meditations notes.
    assert len(list((vault / "zettel").glob("Meditations Insight *.md"))) == 5
    assert len(list((vault / "note").glob("Meditations Insight *.md"))) == 0


# --- CLI dry-run + apply --------------------------------------------------


def test_cli_dry_run_does_not_write(tmp_path: Path, mig, capsys) -> None:
    """Default invocation (no --apply) prints the plan but writes nothing."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    # Snapshot before.
    note_count_before = len(list((vault / "note").glob("*.md")))
    legacy_author_before = (vault / "author" / "Aurelius.md").exists()

    rc = mig.main(["--vault", str(vault)])
    assert rc == 0

    # No writes.
    assert len(list((vault / "note").glob("*.md"))) == note_count_before
    assert (vault / "author" / "Aurelius.md").exists() == legacy_author_before
    # No zettel files.
    assert list((vault / "zettel").glob("*.md")) == []

    # Output mentions the plan.
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Meditations Insight" in out


def test_cli_apply_executes_migration(tmp_path: Path, mig, capsys) -> None:
    """--apply mode actually writes the migration."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    rc = mig.main(["--vault", str(vault), "--apply"])
    assert rc == 0

    # Migration applied.
    assert len(list((vault / "zettel").glob("Meditations Insight *.md"))) == 3
    assert (vault / "author" / "Aurelius, Marcus.md").exists()
    assert not (vault / "author" / "Aurelius.md").exists()

    out = capsys.readouterr().out
    assert "APPLYING" in out
    assert "Migration complete" in out


def test_cli_returns_error_on_missing_vault(tmp_path: Path, mig, capsys) -> None:
    """Pointing at a non-existent vault returns exit code 2."""
    rc = mig.main(["--vault", str(tmp_path / "does-not-exist")])
    assert rc == 2
