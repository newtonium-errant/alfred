---
type: session
name: Inbox processed exclusion policy
date: 2026-04-20
tags:
  - janitor
  - distiller
  - surveyor
  - config
  - hygiene
---

# Inbox processed exclusion policy

## Goal

Codify "`inbox/processed/` is out-of-bounds for every processing tool"
as a default so fresh installs don't accidentally scan the curator's
raw-input audit trail. Unifies with surveyor's existing behavior
(surveyor has excluded all of `inbox/` since its first release) and
eliminates two classes of noise:

- **Janitor** would flag FM001 (missing name), LINK001 (broken
  wikilinks), and similar structural issues against raw email bodies
  that aren't meant to be canonical records.
- **Distiller** would double-extract — once from the raw email in
  `inbox/processed/`, once from the derived note/task the curator
  produced.

## Change

### New shared helper: `alfred.vault.ops.is_ignored_path`

Supports two entry shapes in `ignore_dirs`:

- **Single component** (no `/`): matches any path component (legacy
  behavior — `.obsidian` matches `foo/.obsidian/bar.md`).
- **Nested path** (contains `/`): matches a path prefix — `"inbox/processed"`
  matches `inbox/processed/*` but NOT `inbox/*` or
  `notes/inbox/processed/*`.

This is what lets janitor/distiller exclude the audit directory without
also excluding the curator's fresh inbox (which the curator's own
watcher needs to see).

### Updated defaults

- `src/alfred/janitor/config.py` — VaultConfig defaults now include
  `"inbox/processed"`
- `src/alfred/distiller/config.py` — same
- `src/alfred/_bundled/config.yaml.example` — top-level `ignore_dirs`
  now includes `"inbox/processed"` with a comment explaining the shape
- `src/alfred/quickstart.py` — fresh-install config template matches

### Call sites updated to use the helper

- `src/alfred/janitor/scanner.py` — `_build_stem_index`,
  `run_structural_scan`, `run_drift_scan`
- `src/alfred/janitor/daemon.py` — `snapshot_vault`
- `src/alfred/janitor/context.py` — `build_vault_context`
- `src/alfred/distiller/candidates.py` — `scan_candidates`,
  `collect_existing_learns`
- `src/alfred/distiller/daemon.py` — `snapshot_vault`
- `src/alfred/distiller/context.py` — `build_vault_context`

Other call sites (curator, brief, vault/ops.py search/list, surveyor
watcher) were left with their existing single-component matching
because none of them currently use or need nested-path entries. The
helper is there to adopt when they do.

## User config audit

User's `config.yaml` top-level `ignore_dirs` contains `inbox` (the
broader exclusion — whole directory). That already covers
`inbox/processed/` transitively. **No manual edit to the user's
`config.yaml` is required** — the new defaults are for fresh installs
only; users with an override list keep their existing exclusion.

This was verified by loading the user's config through
`load_from_unified` for all three tools:

```
janitor ignore_dirs:   [_templates, _bases, _docs, .obsidian, view, session, inbox]
distiller ignore_dirs: [_templates, _bases, _docs, .obsidian, view, session, inbox]
surveyor ignore_dirs:  [_templates, _bases, _docs, .obsidian, view, session, inbox]
```

## Backward compat for `ignore_dirs` merging

Confirmed via source: `ignore_dirs` is **override-only**, not
additive-merge. The tools' `load_from_unified` passes the user's raw
`vault.ignore_dirs` list straight into the `VaultConfig` dataclass,
which replaces the default. That means:

- Users who haven't defined their own `ignore_dirs` pick up the new
  default automatically (fresh installs + anyone with a minimal
  config).
- Users who DID define `ignore_dirs` keep their list as-is. If they
  want the new `inbox/processed` entry they must add it manually —
  but if their list already contains the broader `inbox`, they don't
  need to.

## Tests

528 → 539 passing (11 new tests):

- `TestIsIgnoredPathHelper` — 6 tests locking the helper's two-shape
  matching contract (single-component, nested-path, mixed, pathlib
  input, leading/trailing slash tolerance, prefix-not-substring).
- `TestJanitorDefaultExcludesInboxProcessed` — default contains
  `inbox/processed`; scanner run on a seeded raw email in
  `inbox/processed/` returns zero issues against that path.
- `TestDistillerDefaultExcludesInboxProcessed` — default contains
  `inbox/processed`; `scan_candidates` and `collect_existing_learns`
  both skip seeded files in `inbox/processed/` (and the former test
  asserts a real candidate elsewhere IS picked up so the assertion
  isn't vacuous).

## Alfred Learnings

- **Path-matching pattern duplication.** The
  `any(part in ignore_dirs for part in rel.parts)` pattern appeared
  in 20+ call sites across curator, janitor, distiller, surveyor,
  brief, and vault/ops. A proper shared helper should have existed
  from day one. The `is_ignored_path` helper now lives in
  `alfred.vault.ops`; adoption is incremental — janitor and distiller
  scanners are migrated, the rest can follow when they need the
  nested-path feature.
- **Scope bleed to check:** when adding a "nested-path" entry shape
  to an existing matcher, grep the whole tree for the old pattern
  and audit which sites need the new behavior. Most don't — picking
  the subset that does (processing-tool scanners and candidate
  pipelines) keeps the blast radius small.
- **User config auditing.** Before deciding whether the user needs a
  manual config edit, load their actual config through the same
  `load_from_unified` path the daemon uses. "The default changed, so
  fresh installs are fine" isn't enough — a user with an override
  list needs explicit reasoning about whether the override already
  covers the new default's intent. In this case the user's `inbox`
  entry was broader than the new `inbox/processed` default, so no
  manual edit was needed.

## Daemon restart

Recommended after both Tier 3 commits land. Running daemons still have
the pre-change `ignore_dirs` set from their current process memory, so
they'll continue to use whatever the config said at startup. A restart
(`alfred down && alfred up`) makes the new defaults (and, for anyone
whose config picks up the example, the new entry) take effect.

For THIS user specifically, their config override already covered the
intent, so a restart is optional — behavior won't change.
