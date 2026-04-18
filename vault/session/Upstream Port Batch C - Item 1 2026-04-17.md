---
type: session
date: 2026-04-17
status: complete
tags: [upstream-port, curator, parallel-processing]
---

# Upstream Port Batch C - Item 1: Parallel Curator Processing

## Scope

Port upstream `163b7f9` — replace the sequential inbox-file loop with an
`asyncio.gather`/`asyncio.Semaphore` bounded parallel processor. Both the
startup scan and the watch loop now process up to `watcher.max_concurrent`
(default 4) inbox files at once.

## What Changed

- `src/alfred/curator/config.py` — add `WatcherConfig.max_concurrent: int = 4`.
- `src/alfred/curator/daemon.py` — wrap startup scan and watch-loop ready
  batches in semaphore-bounded `asyncio.gather(..., return_exceptions=True)`.

## Interaction with Batch B

Batch B added two pieces to this path: the cross-process `_claim_file` lock
and the mark-on-failure fallback. Both are preserved per-task inside the new
gather structure. Key decisions:

- **Claim moved inside the gather task** (not pre-claim on the main loop) so
  two coroutines racing for the same file interleave safely: whichever enters
  the semaphore slot first wins the lock, the other one logs `skip_locked`
  and drops out without blocking peers.
- **`_processing` set maintained for pre-filtering**, but each task also
  consults `_claim_file` inside the semaphore — belt and suspenders because
  the in-memory set is process-local and the file lock is cross-process.
- **Exception handler per task** calls `mark_processed` on failure to prevent
  infinite reprocess loops. `return_exceptions=True` on the gather is
  defensive against programmer error (a bare exception leaking out of
  `_watch_process`) rather than the expected path.

## Smoke Test

`/tmp/alfred_smoke/smoke_item1_parallel.py`: 8 mock files, 0.5s each,
max_concurrent=4, one deliberate failure.

```
OK: 8 files, 1 failure isolated, peak concurrency=4, elapsed=1.01s
```

- Peak concurrency = 4 (semaphore bound respected).
- 7 successes + 1 exception returned (no cancellation of peers).
- Elapsed 1.01s vs 4.0s sequential (parallel path confirmed).

Also verified `CuratorConfig` default (4) and config.yaml round-trip override
(2) both load correctly, and `alfred.curator.daemon` imports cleanly.

## Not Shipped (By Design)

- No `config.yaml.example` change. Team-lead constraint: code defaults only.
- Daemon not restarted. Validation is code-path only; real-world concurrency
  will be exercised when daemons come back up.

## Alfred Learnings

- **Semaphore placement matters with cross-process locks.** Upstream's
  version acquires the file lock in the main loop before even entering the
  gather, which means the main loop blocks on lock acquisition and can't
  move to the next file. Our pattern (lock inside the semaphore slot) is
  better for tail latency — a file held by a zombie daemon doesn't stall
  the loop, it just fails fast and frees the slot.
- **`return_exceptions=True` is belt-and-suspenders when each task already
  has a try/except.** Kept it anyway so a future refactor that drops the
  per-task handler doesn't tank the whole daemon.

## Commit

- Code: a68f3ec
- Session note: (this file)
