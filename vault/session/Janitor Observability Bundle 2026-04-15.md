---
alfred_tags:
- software/alfred
- software/janitor
- observability
created: '2026-04-15'
description: Three small janitor observability and hygiene fixes discovered during
  today's Layer 3 verification — route janitor CLI logs to the right file,
  add a per-sweep heartbeat for the Layer 3 triage helper, and reorder
  cleanup_session_file after the triage scan so the pattern is read-act-cleanup
intent: Close three small observability and ordering gaps that surfaced while
  verifying Layer 3 end-to-end
name: Janitor Observability Bundle
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Layer 3 Janitor Triage Queue 2026-04-15]]'
status: completed
tags:
- janitor
- observability
- bugfix
type: session
---

# Janitor Observability Bundle — 2026-04-15

## Intent

During today's Layer 3 verification (`4ce03d1`), the diagnosis surfaced three small janitor observability and hygiene gaps. None affects correctness of Layer 3, but together they cost real diagnostic effort when the wiring appeared "not firing" (it was firing — we just couldn't see it, and the cleanup ordering was brittle enough to worry about). This commit closes all three in one bundled fix.

## What Changed

### Fix 1 — `alfred janitor fix` and friends now log to `janitor.log`

**Root cause was deeper than expected.** The janitor CLI dispatcher at `src/alfred/cli.py::cmd_janitor` WAS calling `_setup_logging_from_config(raw)` — that's not the bug. The bug is that `_setup_logging_from_config` hardcodes `alfred.log` as the destination, so every CLI-invoked janitor sweep was writing events to the wrong log file. The daemon path (`alfred up`) works because `orchestrator.py::_run_janitor` imports `alfred.janitor.utils.setup_logging` directly with an explicit `log_file="./data/janitor.log"` argument.

Fix: `cmd_janitor` now imports the same `setup_logging` helper and passes `log_file=f"{log_dir}/janitor.log"` explicitly, wrapped in a `try/except` with a fallback to `_setup_logging_from_config` so the handler cannot fail entirely. Applies to all janitor subcommands (`scan`, `fix`, `drift`, `watch`, `status`, `history`, `ignore`) because the fix lives at the dispatcher level, not per-handler.

**Verified by running `alfred janitor scan`:** `janitor.log` mtime advanced, size +2052 bytes, new events under a fresh `sweep_id=38a46f2a` including `sweep.start`, `scanner.scan_start`, `scanner.scan_complete` (2547 issues), and `sweep.complete`. Before the fix, those events had been going to `alfred.log` or nowhere.

### Fix 2 — `cleanup_session_file` reordered after `_record_triage_ids_from_created`

Both the pipeline path and the legacy path in `daemon.py::run_sweep` previously called `cleanup_session_file(session_path)` BEFORE the Layer 3 triage helper. Fine today because `_record_triage_ids_from_created` reads the created task files directly from the vault, not from the session JSONL — but brittle to future changes that might want to read session-derived data during the helper. The pattern should be **read mutations → act on them → cleanup**, not **read → cleanup → act**.

Fix: `cleanup_session_file` now runs AFTER both `_record_triage_ids_from_created` AND `append_to_audit_log`, effectively as the last session-related operation in the block. In the legacy path the cleanup is guarded by `if use_mutation_log and session_path` to preserve the original conditional (the pre-existing cleanup only ran when both were truthy).

### Fix 3 — Per-sweep heartbeat log for the triage scan

The Layer 3 "wiring not firing" diagnosis this afternoon required ~350 words of investigation because the absence of `daemon.triage_id_recorded` events was ambiguous: was the helper not called, or was it called with an empty `created` list, or did it find nothing relevant?

Fix: one-line `log.info("daemon.triage_scan", created_count=len(created), sweep_id=sweep_id)` added immediately before the helper call in both paths. Every fix-mode sweep now emits at least one `daemon.triage_scan` event regardless of whether anything was found. The next time the helper appears "silent," the diagnosis starts by checking whether the heartbeat is present, which instantly narrows the problem.

## Out of Scope (Flagged by Builder)

`_setup_logging_from_config` being hardcoded to `alfred.log` is a **broader bug pattern**. Every `cmd_*` dispatcher in `src/alfred/cli.py` that calls it — `cmd_curator`, `cmd_distiller`, `cmd_surveyor`, and others — will route CLI-invoked events to `alfred.log` instead of the per-tool log file. The long-running daemon paths (via `orchestrator.py`) are fine because they import `setup_logging` directly with an explicit `log_file`. So `alfred curator sweep` events land in `alfred.log`, not `curator.log`. Same for distiller, surveyor.

The right structural fix is to make `_setup_logging_from_config` take a tool-name argument and route to the matching per-tool log file. That's a refactor across ~6 handlers and deserves its own commit. Flagged for a future small cleanup session; not in scope here.

## Verification

- **Fix 1:** `alfred janitor scan` produces events in `data/janitor.log` (mtime advanced, new sweep_id visible). Before: no events. After: full sweep lifecycle logged.
- **Fix 2:** static `grep -n 'cleanup_session_file' src/alfred/janitor/daemon.py` confirms both occurrences now come AFTER their respective `_record_triage_ids_from_created` call sites.
- **Fix 3:** static `grep -n 'daemon.triage_scan' src/alfred/janitor/daemon.py` confirms the heartbeat is present in both paths. The heartbeat correctly did NOT fire during the Phase-1-only `scan` verification because `scan` doesn't enter the fix path at all.
- **Import sanity:** `from alfred.janitor import daemon, cli as jcli; from alfred import cli` succeeds.

## Alfred Learnings

### New Gotchas

- **`_setup_logging_from_config` lies by omission.** The helper's name suggests "configure logging based on config," but it actually writes to a hardcoded `alfred.log` regardless of which tool is invoking it. CLI handlers calling it think they have logging when they have MISDIRECTED logging. This class of bug is tricky because manual testing against stdout shows "logging works" and you only notice the problem when you grep the tool-specific log file for events you know should be there and find nothing. Pattern for elsewhere: when a "setup_logging"-style helper takes only a config argument, audit what destination it writes to — the naming can be misleading.
- **Heartbeat logs are cheap insurance against "did this even run?" ambiguity.** Today's Layer 3 diagnosis burned real time figuring out whether `_record_triage_ids_from_created` was being called at all. One `log.info(...)` at the call site would have made that a 5-second check. Rule: at every operation that can produce ZERO visible side effects in its happy path (no new files, no frontmatter changes, etc.), add a heartbeat log so the happy path is still observable.

### Patterns Validated

- **Read-act-cleanup ordering as a default.** When a block reads data, acts on it, and then cleans up, the cleanup should always be LAST. Any other order is brittle because future changes may want to read cleanup-dependent data during the action step. Today's reordering is purely defensive — nothing is broken with the original order — but the new order is self-documenting and future-proof. Worth adopting as a project convention: if you see cleanup-then-act, flag it for reordering even when the current code path doesn't need it.
- **Bundling related small observability fixes is the right commit cadence.** Three independent one-line fixes could have been three commits with three session notes. Combined they're one commit with one session note telling the whole "this class of gap surfaced during verification" story. The split would have been over-granular for changes this small and this related.
