---
type: session
name: Transport c6 — Orchestrator + CLI + BIT probe + talker integration
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
  - orchestrator
  - cli
  - bit
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c6 — Orchestrator + CLI + BIT probe + talker integration

Final commit. End-to-end wire-up.

## What shipped

### Orchestrator env injection

`src/alfred/orchestrator.py` — new `_inject_transport_env_vars(raw)`
sets `ALFRED_TRANSPORT_{HOST,PORT,TOKEN}` in the current process env
before spawning child tool processes. Children inherit via
`multiprocessing.Process` / fork. Matches the `MAIL_WEBHOOK_TOKEN`
injection pattern. Guardrails:

- Doesn't clobber existing env vars (manual `.env` / shell export wins).
- Doesn't propagate unresolved `${VAR}` placeholders — leaking them
  would poison the client's "missing token" check.

### Talker daemon integration

`src/alfred/telegram/daemon.py` — imports + starts two sibling
asyncio tasks alongside the PTB long-poller and gap-timeout sweeper:

- `run_transport_server(app, config, shutdown_event)` — aiohttp
  server on `127.0.0.1:8891`. Send callable is a real Telegram
  dispatcher with a 250ms per-chat inter-message floor (asyncio.Lock
  keyed by user_id).
- `run_scheduler(config, state, send_fn, vault_path, user_id,
  shutdown_event)` — fires task-record reminders, drains the
  pending queue.

Both tasks observe the same `shutdown_event`; SIGTERM cascades
cleanly. Setup failure is non-fatal — the talker still handles chat
without the transport server if config resolution hiccups.

### BIT probe

`src/alfred/transport/health.py` — five checks:

1. `config-section` — transport: present.
2. `token-configured` — env var set, not a placeholder, ≥ 32 chars.
   Data field includes length; token contents never logged.
3. `port-reachable` — GET /health. Connection refused is WARN
   (transport is optional), not FAIL.
4. `queue-depth` — warns at > 100 pending.
5. `dead-letter-depth` — warns at > 50 entries.

Registered with the aggregator via `KNOWN_TOOL_MODULES["transport"]`.
`alfred check` now includes transport in the sweep.

### CLI

`src/alfred/transport/cli.py` + `alfred.cli.cmd_transport`:

- `alfred transport status` — queue + dead-letter + health summary.
  Supports `--json`.
- `alfred transport send-test <user_id> <text>` — direct smoke test
  via the client.
- `alfred transport queue` — list pending scheduled sends.
- `alfred transport dead-letter {list|retry <id>|drop <id>}` —
  maintenance.
- `alfred transport rotate` — generates 64-char hex token, rewrites
  `.env` with a `.env.bak` backup, prints the new token + restart
  reminder. Handles the no-.env case by creating one.

### Manual smoke results

- `alfred transport --help` → proper subcommand tree.
- `alfred transport status` → Health: WARN (port-reachable is
  unreachable while the talker isn't running — expected), token
  length 64 OK, state checks OK.
- `alfred check --tools transport` → five probes render cleanly in
  the BIT report.

## Tests

### `tests/test_transport_health.py` — 8 tests

- SKIP on missing config section.
- Token probe: missing, short, OK, placeholder FAIL.
- Port-reachable WARN when server is down.
- Queue/dead-letter depths reported correctly, warn on overflow.

### `tests/test_transport_cli.py` — 13 tests

- `status` JSON shape.
- `queue` empty + populated.
- `dead-letter` list / drop / retry (re-enqueues without
  scheduled_at so the next tick fires it).
- `dead-letter drop` without id returns 1.
- `rotate` creates a fresh `.env` with a valid 64-char hex token.
- `rotate` replaces an existing token, backs up the prior `.env`,
  preserves unrelated vars.
- **Orchestrator env injection** — resolves values, skips
  placeholders, preserves manual overrides.
- Top-level parser accepts every `alfred transport …` command combo.

Suite: 671 → 692 (+21). All green.

## Alfred Learnings — Full 6-commit arc recap

Same convention as instructor's c6. Here is the whole outbound-push
transport arc in one place.

### Commits shipped

| c | SHA (local) | Description | Tests added |
|---|---|---|---|
| 1 | aca34b1 | Config + auth schema + state scaffolding | 10 |
| 2 | 15c4802 | HTTP server + outbound routes + 501 stubs | 17 |
| 3 | 04ad87a | Client helper + exception hierarchy | 13 |
| 4 | 1d410d6 | Scheduler + remind_at + SKILL update | 18 |
| 5 | a99592d | Brief auto-push + chunker | 16 |
| 6 | (this) | Orchestrator + CLI + BIT + talker integration | 21 |
| **Total** | | | **95 new tests, 597 → 692** |

### Stage 3.5 pre-commit contract status

Three forward-compat decisions locked in by this arc:

- **D1** (HTTP REST + JSON) — locked. aiohttp Application factory
  is a one-line swap-over point.
- **D2** (per-peer bearer tokens) — locked. `auth.tokens` dict in
  the config + the auth middleware already support arbitrary peer
  entries. Test `test_multiple_peer_tokens_each_authenticate_independently`
  proves it.
- **D7** (config-driven peer discovery) — locked. Same dict.

### Anti-patterns avoided

- No `sk-`-prefixed test tokens. Every fixture uses
  `DUMMY_TRANSPORT_*_TEST_TOKEN` per builder.md's
  GitGuardian-scrub rule.
- Token contents never logged. Probes report length + first-4-hex
  only; failure logs redact the body to 500 chars and fingerprint
  the prefix.
- No direct filesystem writes from the transport module onto a
  tool's scope — the talker's send callable uses PTB's Bot API, and
  the scheduler uses `frontmatter.load` + atomic file writes under
  the talker process (no vault CLI subprocess hop).

### New patterns validated

- **Route namespace registry** — `ROUTE_NAMESPACES` dict of
  ``prefix → registrar_fn`` makes the Stage 3.5 peer handler swap a
  one-line edit. Worth reusing for extensibility-by-registration
  surfaces.
- **Log-spy pattern for failure-path assertions** —
  `monkeypatch.setattr(module.log, "warning", capture_list.append)`
  is robust to structlog's `cache_logger_on_first_use` caching
  against the handler that was active at first use. Other tests'
  `setup_logging` calls can swap handlers mid-suite; a log-spy
  side-steps the whole problem.
- **Best-effort post-write dispatch** — brief daemon calls
  `send_outbound_batch`, logs-and-continues on any `TransportError`.
  Keeps brief generation decoupled from talker daemon liveness.

### Risks / follow-ups

- **User must `alfred down && alfred up`** for the changes to take
  effect — the running talker has no transport server loaded. Team
  lead will do this after review.
- **Telegram per-chat rate limits under load** — the 250ms floor is
  a conservative first pass. If the scheduler drains 50 stale
  reminders in one tick after a week's downtime, that's ~13 seconds
  of serial sending. Acceptable; stale-window dead-lettering
  shortens it dramatically in practice.
- **ISO-timestamp parsing duplicated** — `state.py` and
  `scheduler.py` each have a local `_parse_iso`. One more consumer
  → consolidate into `utils.py`. Not urgent.
- **Scheduler user_id derivation** — hardcoded as
  `config.allowed_users[0]`. Stage 3.5 multi-user deploys will need
  a routing layer.

### Gotchas documented (promotable to builder.md)

1. `pytest-aiohttp` plugin required for `aiohttp_client` fixture —
   error message doesn't hint the plugin is missing.
2. Monkey-patching `httpx.AsyncClient` requires capturing the real
   class first or the replacement recurses infinitely.
3. Chunker join-cost accounting bug: paragraphs joined with `\n\n`
   need +2 in the packing counter, not +1.
4. Structlog's `cache_logger_on_first_use=True` caches loggers
   against whatever log handler was first configured — tests
   asserting on log content should use log-spies, not `capsys`.
