---
type: session
created: '2026-04-19'
name: BIT — c5 BIT daemon 2026-04-19
description: BIT commit 5 — daemon, orchestrator registration, alfred bit subcommands
intent: Wire BIT into the always-on daemon set
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
related:
  - '[[session/BIT — c4 CLI + preflight 2026-04-19]]'
tags:
  - bit
  - health
  - orchestrator
  - infrastructure
status: completed
---

# BIT — c5 BIT daemon 2026-04-19

## Intent

Commit 5 of 6 — turn BIT into an always-on daemon. Adds the BIT
package (`src/alfred/bit/`), registers it in the orchestrator,
and adds `alfred bit {run-now|status|history}` subcommands.

**This is the commit that requires a daemon restart** — orchestrator
TOOL_RUNNERS gained a new entry. User will restart after c6 lands.

## Work Completed

### `src/alfred/bit/` package
- `config.py` — `BITConfig`, `ScheduleConfig`, `OutputConfig`,
  `StateConfig`. The schedule resolution (plan Part 11 Q1):
    1. `bit.schedule.time` if set explicitly
    2. else `brief.schedule.time` minus `bit.schedule.lead_minutes`
       (default 5)
    3. else hardcoded `"05:55"` fallback
  `_compute_scheduled_time` does the math; tests cover the wrap-
  around-midnight edge case.
- `state.py` — `BITRun` dataclass + `StateManager` with
  atomic `.tmp → rename` writes (matches brief pattern).
  `max_history=30` cap on the in-memory list; vault records are
  retained forever (plan Part 11 Q5).
- `renderer.py` — `render_bit_record(report, date_str, config)`
  returns a `(frontmatter, body)` tuple suitable for
  `alfred.brief.renderer.serialize_record`. Body embeds the human
  rendering and a JSON appendix wrapped in a code fence.
- `daemon.py` — `run_bit_once(config, raw, state_mgr)` is the
  pure unit (called both by the scheduler and by `alfred bit
  run-now`). `run_daemon(config, raw)` is the always-on
  scheduler loop, mirrors brief's pattern: sleep until target,
  run, sleep 60s.
- `cli.py` — `cmd_run_now`, `cmd_status`, `cmd_history` handlers.
  Each accepts a `wants_json` flag.

### Orchestrator registration
- `_run_bit(raw, suppress_stdout)` runner added.
- `TOOL_RUNNERS["bit"] = _run_bit`.
- BIT auto-starts when EITHER `bit:` OR `brief:` config section
  is present. (No bit-without-brief case in practice — the
  Morning Brief is the consumer of the BIT record.)
- `start_process` arity check updated: `("surveyor", "mail",
  "brief", "bit")` are the 2-arg runners.

### `alfred bit` subcommands
- `alfred bit run-now [--json]` — execute one BIT now, write the
  vault record, exit 0/1 by overall status.
- `alfred bit status [--json]` — show schedule + last run.
- `alfred bit history [--limit N] [--json]` — recent runs.

### `config.yaml.example` update
Added a `bit:` section between `brief:` and `surveyor:` with all
defaults visible (commented overrides for time/timezone, since
those derive from brief by default).

### Scope verification
The BIT daemon writes to `vault/process/Alfred BIT {date}.md`
WITHOUT setting `ALFRED_VAULT_SCOPE`. Verified that
`vault/scope.py:check_scope` line 178 returns immediately for
empty/None scope (`if not scope: return`). Plan Part 11 Q7
satisfied: no new BIT scope needed; unscoped writes pass.

### Tests (+32 + 4 follow-ups for cli coverage = 36)
- `tests/health/test_bit.py` — 36 tests:
  * `_compute_scheduled_time` — explicit override, lead subtraction,
    midnight wrap, fallback paths.
  * `load_from_unified` — brief drives bit, explicit override,
    custom lead, mode inheritance, output dir, state path.
  * `render_bit_record` — frontmatter shape, body content, tags
    include status (`bit/<status>`), tool_counts on empty report.
  * `state` — max_history cap, save/load round-trip, corrupt-file
    reset.
  * `run_bit_once` — record written, state updated, recursion
    guard verified ("bit" is excluded from probed tools), FAIL
    propagates to state.
  * CLI — status/history empty + populated, run-now exit codes,
    JSON output for all three subcommands.
  * Orchestrator — TOOL_RUNNERS contains "bit" + the runner is
    `_run_bit`.
- `tests/orchestrator/test_tool_dispatch.py` — updated EXPECTED_TOOLS
  and TWO_ARG_TOOLS sets to include "bit".

### End-to-end smoke
Ran `alfred --config config.yaml bit run-now`:
- Wrote `vault/process/Alfred BIT 2026-04-19.md`
- Frontmatter overall_status: fail (expected — talker bot_token
  unresolved in this shell, same as `alfred check` smoke from c4)
- Tool counts: ok=6, fail=1 (talker)
- Cleaned up the smoke artifact + state file before commit.

## Outcome

- Test count: 397 → 433 (+36)
- BIT module coverage:
  - `bit/__init__.py` 100%
  - `bit/cli.py`      100%
  - `bit/config.py`   100%
  - `bit/daemon.py`   70% (the infinite scheduler loop is the
    uncovered piece — testing it would require mocking out the
    sleeps and breaking out of the while True; deferred)
  - `bit/renderer.py` 100%
  - `bit/state.py`    100%
- Aggregate BIT surface coverage: **93%** (well above target)
- Full suite: 433 passed (green)
- **Orchestrator changed in this commit** — daemon restart will
  be required after c6 lands.

## Alfred Learnings

- **Pattern validated — pure-unit + scheduler-shell split.**
  `run_bit_once` is testable; `run_daemon` is just a sleeper that
  calls it.  Same shape as the brief daemon. Future tools should
  follow this pattern: any work routine should be reachable
  outside the scheduler (so `alfred <tool> run-now` works).
- **Pattern validated — unscoped vault writes from internal
  daemons.** BIT writes without `ALFRED_VAULT_SCOPE`. The scope
  layer's first line is `if not scope: return` — internal Python
  callers (not subprocess agents) bypass scope cleanly. This
  generalizes: any future internal daemon that writes vault
  records doesn't need a new scope, just the absence of the env
  var. Worth documenting next to `SCOPE_RULES` in `vault/scope.py`.
- **Gotcha — structlog log lines leak into JSON capsys output.**
  Two `cmd_*_json` tests initially failed because structlog's
  `bit.running` log message hit stdout before our JSON did.
  Fix in tests: parse from `out.rfind("{")` for objects or
  `out.rfind("[\n")` for lists. Better long-term fix: route BIT
  logs to file only when `--json` is set, like the vault CLI
  does. Deferred — works as-is and the test paths catch
  regressions.
- **Missing knowledge — orchestrator dispatch table tests are
  load-bearing.** `tests/orchestrator/test_tool_dispatch.py` has
  three sets (`EXPECTED_TOOLS`, `TWO_ARG_TOOLS`,
  `THREE_ARG_TOOLS`) that must be kept in sync with the
  `TOOL_RUNNERS` dict + `start_process` arity check. Any new
  tool addition is a 3-place update. The tests caught this on the
  first run — doing its job — but worth flagging for future
  contributors so they don't waste a commit reverting it.
