---
type: session
created: '2026-04-20'
name: Instructor c3 — watcher and detector 2026-04-20
description: Commit 3 of the 6-commit alfred_instructions watcher rollout — daemon poll loop, pure detect_pending(vault, state) function, CLI skeleton (scan/run/status) that uses the detector without the executor
intent: Land the poll + hash-gate detection layer with full test coverage (8 tests) so commit 4 can plug in the executor without touching detection logic
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Instructor c1 — scope and schema 2026-04-20]]'
  - '[[session/Instructor c2 — state and config 2026-04-20]]'
tags:
  - instructor
  - daemon
  - detector
  - alfred-instructions
status: completed
---

# Instructor c3 — watcher and detector 2026-04-20

## Intent

Commit 3 of the 6-commit `alfred_instructions` watcher rollout. Lands
the poll loop + detection logic with zero executor wiring — the
detector enqueues directives and logs them, but actual execution is
the commit 4 payload.

Split this way because `detect_pending` is a pure function the
executor just consumes: commit 4 will drop in `executor.execute(...)`
against each `PendingInstruction` without touching detection code.

## What shipped

### `src/alfred/instructor/daemon.py`

- `PendingInstruction` frozen dataclass — `{rel_path, directive,
  record_hash}`. One per directive on each record that needs a re-run.
- `_iter_vault_md(vault_path, ignore_dirs)` — walks `rglob("*.md")`
  with the shared `is_ignored_path` filter. Returns a sorted list so
  detection is deterministic across runs.
- `_read_pending_directives(md_path)` — parses frontmatter, handles
  the YAML oddities: scalar promoted to single-entry list,
  None/missing yields `[]`, non-list shapes logged + dropped, non-
  string entries logged + dropped. Malformed YAML swallowed with a
  warning.
- `detect_pending(vault, state, ignore_dirs)` — pure function. Walks
  the vault, hashes each `.md`, skips records whose hash matches the
  cached one, parses pending directives from the rest, returns one
  `PendingInstruction` per directive. Refreshes the cached hash on
  files that changed but have no pending queue so we don't re-parse
  them every poll (cheap steady-state path).
- `run(config, state, suppress_stdout)` — async poll loop matching
  the other tools' entry-point signature. Commit 3 scope: it logs
  pending directives at INFO, seals the hash into state so we don't
  re-emit the same log entry every cycle, and stamps `last_run_ts`.
  Commit 4 replaces the "placeholder seal" block with the real
  executor dispatch.
- `PENDING_FIELD` / `ARCHIVE_FIELD` — module-level re-exports of
  `INSTRUCTION_FIELDS[0]` / `[1]` so the executor + CLI can import
  one name instead of re-indexing.

### `src/alfred/instructor/cli.py`

Three subcommands, fully wired for what the detector alone can do:

- `cmd_scan(config)` — throwaway state, one detection pass, prints a
  summary of pending directives by record. Non-mutating — useful for
  "what's queued right now?" operator checks.
- `cmd_run(config)` — loads state, runs the poll loop in foreground.
- `cmd_status(config)` — prints tracked-file count, pending retries,
  last run timestamp, poll interval, model.

CLI is not yet wired into `src/alfred/cli.py` — that's commit 6 along
with the orchestrator registration.

### Tests — 8 new

`tests/test_instructor_detector.py`:

1. `test_no_instruction_fields_yields_empty_queue` — record with no
   `alfred_instructions` field at all → empty queue.
2. `test_empty_list_yields_empty_queue` — `alfred_instructions: []`
   → empty queue. Hash refresh still fires so we don't re-inspect.
3. `test_populated_list_yields_one_pending_per_directive` — two
   directives → two `PendingInstruction`s, same `record_hash`, correct
   paths.
4. `test_hash_unchanged_skips_file` — after recording the hash in
   state, a second detect pass returns empty. The core steady-state
   gate.
5. `test_hash_changed_re_detects` — operator edits file, hash
   advances, detect returns the new directive list.
6. `test_malformed_frontmatter_is_skipped_not_raised` — broken YAML
   in one record doesn't stop detection on other records.
7. `test_scalar_directive_promoted_to_single_entry_list` —
   `alfred_instructions: "do X"` is legal YAML shorthand; parse-time
   coercion gives us a single-entry list downstream.
8. `test_ignore_dirs_filters_paths` — `_templates` records don't
   fire even if they happen to contain an `alfred_instructions` field
   (e.g., the record-type template itself).

## Verification

Full `pytest tests/ -x`: **568 passed** in 22.40s. Baseline after c2
was 560; this commit adds 8 new detector tests.

## Deviations from spec

None. The planned detector tests (a–e in the spec) all landed plus
three extras (malformed, scalar, ignore_dirs) that guard specific
YAML/FS edge cases the planner flagged as concerns.

## Guardrails honoured

- No SDK calls — commit 4.
- No SKILL file references yet — daemon's executor hook is a
  placeholder `log + record_hash` block, not a failing import.
- No orchestrator / CLI top-level registration — commit 6.
- No health probe — commit 6.

## Alfred Learnings

- **Pattern validated — pure detector + stateful loop is the right
  seam.** Splitting `detect_pending` (pure, takes a state view, no
  mutation) from `run` (loop + state mutation + future executor
  dispatch) makes the detector trivially testable — 8 tests, zero
  mocks, no `AsyncMock` gymnastics. Commit 4 can focus all of its
  test energy on the executor's tool-use loop without having to
  exercise the detector again.

- **Gotcha confirmed — python-frontmatter + scalar coercion.**
  `alfred_instructions: "do X"` parses as a string, not a list,
  because `frontmatter.load` doesn't run the `LIST_FIELDS`
  coercion — that only fires inside `vault.ops._coerce_list_fields`
  at write time. Any code that reads frontmatter directly has to
  promote scalars itself. Worth watching for any other tool that
  consumes `LIST_FIELDS`-tagged fields via raw frontmatter parsing.

- **Pattern validated — hash gate on full-file bytes.** Hashing the
  whole record (frontmatter + body) means any operator edit
  invalidates the cache, not just changes to the instruction list.
  That's deliberate: if the operator edits context elsewhere in the
  file, a re-inspection is cheap, and we avoid the hazard of a
  directive like "add X to the body" silently no-oping because the
  frontmatter didn't change.

- **Pattern validated — sealing the hash in the placeholder block.**
  The commit 3 `run` loop seals `state.record_hash(p.rel_path,
  p.record_hash)` even though no executor fired yet. Without that,
  every poll would re-detect the same pending directives forever
  (nothing mutated the file). Commit 4 naturally advances the hash
  by editing the record, so the placeholder block is a no-op once
  the real executor is wired in — but it keeps the intermediate
  state honest during the 3→4 gap.
