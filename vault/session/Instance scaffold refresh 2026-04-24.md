---
type: session
status: completed
name: Instance scaffold refresh
created: 2026-04-24
description: Refresh the `alfred instance new` scaffold with the per-instance config blocks that accumulated since KAL-LE launched. Rename `config.kalle.yaml.example` → `config.instance.yaml.example`. Unblocks STAY-C (and all future instances) from the "manual derivation of per-instance blocks" trap.
intent: Make the universal instance scaffold a superset with commented-out optional blocks so operators opt in to what they need; stop requiring each new instance to re-derive email_classifier + daily_sync + brief.peer_digests manually.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Heartbeat coverage + email backfill + upstream refresh 2026-04-22]]'
tags:
- instance-scaffold
- multi-instance
- stayc
- kalle
---

# Instance scaffold refresh

## Intent

`alfred instance new` was shipped 2026-04-20 as KAL-LE c8 and scaffolded a starter `config.{instance}.yaml` covering core per-instance config (telegram bot, transport peer, BIT, vault, agent backend). Since then we've added 5+ per-instance blocks (email_classifier, daily_sync, brief_digest_push, brief.peer_digests, allowed_clients extension) that the scaffold didn't know about. Andrew flagged this as a pre-STAY-C blocker 2026-04-23.

## Work Completed

Three commits on master:

- `5b23267` — c1: add commented-out optional blocks to scaffold. Added `email_classifier`, `daily_sync`, `brief` (with `peer_digests` sub-block only, not full Salem weather/schedule block) as commented YAML with inline guidance on when to uncomment. Updated `allowed_clients` comment to mention adding `cli` + `daily_sync`.
- `cf49efe` — c2: rename `config.kalle.yaml.example` → `config.instance.yaml.example`. Filename was misleading (it's the universal scaffold, not KAL-LE-specific). `cmd_instance` probes both filenames for backward compat. `tests/test_instance_cli.py` updated for dual-probe paths (7 hard-coded references).
- `98c0fe6` — c3: refresh `cmd_instance` docstring + next-steps guidance. Explicit "Uncomment what the new instance needs" framing in header comment.

## Validation

Builder ran `alfred instance new validate-me --force` against the renamed template in a tmp dir; output parsed cleanly as YAML and substituted paths + token names correctly. Cleanup deleted the test instance dir afterward. No daemons restarted.

## Outcome

STAY-C launch is no longer blocked on manual config derivation. Scaffold handles subordinate (like KAL-LE), primary-adjacent (hybrid with email), and primary-shape (like Salem minus orchestration) instances with optional blocks the operator uncomments per need.

**Context shift mid-session:** After this arc shipped, Andrew opened a broader architectural question (distiller/janitor foundation rebuild) that paused STAY-C and V.E.R.A. launches behind rebuild Week 2 validation. So the scaffold is ready but not immediately exercised — see `Distiller rebuild Week 1 MVP 2026-04-24`.

## Alfred Learnings

- **Pattern validated**: dual-probe backward compat on renames is cheap and avoids breaking in-place upgrades. 7 test references mechanically updated; zero runtime risk.
- **Pattern validated**: superset scaffold with commented optional blocks mirrors how `config.yaml.example` documents all possible blocks. Operators see what's available; uncomment what they need. No "which blocks do I need to remember" cognitive load.
- **Anti-pattern avoided**: hard-break rename without a legacy fallback would have broken any operator with the old filename cached locally or referenced in scripts. Dual-probe costs ~5 lines.
