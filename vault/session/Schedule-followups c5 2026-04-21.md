---
type: session
title: Schedule-followups c5 — bit/daemon.py sleep_until adoption
date: 2026-04-21
tags: [infrastructure, scheduling, bit, regression]
---

## Summary

Closes the open follow-up flagged in `Schedule-followups c1-c4 2026-04-21.md`:
adopt `sleep_until` in `src/alfred/bit/daemon.py` to remove the same
overnight-drift exposure that c2 fixed in the brief daemon. Single
commit `bc50a5e`.

## Why

BIT runs ~5 min before the brief on the consolidated overnight schedule.
A drifted-early BIT pollutes the brief's health section with a stale
snapshot — same root cause as Bug 1 (long `asyncio.sleep` over 24h
horizon drifts on WSL2 due to host suspend/resume + NTP). Builder agent
+ code-reviewer both flagged this during the c2 review. Pattern was hot
in cache; cheaper to ship same-cycle than to rediscover later.

## What changed

- `src/alfred/bit/daemon.py`
  - Long-horizon overnight `await asyncio.sleep(sleep_seconds)` → `await sleep_until(target)` with `intended_seconds` / `actual_seconds` / `drift_seconds` log on wake (mirrors `brief.daemon.woke` shape from c2).
  - `_next_run_time` reduced to a thin wrapper over `compute_next_fire` so the existing `TestNextRunTime` test in `tests/health/test_bit.py` still passes; the daemon loop now calls `compute_next_fire` directly.
  - 60s double-fire guard at the bottom of the loop kept as plain `asyncio.sleep(60)` — short-horizon, no drift exposure.

## Design decisions

- **No new BIT-specific test added.** `tests/test_schedule.py` already has
  thorough drift-bound coverage on the shared primitive (added in c2).
  `tests/health/test_bit.py` doesn't currently exercise the daemon loop
  end-to-end — only `_next_run_time` and `run_bit_once`. Per "no
  speculative test infrastructure" rule, did not invent one. If desired
  later, a future test could mock `sleeper`/`clock` to assert
  `bit.daemon.woke` is emitted, mirroring what's done for `sleep_until`
  in isolation.
- **`_next_run_time` kept as a wrapper** rather than deleted. Existing
  `TestNextRunTime` covers it; deleting would require either deleting
  that test (loses regression coverage) or porting it to call
  `compute_next_fire` directly (churn for no benefit). The thin wrapper
  is a one-liner — keeping it costs nothing.

## Alfred Learnings

- **Pattern validated — same-cycle follow-up beats deferred follow-up
  when context is hot.** The reviewer flagged `bit/daemon.py` during c2
  code review; shipping it the same session was ~30 min total. Deferring
  would have cost a full re-investigation cycle when the next session
  rediscovered "what was that pattern again?" Confirms the
  `feedback_multi_instance_usability_pass.md` principle (polish
  per-step, don't batch to end) generalizes beyond multi-instance work.
- **Pattern validated — operator-visible drift logging.** Both c2 and
  c5 emit `intended_seconds` / `actual_seconds` / `drift_seconds` on
  every wake. If WSL2 ever drifts again, the symptoms are now in the
  daemon log directly, not buried in operator pattern-matching against
  fire times. Worth adopting for any future long-horizon sleep callsite
  (none currently identified, but the pattern is now standard).

## Next

All three Schedule-followups (Bug 1 brief drift, Bug 2 janitor heartbeat,
Bug 3 BIT env subst) plus this c5 BIT-side drift adoption are closed.
The Schedule-followups arc is complete; next priority is per the
roadmap split — STAY-C Phase 1 on Axis 1, or Email surfacing on Axis 2.
