---
type: session
title: Schedule-followups c1-c4 — drift fix, janitor heartbeat, BIT env subst
date: 2026-04-21
tags: [infrastructure, scheduling, brief, janitor, transport, bit, regression]
---

## Summary

Four-commit follow-up arc to the 2026-04-20 scheduling consolidation
(`3f14226..d1b4d6c`). Three operational bugs surfaced during 2026-04-21
overnight validation got fixed; one runtime-hygiene cleanup landed in the
same arc. Shipped as `45b41a4..9a40d01`.

| Commit  | Subject                                                       |
| ------- | ------------------------------------------------------------- |
| 45b41a4 | c1 — gitignore per-instance configs and skill lockfile         |
| f40d5c7 | c2 — chunked wall-clock sleep for brief daemon                 |
| 6202fd9 | c3 — deep-sweep fix-mode heartbeat + None coercion             |
| 9a40d01 | c4 — env-substitute peer tokens in BIT handshake probe         |

## Why

Three bugs from `project_scheduling_followups.md`:

1. **Brief fired ~16 min early** on 2026-04-21 (target 06:00 ADT, actual
   05:44 ADT). 2026-04-16..20 showed similar 14–40 min early fires.
   Same daemon process; no restart between sleep computation and wake.
   Hypothesis: a single `asyncio.sleep` over a ~10h horizon drifts on
   WSL2 when the host suspends/resumes or NTP adjusts the clock — the
   monotonic clock and wall clock fall out of sync.
2. **Janitor deep sweeps reported `fixed=None, deleted=None`**. State
   showed populated `issues_found` but null counters, so operators
   could not tell whether the LLM-fix path engaged at all (versus
   engaged-and-fixed-zero).
3. **`alfred check --peer kal-le` reported FAIL auth-rejected** even
   though direct curl with the env-substituted token returned 200 OK.
   Root cause: `_run_peer_probes` read `raw["transport"]["peers"][...]`
   directly, leaking the literal `${ALFRED_KALLE_PEER_TOKEN}` text into
   the bearer header.

## What changed

### c1 — `.gitignore` (1 file, +6 lines)

- `config.*.yaml` so `config.kalle.yaml` (and future `config.stayc.yaml`)
  share `config.yaml`'s hygiene. Negation `!config.*.yaml.example` keeps
  example files trackable.
- `.claude/scheduled_tasks.lock` — runtime lockfile written by the
  `/schedule` skill that was leaking into `git status` every session.

### c2 — Bug 1: brief sleep (3 files, +318/-3)

- New `alfred.common.schedule.sleep_until(target, *, chunk_seconds=60.0,
  sleeper=None, clock=None)` — async helper that sleeps in capped
  chunks, re-reading the wall clock between each. Drift bounded to one
  chunk (default 60s) regardless of monotonic skew. Injectable
  sleeper/clock for tests.
- Brief daemon swaps its single long `asyncio.sleep` for `sleep_until`
  and logs `intended_seconds`/`actual_seconds`/`drift_seconds` after
  wake-up — future drift becomes observable instead of having to be
  reverse-engineered from operational evidence.
- 9 new tests for `sleep_until` with a `_FakeClock` driving deterministic
  fast/slow/perfect-clock scenarios.

### c3 — Bug 2: janitor heartbeat + coercion (3 files, +176/-8)

- Janitor `run_watch` emits `daemon.deep_sweep_fix_mode` in **both**
  branches of the deep-sweep gate. `fix_mode=True` on the proceed path
  with diagnostic counts; `fix_mode=False` on the skip path with reason.
  One `grep daemon.deep_sweep_fix_mode` answers "did fix-mode engage on
  date X?" — previously inferred from absence/presence of downstream
  `sweep.agent_invoke` events.
- `SweepResult.from_dict` switches counter coercion from
  `d.get(key, 0)` to `d.get(key) or 0`. The old form returns `None` if
  the key is present-but-null (e.g. half-written state file); the new
  form collapses null to zero. Defense in depth — daemon writes ints
  already.
- 5 new tests covering `from_dict` coercion, round-trip, integer
  preservation, and a source-level grep for both heartbeat literals.

### c4 — Bug 3: BIT env substitution (2 files, +119/-11)

- `_run_peer_probes` now routes through
  `alfred.transport.config.load_from_unified(raw)` — `_substitute_env`
  resolves `${VAR}` placeholders before `PeerEntry` dataclasses get
  built, so the bearer token in `Authorization: Bearer …` is the real
  value, not the literal placeholder text.
- `_check_peer_reachable` and `_check_peer_handshake` widened to accept
  `PeerEntry | dict` via a small `_peer_attr` helper. Production gets
  env-substituted dataclasses; existing dict-passing test callsites keep
  working without churn.
- 2 new regression tests using the existing `peer_server` aiohttp
  fixture — happy path with env var set, FAIL path with env var unset
  (literal placeholder text rejected by the peer).

## Design decisions

- **One arc, not three** — bundled per project-followups note. The four
  commits are independent (any order leaves the tree green) but ship
  as a single Schedule-followups arc for readability and rollback
  symmetry with the original Schedule c1-c5 consolidation.
- **`sleep_until` lives in `alfred.common.schedule`** next to
  `compute_next_fire`. The two are a pair: compute the target, sleep to
  it. Both pure (compute) or injection-friendly (sleep) so tests don't
  need real time.
- **Don't change `compute_next_fire`** even though it was a hypothesis
  for Bug 1. The chunked re-check defends against any drift source
  including a bug in compute_next_fire — defense beats fix when
  observability of the original symptom is poor.
- **`value or 0` instead of `value if value is not None else 0`** in
  `SweepResult.from_dict`. The shorter form collapses any falsy value
  (None, 0, empty dict) to its own type's zero, which is fine for
  counter fields. The 0-roundtrip test pins this behavior.
- **Surgical staging** — `vault/process/Alfred BIT 2026-04-20.md` had
  pre-existing janitor frontmatter drift in the working tree at session
  start. Per CLAUDE.md surgical-staging rule, that file was kept out of
  this arc; deferred for a separate decision.

## Alfred Learnings

- **Gotcha — `asyncio.sleep` over long horizons drifts on WSL2**.
  Empirically observed: 14–40 min early fires on overnight 10h sleeps
  during 2026-04-16..21. The brief daemon was the canary; `bit/daemon.py`
  has the same shape (long `asyncio.sleep` with a clock-aligned target)
  and is a candidate for `sleep_until` adoption in a follow-up arc.
- **Test-fake gotcha — Zeno's paradox via floating-point underflow**.
  The first cut of `_FakeClock` for `sleep_until` modelled
  `wall_advance = requested * factor`. With `factor < 1` the production
  code's tail-shortening `min(remaining, chunk_seconds)` made `remaining`
  decay geometrically toward subnormal floats; arithmetic asymptoted at
  ~5e-324 and the loop ran forever. Pegged a python process at 95% CPU
  + 23 GB RSS, which the WSL2 host responded to by killing the entire
  Ubuntu instance with exit code 1. Crashed the Claude Code session
  twice in this work block before the cause was isolated. Fix: add a
  1 ms minimum advance per fake `sleep` call to model the real asyncio
  event-loop tick floor. Also surfaced as
  `feedback_pytest_wsl_hang.md` — never run `pytest` without a
  `timeout` shell prefix going forward.
- **Pattern validated — operator-grep heartbeat events**. The Bug 2
  observability fix replaces "infer fix-mode engagement from absence
  of downstream events" with "grep one event name." Same pattern would
  pay off elsewhere where a gate has both proceed/skip branches and
  operators currently have to reason from absence (e.g. surveyor's
  silent-writer issue, talker capture-extract gate).
- **Pattern validated — `value or 0` defense in depth**. Pragmatic
  guard for state files that may have been written by an older daemon
  version with different shape. The cost is collapsing literal `False`
  to `0` for fields typed as bool — only an issue if a counter field
  is ever migrated to a bool, which is unlikely.

## Next

Active follow-ups from `project_scheduling_followups.md` are now closed.
Open candidates surfaced during this arc:

- **`bit/daemon.py` long-horizon `asyncio.sleep` callsites** — same
  drift exposure as Bug 1. Adopt `sleep_until` in a future polish arc.
- **`vault/process/Alfred BIT 2026-04-20.md`** — pre-existing janitor
  drift in the working tree, deferred from this arc. Decide
  commit/discard separately.
- **Untracked vault notes** (~30 files, mostly 2026-04-20/21 session
  and process notes from the marathon) — separate housekeeping pass.
