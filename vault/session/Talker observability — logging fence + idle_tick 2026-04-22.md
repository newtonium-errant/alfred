---
type: session
title: Talker observability — logging fence + idle_tick heartbeat
date: 2026-04-22
tags: [talker, telegram, observability, logging, heartbeat, intentionally-left-blank]
---

## Summary

Two-commit observability arc on the talker daemon. `59938af` adds a regression fence + tests around `setup_logging` to prevent the surveyor-class silent-writer regression from creeping back. `5a26d13` adds a 60-second `talker.idle_tick` heartbeat so silence is distinguishable from broken — the "intentionally left blank" pattern Andrew named.

Plus two new memos: `feedback_intentionally_left_blank.md` (codified rule for future builders), `project_capture_burst_replay_edge.md` (open question on async capture-batch dispatch under burst-replay conditions).

## Why

A misdiagnosis cascade revealed an underlying gap. While investigating whether the 2026-04-22 03:13 capture session (`ec1db330`) ever produced a `## Structured Summary` block, I observed that talker.log had zero structured events for 2026-04-22 — and concluded "talker structured logging is broken!" A builder spawn verified that the `setup_logging` was already correct (byte-for-byte equivalent to surveyor's fixed pattern); the actual explanation was "no Telegram traffic since 03:36 UTC."

Andrew's correction caught the meta-pattern: **silence is ambiguous**. A daemon that emits zero log events can mean any of: didn't run, ran with nothing to do, or ran and crashed silently. Without a positive idle signal, an operator (or a misdiagnosing team-lead) can't distinguish.

The fix has two pieces:

1. **Fence the regression** — even though `setup_logging` is currently correct, no test was pinning that contract. A future "simplification" to `PrintLoggerFactory` would have re-introduced the surveyor-class regression with zero CI signal. `59938af` adds three regression tests modelled on `test_surveyor_logging.py`.
2. **Emit positive idle signal** — `5a26d13` introduces `talker.idle_tick interval_seconds=60 inbound_in_window=N` every 60 seconds, regardless of traffic. Zero events for a window = "intentionally left blank, polling normally." Non-zero = activity, look at adjacent log lines.

## What changed

### `59938af` — Talker structured-logging fence

- `src/alfred/telegram/utils.py:12-43` — expanded docstring on `setup_logging` documenting the silent-writer hazard, the orchestrator's `_silence_stdio` interaction, and why `cache_logger_on_first_use=True` is safe given module-level loggers in `bot.py` and `session.py`. Code unchanged.
- `tests/test_talker_logging.py` (new) — 3 regression tests:
  - Events land in the FileHandler
  - Round-trip works after a simulated `_silence_stdio` redirect
  - Module-level logger picks up the factory after `setup_logging`

### `5a26d13` — Talker idle_tick heartbeat

- `src/alfred/telegram/heartbeat.py` (new) — module-level `int` counter, `record_inbound()`, `tick()` (emits + resets), async `run()` loop
- `src/alfred/telegram/bot.py` — calls `record_inbound()` after each `talker.bot.inbound` log (text + voice paths)
- `src/alfred/telegram/config.py` — new `IdleTickConfig` dataclass (`enabled`, `interval_seconds`), defaulted-on, wired into `TalkerConfig` and `load_from_unified`. Absent block = `(True, 60)`.
- `src/alfred/telegram/daemon.py` — heartbeat task spawned alongside the existing sweeper, cleaned up in the same teardown loop
- `config.yaml.example` — `telegram.idle_tick.{enabled,interval_seconds}` block
- `tests/telegram/test_idle_tick.py` (new) — 7 tests covering counter increment, tick emit + reset, disabled path, zero-traffic tick (load-bearing case), concurrent increments

### Memos filed (not commits)

- `feedback_intentionally_left_blank.md` — codifies the rule for future builders: when designing any process/section/section-provider, emit a positive "ran, nothing to do" signal rather than silent absence. Includes catalog of where this pattern is already shipped (brief "No upcoming events.", Daily Sync "No items today", janitor `deep_sweep_fix_mode` heartbeat) and the anti-pattern callout.
- `project_capture_burst_replay_edge.md` — open: when daemon comes up after downtime and Telegram replays queued voice notes in a burst, capture session opens + processes silently + closes via `/end` in <30s. Async `capture_batch.py` may not produce its `## Structured Summary` output before the close path finalizes the session record. Andrew testing naturally; surface at next session start if not validated.

## Design decisions

- **60-second cadence, not 1Hz.** 1Hz = 86,400 events/day per daemon = ~17 MB/day = signal-to-noise wreck. 60s = 1,440 events/day = ~290 KB/day = negligible disk + greppable. Matches operator inspection cadence (no one needs 1-second precision to answer "is this daemon alive?").
- **`inbound_in_window`, not `polling_count`.** The polling count would be ~constant (60s ÷ getUpdates interval), so it adds no signal. Inbound count gives "alive AND rough activity rate" in one line.
- **Counter is plain `int`, no lock.** Single asyncio loop = no thread safety needed.
- **Defaulted on with sensible defaults.** Absent config block = `(True, 60)`. New installs get the heartbeat without explicit opt-in.
- **Talker-only for now.** The pattern is worth propagating to curator/janitor/distiller/surveyor/instructor once it proves out on talker, but each daemon's wire-in is small enough that it's better to validate the talker version for a few days before propagating.
- **Logging fence is documentation + tests, not a behavior change.** The talker setup was already correct. We're hardening the contract against future drift, not fixing a current bug.

## Alfred Learnings

- **Anti-pattern confirmed — silence as default.** When a section/process/event provider has nothing to report, the lazy default is to skip emission entirely. That breaks observability. Codified as `feedback_intentionally_left_blank.md`. Every builder spec going forward should include "emit positive idle signals on the no-activity branch" in its acceptance criteria.
- **Pattern validated — fence regressions even when nothing's broken.** The talker `setup_logging` was already correct, but no test pinned it. The fence-then-fix pattern (when a regression is suspected even if not confirmed) has zero downside if there's no bug — and significant upside (CI signal) if a future refactor breaks the contract. Same shape as the surveyor regression test added in `80b3344` despite the bug already being fixed.
- **Pattern validated — operator-grep events at the natural cadence of work.** The brief logs once per ~24h sleep. Janitor's deep_sweep_fix_mode logs once per gate. Idle ticks log once per 60s window. Each cadence matches the question being asked. Sub-second cadence would only be appropriate for a daemon doing sub-second work; talker's polling is sub-second but its operator-question is "alive within the last minute," so 60s wins.
- **Gotcha — daemon misdiagnosis is faster than memory verification.** I jumped to "talker logging is broken" without first verifying recent traffic. Per `feedback_verify_stale_memos.md` (filed earlier this session), claims about broken behavior should be verified by a fresh repro before specing a fix. The talker case extends the rule: even **fresh observations** can be misdiagnosed if you don't check the *positive* baseline (was there traffic to log?). The fix here was "make the positive baseline visible always" — i.e., the heartbeat itself guards against this misdiagnosis class going forward.

## Pattern propagation candidate (out of scope)

`feedback_intentionally_left_blank.md` is generic but only the talker has it implemented. Curator, janitor, distiller, surveyor, instructor are all still silent on idle. If the talker heartbeat proves useful for a few days, the natural follow-up is a small arc adding the same pattern to the other daemons — single shared module, one wire-in per daemon, ~1-2 hours total.

## Next

- Talker heartbeat is live. Watch `data/talker.log` for `talker.idle_tick` lines (one per minute) — silence at this point really does mean broken.
- Capture burst-replay edge case is queued for Andrew's natural validation; surface at next session start if not validated.
- Pattern propagation to other daemons is the obvious follow-up if the talker version proves out.
- Otherwise: bigger arcs queued (STAY-C Phase 1, email c3-c6 gated on calibration), nothing operational pressing.
