---
type: session
status: completed
name: "Distiller Learnings Merge Fix"
created: 2026-04-17
description: "Bug 1 of 3 drift-bug batch. Stop distiller from wiping distiller_learnings on re-process — union existing frontmatter list with newly-created batch links instead of replacing."
tags: [distiller, drift-bug, frontmatter, session-note]
---

# Distiller Learnings Merge Fix — 2026-04-17

## Intent

The vault accumulated 194 dirty files since the 08:59 UTC snapshot. Triage identified three distinct daemon drift bugs. This commit is Bug 1 — the HIGHEST severity case because it silently destroys prior distillation attributions every time a source record is re-processed.

**Bug**: `src/alfred/distiller/pipeline.py:974-978` unconditionally called `vault_edit(..., set_fields={"distiller_learnings": learn_links})` where `learn_links` is only the wikilinks created by the current batch. `set_fields` is a full replace, so any prior distiller run's attributions on that source record were overwritten on every re-process.

**Fix**: read the existing `distiller_learnings` value first via `vault_read`, coerce it to a list (None → `[]`, str → single-item list, list → filtered str cast), then union with the new batch keeping order (existing first, new unique appended). Write the merged list.

## Files changed

- `src/alfred/distiller/pipeline.py` — added `vault_read` import; replaced the bare `set_fields={"distiller_learnings": learn_links}` write with a read-merge-write pattern.

## Verification

Smoke test at `/tmp/alfred_smoke/smoke_distiller.py` against a temp vault with a `person/Alice.md` record carrying three existing learnings (`[[assumption/A1]]`, `[[decision/D1]]`, `[[constraint/C1]]`) and a simulated new batch of two links (`[[decision/D1]]` overlapping, `[[assumption/A2]]` new).

Assertions passed:

1. **Primary case** — final list is the four unique links, originals preserved in order, new unique link appended, no duplicate for the overlapping `[[decision/D1]]`.
2. **Idempotent** — running the merge a second time with the same batch is a no-op.
3. **None existing** — field missing / None → list starts empty, becomes the new batch.
4. **String existing** — single-wikilink-as-string edge case → coerced to `[s]`, merged with batch.

All four PASSED. Daemon was not restarted (per instructions).

## Alfred Learnings

- **Field-writer bugs are invisible until you diff against a past snapshot.** This bug existed for the full lifetime of the distiller's Pass-A loop and never produced a stack trace, a failed sweep, or a log warning — the file just quietly lost data on every re-process. The only way it surfaced was diffing vault git against an 08:59 UTC snapshot and noticing the frontmatter history was churning. Worth capturing as a first-class pattern: **any `set_fields={list_field: new_batch}` call on a frontmatter list is suspect — check whether "batch" is authoritative (safe to replace) or incremental (must merge)**. Default to merge for any field whose name suggests accumulation (`_learnings`, `_observations`, `_tags`, `_history`).
- **Smoke-script workflow pays off for frontmatter bugs.** Rather than rerunning the full distiller pipeline to validate a five-line change, a 60-line throwaway script hitting `vault_read` + the merge logic + `vault_edit` directly against a temp vault exercised every edge case in under a second. This is a reusable pattern for any vault-ops-layer change.
- **`vault_edit` with `set_fields` is strictly replace.** There is an `append_fields` path in `vault_edit` that appends to lists, but it appends unconditionally (no dedup) and so can't substitute for a merge that wants union-style semantics. Builder rule: if you need set-union-into-list, do the read/merge in the caller, not by layering on `append_fields`.
