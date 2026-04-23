---
type: session
title: Idle-tick heartbeat propagation to curator, janitor, distiller, surveyor, instructor, mail
date: 2026-04-22
tags: [observability, heartbeat, intentionally-left-blank, daemons, cross-cutting]
---

## Summary

Single commit `7cc89e5` propagates the talker's `idle_tick` pattern (shipped earlier 2026-04-22 in `5a26d13`) across all six remaining long-running daemons: curator, janitor, distiller, surveyor, instructor, mail. Shared module extracted to `src/alfred/common/heartbeat.py`. Each daemon emits `<daemon>.idle_tick interval_seconds=60 events_in_window=N` every 60 seconds regardless of traffic. Validated live post-restart — all 7 daemons now heartbeating cleanly.

## Why

The "intentionally left blank" pattern (`feedback_intentionally_left_blank.md`) says: **silence is ambiguous; emit a positive "ran, nothing to do" signal so observers can distinguish idle from broken**. Shipped on talker first as validation; propagation was filed as the obvious follow-up if the pattern proved out.

The talker version proved out immediately — one misdiagnosis cycle earlier this same day ("talker logging is broken!" → actually "no traffic since 03:36") is exactly what the pattern prevents going forward. No reason to delay propagation across the daemon set.

## What changed (commit `7cc89e5`)

**Shared module extracted:**
- **`src/alfred/common/heartbeat.py`** (new) — generic `Heartbeat(daemon_name=..., ...)` class + `run_in_thread()` helper for sync daemons (mail uses a sync HTTPServer thread, others use asyncio).
- **`src/alfred/telegram/heartbeat.py`** (existing) — now a thin wrapper preserving its legacy `inbound_in_window` field name for talker backward compatibility. Talker behavior identical to the original `5a26d13` ship; no regression surface.

**Per-daemon wire-ins** (counter semantic + callsite, verified in commit):

| Daemon | Counter = "one event" when | Callsite |
| --- | --- | --- |
| curator | one inbox file processed | `src/alfred/curator/daemon.py:46` + `_process_file` end |
| janitor | one issue fixed OR deleted | `src/alfred/janitor/daemon.py:46` — loop `record_event()` `(files_fixed + files_deleted)` times after `sweep.complete` |
| distiller | one learn record created | `src/alfred/distiller/daemon.py:54` — per `state.add_log_entry` call in both pipeline and legacy paths |
| surveyor | one record re-embedded in a tick | `src/alfred/surveyor/daemon.py:33` — per record returned from `embedder.process_diff` |
| instructor | one instruction executed (status ∈ {done, dry_run}; errors don't count) | `src/alfred/instructor/daemon.py:51` |
| mail | one email fetched OR one webhook received | `src/alfred/mail/webhook.py:18` (webhook) + `src/alfred/mail/fetcher.py` (fetch) |

**Config additions:** each daemon's `<daemon>:` block in `config.yaml.example` gets an `idle_tick: {enabled, interval_seconds}` sub-block. Defaulted-on, 60-second interval. Absent block in user `config.yaml` resolves to `(True, 60)` via the dataclass defaults — so the restart picks up the heartbeats without requiring explicit config edits.

**Out of scope on purpose:**
- **brief, bit, daily_sync** — clock-aligned scheduled fires. The wake event itself is their natural positive signal; a 60s heartbeat during 23 hours of sleep would generate ~1,380 noise events for one signal event. Skipped.

**Tests added:** ~50 tests across 7 test files (one per daemon + updated talker tests). All pass individually under `timeout 30`, RSS stayed under 1 GB throughout.

## Live validation (2026-04-22 restart at 02:17 UTC)

Each daemon emitted `daemon.heartbeat_started interval_seconds=60` on startup (or `<namespace>.daemon.heartbeat_started` for talker/instructor/webhook whose loggers are namespaced). First ticks fired 60s after each startup:

| Daemon | heartbeat_started | first idle_tick |
| --- | --- | --- |
| curator | 02:17:01 | 02:18:00 |
| janitor | 02:17:12 | 02:18:13 |
| distiller | 02:17:21 | 02:18:20 |
| surveyor | 02:17:35 | 02:18:34 |
| mail | 02:17:41 | 02:18:40 |
| talker | 02:18:12 | 02:19:11 |
| instructor | 02:18:20 | 02:19:20 |

All `events_in_window=0` on first ticks (no traffic), which IS the load-bearing case — confirms the "intentionally left blank" signal works correctly on empty windows.

## Design decisions

- **Shared module in `src/alfred/common/`** — matches the pattern established by `src/alfred/common/schedule.py` (shared `ScheduleConfig` + `compute_next_fire` + `sleep_until`). One implementation, per-daemon parameterization.
- **Talker keeps its legacy field name (`inbound_in_window`)** via the thin wrapper, rather than forcing a migration to the new `events_in_window` name. Backward-compatible; any existing dashboards/alerts built on the talker's specific field keep working.
- **Per-daemon counter semantics are explicit**, not auto-detected — each daemon's builder made an intentional call about what "meaningful work" looks like. Janitor picked fixed/deleted (not issues-found, which is noisy when nothing's actually broken). Instructor picked done/dry_run (not errors — errors are failure, not work). These semantics are load-bearing: they make zero-event windows meaningful.
- **`run_in_thread()` helper for sync daemons** — mail's webhook uses a blocking `HTTPServer`; wrapping the heartbeat in a daemon thread rather than asyncio is cleaner than refactoring mail to async.
- **Defaulted on with sensible defaults** — fresh installs get heartbeats without opt-in.

## Alfred Learnings

- **Pattern propagated fast when validated.** Talker heartbeat shipped at ~17:00 UTC, one misdiagnosis cycle later at ~19:00 confirmed the value, propagation to the other 6 daemons shipped at ~21:00 UTC as a single commit. Total: ~4 hours from "codified the rule" to "pattern operational codebase-wide." Compare to: rule codified in a memo + deferred = next session rediscovers the pattern cold. Speed of propagation-when-validated matters.
- **Gotcha confirmed — mail-type daemons duplicate-emit some events.** Mail's webhook + fetcher share the orchestrator's combined log and both initialize, so `webhook.heartbeat_started` and `mail.idle_tick` appear twice in `data/alfred.log`. Cosmetic, not a bug. Worth knowing for operator grep scripting.
- **Pattern validated — dataclass default = `(True, N)` means "absent block opts in."** Used here (idle_tick defaults enabled with 60s interval), used in email_classifier (defaults) and daily_sync (defaults). Fresh installs getting the feature without explicit opt-in is a quiet but important UX win for "recommended defaults" features.
- **Pattern validated — shared-module extraction at the second use, not the first.** Talker's heartbeat was in `src/alfred/telegram/heartbeat.py` because it was the only consumer. When the second consumer (curator) appeared, the extraction to `src/alfred/common/heartbeat.py` + backward-compat wrapper was a single commit. Not a refactor debt; a natural evolution point. Don't extract before the second consumer exists; don't resist extraction when the second consumer arrives.

## Next

- Observability gap closed for the daemon set. Silence in any `data/<daemon>.log` now meaningfully diagnoses broken-vs-idle.
- No further propagation targets — brief/bit/daily_sync deliberately skipped (clock-aligned fires are their own signal), transport runs inside talker (covered), orchestrator itself doesn't need a heartbeat (its aliveness is implied by any daemon's aliveness).
- Email arc c3-c6 still queued, gated on calibration validation.
- STAY-C Phase 1 still the next ratified big arc.
- V.E.R.A. still a roadmap-reorder candidate if RRTS calendar in brief becomes higher priority.
