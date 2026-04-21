---
type: session
name: Transport c5 — Brief auto-push + chunker
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
  - brief
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c5 — Brief auto-push + chunker

## What shipped

### Brief daemon hook

`src/alfred/brief/daemon.py` — after `file_path.write_text(content)`,
the daemon dispatches the brief via `send_outbound_batch`:

1. `chunk_for_telegram(content)` splits at paragraph breaks, 3800-char
   ceiling.
2. `send_outbound_batch(user_id, chunks, dedupe_key="brief-{today}",
   client_name="brief")` delivers in order.
3. `TransportError` subclasses are logged with the subprocess-contract
   shape and **swallowed** — the brief is already in the vault, the
   push is best-effort.

### Config plumbing

`src/alfred/brief/config.py` — new `primary_telegram_user_id` field.
`load_from_unified` reads the unified config's
`telegram.allowed_users[0]`; absence means the push is skipped
silently. Single-user v1 per the plan.

### Chunker (was in c1 utils, now battle-tested)

`chunk_for_telegram(text, max_chars=3800)` — paragraph-break
preference, sentence-boundary fallback for overlong paragraphs,
hard-wrap last resort. The `\n\n` join-cost accounting bug caught
in c5 chunker tests (off by 4 on sentence packing) is now fixed.

## Tests

### `tests/test_transport_chunker.py` — 8 tests

Empty, short, multi-para-under-limit, multi-para-over-limit with
round-trip assertion, long-single-para with sentence split, no-
punctuation hard-wrap, paragraph-break-preserved-within-chunk,
mixed sizes pack greedily.

### `tests/test_brief_dispatch.py` — 8 tests

Config (3 tests): resolves primary_user, no telegram section, empty
allowed_users.

Dispatch (3 tests): transport invoked with right fields (user_id,
chunks, dedupe_key='brief-{date}', client_name='brief'), multi-chunk
split, empty content skipped.

Failure paths (2 tests): `TransportServerDown` and `TransportRejected`
are logged (event=brief.push_failed, error_type=..., response_summary=...)
and swallowed — brief stays in vault.

Suite: 655 → 671 (+16). All green.

## Alfred Learnings

- **Gotcha** — structlog's `cache_logger_on_first_use=True` caches
  the BoundLogger against whatever handler was configured when the
  logger was first instantiated. If a prior test in the suite has
  called `setup_logging(log_file=...)`, later tests that read stdout
  via `capsys` see empty strings because the log lines went to the
  file handler instead. Fix: patch the module's `log` attribute
  directly with a spy in tests that assert on log content. Adding
  to builder.md as a pattern.
- **Gotcha fixed** — the chunker had an off-by-4 on sentence-pack
  join cost: pieces joined with `\n\n` on flush, but the in-loop
  accounting used +1 for the join cost. Caught by the
  `long_single_paragraph` test. Now uses +2 consistently.
- **Pattern validated** — "log-and-continue" with subprocess-contract
  fields (`event`, `error_type`, `error`, `response_summary`) gives
  the operator a grep-able trail of brief-push failures without
  coupling brief generation to the talker daemon's liveness. The
  brief is always in the vault even if Telegram is down.
- **Decision recorded** — brief daemon imports `alfred.transport.client`
  lazily inside `_push_brief_to_telegram` rather than at module top.
  Keeps the import graph clean for tests that don't exercise
  transport, and makes the telegram-optional nature visible at the
  call site.
