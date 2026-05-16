"""One-time migration: Meditations orphan notes → zettel/ + author rename.

Phase 1 of the Hypatia Zettelkasten schema cutover (2026-05-16). Migrates
the artifacts of the morning's misconfigured capture-source-anchor ship
(commits ``357f732`` + ``54a069c`` + ``253b295``):

  * 22 orphan ``note/`` records auto-created with ``source: [[source/
    Meditations]]`` → moved to ``zettel/`` with ``type`` updated.
  * ``author/Aurelius.md`` (legacy last-name-only filename) → renamed
    to canonical ``author/Aurelius, Marcus.md`` with ``aliases:``
    populated for the Phase 1 resolver.
  * Wikilinks vault-wide updated: ``[[note/<Title>]]`` for the migrated
    22 → ``[[zettel/<Title>]]``; ``[[author/Aurelius]]`` →
    ``[[author/Aurelius, Marcus]]``.
  * ``source/Meditations.md``'s ``## Permanent Notes spawned`` section
    (if present) gets its wikilinks updated.

Idempotency: re-runs are safe. The "already migrated" detection skips
records that have already been moved (no source/Meditations link found
in note/, but zettel/<Same Title>.md exists).

Usage (recommended — module form):
    python -m alfred.scripts.migrate_2026_05_16_meditations_zettels [--vault PATH]
    python -m alfred.scripts.migrate_2026_05_16_meditations_zettels --apply [--vault PATH]

Backwards-compat shim (dash-form path from the original ship):
    python scripts/migrate_2026-05-16_meditations_zettels.py [--vault PATH]

If ``--vault`` is omitted, the script defaults to
``$ALFRED_VAULT_PATH`` then ``/home/andrew/library-alexandria`` (the
Hypatia vault). The Salem vault is untouched.

Output: a per-file action log (dry-run report) followed by a summary
of records moved + wikilinks updated. The apply mode emits the same
output but also writes the changes.

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


SOURCE_TITLE = "Meditations"
SOURCE_WIKILINK = f"[[source/{SOURCE_TITLE}]]"

LEGACY_AUTHOR_STEM = "Aurelius"
CANONICAL_AUTHOR_STEM = "Aurelius, Marcus"
CANONICAL_AUTHOR_NAME = "Marcus Aurelius"
LEGACY_AUTHOR_WIKILINK = f"[[author/{LEGACY_AUTHOR_STEM}]]"
CANONICAL_AUTHOR_WIKILINK = f"[[author/{CANONICAL_AUTHOR_STEM}]]"

# Frontmatter source field shapes we recognise as "anchored to Meditations".
# Three forms in the wild:
#   * "[[source/Meditations]]" (wikilink string)
#   * "source/Meditations"     (bare wikilink-target)
#   * "Meditations"            (free-text — legacy resolver behaviour)
_SOURCE_ANCHOR_FORMS: tuple[str, ...] = (
    f"[[source/{SOURCE_TITLE}]]",
    f"source/{SOURCE_TITLE}",
    SOURCE_TITLE,
)


# --- Data types -----------------------------------------------------------


@dataclass
class MigrationPlan:
    """End-to-end plan structure — populated by ``identify_*`` helpers,
    then consumed by ``apply_plan``.

    Each list is empty when nothing needs to change.
    """
    # Note files to move from note/ → zettel/.
    notes_to_move: list[Path] = field(default_factory=list)
    # Author rename: (legacy_path, canonical_path) — empty when legacy
    # author doesn't exist (e.g. already migrated, or never existed).
    author_rename: tuple[Path, Path] | None = None
    # Files (across the whole vault) needing wikilink updates.
    # Each entry: (path, replacements_count).
    wikilink_update_targets: list[tuple[Path, int]] = field(default_factory=list)
    # Already-migrated note titles (idempotency-skip evidence).
    already_migrated_titles: list[str] = field(default_factory=list)
    # Source/Meditations.md path (if present) — its body may need wikilink
    # updates in the ``## Permanent Notes spawned`` section.
    source_record: Path | None = None


# --- Plan discovery -------------------------------------------------------


def _scan_dir_for_meditations_anchor(vault: Path, subdir: str) -> list[Path]:
    """Scan ``vault/<subdir>/*.md`` for records anchored to Meditations.

    Anchoring is detected by frontmatter ``source:`` matching one of
    :data:`_SOURCE_ANCHOR_FORMS` (wikilink, bare-target, or free-text).

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
        source_value = post.get("source")
        if not source_value:
            continue
        source_str = str(source_value).strip()
        if source_str in _SOURCE_ANCHOR_FORMS:
            matches.append(path)
    return matches


def identify_orphan_meditations_notes(vault: Path) -> list[Path]:
    """Return the list of ``note/*.md`` records anchored to Meditations.

    Sorted by filename for deterministic output.
    """
    return _scan_dir_for_meditations_anchor(vault, "note")


def identify_migrated_meditations_zettels(vault: Path) -> list[Path]:
    """Return the list of ``zettel/*.md`` records anchored to Meditations.

    Bug-fix companion to :func:`identify_orphan_meditations_notes`:
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


def identify_author_rename(vault: Path) -> tuple[Path, Path] | None:
    """Return (legacy, canonical) rename pair, or None if already done.

    Detects three states:
      * Legacy ``author/Aurelius.md`` exists, canonical doesn't → return
        the rename pair.
      * Canonical exists (legacy may or may not still exist) → return
        None (already migrated; idempotent skip).
      * Neither exists → return None (nothing to migrate).
    """
    legacy = vault / "author" / f"{LEGACY_AUTHOR_STEM}.md"
    canonical = vault / "author" / f"{CANONICAL_AUTHOR_STEM}.md"
    if canonical.exists():
        return None  # already migrated
    if not legacy.exists():
        return None  # nothing to rename
    return (legacy, canonical)


# --- Wikilink updates -----------------------------------------------------


def _make_wikilink_patterns(
    migrated_titles: set[str],
) -> list[tuple[re.Pattern[str], str]]:
    """Build the regex patterns + replacements for vault-wide updates.

    Two flavours:

      1. ``[[author/Aurelius]]`` → ``[[author/Aurelius, Marcus]]``
         (and the bare ``[[author/Aurelius|Display]]`` form with
         optional pipe-aliased display text).
      2. ``[[note/<Title>]]`` → ``[[zettel/<Title>]]`` for each
         migrated title.

    Both patterns are conservative: they match the wikilink form with
    word-boundary safety so ``[[author/AureliusJr]]`` (hypothetical
    other author) doesn't accidentally rewrite.
    """
    patterns: list[tuple[re.Pattern[str], str]] = []

    # 1. Author rename — wikilink with optional pipe-alias.
    patterns.append((
        re.compile(
            re.escape(f"[[author/{LEGACY_AUTHOR_STEM}")
            + r"(?P<rest>(?:\|[^\]]*)?\]\])"
        ),
        f"[[author/{CANONICAL_AUTHOR_STEM}\\g<rest>",
    ))

    # 2. Note → zettel rename, per migrated title.
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
    exclude_paths: tuple[Path | None, ...] = (),
) -> list[tuple[Path, int]]:
    """Scan all ``*.md`` files vault-wide and return paths that need
    wikilink updates, plus the count of replacements per file.

    ``exclude_dirs`` skips Obsidian system + trash folders.

    ``exclude_paths`` skips specific files entirely. Used by the
    orchestrator to exclude ``source/Meditations.md`` from the vault-
    wide pass — that file is owned by :func:`update_source_permanent_notes`
    so the counter accounting can separate body-section replacements
    (counted under ``source_section_updates``) from generic vault-wide
    rewrites (counted under ``wikilink_replacements``). Without the
    exclusion, the source record's body wikilinks would get rewritten
    twice: once at step 3 (vault-wide pass, no-op the second time but
    counted in ``wikilink_replacements``) and again at step 4
    (``update_source_permanent_notes``, finds zero remaining matches,
    counted as 0 in ``source_section_updates``).
    """
    if not migrated_titles and not (vault / "author" / f"{LEGACY_AUTHOR_STEM}.md").exists():
        # Nothing to scan for — caller should bail early.
        return []

    patterns = _make_wikilink_patterns(migrated_titles)
    if not patterns:
        return []

    # Normalise excluded paths to a set of resolved absolute paths so
    # the membership check works regardless of how the caller passed
    # the path in. ``None`` entries are filtered (callers like
    # ``build_plan`` may pass ``None`` when no source record exists).
    exclude_resolved: set[Path] = {
        p.resolve() for p in exclude_paths if p is not None
    }

    targets: list[tuple[Path, int]] = []
    for path in sorted(vault.rglob("*.md")):
        # Skip excluded directories.
        if any(part in exclude_dirs for part in path.parts):
            continue
        # Skip excluded paths.
        if path.resolve() in exclude_resolved:
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
    """Rewrite ``path`` in-place with author + note→zettel wikilink
    updates. Returns the number of replacements applied.

    Idempotent: re-running on an already-updated file is a no-op
    (the patterns only match LEGACY forms; updated forms don't match).
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
    """
    zettel_dir = vault / "zettel"
    zettel_dir.mkdir(parents=True, exist_ok=True)

    dest = zettel_dir / note_path.name
    if dest.exists():
        raise FileExistsError(
            f"Destination {dest} already exists — would overwrite. "
            f"Resolve manually then re-run."
        )

    # Load + mutate frontmatter, then write.
    post = frontmatter.load(note_path)
    post["type"] = "zettel"
    # Drop any ``confidence_tier`` etc. that doesn't apply to zettels —
    # actually no, we preserve all fields; the operator can clean these
    # later. Migration is conservative.

    dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    note_path.unlink()
    return dest


# --- Author rename --------------------------------------------------------


def rename_author_record(
    legacy_path: Path, canonical_path: Path,
) -> Path:
    """Rename the legacy author record + update its frontmatter.

    Sets:
      * ``name``: ``CANONICAL_AUTHOR_NAME`` (``Marcus Aurelius``)
      * ``aliases``: list containing both forms (input + canonical)
      * Drops ``last_name`` field if present (Phase 1 strip).
      * Drops ``status`` field if it has the legacy ``active`` default
        (Phase 1 author template no longer sets it).

    Returns the new canonical path.
    """
    post = frontmatter.load(legacy_path)
    post["name"] = CANONICAL_AUTHOR_NAME
    # Aliases: ensure both forms are present.
    existing_aliases = post.get("aliases")
    if isinstance(existing_aliases, list):
        aliases = [str(a) for a in existing_aliases if a]
    else:
        aliases = []
    for canonical_alias in (CANONICAL_AUTHOR_NAME, CANONICAL_AUTHOR_STEM):
        if canonical_alias not in aliases:
            aliases.append(canonical_alias)
    post["aliases"] = aliases
    # Drop fields stripped by the Phase 1 author template.
    for dropped_field in ("last_name", "era", "school", "description"):
        if dropped_field in post.metadata:
            del post.metadata[dropped_field]
    # Drop status only if it carries the legacy default (active) —
    # operator-set status values stay.
    if post.metadata.get("status") == "active":
        del post.metadata["status"]

    # Write to the canonical path; remove the legacy.
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )
    if legacy_path.exists() and legacy_path != canonical_path:
        legacy_path.unlink()
    return canonical_path


# --- Source record body update --------------------------------------------


# Match ``- [[note/<Title>]]`` lines in the ``## Permanent Notes spawned``
# section. Conservative — only rewrites note/ wikilinks; doesn't touch
# author or other links in the same section.
_PERM_NOTES_SECTION = re.compile(
    r"##\s+Permanent\s+Notes\s+spawned\s*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)


def update_source_permanent_notes(
    source_path: Path, migrated_titles: set[str],
) -> int:
    """Update ``[[note/X]]`` → ``[[zettel/X]]`` in the ``## Permanent
    Notes spawned`` section of source_path. Returns the count of body-
    section replacements.

    Also opportunistically rewrites the source record's frontmatter
    ``author: [[author/Aurelius]]`` → ``[[author/Aurelius, Marcus]]``
    if present. That rewrite is NOT counted in the return value — the
    return is specifically the body-section counter that the
    orchestrator surfaces as ``source_section_updates``. The author
    frontmatter update is bundled here (rather than in the vault-wide
    pass) because we deliberately exclude the source record from the
    vault-wide pass so the body-section counter stays accurate.

    Idempotent: re-running on an already-updated source is a no-op.
    """
    if not source_path.exists():
        return 0
    try:
        text = source_path.read_text(encoding="utf-8")
    except Exception:
        return 0

    original = text
    body_section_total = 0

    # 1. Body section — count ``[[note/<migrated-title>]]`` → ``[[zettel/...]]``
    # replacements. This is the counter the test asserts on.
    match = _PERM_NOTES_SECTION.search(text)
    if match is not None:
        section_body = match.group(1)
        new_section_body = section_body
        for title in migrated_titles:
            old = f"[[note/{title}]]"
            new = f"[[zettel/{title}]]"
            count_for_title = section_body.count(old)
            if count_for_title > 0:
                new_section_body = new_section_body.replace(old, new)
                body_section_total += count_for_title
        if body_section_total > 0:
            text = text[: match.start(1)] + new_section_body + text[match.end(1):]

    # 2. Frontmatter author wikilink — rewrite legacy
    # ``[[author/Aurelius]]`` to ``[[author/Aurelius, Marcus]]``. This
    # is the source record's own frontmatter author field, distinct
    # from the body section above. Bundled here because the source is
    # excluded from the vault-wide pass.
    #
    # NOT counted in the return value — the orchestrator's
    # ``source_section_updates`` counter is specifically the body
    # section, per the contract pinned by the end-to-end test.
    legacy_link = f"[[author/{LEGACY_AUTHOR_STEM}]]"
    canonical_link = f"[[author/{CANONICAL_AUTHOR_STEM}]]"
    if legacy_link in text:
        text = text.replace(legacy_link, canonical_link)

    if text != original:
        source_path.write_text(text, encoding="utf-8")

    return body_section_total


# --- Plan + apply orchestrator --------------------------------------------


def build_plan(vault: Path) -> MigrationPlan:
    """Discover everything that needs migrating. No vault writes.

    Two sources of ``already_migrated_titles`` evidence:

      1. ``note/<title>.md`` AND ``zettel/<title>.md`` both exist —
         partial-migration state where the move-step crashed after
         the zettel was written but before the note was deleted. The
         scan picks the title up via :func:`identify_orphan_meditations_notes`
         and flags it via :func:`already_migrated`.
      2. Only ``zettel/<title>.md`` exists (the common steady-state
         after a successful prior migration). The scan picks the title
         up via :func:`identify_migrated_meditations_zettels` — this
         catches re-run-after-success cases that the note/ scan
         can't see (the note is gone).

    Both shapes deduplicate into ``plan.already_migrated_titles``.
    The vault-wide wikilink-update pass uses the union of all known
    Meditations titles so any lingering ``[[note/<title>]]`` references
    get rewritten regardless of which path detected them.
    """
    plan = MigrationPlan()

    # 1. Identify orphan Meditations notes still in note/. Partition into
    # "needs move" vs. "already-migrated duplicate" (case 1 above).
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

    # 3. Identify author rename.
    plan.author_rename = identify_author_rename(vault)

    # 4. Source record path (if present).
    source_rel = vault / "source" / f"{SOURCE_TITLE}.md"
    if source_rel.exists():
        plan.source_record = source_rel

    # 5. Wikilink targets — based on the notes that WILL move + every
    # known migrated title (so lingering [[note/<title>]] references
    # elsewhere in the vault get rewritten on re-run).
    migrated_titles: set[str] = set()
    for note_path in plan.notes_to_move:
        migrated_titles.add(note_path.stem)
    for title in plan.already_migrated_titles:
        migrated_titles.add(title)

    plan.wikilink_update_targets = find_wikilink_update_targets(
        vault, migrated_titles,
        exclude_paths=(plan.source_record,) if plan.source_record else (),
    )

    return plan


def print_plan(plan: MigrationPlan, vault: Path) -> None:
    """Emit the human-readable migration report."""
    print(f"Hypatia Zettelkasten Phase 1 migration plan")
    print(f"  Vault: {vault}")
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

    print(f"--- Author record rename ---")
    if plan.author_rename is None:
        print("  (none — Aurelius.md not found or canonical already present)")
    else:
        legacy, canonical = plan.author_rename
        print(f"  {legacy.name}  →  {canonical.name}")
    print()

    print(f"--- Wikilink updates (vault-wide) ---")
    if not plan.wikilink_update_targets:
        print("  (none — no [[note/<title>]] or [[author/Aurelius]] references found)")
    else:
        total_replacements = sum(c for _, c in plan.wikilink_update_targets)
        for path, count in plan.wikilink_update_targets:
            rel = path.relative_to(vault)
            print(f"  {rel}  ({count} replacement{'s' if count != 1 else ''})")
        print(f"  TOTAL: {total_replacements} wikilink replacement(s) "
              f"across {len(plan.wikilink_update_targets)} file(s)")
    print()

    print(f"--- Source record (source/Meditations.md) ---")
    if plan.source_record is None:
        print("  (not present — no Permanent Notes spawned section to update)")
    else:
        print(f"  {plan.source_record.relative_to(vault)}")
        print(f"  (any ``## Permanent Notes spawned`` body wikilinks will be")
        print(f"   rewritten; the source's frontmatter author wikilink will")
        print(f"   also be updated to the canonical form)")
    print()


def apply_plan(plan: MigrationPlan, vault: Path) -> dict[str, int]:
    """Execute the plan against the live vault. Returns counters dict."""
    counters = {
        "notes_moved": 0,
        "author_renamed": 0,
        "wikilink_files_updated": 0,
        "wikilink_replacements": 0,
        "source_section_updates": 0,
    }

    # 1. Author rename first — so subsequent wikilink-update pass sees
    # the new canonical name without race vs. moves.
    if plan.author_rename is not None:
        legacy, canonical = plan.author_rename
        print(f"  renaming author: {legacy.name} → {canonical.name}")
        rename_author_record(legacy, canonical)
        counters["author_renamed"] = 1

    # 2. Move notes → zettels.
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

    # 3. Update wikilinks vault-wide — EXCLUDING the source record.
    # Re-scan to pick up any links inside files that may have been
    # mutated by step 1/2 (rare — author/Aurelius.md was moved, but
    # its OWN body might link back to a moved note). The source record
    # is owned by step 4 (``update_source_permanent_notes``) so the
    # ``source_section_updates`` counter accounting stays accurate;
    # without the exclusion, step 3 would pre-emptively rewrite the
    # body section and step 4 would find zero replacements left.
    refreshed_targets = find_wikilink_update_targets(
        vault, migrated_titles,
        exclude_paths=(plan.source_record,) if plan.source_record else (),
    )
    for path, _expected_count in refreshed_targets:
        applied = apply_wikilink_updates(path, migrated_titles)
        if applied > 0:
            counters["wikilink_files_updated"] += 1
            counters["wikilink_replacements"] += applied
            print(f"  wikilinks updated in {path.relative_to(vault)} "
                  f"({applied} replacement{'s' if applied != 1 else ''})")

    # 4. Source record — body's Permanent Notes spawned section + its
    # own frontmatter author wikilink. ``update_source_permanent_notes``
    # handles BOTH surfaces (the source is excluded from the vault-wide
    # pass above) and returns ONLY the body-section counter — that's
    # what ``source_section_updates`` reports.
    if plan.source_record is not None and migrated_titles:
        applied = update_source_permanent_notes(plan.source_record, migrated_titles)
        if applied > 0:
            counters["source_section_updates"] = applied
            print(f"  source/Meditations.md: ## Permanent Notes spawned "
                  f"updated ({applied} replacement{'s' if applied != 1 else ''})")

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
            "Migrate the 22 orphan Meditations notes from note/ to zettel/ "
            "and rename author/Aurelius.md to canonical Lastname-comma-"
            "Firstname form. Default mode is DRY-RUN; pass --apply to "
            "execute. Idempotent — safe to re-run."
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
    print(f"  author renamed:           {counters['author_renamed']}")
    print(f"  wikilink files updated:   {counters['wikilink_files_updated']}")
    print(f"  wikilink replacements:    {counters['wikilink_replacements']}")
    print(f"  source-section updates:   {counters['source_section_updates']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
