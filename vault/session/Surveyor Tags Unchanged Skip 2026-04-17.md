---
type: session
status: completed
name: "Surveyor Tags Unchanged Skip"
created: 2026-04-17
description: "Bug 2 of 3 drift-bug batch. Skip the alfred_tags frontmatter write when the normalized (sorted + deduped) new tag list equals the existing one, and log the skip path."
tags: [surveyor, drift-bug, frontmatter, session-note]
---

# Surveyor Tags Unchanged Skip — 2026-04-17

## Intent

Bug 2 of 3 from the daemon-drift triage. The surveyor's `write_alfred_tags` rewrote the frontmatter on every sweep even when the proposed tag list was semantically identical to what was already on disk. The pre-existing `sorted(existing) == sorted(tags)` check was a skip attempt, but:

1. It did not deduplicate — `["a", "b"]` vs `["b", "a", "b"]` compared unequal and drove a write.
2. It had no observable log path for the skip case, so "did the sweep touch this file or not?" was only answerable by mtime inspection.
3. It only handled a list-typed existing value (no defensive None / string coercion).

The labeler is already pinned to temperature=0 (`labeler.py:122`), so identical cluster input produces identical tag output. But cluster membership shifts each sweep when new records land or re-embed, and the labeler's input changes with it — producing tag-list outputs that differ only in ordering or incidental duplication. Those were all churning the vault.

**Fix**: Normalize both existing and new tag lists by sorting + deduping (cast to str to defend against stray non-str values). Early-return on equality and log `writer.tags_unchanged` with the tag count. On change, log `writer.tags_updated` with before/after counts and the new tag list. Coerce a string-typed existing value to `[str]`.

## Files changed

- `src/alfred/surveyor/writer.py` — rewrote `write_alfred_tags` normalization + logging. No behavior change on the "different tags" path beyond the new structured log event name.

## Verification

Smoke script at `/tmp/alfred_smoke/smoke_surveyor_tags.py` exercised four cases against a temp vault `person/Bob.md`:

1. **Equivalent-but-reordered-and-duplicated** — existing `["alpha", "beta"]`, new `["beta", "alpha", "beta"]`. Expected skip. PASS (mtime unchanged; `writer.tags_unchanged` logged).
2. **Genuine change** — existing `["alpha", "beta"]`, new `["alpha", "gamma"]`. Expected write. PASS (mtime advanced; `writer.tags_updated` logged with before=2 after=2).
3. **Second identical write with different order** — same normalized set as case 2's output. Expected skip. PASS.
4. **Empty-to-empty** — frontmatter missing `alfred_tags`, new list is `[]`. Expected skip. PASS.

All four PASSED. No daemon restart (daemons were pre-stopped per instructions).

## Alfred Learnings

- **`sorted(a) == sorted(b)` is the wrong idempotency check for tag lists.** It tolerates reordering but not duplication. Set-based normalization (`sorted(set(...))`) is the right gate. Generalizable: any list-field idempotency check should dedupe before comparing unless duplicates carry meaning.
- **Explicit "no-op" log events are worth the log line.** The pre-fix skip path was silent — so when the daemon appeared to "keep writing to the same file every sweep," the first instinct was to suspect the write path itself instead of realizing the sweep was re-entering `write_alfred_tags` but the guard was failing on a duplicate-seeded input. A named `writer.tags_unchanged` event makes the guard auditable. Apply this to every idempotent writer — pair `*_unchanged` / `*_updated` log events so logs tell a full story without mtime inspection.
- **Temperature=0 is necessary but not sufficient for deterministic output writing.** The labeler is deterministic *given identical input*. But the input to the labeler changes every sweep as cluster membership drifts. Downstream consumers of deterministic-model output still need their own "did the output actually change?" gate. This pairs with Bug 1's learning about accumulating-semantics fields: model determinism alone will not save you from vault churn — the writer itself has to early-return on equivalence.
