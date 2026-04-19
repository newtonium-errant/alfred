---
type: session
created: '2026-04-19'
name: BIT — c4 CLI + preflight 2026-04-19
description: BIT commit 4 — alfred check CLI + alfred up --preflight gate
intent: Expose the BIT aggregator to operators via the CLI
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
related:
  - '[[session/BIT — c3 per-tool (telemetry) 2026-04-19]]'
tags:
  - bit
  - health
  - cli
  - infrastructure
status: completed
---

# BIT — c4 CLI + preflight 2026-04-19

## Intent

Commit 4 of 6 — add the user-facing surfaces for the BIT system:
the `alfred check` subcommand and the `alfred up --preflight` gate.

## Work Completed

### `alfred check` subcommand
Implemented in `cmd_check(args)` in `src/alfred/cli.py`:
- Human output (default): streams rendered lines to stdout one at a
  time via `render_human(report, write=print)`. Slow probes don't
  leave the user staring at a blank terminal.
- JSON output (`--json`): batch-serialized via `render_json(report)`.
  Suitable for pipe to `jq` or for machine consumption.
- `--full` switches the aggregator to 15s-per-tool timeouts; default
  is quick mode (5s).
- `--tools alpha,bravo` restricts the probed set.
- Exit code: 0 when overall is OK/WARN/SKIP; 1 when any tool FAILs
  (plan Part 11 Q3 — WARN does not block).

### `alfred up --preflight`
New flag on the `up` subcommand:
- Runs a quick BIT sweep before spawning daemons.
- If `overall_status == FAIL`, prints the report, prints
  "Preflight FAILED", and exits 1 without spawning anything.
- WARN/OK/SKIP proceed to the normal spawn path.
- No flag = original behavior (no BIT probe).

### Tests (+10)
- `tests/health/test_cli_check.py` — 10 tests.
- cmd_check covers: human output + exit 0 on OK, exit 1 on FAIL,
  WARN → exit 0, JSON output validity, `--tools` filter, `--full`
  mode propagation.
- cmd_up --preflight covers: FAIL aborts and spawn_daemon is NOT
  called; OK passes through; WARN passes through; no-flag path
  skips BIT entirely.
- Tests patch `alfred.daemon.check_already_running` and
  `alfred.daemon.spawn_daemon` so the preflight test doesn't
  actually spawn anything.

### End-to-end smoke verified
Ran `alfred --config config.yaml check` against the real vault:
- curator, janitor, distiller, surveyor, brief, mail → all OK
  (anthropic count_tokens ok in 200-300ms; ollama + milvus
  reachable; openrouter key set)
- talker → FAIL because `${TELEGRAM_BOT_TOKEN}` placeholder
  wasn't resolved in this shell (expected — .env wasn't loaded
  for this one-off invocation; will resolve correctly when run
  via `alfred check` inside a shell where .env is active).
- Total elapsed 2.1s in quick mode.

## Outcome

- Test count: 387 → 397 (+10)
- Full suite: 397 passed (green)
- BIT surface coverage remains at 96%
- No orchestrator changes yet (c5 lands those) — daemon restart
  still not required.
- `alfred check` is now a usable operator surface; `alfred up
  --preflight` is ready for CI / startup scripts.

## Alfred Learnings

- **Pattern validated — streaming human output, batch JSON.**
  Plan Part 11 Q4 resolves to this split. Implemented via the
  renderer's dual API: `render_human(report, write=callable)`
  pushes lines through `print`, while `render_json(report)`
  returns the whole string. Clean separation.
- **Env-var placeholders in health checks have a subtle gotcha.**
  `_load_unified_config` returns the raw YAML dict WITHOUT env
  substitution — that happens in each tool's `load_from_unified`.
  Health checks read from the raw dict, so they see
  `${TELEGRAM_BOT_TOKEN}` literally when the env var isn't
  resolved *inside the YAML*. This is correct (the check should
  say "placeholder not resolved" when it's not) but there's
  polish opportunity: a shared `_resolve_env` pass before probes
  would make the talker check match curator/janitor/distiller
  (which resolve via `os.environ["ANTHROPIC_API_KEY"]` in the
  auth probe). Not fixing now — out of scope for c4.
- **Anti-pattern confirmed — hardcoded sys.exit inside handlers
  breaks unit tests that expect to continue.** The `cmd_check`
  and `cmd_up --preflight` tests had to use `pytest.raises(SystemExit)`
  to catch the exit. Acceptable in Python CLIs but worth noting
  for future handlers: `return (code, message)` scales better if
  we ever want richer composition.
