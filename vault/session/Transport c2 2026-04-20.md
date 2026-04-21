---
type: session
name: Transport c2 — HTTP server + outbound routes + 501 stubs
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
  - http
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c2 — HTTP server + outbound routes + 501 stubs

## What shipped

- `src/alfred/transport/server.py` — aiohttp ``Application`` factory,
  bearer-token auth middleware, and all route registrars.

### Routes live today

- `POST /outbound/send` — immediate or scheduled send. Honours 24h
  dedupe window; returns `503 telegram_not_configured` when no send
  callable is registered.
- `POST /outbound/send_batch` — multi-chunk send for brief auto-push.
- `GET /outbound/status/{id}` — lookup in send_log / pending_queue /
  dead_letter.
- `GET /health` — public (unauthenticated) — reports
  `telegram_connected`, `queue_depth`, `dead_letter_depth`.

### Stage 3.5 stubs (501 today)

- `POST /peer/send`, `POST /peer/query`, `POST /peer/handshake`
- `GET /canonical/{type}/{name}`

All four return `{"reason": "peer_not_implemented"}` with status 501.

### Route namespace registry

``ROUTE_NAMESPACES`` maps a prefix (``outbound``/``peer``/``canonical``/``health``)
to its registrar function. Swapping a stub for a real handler in
Stage 3.5 is a one-line edit. This is the Stage 3.5 D1 pre-commit
contract in code.

### Auth middleware

- Reads `Authorization: Bearer <token>` + `X-Alfred-Client: <name>`.
- Looks up the token in the config's ``auth.tokens`` dict — keyed by
  peer name — then verifies the client is in that peer's
  ``allowed_clients`` list.
- Never logs the token contents; emits only ``token_length`` and
  ``token_prefix`` (first 4 chars) on rejection. Per builder.md
  secret-logging contract.
- `/health` is the only public route — it's the bootstrap probe.

## Tests

`tests/test_transport_server.py` — 17 tests covering:

- Auth accept / missing header / wrong token / client-not-allowed.
- Immediate send delivers via callable; state persists send_log.
- Scheduled-at parks in pending_queue instead of dispatching.
- Dedupe returns the recorded entry without re-dispatching.
- Batch send delivers chunks in order; empty-chunks rejected.
- Status lookup; 404 on unknown.
- 501 stubs for every `/peer/*` and `/canonical/*` route.
- 503 when no send callable registered; `register_send_callable`
  enables delivery on an already-built app.
- **Multi-peer token dict** — Stage 3.5 D2/D7 pre-commit sanity.
  Two `auth.tokens` entries authenticate independently with different
  `allowed_clients`. This is the Stage 3.5 smoke test Mission Control
  will run again once peer handlers ship.

Suite: 607 → 624 (+17). All green.

## Alfred Learnings

- **Gotcha / dependency** — aiohttp's `aiohttp.test_utils` needs the
  `pytest-aiohttp` plugin to expose `aiohttp_client` as a pytest
  fixture. Added to `[dev]` extras. This is obvious in retrospect but
  the error message (``fixture 'aiohttp_client' not found``) doesn't
  mention the missing plugin.
- **Pattern validated** — the route-namespace registry pattern is
  simple (one dict, four functions) and drops the Stage 3.5 peer
  swap to a one-line diff. Worth reusing for future
  extensibility-via-registration surfaces.
- **Anti-pattern avoided** — the dedupe lookup uses a short-circuit
  on empty string rather than treating absence as "always match".
  Callers that don't pass a key get no dedupe, which is the right
  default — scheduled reminders always pass a key, brief always
  passes `brief-{date}`, but ad-hoc sends (e.g. a test command) don't
  need to be deduped.
