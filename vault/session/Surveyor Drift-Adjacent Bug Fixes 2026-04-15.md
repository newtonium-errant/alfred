---
alfred_tags:
- software/alfred
- software/surveyor
- bugfix/cleanup
created: '2026-04-15'
description: Two small surveyor bugs the prior drift-fix audit flagged but explicitly
  left out of scope — the relationships writer was emitting duplicate log lines per
  tick because the labeler's near-duplicate rels were each appended individually,
  and the daemon LOOP_INTERVAL was more aggressive than the watcher debounce window.
intent: Land the two follow-up surveyor cleanups so the deferred-items list shrinks
  and the daemon stops doing wasteful work
name: Surveyor Drift-Adjacent Bug Fixes
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Stop Surveyor Session Drift 2026-04-15]]'
status: completed
tags:
- surveyor
- bugfix
type: session
---

# Surveyor Drift-Adjacent Bug Fixes — 2026-04-15

## Intent

Earlier today the surveyor drift-fix commit (`233075c`) closed the main bug — the daemon was re-labeling session/inbox files with non-zero LLM temperature on every tick. While doing that, the builder flagged four out-of-scope issues. This commit lands two of them: the duplicate-log-line bug in `write_relationships` and the over-aggressive `LOOP_INTERVAL`. The other two flagged issues (`mark_pending_write` race, `inbox/processed/` permanent exclusion question) stay deferred — they need design decisions before code can move.

## What Changed

### Bug 1 — `writer.relationships_written` log spam, two co-causes

The duplicate `writer.relationships_written added=1` lines per tick had **two** independent causes, both fixed in this commit:

**Cause 1A — daemon was calling the writer once per rel.** In `daemon.py::_cluster_and_label`, the labeler returned a list of relationships, and the loop called `self.writer.write_relationships(source, [rel])` *individually for each one*. For five rels with the same source, the writer was hit five times, producing five separate file reads, five separate writes, five separate log lines. Fixed by grouping the labeler output by `source` first, then calling `write_relationships(source, source_rels)` once per source per tick. One file read and write per affected source, one log line.

**Cause 1B — writer had no in-batch self-dedup.** Even if the daemon was already grouping by source, the labeler can return near-duplicates within a single call (same `target`, possibly different `confidence` or `context`), and the writer's existing dedup only checked against the file's pre-existing relationships — not against the incoming batch. Fixed by adding an in-memory dedup pass at the top of `write_relationships`, keyed on `(target, type)`. Empty targets are filtered as part of the same pass. Choice of `(target, type)` rather than `target`-only is forward-compatible with a future policy that allows multiple distinct relationship types between the same pair.

Both fixes are needed: Cause 1A reduces the number of write calls per source from N to 1, Cause 1B ensures that when the labeler emits N near-duplicate rels in one batch, the writer collapses them to the unique set before computing `added`.

### Bug 2 — `LOOP_INTERVAL = 5.0` was hardcoded, more aggressive than watcher debounce

Module-level `LOOP_INTERVAL = 5.0` replaced with `DEFAULT_LOOP_INTERVAL = 30.0` (used only as fallback). The daemon's `run()` method now reads `self.cfg.watcher.debounce_seconds` programmatically and sleeps at that cadence:

```python
loop_interval = getattr(
    self.cfg.watcher, "debounce_seconds", DEFAULT_LOOP_INTERVAL
)
```

Polling faster than the debounce window just spins through ticks that find nothing to do (the diff-since-last-sweep is empty). Reading the value from config means future tuning of `watcher.debounce_seconds` automatically re-tunes the daemon loop without a code change.

The builder verified the full `_tick` body before changing — nothing in it needs a sub-30s cadence: the tick early-returns on empty diff, `state.save()` only runs after actual work, and there are no health checks or cluster-signature monitors that would be starved by a 30s interval.

## Verification

### Bug 1 (writer dedup)

Builder wrote a standalone harness that instantiates `VaultWriter` against a tempdir vault and calls `write_relationships` with three deliberately crafted inputs:

- 3 near-duplicates with same `target` + `type` but different `confidence` / `context` → final count 1, one log line `added=1`
- 2 unique targets + 1 duplicate → final count 2, one log line `added=2`
- Same target with two different `type` values → final count 1 (existing-vs-new target-only policy wins), one log line `added=1`

All three cases pass and each call emits exactly one log line. Did not run the live daemon for this verification — the test was tight enough to be unambiguous.

### Bug 2 (loop interval)

Imported `WatcherConfig` and `DEFAULT_LOOP_INTERVAL` and confirmed both resolve to `30.0`. Confirmed `alfred.surveyor.daemon` and `alfred.surveyor.writer` import cleanly. Did not restart the running daemon — the change is config-driven and the import-time check was sufficient.

## What's Still Out of Scope (deferred)

Two of the four flagged surveyor issues stay open per the prior session note:

1. **`writer._write_atomic` `mark_pending_write` race.** The pending-write registry is keyed on path, not content hash. Back-to-back writes with different contents can have the second write's `expected_md5` overwrite the first, and a pending watcher event may ignore a real user edit. Narrow race that has not been observed in practice. Needs a small redesign of `mark_pending_write` to key on `(path, hash)` or maintain a list per path.

2. **Whether `inbox/processed/` should remain permanently excluded** from the surveyor or whether some future consumer expects those emails indexed. Policy decision, not a code fix.

## Alfred Learnings

### New Gotchas

- **Per-call methods called in a loop produce per-call log spam, even before any internal dedup is involved.** The `write_relationships` log line discipline was correct (one log per call), but the daemon was making N calls when it should have been making one. The fix in `writer.py` would have helped, but only the daemon-side grouping fix actually collapses the log volume to one entry per source per tick. Pattern for elsewhere: when a method is logged at `info` level "once per call," and a caller makes N calls in a tight loop, you get N log lines in your face — even if each individual log line is correct. Group at the caller before reaching for log-throttling at the callee.
- **Hardcoded loop intervals drift from related config values silently.** `LOOP_INTERVAL = 5.0` was set to a guess at some point, the watcher debounce defaulted to `30.0`, and nothing connected the two. Six months later the daemon was spinning through five useless ticks per debounce window. Fix: read the related config value programmatically rather than hardcoding. Generalisable: any time a daemon has a "tick interval" setting, it should be derived from (or capped at) whatever upstream debounce/poll/wait it's downstream of, not a free-floating constant.

### Patterns Validated

- **Two co-causes hiding in one symptom.** The duplicate-log-line bug had a clear-looking root cause ("the writer doesn't dedupe its input batch") that was correct but incomplete. The daemon-side per-rel calling pattern was a co-cause. Fix one in isolation and the symptom only partially abates. The audit was right to call out the writer dedup; the deeper investigation found the daemon-side grouping issue at the same time. Rule: when a fix turns out to be smaller than expected, look upstream — there may be a second cause worth catching in the same pass.
