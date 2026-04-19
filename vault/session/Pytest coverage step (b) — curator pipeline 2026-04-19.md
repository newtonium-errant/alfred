---
type: session
status: completed
name: "Pytest coverage step (b) — curator pipeline"
intent: "Drive curator/pipeline.py coverage from 0% to ≥70% via a focused test package"
project: "[[project/Alfred]]"
created: 2026-04-19
tags: [testing, coverage, pytest, curator, quality]
related:
  - "[[project/Alfred]]"
  - "[[session/Pytest coverage step (a) — baseline + top-5 2026-04-19]]"
---

# Pytest coverage step (b) — curator pipeline

Step (b) of the 3-part pytest expansion plan (`project_pytest_expansion.md`).
Step (a) landed earlier today with baseline 14% → 15%. This commit targets
the single biggest zero-coverage surface: `src/alfred/curator/pipeline.py`
(324 statements, 0% covered as of step a).

## Work Completed

### Test infrastructure

- **`tests/curator/__init__.py`** — new test package for the curator pipeline.
- **`tests/curator/_fakes.py`** — `FakeLLMResponse` dataclass + `FakeAgentBackend`
  class. Drop-in replacement for `_call_llm` via `monkeypatch.setattr`.
  Features:
  - Queue-based programmable responses keyed by stage-label prefix
    (`s1-analyze`, `s4-*`).
  - Optional `match_inbox_stem` for concurrency tests — routes responses
    by inbox filename so asyncio.gather execution order can't mismatch
    the queued responses.
  - Concurrency instrumentation (`peak_concurrent`, `track_concurrency`,
    `hold_seconds`) to verify the semaphore bound.
  - Side effects: writes the manifest file to the path extracted from
    the prompt, creates the note record via `vault_create`, appends
    mutation-log entries. All real vault state — the fake LLM does the
    writes an actual LLM would trigger through the agent's tool use.
- **`tests/curator/conftest.py`** — five fixtures:
  - `curator_vault` — minimal vault layout (11 scaffold dirs + seed
    project + `_templates/person.md`, `_templates/note.md`).
  - `curator_config` — `CuratorConfig` pointed at the vault, OpenClaw
    backend selected so `_use_pipeline()` returns True.
  - `seeded_inbox` — factory (takes `count`/`contents`/`stems`) for
    dropping test files into the inbox.
  - `fake_agent_backend` — fresh per test.
  - `pipeline_runner` — wires `_call_llm` → fake backend and hands back
    a `PipelineHarness` with a one-call `.run(inbox_file)`.

### Test files (44 new tests, all passing)

- **`test_pipeline_stages.py`** (10 tests) — stage-by-stage happy path:
  - Stage 1: manifest `body` field becomes entity body (upstream cbedd04
    full-record output shape); name normalisation title-cases persons.
  - Stage 2: body-less manifest entry falls back to description stub;
    existing entity not recreated (case-insensitive dedup via
    `_entity_exists`).
  - Stage 3: note receives `related` wikilinks to all entities; each
    entity gets a back-link; no-op when note missing.
  - Stage 4: `skip_entity_enrichment=True` skips all Stage 4 calls
    (upstream ba1f7d0 default); `=False` triggers per-entity calls,
    but `location` / `event` types still skip via `_SKIP_ENRICH_TYPES`.
  - PipelineResult shape: populated summary, paths, success flag.
  - Total failure: no note + no manifest after 3 retries → `success=False`.

- **`test_pipeline_concurrency.py`** (4 tests):
  - Semaphore gate: 6 parallel `run_pipeline` calls with max_concurrent=2
    never exceeds peak of 2 (but does reach 2 — not serialised to 1).
  - Mixed success/failure: one file raises in Stage 1, the other two
    complete cleanly (mirrors upstream 163b7f9's `return_exceptions=True`).
  - `mark_processed` fallback: failed file ends up in `inbox/processed/`
    (upstream 7745ea7).
  - No-retry: after the fallback move, the file no longer surfaces on a
    `glob('*.md')` of the inbox.

- **`test_vault_context.py`** (12 tests) — `VaultContext.to_prompt_text`:
  - `### <type> (<N>)` header per group, comma-separated names,
    line-wraps at ~120 chars.
  - NO full wikilinks, NO per-entity status lines — regression guard for
    the upstream ba1f7d0 token reduction.
  - Empty context renders as empty string (no crash).
  - Groups sorted by type name; names sorted within a group.
  - `build_vault_context` skips inbox, honours `ignore_dirs`, silently
    drops records without a `type` frontmatter field.

- **`test_pipeline_errors.py`** (18 tests):
  - `_parse_entity_manifest`: all three tiers (fenced block, inline,
    whole-stdout); fenced-over-inline preference; nested braces in body.
  - `_extract_entities_from_text`: malformed JSON, non-list entities value.
  - `_find_created_note`: returns first `note/`-prefixed path; empty
    when no note was created.
  - Stdout-fallback: manifest file missing → pipeline parses stdout
    (upstream 44cf675 openclaw mount-mismatch fix).
  - Retry loop: 2 empty responses then a populated third → pipeline
    succeeds after 3 Stage 1 calls.
  - Malformed manifest entries (missing type/name) are skipped, not fatal.
  - Near-match collision: existing `org/PocketPills.md` + manifest says
    `"Pocketpills"` → resolves to canonical path (covers `_entity_exists`
    branch; the `vault_create` near-match VaultError recovery branch
    sits behind the pre-check in practice, so that specific branch
    remains uncovered — see "harder than expected" below).
  - Stage 3 note-link failure is logged but not fatal — continues to
    back-link entities.

### Results

- **Curator pipeline coverage: 0% → 76%.** Target was ≥70%.
- **Full suite: 204 → 248 tests, all passing** (44 new tests across 4 files + 1 fakes module + 1 conftest).
- **Per-module bumps** (from this push alone):
  - `curator/pipeline.py`: 0% → **76%**
  - `curator/context.py`: 0% → 37%
  - `curator/backends/openclaw.py`: 0% → 24% (import side-effect)
  - `curator/backends/__init__.py`: 0% → 94%
  - `curator/config.py`: 69% → 69% (config fixtures exercised the
    dataclass path but load_from_unified still 0)
- **Overall**: 15% → 19% (+4pp). The 324-statement pipeline was ~2.4%
  of the codebase.

### What's still uncovered in pipeline.py

The remaining 24% (78 lines) is dominated by:

- `_call_llm` itself (lines 168-258, ~90 lines) — the subprocess wrapper.
  Testing this requires faking `asyncio.create_subprocess_exec`, which
  the task explicitly defers. Every test monkeypatches the whole
  function so the wrapper never runs.
- `_load_stage_prompt` failure path (lines 53-54) — skill file missing.
- `_load_user_profile` (lines 70-75) — user-profile.md lookup.
- `vault_create` near-match VaultError recovery (lines 464-483) —
  practically unreachable because `_entity_exists` short-circuits first.
- A handful of tiny logging branches (101, 135-143, 325-326, 331-332,
  386, 423, 440, 519-520, 532-533, 559, 579, 595-596).

All of these are either pure infra (the subprocess path), unreachable
in practice (near-match after pre-check), or cheap logging lines
not worth fake-engineering to exercise.

## Outcome

The curator pipeline — biggest untested surface in the project — now has
a 44-test safety net covering every stage boundary, the semaphore bound,
the mark-processed fallback, the slim vault-context contract, and the
manifest-parser tiers. Before this commit, any change to `pipeline.py`
carried the live-E2E-as-the-only-validation risk flagged in the
`_ts` leak incident. That risk is now materially lower.

The conftest fixture pattern (fake backend + programmable queue +
real vault operations) is directly reusable for step (c) — the
`FakeAgentBackend` class generalises to any subprocess-driven agent
call, and the queue-by-stage-prefix scheme works for any stage-labelled
pipeline.

## Alfred Learnings

- **Pattern validated — fake-by-monkeypatch over mocks.** Monkeypatching
  the module-level `_call_llm` with a `FakeAgentBackend` that performs
  real side-effects (writing manifests, creating notes, logging
  mutations) reads far cleaner than a `MagicMock` stack. Tests are
  essentially prose: "queue this response, run the pipeline, assert on
  what landed in the vault." Zero assertion plumbing. Reuse this pattern
  for step (c).
- **Pattern validated — filename-routed queues for concurrency tests.**
  Queueing responses by stage-label alone is fine for sequential tests
  but breaks under `asyncio.gather` because task-start order isn't
  guaranteed. Adding `match_inbox_stem` to `FakeLLMResponse` solved it:
  the fake scans the prompt for each file's stem and picks the right
  response regardless of execution order. Shipped as a general facility
  in `_fakes.py`.
- **Gotcha — empty `manifest_entities=[]` triggers the Stage 1 retry
  loop.** Early version of the concurrency test queued responses with
  `manifest_entities=[]` to mean "no entities, but successful." The
  pipeline treats empty-list as "failed manifest extraction" and retries
  up to 3 times, blowing through the queued responses. Fix: always queue
  at least one entity so the manifest is truthy, OR use `None` to
  explicitly signal "don't write the manifest file." Worth flagging in
  pipeline code? The `if manifest:` check conflates "parse failed" with
  "no entities found" — arguably a minor testability issue but not a
  bug. Leaving it alone per scope-creep rule.
- **Gotcha — `_entity_exists` short-circuits the `vault_create`
  near-match branch.** Covering lines 464-483 (the `VaultError(reason=
  near_match)` recovery) requires either (a) deleting the pre-check
  that runs first in `_resolve_entities`, or (b) simulating a race
  where a file lands between the check and the create call. Neither is
  worth the complexity for one branch. Noted for step (c) if the
  orchestrator harness gives us a natural way to stage the race.
- **Anti-pattern confirmed — testing `CuratorDaemon.run()` directly.**
  Initial instinct was to invoke the daemon's main loop with a
  tear-down signal. The loop's lifecycle (watcher setup, rescan
  interval, asyncio sleeps, signal handlers) makes the harness
  brittle. Extracting the semaphore + `mark_processed` fallback into
  a `_process_with_fallback` test helper that mirrors the production
  pattern gave 100% of the testable value without 5× the code.
- **Missing knowledge — `_fakes.py` vs. `conftest.py` split convention.**
  Pytest auto-injects fixtures from conftest but doesn't help with
  importing classes. Dropping `FakeLLMResponse` / `FakeAgentBackend`
  into a sibling `_fakes.py` (and importing via `from ._fakes import …`)
  keeps tests importable as regular modules. This should probably be
  documented in `.claude/agents/builder.md` as the test-package
  convention — every new test package will hit it.
- **Bugs flagged, none fixed (per scope rule):**
  - `_resolve_entities` empty-manifest-vs-no-entities conflation (see
    first gotcha above) — stylistic, not a correctness bug.
  - Nothing else surfaced.
- **Testability tweaks required: zero.** The pipeline was already
  testable via module-level `_call_llm`. No helper exports, no
  signature changes, no new test hooks needed.

## Flagged for follow-up

- Step (c): orchestrator daemon spawn/restart + state persistence.
  The `_fakes.py` pattern should transplant cleanly — the orchestrator
  runs subprocess children, and the same monkeypatch-the-entry-point
  trick should work.
- The test-package convention (`_fakes.py` sibling to `conftest.py`)
  deserves a line in `.claude/agents/builder.md` under "Dependencies"
  or a new "Testing conventions" section.
- `curator/pipeline.py` line 335-341 — the pipeline will bail out of
  the Stage 1 retry loop on the FIRST non-empty `manifest` it sees,
  even if the manifest is from an earlier attempt's stale state.
  Not a bug today (the file is unlinked between attempts) but worth
  a closer look if Stage 1 ever starts caching across retries.
