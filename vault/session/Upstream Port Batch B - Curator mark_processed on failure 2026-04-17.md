---
type: session
title: Upstream Port Batch B - Curator mark_processed on failure
date: 2026-04-17
tags: [session, upstream-port, curator, bugfix]
---

## Summary

Ported upstream commit `7745ea7` — curator `mark_processed` on failure. Before this change, if `_process_file` threw (LLM timeout, manifest parse error, any exception), the except handler only logged and left the file in the inbox with no `status: processed` frontmatter. `InboxWatcher.full_scan()` would re-pick it up on the next rescan tick — classic infinite reprocessing loop.

Our daemon has two exception handlers in `run()` — one in the startup scan (pre-watcher) and one in the periodic loop. Upstream's patch only touched the periodic loop. I applied the same fix to both paths since they share the same bug surface.

## Changes

- `src/alfred/curator/daemon.py`:
  - Startup scan `except Exception` block now calls `mark_processed(inbox_file, config.vault.processed_path)` as a fallback (guarded by `.exists()` check and a nested try/except that logs `daemon.mark_processed_fallback_failed`).
  - Periodic loop `except Exception` block gets the same treatment.

`mark_processed` updates frontmatter to `status: processed` AND moves the file under `processed/`. Either condition alone is enough to satisfy the `InboxWatcher.full_scan()` skip rules, so the fallback is double-robust.

## Smoke Test

- Temp inbox + processed dirs
- Wrote a fake `.md` file
- Called `mark_processed(fake, processed)` (simulating the new failure-handler path)
- `full_scan()` returned zero entries → file is no longer a reprocessing candidate

Test passed.

## Alfred Learnings

- **Drift family widens**: this is the same family of bugs as the janitor-status-drift and curator-state-drift issues already fixed — "loop keeps finding the same item because the terminal marker never got written." Pattern: always write the terminal state marker in `finally` or in the exception handler, not just on the success branch.
- **Upstream patched one path; we had two.** When porting from upstream, always check whether our code has been refactored to duplicate the affected logic. Grep for the error signature (`log.exception("daemon.process_error"`) rather than trusting the upstream diff covers everything.
