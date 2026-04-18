---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, autofix, sem, dup001]
---

# Option E - Commit 2: Autofix SEM + Learn-Type DUP Routing

## Scope

Second of the six-commit Option E sequence. Plan Part 2: route SEM001-
SEM004 (stale active records) and learn-type DUP001 (assumption,
decision, constraint, contradiction, synthesis duplicates) through
Stage 1 deterministic flagging so the LLM never sees them.

## What Changed

`src/alfred/janitor/autofix.py`:

- New `_SEM_FLAG_CODES` set covers all four SEM codes. `_apply_fix`
  dispatches SEM issues to `_flag_issue`, which writes a janitor_note
  with the scanner's own message (e.g. "SEM001 -- status='active' but
  no activity in 42 days").
- New `_dup_is_learn_type(issue, vault_path)` helper reads the
  record's `type` field and checks against `LEARN_TYPES` from the
  vault schema.
- DUP001 branch: if `_dup_is_learn_type` is true, write the fixed
  note `"DUP001 -- learn-type duplicate, not a triage candidate,
  ignored"` via the new `_flag_issue_with_note` helper. Entity-type
  DUP001 returns `"skipped"` so the existing LLM triage-task path in
  the agent SKILL continues to run.
- New `_flag_issue_with_note(issue, rel_path, vault_path,
  session_path, note)` helper accepts a fixed note string instead of
  deriving it from the Issue. The existing `_flag_issue` now delegates
  to it for the generic `{code} -- {message}` pattern, so both paths
  share the write + log + mutation_log tail.

`src/alfred/janitor/pipeline.py`:

- Extended the Stage 1 `autofix_codes` set to include the four SEM
  codes. Without this, the scanner would produce SEM issues that
  `run_pipeline` never routed anywhere. Comment now documents the
  intent: SEM001-004 and learn-type DUP001 deterministic, SEM005-006
  still reserved for LLM detection, entity-type DUP001 still
  triage-task via the SKILL.

## Why This Matters

The old flow sent SEM001-004 to the LLM with "do not change status
automatically, just flag" instructions — a 100% wasteful round trip
for every stale record. Same for learn-type DUP001, which was
ambiguous in the SKILL (entity triage vs learn-type note).

Deterministic flagging puts the idempotency guarantee into Python
where it belongs:
- SKILL "idempotency rule" is about prose stability — Python writes
  the exact same janitor_note string every time, zero drift.
- LLM capacity freed for codes that actually need judgment (STUB001
  body enrichment, entity DUP001 triage, LINK001 ambiguous cases,
  SEM005-006 semantic detection).

## Smoke Test

Temp vault with three seeded records:

- `project/Old.md` with SEM001 issue → flagged with
  `"SEM001 -- status='active' but no activity in 42 days"`
- `assumption/Dup Learn.md` with DUP001 issue →
  flagged with `"DUP001 -- learn-type duplicate, not a triage
  candidate, ignored"`
- `org/Acme.md` with DUP001 issue → `"skipped"` (falls through to
  LLM triage per existing SKILL)

`_apply_fix` returns `['flagged', 'flagged', 'skipped']` as expected.
Frontmatter inspection confirms:
- SEM001 record got the scanner's message verbatim with SEM001 prefix
- Learn-type DUP001 record got the fixed deterministic note
- Entity-type DUP001 record had no janitor_note written (the LLM
  triage-task path owns that flow)

`autofix_issues` aggregate: `fixed=[], flagged=['project/Old.md',
'assumption/Dup Learn.md'], skipped=['org/Acme.md']`.

## Alfred Learnings

- **Deterministic flag text belongs in Python, not the SKILL.** The
  SKILL's idempotency rule (issue-code prefix match) is load-bearing,
  and LLM prose variance across sweeps was the exact failure mode
  that rule was designed to catch. Moving the write into Python
  makes prose stability a guarantee, not a contract. This is the
  pattern that commit 4 (SKILL strip) will generalize across all
  deterministic codes.
- **Resolves plan Part 6 Q2 and Q4:** learn-type DUP001 gets its own
  deterministic note (Q2), and the `autofix_codes` partition needed
  widening in pipeline.py so the scanner-produced SEM issues actually
  reach Stage 1 (Q4 — the partition was the missing piece).

## Commit

- Code: (this commit)
- Session note: (this file)
