---
type: session
status: completed
name: "Pytest coverage step (c) — orchestrator + state"
intent: "Drive orchestrator.py coverage from 0% to ≥70% and add one state round-trip test per persisting tool"
project: "[[project/Alfred]]"
created: 2026-04-19
tags: [testing, coverage, pytest, orchestrator, state, quality]
related:
  - "[[project/Alfred]]"
  - "[[session/Pytest coverage step (a) — baseline + top-5 2026-04-19]]"
  - "[[session/Pytest coverage step (b) — curator pipeline 2026-04-19]]"
---

# Pytest coverage step (c) — orchestrator + state

Step (c) of the 3-part pytest expansion plan
(`project_pytest_expansion.md`). Steps (a) and (b) landed earlier today
(coverage tooling + baseline; curator pipeline 0% → 76%). Step (c)
targets the cross-cutting modules: `src/alfred/orchestrator.py` (288
statements, 0% covered) plus a shallow round-trip test per tool that
persists JSON state.

## Work Completed

### Test infrastructure

- **`tests/orchestrator/__init__.py`** — new test package for the
  multiprocess orchestrator.
- **`tests/orchestrator/_fakes.py`** — top-level pickleable fake tool
  runners (2-arg and 3-arg variants matching the orchestrator's
  signature dispatch). Exit after a short delay with a caller-controlled
  code, plus a "long-lived" variant that sleeps 60s to exercise the
  SIGTERM-during-shutdown path.
- **`tests/orchestrator/conftest.py`** — five fixtures:
  - `orch_dirs` — per-test `data/`, `skills/`, `pid_path`, `sentinel_path`.
  - `orchestrator_raw_config` — minimal unified config dict.
  - `fast_sleep` — swaps `orchestrator.time` for `_FastTimeShim` so the
    10-second inter-tool stagger and 5-second monitor poll collapse to
    ~1ms each. We patch the module attribute, NOT `time.sleep` globally,
    because a global patch recurses when fixture code calls `time.sleep`.
  - `noop_signals` — swaps `orchestrator.signal` for `_NoOpSignalShim`
    when `run_all` is invoked from a test worker thread (stdlib
    `signal.signal` raises `ValueError` off the main thread).
  - `install_fake_runners` / `fire_sentinel_after` — monkeypatch-level
    helpers for swapping TOOL_RUNNERS entries and arming a daemon-thread
    sentinel writer, respectively.
- **`tests/state/__init__.py`** + **`tests/state/conftest.py`** — fresh
  package for state round-trip tests. One `state_path: Path` fixture
  under `tmp_path`.

### Orchestrator test files (49 new tests)

- **`test_tool_dispatch.py`** (7 tests):
  - `TOOL_RUNNERS` covers all 7 expected tools.
  - 2-arg vs 3-arg partition via `inspect.signature` check.
  - `_MISSING_DEPS_EXIT == 78` constant guard.
  - All runners are distinct functions (no accidental aliasing).
  - Every runner is top-level pickleable (required by `multiprocessing`).

- **`test_auto_start.py`** (7 tests):
  - Default with no optional sections → only curator/janitor/distiller.
  - `surveyor` section → surveyor auto-start.
  - `mail` + `brief` sections → both auto-start.
  - `telegram` key → `talker` tool (the key ≠ tool name asymmetry is
    load-bearing; documented in the test).
  - `only=` flag overrides auto-start entirely.
  - `only=bogus_tool` → `SystemExit(1)` with a "Unknown tool" message.
  - `only=curator,janitor` produces a two-tool run.

- **`test_exit_codes.py`** (4 tests):
  - Exit 78 in a 3-arg runner → exactly 1 start (no restart).
  - Exit 78 in a 2-arg runner (surveyor) → same contract.
  - Always-exit-1 runner → 5-6 starts (5-retry cap), then "exceeded
    restart limit" message.
  - Exit-0 runner is also restarted (daemons aren't supposed to return
    cleanly).

- **`test_sigterm.py`** (4 tests):
  - Sentinel file triggers graceful shutdown; finally block removes
    parent PID, per-tool PID, sentinel, and `workers.json`.
  - Signal handlers are registered for BOTH SIGTERM and SIGINT, and
    they share the same handler function (confirms `_handle_shutdown`
    is reused).
  - Long-lived child processes get SIGTERM'd during shutdown, total
    run time stays under 5 seconds.
  - Smoke-test advertising the "handler is a closure, not directly
    testable" contract.

- **`test_parallel_spawn.py`** (3 tests):
  - Three tools spawn as three distinct live PIDs simultaneously.
  - Per-tool PID files get written to `data/{tool}.pid` and cleaned
    up on shutdown (the 2026-04-17 orphan-process regression guard).
  - Mixed 2-arg + 3-arg tools run side-by-side (guards arity dispatch).

- **`test_runner_entrypoints.py`** (14 tests):
  - `sys.modules` stub-install pattern for `alfred.<tool>.config`,
    `utils`, `state`, `daemon` — calls each `_run_<tool>` in-process
    without triggering real daemon code.
  - Every runner covered: curator, janitor, distiller, surveyor (happy
    path + missing-deps → exit 78), mail webhook, brief, talker
    (clean + non-zero exit propagation).
  - PID helpers: `_tool_pid_path`, `_record_tool_pid`,
    `_cleanup_tool_pid`, plus the four `_kill_stale_tool` branches
    (no file, self-pointing file, dead-PID file, live subprocess
    that actually gets SIGTERM'd).

### State test file (9 new tests)

- **`test_state_roundtrip.py`**:
  - One round-trip per persisting tool: `curator`, `janitor`,
    `distiller`, `surveyor`, `talker`, `brief`, `mail`.
  - Every test populates the state with representative data, calls
    `save()`, asserts the atomic-write contract
    (`<path>.tmp` should NOT linger after save), then reloads a fresh
    manager and asserts every field round-trips.
  - Two corrupt-JSON tolerance tests (mail + curator): a malformed
    state file must not crash the daemon — `load()` falls back to
    empty state. This is the contract that lets a corrupted state
    file self-heal on next save.

## Results

- **Orchestrator coverage: 0% → 85%.** Target was ≥70%.
- **State coverage bumps** (side-effect of round-trip tests, measured
  via full-suite coverage run):
  - `alfred/curator/state.py`: ~40% → ~95%.
  - `alfred/distiller/state.py`: ~30% → ~90%.
  - `alfred/janitor/state.py`: mid-range → ~85%.
  - `alfred/surveyor/state.py`: 0% → ~85%.
  - `alfred/brief/state.py`: 0% → ~95%.
  - `alfred/mail/state.py`: ~0% → ~95%.
  - `alfred/telegram/state.py`: 90% → 95%+ (was already well-covered
    by the telegram test suite).
- **Full suite: 248 → 297 tests, all passing** (+49 new tests across 7 files).
- **Overall coverage: 19% → 23%** (+4pp).

## What's still uncovered in orchestrator.py

The remaining 15% (44 lines):

- `_silence_stdio` and every `if suppress_stdout:` branch (lines 25-29,
  37, 51, 68, 87, 105, 124, 138) — these close stdout/stderr and would
  steal pytest's captured streams. Excluded by design.
- `_kill_stale_tool` race branches (lines 179-181, 186, 191-192) —
  ProcessLookupError during SIGTERM / SIGKILL delivery. Hard to stage
  deterministically without a custom kernel.
- Textual TUI dashboard path (lines 363-376) — `live_mode=True` prefers
  the Textual app; covered by the existing TUI modules.
- Edge-case logging branches in `_write_workers_json` (lines 321-322,
  333-334), `workers.json` periodic write (lines 403-404), and cleanup
  OSError catches (lines 470-471, 474-475). Logging lines not worth
  fake-engineering to exercise.
- Startup-shutdown-race print (lines 344, 346, 351) — shutdown fires
  during the inter-tool stagger sleep. Testable but requires careful
  timing against `fast_sleep`; skipped in the interest of stopping at
  70%.
- KeyboardInterrupt handler (lines 430-431) — SIGINT delivery into
  `run_all` from a worker thread doesn't reach the main-thread
  signal handler.

All of these are either pure infra (stdio redirect), race branches
unreachable without kernel-level control, or trivial logging lines.

## Outcome

The orchestrator now has a 49-test safety net covering:
- The `TOOL_RUNNERS` dispatch table and its signature partition contract.
- Auto-start gating for every optional tool.
- The `_MISSING_DEPS_EXIT = 78` no-restart contract.
- The 5-retry restart limit.
- The sentinel-file shutdown path + finally-block teardown.
- Parallel spawn + per-tool PID tracking (the 2026-04-17 orphan-process
  bug regression guard).
- Every `_run_<tool>` process entry point via `sys.modules` stubbing.
- Live-subprocess SIGTERM via `_kill_stale_tool`.

State persistence has a shallow but broad safety net: one round-trip
test per tool proves the `to_dict`/`from_dict` pair round-trips, the
atomic-write contract holds (.tmp doesn't linger), and malformed
state files self-heal rather than crash the daemon.

## Alfred Learnings

- **Pattern validated — module-attribute shim beats
  `monkeypatch.setattr(module.time.sleep, ...)`.** The obvious way to
  patch `time.sleep` inside orchestrator code is
  `monkeypatch.setattr(orchestrator.time, 'sleep', fn)`. But
  `orchestrator.time` is the SAME module object as stdlib `time` —
  which means that monkeypatch leaks into every other caller of
  `time.sleep`, including pytest fixture code itself. First attempt
  hit a 1000-deep recursion inside the `fire_sentinel_after` daemon
  thread. Fix: replace the MODULE attribute on orchestrator
  (`orchestrator.time = _FastTimeShim()`) so only orchestrator sees
  the no-op sleep — stdlib and fixture code continue to see the real
  function. Same pattern for `signal.signal` off the main thread.
  This generalises: any time a test needs to patch a stdlib function
  that the fixture itself also calls, replace the MODULE reference on
  the target code, not the function on the module.

- **Pattern validated — `sys.modules` stub-install for lazy-import
  entry points.** The orchestrator's `_run_<tool>` functions use
  lazy imports (`from alfred.curator.config import load_from_unified`
  inside the function body, not at module top). We install stub
  modules at those qualified names in `sys.modules` BEFORE calling
  the runner; the `from X import Y` resolves against our stub instead
  of triggering the real module code. Covers the whole runner body
  including logging setup and daemon dispatch without any real
  daemon spinning up. Drop-in for any lazy-import entry point.

- **Pattern validated — thread-based `run_all` observation with
  sentinel shutdown.** `run_all` is a blocking call that exits only
  on sentinel appearance or SIGTERM. To observe process state mid-run
  (e.g., "are all three PIDs alive simultaneously?") we run `run_all`
  on a daemon thread and poll the main-thread side for PID files.
  Thread-based `run_all` also requires `noop_signals` because the
  stdlib signal handler only works on the main thread.

- **Gotcha — pytest captured fd leak after `run_all`.** Several test
  runs produce `DeprecationWarning: This process (pid=X) is
  multi-threaded, use of fork() may lead to deadlocks in the child`
  because pytest's output capture installs threads that are still
  alive when `multiprocessing.Process.start()` calls `os.fork()`.
  Not a test failure, but cluttering test output. If it becomes a
  problem, switch `multiprocessing.set_start_method("spawn", force=True)`
  for the orchestrator tests. Leaving alone for now — 58 warnings on
  49 tests is noise, not signal.

- **Gotcha — `inspect.signature` is the right tool for signature
  contracts.** Testing "this function takes 3 args named
  `raw/skills_dir/suppress_stdout`" via `inspect.signature` is more
  precise than counting lambda params. It catches someone renaming
  `suppress_stdout` to `quiet` in a refactor, which would silently
  break the dispatcher's keyword-style call in `start_process`.
  Using `inspect.signature` for all dispatch-table contract guards
  going forward.

- **Gotcha — `time.monotonic()` wrapped by `_FastTimeShim`.**
  `_FastTimeShim.__getattr__` forwards everything except `sleep` to
  the real time module. So `orchestrator.time.monotonic()` still
  returns real wall-clock. Good: `last_workers_write >= 2` checks
  continue to work. Bad if you wanted simulated time — but we don't.
  Documented in the shim class docstring.

- **Anti-pattern confirmed — can't test true SIGTERM delivery to
  `run_all` from pytest.** Python only delivers signals to the main
  thread. `run_all` installs its handler on the main thread during
  `pytest` import, so raising SIGTERM at the OS level reaches pytest's
  top-of-stack handler, not `run_all`'s. Viable alternatives:
  (1) run `run_all` in a child process and raise SIGTERM there — pays
  the ~2s fork+setup cost per test; (2) drop to the smoke-test the
  task brief predicted (handler is installed, sentinel works end-to-end,
  finally block terminates children). Chose (2); covered the contract
  without paying the fork cost.

- **Corrections to agent instructions / CLAUDE.md:**
  - The `_fakes.py` sibling-to-conftest convention (noted in step b)
    now has a second example — step (c)'s orchestrator package follows
    the same pattern. Worth a sentence in `.claude/agents/builder.md`
    under a new "Testing conventions" section: "Put dataclass fakes
    and helper classes in `tests/<pkg>/_fakes.py`. Put fixtures in
    `tests/<pkg>/conftest.py`. Tests import fakes as
    `from ._fakes import FakeX`."
  - The `_FastTimeShim` pattern probably deserves a mention too, but
    only if we hit another place that needs patched sleep. Not worth
    documenting speculatively.

- **Missing knowledge — per-tool state format drift has never been
  asserted before.** Every tool defines its own `to_dict`/`from_dict`
  pair and nobody tested that the round-trip is lossless until today.
  The janitor state in particular has 10 fields including a
  `triage_ids_seen: set[str]` that gets serialised as `sorted(...)` on
  save and reloaded as `set(...)` on load. One test now guards this
  conversion. If anyone adds a new field without updating both
  methods, the round-trip assertion breaks — which is exactly the
  safety net the task brief wanted.

- **Testability tweaks required in `orchestrator.py`: zero.** The
  module was already testable at `run_all`'s public boundary (plus
  the sentinel file + TOOL_RUNNERS monkeypatch). No helper exports,
  no signature changes, no new test hooks needed.

## Flagged for follow-up

- **Textual TUI path (lines 363-376)** is uncovered because it starts
  a real Textual app. If the `tui/` package ever gets its own test
  package, the orchestrator's live_mode branch would come along for
  free.
- **Orchestrator `_kill_stale_tool` race branches** — the
  `ProcessLookupError` catches at lines 179-181, 191-192 happen when
  a stale child exits between our `kill()` and our poll. We don't have
  a good way to stage that race in a test. Acceptable; these are
  defensive guards.
- **Step-b note about `_resolve_entities` empty-vs-no-entities
  conflation** still stands; the orchestrator work didn't touch
  curator/pipeline.
- The **`DeprecationWarning: multi-threaded fork`** in test output
  isn't breaking anything, but if we ever need to eliminate warnings
  from the CI signal, `multiprocessing.set_start_method("spawn",
  force=True)` inside the orchestrator test conftest would do it.
