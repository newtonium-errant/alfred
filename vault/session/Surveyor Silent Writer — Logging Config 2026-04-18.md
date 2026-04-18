---
type: session
status: completed
name: "Surveyor Silent Writer — Logging Config"
created: 2026-04-18
description: "Fix 1 of 2 for the surveyor silent-writer observability bug. Switch surveyor.utils.setup_logging from PrintLoggerFactory to stdlib-routed structlog so writer.tags_* and daemon.* events actually land in data/surveyor.log instead of being dropped to /dev/null."
tags: [surveyor, observability, logging, drift-bug, session-note]
---

# Surveyor Silent Writer — Logging Config — 2026-04-18

## Intent

The surveyor daemon's write path (`writer.write_alfred_tags`, `writer.write_relationships`) executes successfully — file mtimes confirm tags land in vault records — but `data/surveyor.log` contains zero structured log events. Only `httpx` HTTP debug lines from the labeler's ollama calls reach the file. The 2026-04-17 skip-if-equal commit (`7c1a452`) added `writer.tags_unchanged` / `writer.tags_updated` events specifically for drift auditability; they never fired in production despite the code path being live.

This commit is fix 1 of 2. The matching audit-log gap (zero `data/vault_audit.log` entries from surveyor) is a separate root cause and lands as a follow-up commit.

## Diagnosis

Reproduced the bug in isolation: `setup_logging` configures a stdlib `FileHandler` for `data/surveyor.log`, then configures structlog with `PrintLoggerFactory` + `make_filtering_bound_logger` — which writes events directly to stdout, bypassing the FileHandler entirely. In daemon mode the orchestrator redirects stdout to `/dev/null` via `_silence_stdio`, so every structlog event is dropped on the floor. `httpx` survives because it uses stdlib logging directly, which IS routed to the file.

Curator/janitor/distiller all use `structlog.stdlib.LoggerFactory()` + `structlog.stdlib.BoundLogger`, which routes structlog through stdlib so the FileHandler catches everything. Surveyor was the odd one out, presumably from an early-bring-up pattern that nobody revisited once the orchestrator started silencing stdout.

This is hypothesis #1 + #2 from the project memory combined: the structlog config doesn't route through stdlib (#1), and the resulting stdout writes get swallowed by the daemon's stdio silencing (#2). Hypothesis #3 (instrumentation in dead branch) and #4 (mutation_log not invoked) are independent — #4 is real and gets its own commit.

## Files changed

- `src/alfred/surveyor/utils.py` — switch `setup_logging` to mirror curator/janitor/distiller: `structlog.stdlib.LoggerFactory()`, `structlog.stdlib.BoundLogger`, `structlog.stdlib.add_log_level`, `StackInfoRenderer`. The four daemons now share an identical logging contract. Comment block calls out the historical bug so the next refactor doesn't reintroduce it.
- `tests/test_surveyor_logging.py` — three new pytest cases pinning the contract: (a) headline test that any structlog event survives `setup_logging` + `suppress_stdout=True` and lands in the configured log file; (b) end-to-end test that `VaultWriter.write_alfred_tags` actually produces a `writer.tags_updated` line; (c) regression test that the skip-if-equal short-circuit emits `writer.tags_unchanged` (the whole point of commit 7c1a452).

## Verification

`pytest -v` — 15/15 pass:

```
tests/test_surveyor_logging.py::test_setup_logging_routes_structlog_events_to_log_file PASSED
tests/test_surveyor_logging.py::test_writer_tags_updated_event_emitted_on_real_write PASSED
tests/test_surveyor_logging.py::test_writer_tags_unchanged_event_emitted_when_skipping PASSED
```

Plus a manual reproduction via the surveyor's actual `setup_logging` from a Python REPL with `suppress_stdout=True` confirmed the structlog event reaches the file post-fix; before the fix only the httpx line did.

**Daemons not restarted** (per task instructions). The fix won't take effect in the running surveyor process until next `alfred down` / `alfred up` cycle. Recommended next-session validation: restart and `tail -f data/surveyor.log` for the next labeling sweep — should see `daemon.starting`, `daemon.processing_diff`, `writer.tags_*` events whereas previously only httpx chatter appeared.

## Alfred Learnings

- **Per-daemon `setup_logging` helpers must agree on factory choice.** Any daemon using `structlog.PrintLoggerFactory()` while the orchestrator silences stdout is structurally guaranteed to drop every structlog event. The curator/janitor/distiller pattern (`structlog.stdlib.LoggerFactory()` routed through a stdlib `FileHandler`) is the correct one. Add a check at code-review time: every new daemon's `utils.py` should match this template, and any divergence needs a written justification because the failure mode is silent.
- **Silent-observability bugs need to be checked from the START of a daemon's lifetime.** This bug shipped from the surveyor's first commit and persisted through five+ feature commits because nobody noticed `data/surveyor.log` was anomalously httpx-only. The signature was visible from day one if anyone had grepped the log for `daemon.starting` and gotten zero hits. New-daemon checklist item: after first run, `grep daemon.starting data/{tool}.log` should return at least one line. If it doesn't, the logging contract is broken before any feature work begins.
- **httpx-leaks-but-structlog-doesn't is the giveaway signature.** If a Python daemon's log file contains only third-party stdlib-logging output (httpx, urllib3, asyncio) and zero application-level structlog events, the fault is almost always in the structlog `logger_factory`, not the file path. Triage shortcut: check `structlog.configure(...)` first, file paths second.
- **Pytest now covers structlog routing contracts.** Before this session, no test asserted that any daemon's `setup_logging` actually produces output in the configured log file. The new `test_surveyor_logging.py` is the template — copy it (or generalize to a parametrized fixture across all four daemons) when expanding test coverage. Same-shape silent-writer regression in curator/janitor/distiller would now be catchable instead of taking days of file-mtime sleuthing to spot.
