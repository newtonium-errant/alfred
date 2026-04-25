---
type: session
status: completed
name: Surveyor cascade safety fix
created: 2026-04-24
description: Ship c1 (membership-stability gate) + c2 (Ollama rate cap) to close the WSL-OOM cascade vector that crashed Alfred at 2026-04-23 22:29 after an audit-sweep write to person/Andrew Newton.md.
intent: Prevent unbounded surveyor re-labeling bursts when a single high-fan-out vault write churns cluster IDs without changing actual cluster membership.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Heartbeat coverage + email backfill + upstream refresh 2026-04-22]]'
tags:
- surveyor
- safety
- wsl
- cascade
- rebuild-adjacent
---

# Surveyor cascade safety fix

## Intent

2026-04-23 22:29 WSL crashed. Root cause (reconstructed from daemon log synchronized-stop analysis, not pytest as initially suspected): `alfred audit infer-marker --apply` wrote 19 attribution markers to `person/Andrew Newton.md` → surveyor woke up → HDBSCAN + Leiden re-ran on full corpus → cluster IDs renumbered non-deterministically → `_detect_changes` flagged every renumbered cluster → Ollama labeler fired on each → `writer.tags_unchanged` dominated (labels were identical) → 80 seconds of wasted LLM calls → RAM spike → WSL OOM-killed. Every daemon log stops at 01:29:10Z (22:29 ADT).

The architectural diagnosis: cluster-ID diffs are not equivalent to cluster-membership diffs. HDBSCAN + Leiden renumber IDs between runs even when the actual document groupings are stable. The legacy pipeline treated ID-diff as "changed" and paid the LLM cost unconditionally.

## Work Completed

Architect plan then builder execution. Two commits on master:

- `eed302b` — Surveyor safety c1: skip labeler when cluster membership unchanged (`src/alfred/surveyor/daemon.py`, +27/-2). Before calling `label_cluster` + `suggest_relationships`, compare `tuple(sorted(members))` to the persisted `ClusterState.member_files`. If identical, log `daemon.membership_unchanged_skip` and continue. Both LLM calls gated behind the same check (both key off membership).
- `b12b4a2` — Surveyor safety c2: hard rate cap on Ollama labeler calls (`src/alfred/surveyor/labeler.py` + `config.py`, +50 lines). Token-bucket sliding window enforcing `max_calls_per_minute: 30` (default) across `_llm_call`. Dropped calls log `labeler.rate_cap_dropped` and return `None` (both callers already handle None). Belt-and-suspenders safety net on top of c1.

## Validation

Live validation 2026-04-24 after restart: touched a low-fan-out assumption file (trailing-newline), tailed `data/surveyor.log`. Result: 62+ consecutive `daemon.membership_unchanged_skip` entries, **zero** `labeler.usage` calls for the tick. Before c1 this would have been dozens of wasted LLM calls. Memory stayed flat at 29GB free throughout.

## Outcome

Cascade vector closed. The `alfred audit infer-marker --apply` CLI is no longer a WSL-crash vector when applied to high-fan-out records. Future audit sweeps can proceed without `alfred down` as a precaution. The broader architectural insight (HDBSCAN ID-renumber vs membership-content equivalence) also informs the distiller rebuild framing — see `Distiller rebuild Week 0 transition 2026-04-24`.

## Alfred Learnings

- **New gotcha**: HDBSCAN + Leiden are non-deterministic across runs. Any code that checks "cluster changed" by ID diff is buggy if the downstream work is expensive — check membership content instead. The fix pattern is 30 LOC.
- **Diagnostic pattern validated**: when every daemon log stops at the same second, it's WSL OOM-kill, not a code-level error. `feedback_surveyor_cascade_oom.md` documents how to distinguish this from pytest OOM (which has a similar screenshot profile).
- **Architect-first workflow validated**: this was the first time we spawned a Plan agent to propose the fix *before* spawning the builder. The Plan agent's diagnosis — "58% of the labeler calls are `writer.tags_unchanged` no-ops" — made the fix obvious and scoped; builder execution took <1 hour. Repeat for bugs where root cause is ambiguous.
- **Anti-pattern avoided**: initial hypothesis (pytest-without-timeout) was wrong. Memory `feedback_pytest_wsl_hang.md` was about a different failure mode (pytest OOM). Went to the daemon logs for evidence instead of pattern-matching memory. See also `feedback_verify_stale_memos.md`.
