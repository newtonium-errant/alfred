---
type: session
status: completed
name: "Option E Q6 — STUB001 fallback flag"
intent: "Close Option E Q6: flag STUB001 stubs that Stage 3 couldn't enrich"
project: "[[project/Alfred]]"
created: 2026-04-19
tags: [janitor, option-e, q6, stub001, skill-audit]
---

# Option E Q6 — STUB001 fallback flag

Closes the last open item from the Option E plan (Part 3.4, deferred on
2026-04-18). With Q3's body-write scope narrowing shipped, STUB001 body
enrichment now lives exclusively in Stage 3 under the `janitor_enrich`
scope. When Stage 3 skipped a stub (cost cap, stale, read-fail, LLM
no-op, or missing template), nothing was flagged and the stub stayed
invisible until the next deep sweep. This commit closes that gap.

## Shape

Mirrors the LINK001 fallback pattern (commit `8d3d33e`):

- `autofix.flag_unenrichable_stubs(unresolved, vault, session)` — writes
  the deterministic note
  `"STUB001 -- body is minimal; Stage 3 enrichment unavailable or skipped. Consider adding content."`
  via the existing `_flag_issue_with_note` helper. Prose pinned in a
  module-level constant (`_STUB001_UNENRICHABLE_NOTE`) so tests and the
  helper can't drift.
- `pipeline._stage3_enrich` now returns `(enriched_count, unresolved)`
  instead of a bare int. Unresolved stubs accumulate across four
  branches: stale, over-cap, read-failed, LLM-no-op. Template-missing
  short-circuits to "everything unresolved."
- `run_pipeline` calls `flag_unenrichable_stubs` on the unresolved list
  and rolls the count into `result.files_flagged` (same tally as LINK001
  unresolved, same surface area).

## Implementation notes

- Stage 3 previously incremented `enriched += 1` on every LLM call,
  including no-ops — a pre-existing accuracy bug. This commit fixes it
  as a side effect: I snapshot file mtime before the LLM call and only
  count real changes. That required because the fallback logic needs
  honest "did anything happen" evidence to decide whether to flag.
  Noted in case surveyor/brief ever read `stubs_enriched` as a metric.
- Template-missing is an absolute short-circuit: if `stage3_enrich.md`
  didn't ship, every stub is unresolved. Prevents silent unbounded
  "I just didn't try" failures.
- The helper tolerates missing files (race with a concurrent move /
  delete) by returning `"skipped"` from the shared `_flag_issue_with_note`
  — the pipeline won't crash because one stub vanished mid-sweep. Test
  `test_stub001_missing_file_is_skipped_not_raised` locks that in.

## Tests

New file `tests/test_janitor_autofix.py` with 8 tests:
- `test_stub001_unenrichable_note_has_exact_prose` — pins the
  deterministic string so it can't drift (SKILL idempotency).
- `test_stub001_unenrichable_gets_flagged` — basic flag path.
- `test_stub001_enriched_not_flagged_by_fallback` — empty unresolved
  list → no notes written.
- `test_stub001_stale_gets_flagged` — stale stubs flow through.
- `test_stub001_multiple_unresolved_flags_each` — all 3 get the same
  prose.
- `test_stub001_missing_file_is_skipped_not_raised` — race tolerant.
- `test_pipeline_flags_unresolved_stubs_when_template_missing` —
  integration-lite against `_stage3_enrich`.
- `test_pipeline_over_cap_stubs_are_unresolved` — cap → unresolved
  branch exercised.

Baseline 160 → 168 passing. No regressions.

## SKILL audit

Per the new CLAUDE.md rule (scope/schema-narrowing triggers a SKILL
audit in the same cycle), checked the janitor SKILL's STUB001 section.
Yesterday's prompt-tuner pass had slimmed it to "flag-only fix" (agent
writes the janitor_note). That's now wrong: the pipeline handles the
flag deterministically after Stage 3, same pattern as FM001/FM002/
LINK001.

Updated `SKILL.md §3 STUB001` to the "Handled by the pipeline — you
should not see this code in your issue report" language. Bundled in
this commit.

## Deviations from spec

None. The mtime-diff accuracy fix for `enriched` was a natural
side-effect of the fallback logic needing honest evidence; noted above.

## Alfred Learnings

- **Pattern validated — scope narrowing triggers SKILL drift.** Q3
  moved body writes out of the janitor scope; yesterday's SKILL pass
  correctly pulled body-write language out but left the flag-writing
  instruction behind. The new CLAUDE.md "scope-narrowing cycles trigger
  a SKILL audit in the same cycle" rule is the right mitigation — this
  commit proves it by catching exactly the kind of drift it's supposed
  to catch.
- **Anti-pattern — bare-int returns from multi-branch stages.** Stage 3
  returning just `int` hid the no-op LLM call accuracy bug. When a
  stage has multiple skip branches (stale, cap, read-fail, no-op), the
  return type should expose which branch each issue landed in so the
  pipeline can react. LINK001 already had this shape; STUB001 was
  behind.
- **Gotcha — deterministic prose must be module-level.** The LINK001
  fallback hardcoded its prose inline. Tests had no anchor to pin it,
  so a future refactor could silently drift. Moved the STUB001 prose
  to `_STUB001_UNENRICHABLE_NOTE` and pinned it with a test. Consider
  backporting to LINK001 in the next cleanup pass.
