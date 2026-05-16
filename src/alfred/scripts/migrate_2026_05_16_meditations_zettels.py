"""One-time migration: Meditations-derived capture notes → zettel/.

Phase 1 of the Hypatia Zettelkasten schema cutover (2026-05-16).
Migrates the subset of capture-derived ``note/`` records that came from
the three Marcus Aurelius / Meditations capture sessions, moving them
to ``zettel/`` with ``type`` updated.

REWORK NOTE (2026-05-16, post-dry-run): the original migration design
assumed the morning's capture-source-anchor ship (commits ``357f732``
+ ``54a069c`` + ``253b295``) had ALREADY stamped ``source: [[source/
Meditations]]`` onto the 22 orphan notes. Dry-run against the live
``/home/andrew/library-alexandria`` revealed otherwise:

  * The 30 capture-derived notes PREDATE the source-anchor ship
    (created 2026-05-15) and carry NO ``source:`` frontmatter.
  * They DO carry ``source_session:`` pointing at the originating
    capture session record.
  * No ``author/Aurelius.md`` exists in the live vault.
  * No ``source/Meditations.md`` exists either.

Andrew's decision (Option C): migrate ONLY the Meditations-related
captures (the 3 Marcus Aurelius / Meditations sessions' worth of
derived notes — not all 30 capture-derived notes; not zero). The
detection criterion shifts from ``source: [[source/Meditations]]``
to ``source_session:`` pointing at a session filename matching the
Meditations pattern (case-insensitive substring match on
``marcus-aurelius`` OR ``meditations``).

Author rename + source-record body update logic is DROPPED entirely
because the live vault has neither artifact.

What the migration does:

  1. Identify ``note/*.md`` records whose ``source_session:``
     frontmatter points to a session filename matching the Meditations
     pattern.
  2. Move each from ``vault/note/<Title>.md`` → ``vault/zettel/<Title>.md``,
     updating ``type: note`` → ``type: zettel``. All other frontmatter
     preserved (including ``source_session:`` — the session pointer
     stays valid post-move).
  3. Vault-wide wikilink rewrite: ``[[note/<Title>]]`` → ``[[zettel/<Title>]]``
     for any of the migrated titles.
  4. Idempotency: scan ``zettel/`` for already-migrated records (same
     pattern detection); skip them. Re-runs are safe no-ops.

Usage (recommended — module form):
    python -m alfred.scripts.migrate_2026_05_16_meditations_zettels [--vault PATH]
    python -m alfred.scripts.migrate_2026_05_16_meditations_zettels --apply [--vault PATH]

Backwards-compat shim (dash-form path from the original ship):
    python scripts/migrate_2026-05-16_meditations_zettels.py [--vault PATH]

If ``--vault`` is omitted, the script defaults to
``$ALFRED_VAULT_PATH`` then ``/home/andrew/library-alexandria`` (the
Hypatia vault). The Salem vault is untouched.

Module-location note: this script lives inside the ``alfred`` package
(rather than the top-level ``scripts/`` tree) so the dataclass below
(``MigrationPlan``) resolves ``cls.__module__`` against ``sys.modules``
cleanly. An earlier ship placed it in ``scripts/`` and the test harness
loaded it via ``importlib.util.spec_from_file_location`` — which
populated ``cls.__module__`` with a name that wasn't in ``sys.modules``,
crashing the dataclass field-resolution on import with
``AttributeError: 'NoneType' object has no attribute '__dict__'``.
The package-located form sidesteps this entirely.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter


# --- Constants ------------------------------------------------------------


#: Case-insensitive substring match against capture session filenames.
#: A note's ``source_session:`` frontmatter (wikilink form
#: ``"[[session/<filename>]]"`` or bare-target ``"session/<filename>"``)
#: is parsed for the session filename; if that filename (lowercased)
#: contains EITHER ``"marcus-aurelius"`` OR ``"meditations"``, the note
#: is in-scope for migration.
#:
#: The three target sessions in the live Hypatia vault (2026-05-16):
#:   * capture-2026-05-16-marcus-aurelius-meditations-notes-5e7a917c
#:   * capture-2026-05-16-marcus-aurelius-reading-notes-11943ac7
#:   * capture-2026-05-16-meditations-introduction-notes-heraclitus-22104fef
#:
#: The pattern intentionally over-matches a tiny bit (e.g. a future
#: capture named ``capture-2026-06-01-meditations-on-fencing-...`` would
#: also be in-scope) — that's acceptable for a one-time migration. The
#: operator reviews the dry-run report and confirms the exact set
#: before --apply.
_MEDITATIONS_SESSION_SUBSTRINGS: tuple[str, ...] = (
    "marcus-aurelius",
    "meditations",
)


# --- Data types -----------------------------------------------------------


@dataclass
class MigrationPlan:
    """End-to-end plan structure — populated by ``build_plan``, then
    consumed by ``apply_plan``.

    Each list is empty when nothing needs to change.
    """
    # Note files to move from note/ → zettel/.
    notes_to_move: list[Path] = field(default_factory=list)
    # Already-migrated note titles (idempotency-skip evidence).
    # Populated from a scan of zettel/ for Meditations-anchored records.
    already_migrated_titles: list[str] = field(default_factory=list)
    # Files (across the whole vault) needing wikilink updates.
    # Each entry: (path, replacements_count).
    wikilink_update_targets: list[tuple[Path, int]] = field(default_factory=list)


# --- Plan discovery -------------------------------------------------------


def _session_target_matches_meditations(target: str) -> bool:
    """True when ``target`` (a session filename or wikilink) matches the
    Meditations capture-session pattern.

    Accepts three input shapes:
      * ``"[[session/<filename>]]"`` (wikilink) — parses out the filename
      * ``"session/<filename>"`` (bare wikilink-target) — same
      * ``"<filename>"`` (bare filename, with or without .md) — same

    Match: case-insensitive substring on the filename stem. Returns
    True if ANY substring in :data:`_MEDITATIONS_SESSION_SUBSTRINGS`
    is present.
    """
    if not target:
        return False
    text = target.strip()
    # Strip wikilink brackets if present.
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    # Strip pipe-alias if present (``[[session/X|display]]``).
    if "|" in text:
        text = text.split("|", 1)[0]
    # Strip leading session/ if present.
    if text.startswith("session/"):
        text = text[len("session/"):]
    # Strip trailing .md if present.
    if text.endswith(".md"):
        text = text[:-3]
    lowered = text.lower()
    return any(sub in lowered for sub in _MEDITATIONS_SESSION_SUBSTRINGS)


def _scan_dir_for_meditations_anchor(vault: Path, subdir: str) -> list[Path]:
    """Scan ``vault/<subdir>/*.md`` for records anchored to a Meditations
    capture session via ``source_session:`` frontmatter.

    Sorted by filename for deterministic output. Returns ``[]`` when
    the subdirectory doesn't exist (handles the case where ``zettel/``
    hasn't been created yet on a fresh vault).
    """
    target_dir = vault / subdir
    if not target_dir.exists():
        return []

    matches: list[Path] = []
    for path in sorted(target_dir.glob("*.md")):
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        session_value = post.get("source_session")
        if not session_value:
            continue
        session_str = str(session_value).strip()
        if _session_target_matches_meditations(session_str):
            matches.append(path)
    return matches


def identify_orphan_meditations_notes(vault: Path) -> list[Path]:
    """Return the list of ``note/*.md`` records anchored (via
    ``source_session:``) to a Meditations capture session.

    Sorted by filename for deterministic output.
    """
    return _scan_dir_for_meditations_anchor(vault, "note")


def identify_migrated_meditations_zettels(vault: Path) -> list[Path]:
    """Return the list of ``zettel/*.md`` records anchored (via
    ``source_session:``) to a Meditations capture session.

    Idempotency companion to :func:`identify_orphan_meditations_notes`:
    after the migration runs, records live in ``zettel/`` (not
    ``note/``). The idempotency-skip-detection path needs to ALSO scan
    ``zettel/`` to recognise prior migrations on re-run, otherwise
    ``already_migrated_titles`` is always empty post-migration and a
    second invocation can't report the skipped state.

    Sorted by filename for deterministic output.
    """
    return _scan_dir_for_meditations_anchor(vault, "zettel")


def already_migrated(vault: Path, note_title: str) -> bool:
    """Idempotency check: True if a zettel/<title>.md already exists
    with the same name (regardless of whether the source note/ file
    has been deleted yet)."""
    return (vault / "zettel" / f"{note_title}.md").exists()


# --- Wikilink updates -----------------------------------------------------


def _make_wikilink_patterns(
    migrated_titles: set[str],
) -> list[tuple[re.Pattern[str], str]]:
    """Build the regex patterns + replacements for vault-wide updates.

    One flavour: ``[[note/<Title>]]`` → ``[[zettel/<Title>]]`` for each
    migrated title, with optional pipe-alias preservation
    (``[[note/<Title>|Display]]`` form).

    The author-rename pattern from earlier ship iterations is DROPPED —
    the live vault has no author records to rename for this migration.
    """
    patterns: list[tuple[re.Pattern[str], str]] = []
    for title in sorted(migrated_titles):
        patterns.append((
            re.compile(
                re.escape(f"[[note/{title}")
                + r"(?P<rest>(?:\|[^\]]*)?\]\])"
            ),
            f"[[zettel/{title}\\g<rest>",
        ))
    return patterns


def find_wikilink_update_targets(
    vault: Path,
    migrated_titles: set[str],
    *,
    exclude_dirs: tuple[str, ...] = (".obsidian", ".trash"),
) -> list[tuple[Path, int]]:
    """Scan all ``*.md`` files vault-wide and return paths that need
    wikilink updates, plus the count of replacements per file.

    ``exclude_dirs`` skips Obsidian system + trash folders.
    """
    if not migrated_titles:
        # Nothing to scan for — caller should bail early.
        return []

    patterns = _make_wikilink_patterns(migrated_titles)
    if not patterns:
        return []

    targets: list[tuple[Path, int]] = []
    for path in sorted(vault.rglob("*.md")):
        # Skip excluded directories.
        if any(part in exclude_dirs for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        total_replacements = 0
        for pattern, _replacement in patterns:
            total_replacements += len(pattern.findall(text))
        if total_replacements > 0:
            targets.append((path, total_replacements))
    return targets


def apply_wikilink_updates(
    path: Path,
    migrated_titles: set[str],
) -> int:
    """Rewrite ``path`` in-place with note→zettel wikilink updates.
    Returns the number of replacements applied.

    Idempotent: re-running on an already-updated file is a no-op (the
    pattern only matches LEGACY ``[[note/...]]`` forms; updated
    ``[[zettel/...]]`` forms don't match).
    """
    patterns = _make_wikilink_patterns(migrated_titles)
    if not patterns:
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return 0
    original = text
    total = 0
    for pattern, replacement in patterns:
        new_text, count = pattern.subn(replacement, text)
        text = new_text
        total += count
    if total > 0 and text != original:
        path.write_text(text, encoding="utf-8")
    return total


# --- Note → zettel move ---------------------------------------------------


def move_note_to_zettel(note_path: Path, vault: Path) -> Path:
    """Move ``note_path`` to ``vault/zettel/<filename>`` and update
    ``type: note`` → ``type: zettel`` in frontmatter.

    Returns the new zettel path. The note file is deleted; the zettel
    file is written. Frontmatter parse failure on the source raises.

    The destination directory is created if missing.

    All other frontmatter is preserved — including ``source_session:``
    which keeps pointing at the originating capture session record.
    """
    zettel_dir = vault / "zettel"
    zettel_dir.mkdir(parents=True, exist_ok=True)

    dest = zettel_dir / note_path.name
    if dest.exists():
        raise FileExistsError(
            f"Destination {dest} already exists — would overwrite. "
            f"Resolve manually then re-run."
        )

    post = frontmatter.load(note_path)
    post["type"] = "zettel"

    dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    note_path.unlink()
    return dest


# --- Plan + apply orchestrator --------------------------------------------


def build_plan(vault: Path) -> MigrationPlan:
    """Discover everything that needs migrating. No vault writes.

    Two sources of ``already_migrated_titles`` evidence:

      1. ``note/<title>.md`` AND ``zettel/<title>.md`` both exist —
         partial-migration state where the move-step crashed after
         the zettel was written but before the note was deleted.
      2. Only ``zettel/<title>.md`` exists (the common steady-state
         after a successful prior migration). The scan picks the title
         up via :func:`identify_migrated_meditations_zettels` — this
         catches re-run-after-success cases that the note/ scan can't
         see (the note is gone).

    Both shapes deduplicate into ``plan.already_migrated_titles``.
    The vault-wide wikilink-update pass uses the union of all known
    Meditations titles so any lingering ``[[note/<title>]]`` references
    get rewritten regardless of which path detected them.
    """
    plan = MigrationPlan()

    # 1. Identify orphan Meditations-anchored notes still in note/.
    # Partition into "needs move" vs. "already-migrated duplicate"
    # (case 1 above — both copies present).
    all_candidates = identify_orphan_meditations_notes(vault)
    for candidate in all_candidates:
        title = candidate.stem
        if already_migrated(vault, title):
            plan.already_migrated_titles.append(title)
        else:
            plan.notes_to_move.append(candidate)

    # 2. Identify already-migrated zettels (case 2 above — the steady-
    # state after a successful prior run). Dedupe with the note/-derived
    # entries from step 1.
    already_known: set[str] = set(plan.already_migrated_titles)
    for zettel_path in identify_migrated_meditations_zettels(vault):
        title = zettel_path.stem
        if title not in already_known:
            plan.already_migrated_titles.append(title)
            already_known.add(title)

    # 3. Wikilink targets — based on the notes that WILL move + every
    # known migrated title (so lingering [[note/<title>]] references
    # elsewhere in the vault get rewritten on re-run).
    migrated_titles: set[str] = set()
    for note_path in plan.notes_to_move:
        migrated_titles.add(note_path.stem)
    for title in plan.already_migrated_titles:
        migrated_titles.add(title)

    plan.wikilink_update_targets = find_wikilink_update_targets(
        vault, migrated_titles,
    )

    return plan


def print_plan(plan: MigrationPlan, vault: Path) -> None:
    """Emit the human-readable migration report."""
    print(f"Hypatia Zettelkasten Phase 1 migration plan")
    print(f"  Vault: {vault}")
    print(f"  Detection: note/*.md with source_session: pointing at a")
    print(f"             Meditations capture session "
          f"({' / '.join(_MEDITATIONS_SESSION_SUBSTRINGS)})")
    print()

    print(f"--- Notes to move (note/ → zettel/) ---")
    if not plan.notes_to_move:
        print("  (none — all candidates already migrated or none found)")
    else:
        for note in plan.notes_to_move:
            print(f"  {note.name}")
        print(f"  TOTAL: {len(plan.notes_to_move)} record(s)")
    print()

    print(f"--- Already-migrated notes (idempotency skip) ---")
    if not plan.already_migrated_titles:
        print("  (none)")
    else:
        for title in plan.already_migrated_titles:
            print(f"  {title}")
        print(f"  TOTAL: {len(plan.already_migrated_titles)} record(s)")
    print()

    print(f"--- Wikilink updates (vault-wide) ---")
    if not plan.wikilink_update_targets:
        print("  (none — no [[note/<title>]] references found)")
    else:
        total_replacements = sum(c for _, c in plan.wikilink_update_targets)
        for path, count in plan.wikilink_update_targets:
            rel = path.relative_to(vault)
            print(f"  {rel}  ({count} replacement{'s' if count != 1 else ''})")
        print(f"  TOTAL: {total_replacements} wikilink replacement(s) "
              f"across {len(plan.wikilink_update_targets)} file(s)")
    print()


def apply_plan(plan: MigrationPlan, vault: Path) -> dict[str, int]:
    """Execute the plan against the live vault. Returns counters dict.

    Three counter keys:
      * ``notes_moved`` — count of note/ → zettel/ moves applied.
      * ``wikilink_files_updated`` — count of *.md files where at least
        one ``[[note/<title>]]`` → ``[[zettel/<title>]]`` replacement
        was applied.
      * ``wikilink_replacements`` — total replacement count summed
        across all touched files.
    """
    counters = {
        "notes_moved": 0,
        "wikilink_files_updated": 0,
        "wikilink_replacements": 0,
    }

    # 1. Move notes → zettels.
    migrated_titles: set[str] = set()
    for note_path in plan.notes_to_move:
        title = note_path.stem
        print(f"  moving note → zettel: {title}")
        move_note_to_zettel(note_path, vault)
        migrated_titles.add(title)
        counters["notes_moved"] += 1

    # Include already-migrated titles in the wikilink-update set
    # (operators may have rewritten files independently; the link
    # rewriter is idempotent so this is safe).
    for title in plan.already_migrated_titles:
        migrated_titles.add(title)

    # 2. Update wikilinks vault-wide. Re-scan to pick up any links
    # inside files that may have been mutated by step 1 (the moved
    # zettels themselves don't carry [[note/<self>]] but they may
    # link to OTHER migrated notes — re-scan catches this).
    refreshed_targets = find_wikilink_update_targets(vault, migrated_titles)
    for path, _expected_count in refreshed_targets:
        applied = apply_wikilink_updates(path, migrated_titles)
        if applied > 0:
            counters["wikilink_files_updated"] += 1
            counters["wikilink_replacements"] += applied
            print(f"  wikilinks updated in {path.relative_to(vault)} "
                  f"({applied} replacement{'s' if applied != 1 else ''})")

    return counters


# --- CLI -------------------------------------------------------------------


def _default_vault_path() -> Path:
    """Resolve the default Hypatia vault path.

    Order: ``$ALFRED_VAULT_PATH`` env var > ``/home/andrew/library-alexandria``
    fallback. The fallback is hardcoded to Andrew's known Hypatia path
    (this script ships ONCE for a specific historical migration; not
    a generic tool).
    """
    env_path = os.environ.get("ALFRED_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path("/home/andrew/library-alexandria")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate the Meditations-derived capture notes from note/ "
            "to zettel/ (Marcus Aurelius / Meditations capture sessions). "
            "Default mode is DRY-RUN; pass --apply to execute. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=_default_vault_path(),
        help=(
            "Path to the Hypatia vault root. Defaults to $ALFRED_VAULT_PATH "
            "or /home/andrew/library-alexandria."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Execute the migration. Without this flag, only the plan is "
            "printed (dry-run)."
        ),
    )
    args = parser.parse_args(argv)

    vault: Path = args.vault.expanduser().resolve()
    if not vault.is_dir():
        print(f"error: vault path is not a directory: {vault}", file=sys.stderr)
        return 2

    plan = build_plan(vault)
    print_plan(plan, vault)

    if not args.apply:
        print("--- DRY-RUN — no changes written. Re-run with --apply to execute. ---")
        return 0

    print("--- APPLYING ---")
    counters = apply_plan(plan, vault)
    print()
    print("Migration complete:")
    print(f"  notes moved:              {counters['notes_moved']}")
    print(f"  wikilink files updated:   {counters['wikilink_files_updated']}")
    print(f"  wikilink replacements:    {counters['wikilink_replacements']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
