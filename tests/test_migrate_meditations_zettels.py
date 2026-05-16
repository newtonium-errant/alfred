"""Migration script tests — Meditations-derived capture notes → zettel/.

Phase 1 commit 5/5 (reworked 2026-05-16) of the Hypatia Zettelkasten
cutover. Tests the migration script
``alfred.scripts.migrate_2026_05_16_meditations_zettels`` (reachable
via the dash-form shim
``scripts/migrate_2026-05-16_meditations_zettels.py``) against a
synthesized fixture vault matching the LIVE Hypatia vault shape.

LIVE STATE (per dry-run against /home/andrew/library-alexandria):
  * Capture-derived notes carry ``source_session:`` (NOT ``source:``).
  * The Meditations-related captures predate the source-anchor ship,
    so their ``source:`` frontmatter is empty.
  * No ``author/Aurelius.md`` record exists.
  * No ``source/Meditations.md`` record exists.

The migration accordingly:
  1. Detects notes by ``source_session:`` pointing at a Meditations-
     pattern session filename (substring match on ``marcus-aurelius``
     or ``meditations``).
  2. Moves them to ``zettel/`` with ``type`` updated.
  3. Updates vault-wide wikilinks ``[[note/<title>]]`` → ``[[zettel/<title>]]``.
  4. Idempotent re-run via a ``zettel/`` scan with the same pattern.

NO author rename. NO source-record body update. Both dropped from
this rework because the live vault has neither artifact.

Test coverage:
  * Session-pattern matcher unit tests (case-insensitive substring,
    wikilink/bare/pipe-alias input shapes).
  * Identification of in-scope notes vs. excluded (other sessions,
    no source_session, missing dirs).
  * Note → zettel move (type field updated, source_session preserved,
    overwrite refused).
  * Wikilink updates (vault-wide rewrites, pipe-alias preservation).
  * Idempotency: re-run after success is a no-op; partial-state
    re-run completes.
  * Dry-run vs apply (CLI).
  * Excluded-shape sanity: notes whose ``source_session:`` points at
    a non-Meditations capture session stay put.

Import shape: the migration logic lives at ``src/alfred/scripts/``
so dataclass field-resolution at decorator-time sees a properly-
registered ``sys.modules`` entry. The ``mig`` fixture imports the
package module normally; no importlib-spec gymnastics.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.scripts import migrate_2026_05_16_meditations_zettels as _mig_module


@pytest.fixture
def mig():
    """Module-level fixture: the migration module — imported normally
    from the alfred.scripts package."""
    return _mig_module


# --- Session-pattern matcher unit tests ----------------------------------


@pytest.mark.parametrize("target,expected", [
    # The three live target sessions (post-dry-run).
    ("session/capture-2026-05-16-marcus-aurelius-meditations-notes-5e7a917c", True),
    ("session/capture-2026-05-16-marcus-aurelius-reading-notes-11943ac7", True),
    ("session/capture-2026-05-16-meditations-introduction-notes-heraclitus-22104fef", True),
    # Wikilink form.
    ("[[session/capture-2026-05-16-marcus-aurelius-meditations-notes-5e7a917c]]", True),
    # With .md extension.
    ("session/capture-2026-05-16-meditations-foo.md", True),
    # Pipe-alias form.
    ("[[session/capture-meditations-x|Meditations capture]]", True),
    # Bare filename without session/ prefix.
    ("capture-2026-05-16-marcus-aurelius-meditations-x", True),
    # Case-insensitivity.
    ("session/CAPTURE-MARCUS-AURELIUS-x", True),
    ("session/Capture-Meditations-X", True),
    # Excluded shapes.
    ("session/capture-2026-05-15-random-thoughts-abc12345", False),
    ("session/Voice Session — 2026-05-10 1000 def67890", False),
    ("[[session/capture-2026-05-16-fencing-practice-xyz]]", False),
    ("", False),
    ("   ", False),
])
def test_session_target_matches_meditations(
    mig, target: str, expected: bool,
) -> None:
    assert mig._session_target_matches_meditations(target) is expected


# --- Fixture-vault builder ------------------------------------------------


# The three live target sessions per the dry-run + Andrew's confirmation.
_MEDITATIONS_SESSIONS: tuple[str, ...] = (
    "capture-2026-05-16-marcus-aurelius-meditations-notes-5e7a917c",
    "capture-2026-05-16-marcus-aurelius-reading-notes-11943ac7",
    "capture-2026-05-16-meditations-introduction-notes-heraclitus-22104fef",
)

# A non-Meditations capture session — its derived notes should NOT migrate.
_UNRELATED_SESSION: str = "capture-2026-05-15-fencing-practice-aaaaaaaa"


def _build_fixture_vault(
    tmp_path: Path,
    num_meditations_notes: int = 22,
    num_unrelated_notes: int = 2,
) -> Path:
    """Build a fixture Hypatia vault matching the LIVE shape (post
    dry-run reveal):

      vault/note/<N Meditations-anchored notes>.md
          (frontmatter ``source_session: "[[session/<meditations-session>]]"``,
           type: note, no source: or author: fields)
      vault/note/<M unrelated capture notes>.md
          (frontmatter ``source_session: "[[session/<fencing-practice>]]"``,
           type: note — must NOT migrate)
      vault/note/Sourceless Note.md
          (no source_session — also must NOT migrate)
      vault/session/<3 Meditations sessions>.md  (existing capture session
          records; presence not strictly required by the migration but
          included so the fixture mirrors live state)
      vault/session/<unrelated session>.md
    """
    vault = tmp_path / "vault"
    for sub in ("note", "zettel", "session"):
        (vault / sub).mkdir(parents=True)

    # 3 Meditations sessions (existence not strictly required by the
    # migration — it looks at the note's source_session frontmatter, not
    # the session record itself — but keeps the fixture realistic).
    for sess_name in _MEDITATIONS_SESSIONS:
        (vault / "session" / f"{sess_name}.md").write_text(
            "---\n"
            "type: session\n"
            f"name: {sess_name}\n"
            "created: '2026-05-16'\n"
            "session_type: capture\n"
            "---\n# Transcript\n\n(transcript body)\n",
            encoding="utf-8",
        )

    # Unrelated capture session — its derived notes must NOT migrate.
    (vault / "session" / f"{_UNRELATED_SESSION}.md").write_text(
        "---\n"
        "type: session\n"
        f"name: {_UNRELATED_SESSION}\n"
        "created: '2026-05-15'\n"
        "session_type: capture\n"
        "---\n# Transcript\n\n(fencing transcript)\n",
        encoding="utf-8",
    )

    # Meditations-derived notes — distribute across the 3 sessions.
    note_titles: list[str] = []
    for i in range(num_meditations_notes):
        title = f"Meditations Insight {i:02d}"
        note_titles.append(title)
        # Round-robin across the 3 sessions for fixture realism.
        sess = _MEDITATIONS_SESSIONS[i % len(_MEDITATIONS_SESSIONS)]
        (vault / "note" / f"{title}.md").write_text(
            "---\n"
            "type: note\n"
            f"name: {title}\n"
            "created: '2026-05-15'\n"
            f"source_session: \"[[session/{sess}]]\"\n"
            "confidence_tier: high\n"
            "tags: []\n"
            "---\n\n"
            "# Meditations Insight\n\n"
            f"Body text for note {i}.\n",
            encoding="utf-8",
        )

    # Unrelated capture-derived notes — same shape but pointing at the
    # fencing session. Must NOT migrate.
    for i in range(num_unrelated_notes):
        title = f"Fencing Drill {i:02d}"
        (vault / "note" / f"{title}.md").write_text(
            "---\n"
            "type: note\n"
            f"name: {title}\n"
            "created: '2026-05-15'\n"
            f"source_session: \"[[session/{_UNRELATED_SESSION}]]\"\n"
            "tags: []\n"
            "---\n\n# Fencing Drill\n",
            encoding="utf-8",
        )

    # A note with NO source_session — also must NOT migrate.
    (vault / "note" / "Sourceless Note.md").write_text(
        "---\n"
        "type: note\n"
        "name: Sourceless Note\n"
        "created: '2026-05-15'\n"
        "tags: []\n"
        "---\n\n# Sourceless\n",
        encoding="utf-8",
    )

    # A cross-link note: contains [[note/<first-meditations-note>]] in
    # body so the vault-wide wikilink-update pass has a target.
    if note_titles:
        (vault / "note" / "Cross-Reference Note.md").write_text(
            "---\n"
            "type: note\n"
            "name: Cross-Reference Note\n"
            "created: '2026-05-15'\n"
            f"source_session: \"[[session/{_UNRELATED_SESSION}]]\"\n"
            "tags: []\n"
            "---\n\n"
            "# Cross-Reference Note\n\n"
            f"This mentions [[note/{note_titles[0]}]] in body.\n",
            encoding="utf-8",
        )

    return vault


# --- Plan discovery ------------------------------------------------------


def test_identify_orphan_meditations_notes_finds_all(
    tmp_path: Path, mig,
) -> None:
    """All N Meditations-anchored notes are identified."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=22)
    found = mig.identify_orphan_meditations_notes(vault)
    assert len(found) == 22


def test_identify_skips_unrelated_session_notes(
    tmp_path: Path, mig,
) -> None:
    """Notes whose source_session points at a non-Meditations session
    (e.g. fencing) are excluded. Same for notes with no
    source_session field."""
    vault = _build_fixture_vault(
        tmp_path, num_meditations_notes=3, num_unrelated_notes=2,
    )
    found = mig.identify_orphan_meditations_notes(vault)
    found_names = {p.name for p in found}
    # Meditations notes present.
    assert {f"Meditations Insight {i:02d}.md" for i in range(3)} <= found_names
    # Fencing notes absent.
    assert "Fencing Drill 00.md" not in found_names
    assert "Fencing Drill 01.md" not in found_names
    # Sourceless absent.
    assert "Sourceless Note.md" not in found_names
    # Cross-Reference Note has source_session pointing at fencing →
    # excluded from migration even though its BODY references a
    # Meditations note (different surface).
    assert "Cross-Reference Note.md" not in found_names


def test_identify_returns_empty_when_no_note_dir(
    tmp_path: Path, mig,
) -> None:
    """Vault without a note/ directory → empty list, no error."""
    vault = tmp_path / "vault"
    vault.mkdir()
    assert mig.identify_orphan_meditations_notes(vault) == []


def test_identify_returns_empty_when_no_zettel_dir(
    tmp_path: Path, mig,
) -> None:
    """``zettel/`` scan tolerates absent directory (fresh-vault state)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note").mkdir()  # note/ exists but zettel/ doesn't
    assert mig.identify_migrated_meditations_zettels(vault) == []


def test_identify_migrated_zettels_via_source_session_pattern(
    tmp_path: Path, mig,
) -> None:
    """After a successful migration, zettel/-scan finds the migrated
    records via the same source_session pattern detection."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    plan = mig.build_plan(vault)
    mig.apply_plan(plan, vault)

    # Now scan zettel/ — should find 3.
    migrated = mig.identify_migrated_meditations_zettels(vault)
    assert len(migrated) == 3
    assert all(p.parent.name == "zettel" for p in migrated)


# --- Note → zettel move ---------------------------------------------------


def test_move_note_to_zettel_updates_type_field(
    tmp_path: Path, mig,
) -> None:
    """Move sets ``type: zettel`` and preserves source_session."""
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
    # source_session preserved — points at a Meditations session.
    assert "source_session" in post.metadata
    src_sess = str(post["source_session"])
    assert mig._session_target_matches_meditations(src_sess)
    # Other fields preserved (confidence_tier from fixture).
    assert post.get("confidence_tier") == "high"


def test_move_note_to_zettel_refuses_overwrite(tmp_path: Path, mig) -> None:
    """If a zettel with the same name already exists, raise rather than
    silently overwrite — partial-migration state needs explicit
    handling."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    note = vault / "note" / "Meditations Insight 00.md"
    (vault / "zettel" / "Meditations Insight 00.md").write_text(
        "---\ntype: zettel\nname: Meditations Insight 00\n---\n# Pre-existing\n",
        encoding="utf-8",
    )
    with pytest.raises(FileExistsError):
        mig.move_note_to_zettel(note, vault)


# --- Wikilink updates -----------------------------------------------------


def test_find_wikilink_update_targets_catches_cross_reference(
    tmp_path: Path, mig,
) -> None:
    """The Cross-Reference Note's body link ``[[note/Meditations Insight 00]]``
    is flagged for update."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=2)
    plan = mig.build_plan(vault)
    target_paths = {p for p, _ in plan.wikilink_update_targets}
    # The cross-reference note has a body link to a Meditations note.
    assert any("Cross-Reference Note.md" in str(p) for p in target_paths)


def test_apply_wikilink_updates_rewrites_note_link(
    tmp_path: Path, mig,
) -> None:
    """``[[note/<title>]]`` body link gets rewritten to
    ``[[zettel/<title>]]``."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    cross_path = vault / "note" / "Cross-Reference Note.md"
    migrated_titles = {"Meditations Insight 00"}

    applied = mig.apply_wikilink_updates(cross_path, migrated_titles)
    assert applied == 1

    text = cross_path.read_text(encoding="utf-8")
    assert "[[zettel/Meditations Insight 00]]" in text
    assert "[[note/Meditations Insight 00]]" not in text


def test_apply_wikilink_updates_preserves_pipe_alias(
    tmp_path: Path, mig,
) -> None:
    """Pipe-aliased wikilink form ``[[note/X|Display]]`` keeps the
    display text after rewrite."""
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    (vault / "zettel").mkdir(parents=True)
    target = vault / "note" / "ref.md"
    target.write_text(
        "---\ntype: note\nname: ref\n---\n# Body\n\n"
        "See [[note/Meditations Insight 00|Insight 0]] for context.\n",
        encoding="utf-8",
    )

    applied = mig.apply_wikilink_updates(target, {"Meditations Insight 00"})
    assert applied == 1

    text = target.read_text(encoding="utf-8")
    assert "[[zettel/Meditations Insight 00|Insight 0]]" in text


def test_apply_wikilink_updates_idempotent(
    tmp_path: Path, mig,
) -> None:
    """Re-running on an already-updated file → 0 replacements (no
    matches for the [[note/...]] pattern)."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=1)
    cross_path = vault / "note" / "Cross-Reference Note.md"
    migrated_titles = {"Meditations Insight 00"}

    # First run: 1 replacement.
    assert mig.apply_wikilink_updates(cross_path, migrated_titles) == 1
    # Second run: 0 replacements (idempotent).
    assert mig.apply_wikilink_updates(cross_path, migrated_titles) == 0


# --- End-to-end apply -----------------------------------------------------


def test_apply_plan_end_to_end(tmp_path: Path, mig) -> None:
    """Full migration cycle on a 22-note fixture: all moved, wikilinks
    rewritten, unrelated notes untouched."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=22)
    plan = mig.build_plan(vault)
    counters = mig.apply_plan(plan, vault)

    assert counters["notes_moved"] == 22
    assert counters["wikilink_files_updated"] >= 1
    assert counters["wikilink_replacements"] >= 1

    # Vault post-state:
    # - 22 zettels exist; 0 Meditations notes remain.
    zettel_count = len(list((vault / "zettel").glob("Meditations Insight *.md")))
    assert zettel_count == 22
    note_meditations_count = len(
        list((vault / "note").glob("Meditations Insight *.md"))
    )
    assert note_meditations_count == 0
    # - Unrelated notes still in note/.
    assert (vault / "note" / "Fencing Drill 00.md").exists()
    assert (vault / "note" / "Fencing Drill 01.md").exists()
    assert (vault / "note" / "Sourceless Note.md").exists()
    assert (vault / "note" / "Cross-Reference Note.md").exists()
    # Cross-reference body was rewritten.
    cross_text = (vault / "note" / "Cross-Reference Note.md").read_text(
        encoding="utf-8",
    )
    assert "[[zettel/Meditations Insight 00]]" in cross_text
    assert "[[note/Meditations Insight 00]]" not in cross_text


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
    # already_migrated_titles populated from zettel/ scan (the
    # source_session pattern still matches the moved records).
    assert len(plan2.already_migrated_titles) == 3

    counters2 = mig.apply_plan(plan2, vault)
    assert counters2["notes_moved"] == 0
    # Wikilink pass on second run is also a no-op (everything already
    # rewritten).
    assert counters2["wikilink_replacements"] == 0


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


def test_partial_state_duplicate_detected(tmp_path: Path, mig) -> None:
    """If a note STILL exists in note/ AND ALSO exists in zettel/
    (the move crashed after writing the zettel but before deleting
    the note), it's flagged as already-migrated (not re-moved)."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    title = "Meditations Insight 00"
    # Write a zettel-copy without deleting the note (the
    # crashed-mid-move state).
    src = vault / "note" / f"{title}.md"
    dest = vault / "zettel" / f"{title}.md"
    post = frontmatter.load(src)
    post["type"] = "zettel"
    dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    # NOTE: do NOT unlink src — this is the partial-state shape.

    plan = mig.build_plan(vault)
    # Insight 00 is flagged as already-migrated (since its zettel exists).
    assert title in plan.already_migrated_titles
    # The remaining 2 still need to move.
    notes_to_move_names = {p.stem for p in plan.notes_to_move}
    assert title not in notes_to_move_names
    assert len(notes_to_move_names) == 2


# --- CLI dry-run + apply --------------------------------------------------


def test_cli_dry_run_does_not_write(tmp_path: Path, mig, capsys) -> None:
    """Default invocation (no --apply) prints the plan but writes nothing."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    note_count_before = len(list((vault / "note").glob("*.md")))

    rc = mig.main(["--vault", str(vault)])
    assert rc == 0

    # No writes.
    assert len(list((vault / "note").glob("*.md"))) == note_count_before
    # No zettel files.
    assert list((vault / "zettel").glob("Meditations Insight *.md")) == []

    # Output mentions the plan.
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "Meditations Insight" in out


def test_cli_apply_executes_migration(tmp_path: Path, mig, capsys) -> None:
    """--apply mode actually writes the migration."""
    vault = _build_fixture_vault(tmp_path, num_meditations_notes=3)
    rc = mig.main(["--vault", str(vault), "--apply"])
    assert rc == 0

    # Migration applied: 3 zettels, 0 Meditations notes in note/.
    assert len(list((vault / "zettel").glob("Meditations Insight *.md"))) == 3
    assert len(list((vault / "note").glob("Meditations Insight *.md"))) == 0
    # Unrelated notes still in note/.
    assert (vault / "note" / "Sourceless Note.md").exists()
    assert (vault / "note" / "Fencing Drill 00.md").exists()

    out = capsys.readouterr().out
    assert "APPLYING" in out
    assert "Migration complete" in out


def test_cli_returns_error_on_missing_vault(tmp_path: Path, mig, capsys) -> None:
    """Pointing at a non-existent vault returns exit code 2."""
    rc = mig.main(["--vault", str(tmp_path / "does-not-exist")])
    assert rc == 2
