---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, pipeline, link001]
---

# Option E - Commit 3: Flag Unresolved LINK001 in Pipeline

## Scope

Third of the six-commit Option E sequence. Plan Part 3: any LINK001
issue that Stage 2 can't resolve (either ambiguous with multiple
candidates or the LLM call didn't modify the file) gets a
deterministic janitor_note written by Python instead of a SKILL
instruction telling the LLM to flag it.

STUB001 fallback flag is DEFERRED per open question #6 — not touched
in this commit.

## What Changed

`src/alfred/janitor/autofix.py`:

- New public `flag_unresolved_links(unresolved, vault_path,
  session_path)` helper. Iterates LINK001 issues, extracts the broken
  target from the scanner message, parses a candidate count from the
  issue's `detail` field (format `"N candidate(s) found"`), and writes
  the note `"LINK001 -- broken wikilink [[{target}]]; {n}
  candidate(s) found, none unambiguous"`. Returns the list of files
  actually flagged for telemetry.

`src/alfred/janitor/pipeline.py`:

- `_stage2_link_repair` return type changed from `int` to
  `tuple[int, list[Issue]]`: `(repaired_count, unresolved_issues)`.
- Each issue that doesn't get an unambiguous Python fix is annotated
  with `issue.detail = f"{len(candidates)} candidate(s) found"` so
  `flag_unresolved_links` can quote the number without re-running the
  search.
- Unresolved categories tracked: message had no extractable wikilink
  target, no stage2 template loaded (shouldn't happen in practice),
  and LLM call completed but target mtime unchanged.
- `run_pipeline` unpacks the tuple and calls `flag_unresolved_links`
  on the unresolved issues, adding the flagged count to the existing
  `files_flagged` counter.
- Log fields now include `unresolved=N` alongside `repaired=N` on
  `pipeline.s2_complete`.

## Why This Matters

Today the LLM receives unresolved LINK001s with an instruction to
"flag with janitor_note" — wasted call, non-deterministic prose,
potential sweep-to-sweep churn. The SKILL's idempotency rule only
works if the prose is stable; Python writes the same string every
time.

Side benefit: the `detail` annotation is a light audit trail. The
janitor_note always cites a real candidate count at the time of the
sweep, which future sweeps can cross-check.

## Smoke Test

Temp vault with two notes referencing broken wikilinks. Mocked
`_is_unambiguous_match` and `_fix_link_in_python` to simulate:

- `note/A.md` → Python path resolves, `_fix_link_in_python` returns
  True → repaired=1, A not in unresolved list.
- `note/B.md` → no unambiguous match, LLM call returns without
  modifying file → B added to unresolved.

Then `flag_unresolved_links([B_issue], ...)`:
- Writes `note/B.md` janitor_note =
  `"LINK001 -- broken wikilink [[Acme]]; 1 candidate(s) found, none
  unambiguous"`.
- `note/A.md` untouched (no janitor_note).

Final counter: `repaired=1, unresolved=1, flagged=['note/B.md']`.

## Alfred Learnings

- **Tuple return for "what got handled + what didn't".** Pattern
  that keeps surfacing: a stage returns both success count and the
  unresolved residue for downstream fallback. Worth noting as a
  pipeline convention — any stage that can fail-gracefully should
  return `(success_count, residue)` rather than `int` so the caller
  can route residue to a deterministic fallback.
- **`Issue.detail` is the cross-stage annotation channel.** It's
  free-form and already plumbed through `Issue.to_dict`, so it's the
  right place to stash "here's context the structural scanner found
  that the flagger needs". Avoids re-running grep.
- **Resolves plan Part 6 Q6 partial:** LINK001 unresolved flow moved
  to Python. STUB001 fallback still deferred per Q6 recommendation.

## Commit

- Code: (this commit)
- Session note: (this file)
