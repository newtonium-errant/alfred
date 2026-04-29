---
type: session
status: completed
name: Upstream merge — 43 commits 7 clusters 2026-04-29
created: 2026-04-29
description: Full upstream/master sync — 43 commits behind, 323 ahead going in. 17 conflict files manually resolved with explicit per-cluster intent from Andrew. All today's-prior preservation rules held. Single merge commit (0c99c6a) plus a small Cluster E gap closeout (6e76496) for the nested-list flatten guard the "TAKE OURS" decision dropped from pipeline.py.
intent: Pull 4 days of upstream improvements (surveyor entity-linking feature set, curator parallelism, janitor token reduction, distiller legacy fixes, surveyor perf) without regressing today's-prior ships (PIQ Phase 1, KAL-LE P0, C polish, outbound chunking, hardcoding sweep, drift_skip, V2 distiller).
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Observability arc and queue-design foundation 2026-04-28]]'
tags:
- merge
- upstream
- conflict-resolution
- entity-linking
- preservation
---

# Upstream merge — 43 commits, 7 clusters

## Trigger

Multi-day session in progress; last upstream fetch was 2026-04-25, 4 days stale. Andrew flagged the gap. Audit revealed 43 commits behind, 323 ahead. Per `feedback_upstream_check.md`, ≥10 commits triggers a full audit. We were 4× past the threshold.

## Cluster decisions (Andrew's call)

All 7 clusters approved with one explicit nuance on Cluster E:

| Cluster | Content | Decision |
|---|---|---|
| A — Surveyor entity-linking | 10 commits, new feature: backfill, noise linking, parallel labeling, full-vault relink CLI, telemetry | TAKE CLEANLY |
| B — Surveyor perf/fix | ~6 commits: chunk caps, query_iterator, batch embeddings | TAKE CLEANLY |
| C — Curator perf + parallel + hermes | ~8 commits: full Stage 1 records, token reduction, parallel processing | TAKE WITH CARE; preserve our scope/agent_slug |
| D — Janitor token reduction + fixes | ~4 commits: stub caps, event-driven sweeps, phantom-issue fix | TAKE WITH CARE on state.py (preserve forward-compat filter) |
| E — Distiller legacy fixes | ~5 commits: embedder dim mismatch, learn_records dedup, nomic-embed default, deep-sweep persistence | **NUANCE — both legacy AND V2 hybrid live in parallel during measurement window** |
| F — Misc | ~3 commits: container paths, binary inbox copy, move-on-success | TAKE SELECTIVELY |
| G — Skill prompts | 2 prompt updates | TAKE CLEANLY unless local divergence |

Cluster E's nuance was load-bearing: Andrew explicitly wants to compare upstream's improved legacy distiller against our V2 hybrid during the v2 parallel-run window. So upstream's legacy fixes had to RIDE alongside V2, not get rejected.

## Resolution discipline

The merge generated 17 conflict files. Approach was per-file conflict resolution, not bulk auto-merge:

- **Distiller files** (`config.py`, `daemon.py`, `pipeline.py`, `state.py`): TAKE OURS as superset, then verify upstream's specific fixes survived. Most did via prior shipped code; one didn't (the flatten guard from `40f3df4` — required a 2-minute cherry-pick afterward).
- **Janitor files** (`state.py`, `daemon.py`, `pipeline.py`, `config.py`): TAKE OURS — superset including upstream's perf changes plus deep_sweep_fix_mode + drift sweep + SUPERSEDED-marker sweep + scope=janitor_enrich.
- **Surveyor files**: INTEGRATE — upstream's new entity-linking + parallel labeling + idempotent startup-sync are real wins. Membership-stability gate (from 2026-04-24 cascade fix) had to be manually relocated from upstream's auto-merge target into the right method.
- **Embedder**: take upstream's chunked + pooled implementation entirely; thread our failure-tracking lists through as kwargs.
- **Curator pipeline/daemon/config**: TAKE OURS as superset (upstream's perf + ours' scope/agent_slug threading + email_classifier wiring).
- **Curator hermes backend**: kept ours — upstream's `dispatch(prompt, context)` signature was incompatible with our `BaseBackend.process()` 5-arg contract.
- **Skill prompts**: kept ours (local prompt-tuner divergence preserved).
- **CLI**: union (our A5 launch hint fix + upstream's new `alfred surveyor relink` command coexist).

## What didn't survive cleanly

`40f3df4`'s defensive flatten guard for nested `[[wikilinks]]` in distiller pipeline's `_stage2_dedup_merge` was dropped by the "TAKE OURS" decision on `pipeline.py`. Code-reviewer caught it during the post-merge audit. 2-minute cherry-pick (`6e76496`) closed the gap on the merge-path. The same nested-list bug exists on the new-candidate `merged.append({...})` path; builder flagged but kept out of the cherry-pick per strict scope. Filed as next-session item #12.

## Submodule gitlinks

Upstream commits `a3a44a4` and `e510cbe` accidentally added gitlinks for `alfred-platform` and `lightswitch` with no `.gitmodules` config backing them. Confirmed never functional in upstream either. Builder removed via `git rm --cached` during the merge. Verified by code-reviewer.

## What's now in service

- **Surveyor entity-linking**: `alfred status` shows new "Entity linking:" telemetry section across all 3 instances (Salem 2446 records scanned, KAL-LE 38, Hypatia 29). New `alfred surveyor relink` CLI command available for full-vault backfill.
- **Curator perf**: parallel processing, full-record Stage 1 extraction, hermes backend (with our compatible signature).
- **Janitor perf**: event-driven sweeps, stub caps, S2 prompt updates.
- **Distiller legacy fixes**: `last_deep_extraction` persistence, embedder dim-mismatch handling, learn_records dedup. V2 hybrid still runs in parallel via `use_deterministic_v2: true`.
- **All today-prior preservation rules verified** by code-reviewer cross-walk.

## Restart + verification

All 3 instances restarted on `6e76496`. `alfred status` clean (no `unavailable` lines). `pending_items_pusher` flushing every 5min on all 3. Salem janitor logs show `superseded.sweep_complete` + `daemon.drift_sweep` running cleanly with the merged code. 1797/1803 tests pass (6 pre-existing failures, none merge-induced).

## Alfred Learnings

- **"TAKE OURS during merge" requires per-commit hunk-level cross-walk, not just headline-feature spot check.** Builder caught 6/7 Cluster E commits' content via the headline-feature audit; the 7th (a small defensive flatten guard) was visible only in the diff hunks. Code-reviewer caught it; almost slipped through. Worth a builder-checklist amendment: when "TAKE OURS" is the decision, list every upstream commit's hunks against the file and verify each survived. Filed as next-session item #13.

- **Upstream submodule additions without `.gitmodules` backing are accidental.** `alfred-platform` and `lightswitch` gitlinks appeared in unrelated upstream commits without proper submodule config. Removing them via `git rm --cached` was correct. Future merges should explicitly check `git diff upstream/master --diff-filter=A -- '*.gitmodules'` and orphan gitlinks (`mode 160000` paths with no .gitmodules) as separate signal from real submodule additions.

- **The forward-compat filter pattern** (`{k: v for k, v in fdata.items() if k in DataclassName.__dataclass_fields__}`) shipped yesterday in P0 saved this merge from breaking. Upstream added new `FileState` fields in surveyor/janitor/curator; our state files stayed loadable on first restart because the filter silently drops unknown keys. Three tools now use this pattern (distiller, surveyor, janitor); curator joined during the merge. Worth promoting to documented standard in CLAUDE.md "State persistence — load() schema-tolerance contract" subheading. Filed as next-session item #11.

- **Cluster E's "both architectures parallel" pattern is preserved by clean code separation.** V2 (`extractor.py`, `writer.py`) is in its own files, untouched by upstream. Legacy paths (`pipeline.py`, `daemon.py`, `state.py`) get upstream's improvements while V2 runs alongside. The flag `use_deterministic_v2: true` in distiller config gates which architecture runs. This is the right shape for parallel-run observation windows: keep the architectures filewise-separate so neither merge nor refactor accidentally clobbers the other.

- **Code-reviewer's pattern-trigger discipline is high-leverage.** Pattern triggers from today's three reviews:
  - PIQ Phase 1: re-reading config.yaml on hot paths is recurring (filed as P1 follow-up)
  - PIQ Phase 1: sync→async bridges are recurring (Phase 2 native-async refactor planned)
  - Upstream merge: TAKE OURS requires per-commit hunk-level cross-walk
  Each is a small architectural rule that prevents the next round of similar bugs. Worth distilling into a separate `feedback_*` memo cluster: "patterns triggered during code review that should become standing rules."

- **Multi-day session work needs upstream check at session-start, not just "session-start" defined as "first message of the day."** Today's session has run 4+ days. The session-start checklist in CLAUDE.md was last applied at the START of this multi-day session; subsequent days' commits accumulated upstream drift unnoticed until 4-day gap forced a 43-commit merge. Worth adding to `feedback_upstream_check.md`: "On multi-day sessions, run cheap upstream check at the start of each Halifax-day, not just at the abstract session start."
