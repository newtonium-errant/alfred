#!/usr/bin/env python3
"""One-time cleanup: remove duplicate base-view embeds from learning records.

Scans assumption/, decision/, constraint/, contradiction/, synthesis/ for
files that contain the same ![[*.base#*]] embed line more than once.
Removes the second (and subsequent) occurrences, plus any orphaned ---
separator that was inserted with them.

Usage:
    python scripts/fix_duplicate_embeds.py              # dry-run (default)
    python scripts/fix_duplicate_embeds.py --apply      # actually write changes
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VAULT_PATH = Path(__file__).resolve().parent.parent / "vault"
LEARNING_DIRS = ["assumption", "decision", "constraint", "contradiction", "synthesis"]
BASE_EMBED_RE = re.compile(r"!\[\[.+\.base#.+\]\]")


def find_and_fix(file_path: Path, apply: bool) -> bool:
    """Remove duplicate base-view embeds from a single file.

    Returns True if duplicates were found (and fixed, if apply=True).
    """
    text = file_path.read_text(encoding="utf-8")

    # Split into frontmatter and body using the python-frontmatter convention:
    # file starts with ---, frontmatter, ---, then body.
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False  # no valid frontmatter

    front = parts[0] + "---" + parts[1] + "---"
    body = parts[2]

    # --- Step 1: remove duplicate embed lines ---
    lines = body.split("\n")
    seen_embeds: set[str] = set()
    new_lines: list[str] = []
    removed_count = 0

    for line in lines:
        stripped = line.strip()
        if BASE_EMBED_RE.search(stripped):
            if stripped in seen_embeds:
                removed_count += 1
                continue  # skip duplicate
            seen_embeds.add(stripped)
        new_lines.append(line)

    if removed_count == 0:
        return False

    # --- Step 2: remove orphaned --- separators ---
    # A --- is orphaned if only blank lines remain between it and end-of-file
    # (the duplicates that used to follow it were removed).
    cleaned: list[str] = []
    for i, line in enumerate(new_lines):
        if line.strip() == "---":
            after_blank = all(l.strip() == "" for l in new_lines[i + 1:])
            if after_blank:
                # Drop the --- and any trailing blank lines before it
                while cleaned and cleaned[-1].strip() == "":
                    cleaned.pop()
                continue
        cleaned.append(line)

    # --- Step 3: collapse runs of 3+ blank lines down to 1 blank line ---
    final: list[str] = []
    blank_run = 0
    for line in cleaned:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                final.append(line)
        else:
            blank_run = 0
            final.append(line)

    # --- Step 4: trim trailing blank lines ---
    while final and final[-1].strip() == "":
        final.pop()

    new_body = "\n".join(final)
    new_text = front + new_body

    # Ensure file ends with single newline
    new_text = new_text.rstrip("\n") + "\n"

    name = file_path.relative_to(VAULT_PATH)
    print(f"  {'FIXED' if apply else 'WOULD FIX'}: {name} ({removed_count} duplicate embed(s) removed)")

    if apply:
        file_path.write_text(new_text, encoding="utf-8")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove duplicate base-view embeds from learning records")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    args = parser.parse_args()

    if not args.apply:
        print("DRY RUN — pass --apply to write changes\n")

    total_scanned = 0
    total_fixed = 0

    for dir_name in LEARNING_DIRS:
        dir_path = VAULT_PATH / dir_name
        if not dir_path.is_dir():
            print(f"Skipping {dir_name}/ (not found)")
            continue

        files = sorted(dir_path.glob("*.md"))
        fixed_in_dir = 0
        for f in files:
            total_scanned += 1
            if find_and_fix(f, apply=args.apply):
                fixed_in_dir += 1
                total_fixed += 1

        if fixed_in_dir == 0:
            print(f"  {dir_name}/: {len(files)} files scanned, no duplicates")

    print(f"\nSummary: {total_fixed}/{total_scanned} files {'fixed' if args.apply else 'need fixing'}")


if __name__ == "__main__":
    main()
