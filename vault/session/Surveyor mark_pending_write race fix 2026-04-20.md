---
type: session
name: Surveyor mark_pending_write race fix
date: 2026-04-20
tags:
  - surveyor
  - concurrency
  - hygiene
---

# Surveyor mark_pending_write race fix

## Goal

Close the theoretical race between `VaultWriter._write_atomic`'s
`mark_pending_write` call and the subsequent `os.replace`. If any reader
of `pending_writes` (today: the asyncio-side `compute_diff`; tomorrow:
a watcher-thread filter that consults pending_writes directly on event
dispatch) observes the dict mid-sequence, it can miss the
"this write is mine" signal and misclassify the surveyor's own write
as external — re-triggering the embed → cluster → label → write cycle.

The race window is microseconds and no drift incidents have been traced
back to it in the wild, but the fix is cheap and closes the door on
future contributors who wire the watcher to consult pending_writes.

## Change

One lock, owned by `PipelineState`, acquired on both sides of the
contract:

- `PipelineState.__init__` now exposes `pending_write_lock: threading.Lock`
- `PipelineState.compute_diff` wraps its entire pass through
  `current_hashes` + `self.files` in `with self.pending_write_lock:`
- `VaultWriter._write_atomic` wraps `mark_pending_write` → `tmp write` →
  `os.replace` → `update_file` in the same lock

That makes the (mark, rename, update) tuple atomic from the perspective
of any reader holding the lock.

## Files touched

- `src/alfred/surveyor/state.py` — add lock, wrap `compute_diff`
- `src/alfred/surveyor/writer.py` — wrap `_write_atomic` critical section
- `tests/test_surveyor_writer_race.py` — 5 new tests covering:
  - Lock exists and is a real `threading.Lock`
  - `compute_diff` blocks when the lock is held elsewhere
  - Concurrent reader sees pending/disk pair that is always consistent
    under the lock
  - `_write_atomic` updates `state.files` inside the lock (guard against
    trim regressions)
  - `os.replace` failure path still clears pending_writes entry

## Tests

523 → 528 passing (5 new tests). All green.

## Alfred Learnings

- **Lock ownership pattern:** the lock guards a dict that belongs to
  the state object, so the state object owns the lock. This keeps the
  writer stateless with respect to synchronization and lets anyone who
  holds a reference to `state` participate in the protocol. If the
  lock had lived on the writer, the watcher thread (which has no
  writer reference) couldn't have joined the contract.
- **Testing a race without racing:** the decisive test is not "run
  writer and reader in threads and hope for the race" — that's flaky.
  Instead: pre-acquire the lock on the test thread and assert that
  the reader blocks. Deterministic, sub-millisecond, no sleeps except
  the "confirm blocked" probe.
- **Within-lock invariant vs across-lock invariant:** the first draft
  of the concurrent-reader test read disk_md5 outside the lock and
  pending_writes inside it — which correctly fired a "pending stale
  vs disk" assertion because the pending entry from a previous write
  hadn't been cleared yet. The lock doesn't make successive snapshots
  consistent; it makes each single-acquisition snapshot consistent.
  Test re-framed to read both under the same lock acquisition.

## Next

Second Tier 3 commit (inbox/processed exclusion policy) lands right
after this one. No daemon restart required for this commit alone —
the lock is in-process and takes effect next run. Daemon restart
recommended after the second commit lands to pick up the ignore_dirs
defaults in one go.
