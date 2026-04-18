---
type: session
date: 2026-04-17
tags: [upstream-port, distiller, surveyor, drift-stability]
commits: [a80ecbe]
ports_from: [a3a44a4, f45d05d, 99cbd25]
---

# Upstream Port Batch A — Item 1: Distiller MD5 refresh + config defaults + surveyor dim mismatch

## Context

Our fork diverged from upstream ssdavidai/alfred at tag v0.3.2 (6268182). Upstream has 25 commits we lack; batch A ports three drift-stability items. This is item 1 of 3. Surgical extraction, not cherry-pick — conflicts likely because our fork has its own drift-fix commits (a9f6ec0 distiller learnings merge) touching the same regions.

## What shipped

Three changes in one commit (a80ecbe):

1. **Distiller infinite-loop guard** — new `recompute_source_md5s()` in `distiller/daemon.py`. After the pipeline writes `distiller_signals`/`distiller_learnings` back to a source record's frontmatter, the file's on-disk MD5 shifts. State still holds the pre-write hash, so the next candidate scan flags the file as "new" and re-processes it. Loop confirmed upstream (their commit message quantifies the damage for janitor: 968 LLM calls over 3 days from restart loops). Our fix re-hashes each source after the write batch and updates state. Called after both pipeline and legacy agent paths. Preserves `learn_records_created` by calling `update_file()` without the `learn_records` arg — our state.update_file already keeps the list intact when the arg is omitted, matching upstream f45d05d.

2. **Config defaults bumped upstream-aligned** in `distiller/config.py`:
   - `candidate_threshold`: 0.3 → 0.6 (fewer false-positive candidates)
   - `interval_seconds`: 3600 → 86400 (scan daily, not hourly)
   - `deep_interval_hours`: 24 → 168 (deep extraction weekly, not daily)
   Did NOT touch `config.yaml` — user's existing overrides stay.

3. **Surveyor embedder dim-mismatch detection** in `surveyor/embedder.py`. On `_ensure_collection()`, if the existing Milvus collection's embedding dim differs from configured (user swapped models), drop and recreate the collection, clear `state.files`, and persist via `state.save()`. The state save is load-bearing: without it, the daemon's next `PipelineState.load()` reloads stale hashes and skips every file. Folded upstream a3a44a4 + f45d05d + 99cbd25 into one path.

## Smoke tests

- `DistillerConfig()` → verified all three defaults match target values (86400 / 168 / 0.6).
- `recompute_source_md5s`: created a temp record, seeded state with initial MD5 + a learn record reference, mutated the file, ran refresh. State MD5 updated to post-write hash, `learn_records_created` preserved intact (not duplicated).
- Embedder: source inspection confirmed dim_mismatch logging, state invalidate fallback, and `self.state.save()` call are all present.

## Alfred Learnings

- **Pattern validated** — "hash-before-write gets stale when the same process writes the file" is a recurring class of bug. Distiller hit it; upstream also confirms janitor had an analogous issue (restart-reset on `last_deep_sweep`). Any orchestrator that stores a hash after scanning and then triggers a write to the scanned file needs a post-write refresh. Flag for CLAUDE.md if it shows up in a third tool.
- **Gotcha** — `for/else` in Python: the `else` branch runs when the `for` loop completes without `break`. Used in our embedder's field scan: if the embedding field matched AND dims matched, we fall through the loop with no break and the `else` returns (no recreate). If dims didn't match we break (no `else` fires) and fall through to the create path. Reading this at a glance is non-obvious; kept a comment would help.
- **Anti-pattern** — dropping a Milvus collection without also invalidating the pipeline state. The pipeline's file-hash dict is the "what did we already embed" memory; dropping the vector store without clearing it produces a vault that looks fully embedded but actually has no vectors. Upstream needed two follow-up commits (f45d05d clears the dict, 99cbd25 persists it) to get this right.
- **Missing knowledge** — we should document in the builder agent instructions that `update_file(rel, md5)` without the third arg is the "refresh hash only, don't touch learn records" call. It's already the behaviour but it reads as a bug on first glance.

## Next

Item 2: persist `last_deep_sweep` / `last_deep_extraction` + cap Stage 2 issues at 15 + reshape Stage 2 prompt. Then item 3: janitor perf (stub cap, stale attempts, event-driven deep sweeps).
