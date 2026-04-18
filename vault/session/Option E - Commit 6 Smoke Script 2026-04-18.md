---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, scope, testing]
---

# Option E - Commit 6: Smoke Script

## Scope

Sixth and final commit of the Option E sequence. Plan Part 6: add a
reusable smoke test script that exercises the scope lock from
commit 5 end-to-end through the `alfred vault` CLI. Executable
documentation of the scope contract — run it to verify the lock
holds, read it to see exactly which behaviors are guaranteed.

## What Changed

Added `scripts/smoke_janitor_scope.sh` (bash, chmod +x). Runs four
assertions, then a cleanup phase:

1. `ALFRED_VAULT_SCOPE=janitor ... --set 'alfred_tags=...'` → non-zero
   exit with "Scope 'janitor'" in the JSON error.
2. `ALFRED_VAULT_SCOPE=janitor ... --set 'janitor_note=...'` → exit 0
   with janitor_note in the success JSON.
3. `ALFRED_VAULT_SCOPE=janitor_enrich ... --set 'description=...'` →
   exit 0 with description in the success JSON.
4. `ALFRED_VAULT_SCOPE=janitor_enrich ... --set 'alfred_tags=...'` →
   non-zero exit with "Scope 'janitor_enrich'" in the error (proves
   the enrich allowlist is also tight, not just wide-open).

Cleanup phase reads the pre-test values of `description` and
`janitor_note` via `alfred vault read`, takes a full-file backup at
the start, writes the originals back at the end, and diffs the file
against the backup. The diff step is itself asserted — a non-empty
diff is a test failure and the script exits 1.

Keeps running even if one assertion fails (uses `set -u` but not
`set -e`) so operators see all four results in one pass.

## Why This Matters

The scope lock is the kind of control that silently rots. If someone
accidentally broadens the janitor allowlist, nothing blows up — it
just allows more writes. Without a test, you find out during an
incident. With this script, you find out by running
`scripts/smoke_janitor_scope.sh` before the commit lands.

The cleanup-and-diff pattern is the key detail. A smoke script that
pollutes the vault is worse than no script; it creates noise the
next sweep has to filter out. Round-trip-to-diff-is-empty is the
test that the script is trustworthy to run on live data.

## Smoke Test

Ran the script against the live vault. All four assertions passed,
cleanup restored the file exactly:

```
PASS: out-of-allowlist field rejected with scope error (exit=1)
PASS: in-allowlist field accepted (exit=0)
PASS: janitor_enrich allows description (exit=0)
PASS: janitor_enrich rejects out-of-allowlist (exit=1)
PASS: vault restored to pre-test state (diff is empty)
--- summary: 5 pass, 0 fail ---
```

External `diff` against the pre-commit-5 backup also came back
clean. No vault pollution.

## Alfred Learnings

- **Smoke scripts are most valuable when they're runnable on live
  data.** The discipline of "back up, test, revert, diff" makes the
  script safe to include in a pre-commit checklist. A test that can
  only be run in isolation gets skipped.
- **Assert the cleanup, not just the test.** If the script can't
  reliably revert its own edits, it shouldn't run on the real vault.
  Promoting "diff empty" to a PASS/FAIL gate catches any future
  regression where an added test case forgets to clean up.
- **Four cases covers the matrix.** Two scopes × two policies
  (in-allowlist / out-of-allowlist) = four cases. Less and you
  miss coverage; more and you're asserting the same thing twice.
- **No pytest for shell-level smoke tests.** The point is it runs
  with only bash and `alfred` on PATH — no Python test infra,
  no fixtures, no dependencies. Closer to a Makefile target than
  a unit test.

## Commit

- Code: fad17e7 (this commit)
- Session note: (this file)

## Option E Sequence Summary

All six commits landed:

1. `433bf33` — scope.py gains `field_allowlist` permission type.
2. `3a21e21` — autofix routes SEM001-SEM004 + learn-type DUP001.
3. `8d3d33e` — pipeline flags unresolved LINK001 deterministically.
4. `9ee94b5` — SKILL strips deterministic-code procedures.
5. `2d5e8cf` — janitor scope locked to allowlist; Stage 3 split
   into `janitor_enrich`.
6. `fad17e7` — smoke script for the scope lock (this commit).

Daemons are still running on pre-commit-1 code. Team lead triggers
the restart when ready; next sweep picks up all six changes at
once. The scope lock is the highest-leverage piece — before
commit 5, an LLM under the janitor scope could write arbitrary
frontmatter; after commit 5, the allowlist is the boundary.
