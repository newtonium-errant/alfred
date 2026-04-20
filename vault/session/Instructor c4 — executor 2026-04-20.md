---
type: session
created: '2026-04-20'
name: Instructor c4 — executor 2026-04-20
description: Commit 4 of the 6-commit alfred_instructions watcher rollout — in-process Anthropic SDK executor with seven-tool surface, destructive-keyword dry-run gate, audit comment + archive, retry/error path, and daemon wiring
intent: Land the executor layer so the daemon actually acts on pending directives; the SKILL file lands in commit 5 but a minimal placeholder keeps the tests independent of skill content
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Instructor c1 — scope and schema 2026-04-20]]'
  - '[[session/Instructor c2 — state and config 2026-04-20]]'
  - '[[session/Instructor c3 — watcher and detector 2026-04-20]]'
tags:
  - instructor
  - executor
  - anthropic-sdk
  - alfred-instructions
status: completed
---

# Instructor c4 — executor 2026-04-20

## Intent

Commit 4 of the 6-commit `alfred_instructions` watcher rollout. Lands
the executor module plus the daemon wiring that calls into it.
Directives now actually run: `alfred_instructions` gets cleared,
`alfred_instructions_last` gets populated, the record body carries an
audit comment, and destructive keywords dry-run cleanly.

## What shipped

### `src/alfred/instructor/executor.py` — new module

- `ExecutionResult` dataclass: `{status, summary, mutated_paths,
  tool_iterations, dry_run}`. ``status`` ∈ `{done, dry_run, ambiguous,
  refused, error}`.
- `is_destructive(directive, keywords)` — case-insensitive substring
  match. Exported so the daemon/CLI can share the same gate.
- `VAULT_TOOLS` (seven entries): `vault_read`, `vault_search`,
  `vault_list`, `vault_context`, `vault_create`, `vault_edit`,
  `vault_move`. Broader than the talker (4) because the `instructor`
  scope permits create/move and has no field allowlist.
- `_dispatch_tool(tool_name, tool_input, vault_path, dry_run,
  session_path, mutated_paths)` — scope-gated vault bridge. Every
  tool goes through `check_scope("instructor", ...)` so a jailbreak
  can't request a denied op (scope denies `delete` anyway; this is
  belt-and-braces for any future scope tightening). Returns JSON
  strings as tool_result content.
- Dry-run short-circuit: when the directive carries a destructive
  keyword, write ops return `{"dry_run": true, "would": {...}}`
  descriptors instead of mutating. Read ops run normally so the model
  can still reason about the plan.
- `_append_audit_comment(md_path, directive, summary,
  audit_window_size)` — adds one `<!-- ALFRED:INSTRUCTION <iso> "<dir>"
  → <summary> -->` block at the bottom of the body and prunes older
  blocks beyond the window.
- `_archive_directive(md_path, directive, result)` — clears the
  directive from `alfred_instructions`, prepends a `{text, executed_at,
  result}` dict to `alfred_instructions_last`.
- `_surface_error(md_path, directive, error_summary)` — drops the
  directive from the queue and writes a string to
  `alfred_instructions_error`. Called when `state.bump_retry()` hits
  `max_retries`.
- `execute(client, directive, record_path, config, state, skills_dir,
  session_path)` — runs the tool-use loop against the Anthropic SDK.
  Iteration cap is 10 (matches the talker's `MAX_TOOL_ITERATIONS`).
  Every vault mutation fires `mutation_log.log_mutation(...,
  scope="instructor")` so the audit log sees instructor activity
  distinctly from curator/janitor/talker.
- `execute_and_record(...)` — wraps `execute` and handles the
  success/error paths (archive + audit comment on success, retry/error
  on failure). This is what the daemon calls per directive.
- `_parse_agent_summary(text)` — parses the SKILL-mandated final
  summary block `{"status": ..., "summary": ...}` out of the agent's
  end-turn text. Falls back to a truncated literal if JSON parsing
  fails so noisy agent output still produces a usable `summary`.
- `_load_skill(skills_dir)` — raises `FileNotFoundError` loudly if the
  SKILL.md is missing so commit 5's dependency on 4 is visible.
  Tests inject a minimal placeholder.

### `src/alfred/instructor/daemon.py` — executor wiring

- `run()` signature extended: now accepts `skills_dir` and `client`
  kwargs (both default lazily — `get_skills_dir()` + a fresh
  `AsyncAnthropic(api_key=...)`). This keeps test injection clean.
- Per-directive block replaced the commit 3 placeholder with an
  `execute_and_record(...)` call. Any executor exception is caught
  so one bad directive never kills the poll loop. The hash is
  re-sealed from disk after execution — the executor typically
  rewrote the file, so the next poll sees a cache hit; on deleted
  records we call `state.forget_hash` so we don't leak stale entries
  forever.

### Tests — 12 new (`tests/test_instructor_executor.py`)

- `is_destructive` happy path (literal, case-insensitive) + negative
  (non-matching directives).
- `_dispatch_tool` unit tests: `vault_read` returns JSON,
  dry-run blocks writes but allows reads, scope rejects the unknown
  `vault_delete` tool (belt-and-braces), `vault_create` records the
  path in `mutated_paths`.
- `execute()` end-to-end: scripted `tool_use → tool_result → end_turn`
  sequence; assertions on mutation, iteration count, and parsed
  summary shape.
- Destructive keyword triggers dry-run: file content preserved even
  when the model tries to issue a `vault_edit`.
- `execute_and_record()` archives the directive to
  `alfred_instructions_last`, clears it from `alfred_instructions`,
  and writes the audit comment to the body.
- Audit-window pruning: after 6 runs with `audit_window_size=5`, the
  1st block is gone and the 5 most recent remain.
- Retry/error surface: 3 SDK failures in a row (with
  `max_retries=3`) drop the directive from the queue and stamp
  `alfred_instructions_error`.
- Body-write allow path: `body_append` under the `instructor` scope
  grows the body and doesn't raise.

## Verification

Full `pytest tests/ -x`: **580 passed** in 22.75s. Baseline after c3
was 568; this commit adds 12 executor tests.

## Deviations from spec

1. **Tool count grew from "seven" listed to seven + a note about
   `vault_delete`.** The plan called for `vault_read, vault_edit,
   vault_create, vault_move, vault_list, vault_search, vault_context`.
   That's exactly what shipped — seven tools. The "belt-and-braces
   delete" test uses `_dispatch_tool("vault_delete", ...)` which
   returns `{"error": "Unknown tool"}` because there's no entry in
   `_TOOL_TO_OP`. The scope check is the second layer: even if we
   accidentally added `vault_delete` to the table later, `check_scope`
   would still deny it because `SCOPE_RULES['instructor']['delete']
   is False`. Both layers stayed intact.

2. **Retry path caught `anthropic.APIError` specifically, not every
   exception.** The executor's `except anthropic.APIError` is the
   retry-triggering branch. Other exceptions in `_dispatch_tool`
   return JSON error strings to the model (so it can recover mid-
   turn) rather than surfacing as retries. This matches the talker's
   contract — tool errors should reach the model, infrastructure
   errors should retry.

3. **Audit comment format uses `<!-- ALFRED:INSTRUCTION <iso>
   "<dir>" → <summary> -->` with iso-minute granularity
   (`YYYY-MM-DDTHH:MMZ`)**, not full-second timestamps. Makes the
   comment readable in Obsidian without being precisely log-grade —
   if an operator needs to-the-second timing, the
   `alfred_instructions_last` archive carries an isoformat stamp with
   full precision.

## Guardrails honoured

- No SKILL file shipped — commit 5. Executor raises
  `FileNotFoundError` loudly if the file is missing.
- No orchestrator / top-level CLI / health registration — commit 6.
- `_archive_directive` / `_surface_error` are the only places that
  re-write `alfred_instructions*` fields. If we ever need to evolve
  the archive shape, there's one function to touch.

## Alfred Learnings

- **Pattern validated — scope check per tool dispatch, not per
  session.** The talker's scope check fires once per tool call, not
  once per session/turn. The instructor mirrors this. Over-broad
  per-session scoping would miss the case where the model issues a
  tool call the caller didn't anticipate; per-call scoping gates
  every mutation individually.

- **Pattern validated — `tool_name -> scope op_name` mapping as a
  dict.** Keeping `_TOOL_TO_OP` as an explicit dict (rather than
  stripping `vault_` and using the suffix) makes unknown tools
  fail closed with a clear error. If we ever expose a tool whose
  name doesn't start with `vault_` (e.g., a future
  `describe_schema`), the mapping stays explicit.

- **Gotcha confirmed — `anthropic.APIError` needs `request` and
  `body` kwargs.** Instantiating `anthropic.APIError("msg")` alone
  fails with `TypeError: missing 2 required positional arguments`.
  The failing-client test fixture uses `httpx.Request("POST",
  "https://example.invalid/...")` to build a minimal request object.
  Any future test that wants to simulate an SDK failure needs the
  same pattern.

- **Pattern validated — dry-run as "would do X" tool_result.** The
  executor doesn't skip the tool_use loop entirely on dry-run; it
  lets the model plan as if the writes were happening, but returns
  descriptors instead of actual mutations. The model sees "the edit
  was queued" and can compose a coherent plan for
  `alfred_instructions_last`. Early sketches tried to short-circuit
  the whole loop on dry-run, but that meant the archive entry had no
  plan detail — the operator couldn't decide whether to confirm.

- **Pattern validated — re-seal the hash after every execution even
  on failure.** The daemon's per-directive finally block re-hashes
  the record on disk and seals into state. Without this, a failed
  execution (which often mutates nothing) would leave the stale
  cached hash and fire the same directive every poll cycle. The
  retry counter in state is the semantic "please retry" signal;
  the hash is just the "have I seen this file version before"
  signal.
