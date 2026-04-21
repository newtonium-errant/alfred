---
type: session
name: Transport c3 — Client helper + exception hierarchy
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c3 — Client helper + exception hierarchy

## What shipped

- `src/alfred/transport/exceptions.py` — `TransportError` base class
  plus `TransportAuthMissing`, `TransportServerDown`,
  `TransportRejected`, `TransportUnavailable`. Narrow categories so
  callers can log-and-continue (brief) vs fail-loud (misconfig).
- `src/alfred/transport/client.py` — `send_outbound`,
  `send_outbound_batch`, `get_status`. httpx-based async client.
  Reads `ALFRED_TRANSPORT_{HOST,PORT,TOKEN}` from env. Raises
  `TransportAuthMissing` when the token is unset — orchestrator
  will inject in c6 but manual runs without the var are now a
  clear error instead of a 401 loop.

### Auto-detection for `X-Alfred-Client`

`sys.argv[0]` → ``alfred-brief`` becomes ``brief``, etc. The
``alfred`` / ``python`` catch-alls default to ``talker`` (the only
process that runs "inside a big entry point"). Callers pass an
explicit `client_name` when the default doesn't match the server's
allowlist.

### Retry policy

1 retry on 5xx / timeout / ConnectionRefused with 0.5s → 2s backoff.
Never retries on 4xx (client error — resending won't help).
Connection errors that exhaust retries surface as
`TransportServerDown`; persistent 5xx surfaces as
`TransportUnavailable`.

### Subprocess-contract logging

On `/outbound/send` failure: `log.warning(...)` emits
`code`, `body`, `response_summary` per builder.md's
subprocess-failure contract adapted for HTTP. Makes
`rg response_summary` return the one-line failure class summary
across all client call sites.

## Tests

`tests/test_transport_client.py` — 13 tests:

- Missing token raises `TransportAuthMissing` with actionable message.
- `ALFRED_TRANSPORT_HOST` / `_PORT` env overrides applied.
- `X-Alfred-Client` auto-detected from argv; explicit override wins.
- `send_outbound` and `send_outbound_batch` payloads; empty-chunks
  rejected client-side.
- `get_status` HTTP shape (GET /outbound/status/{id}).
- **Retry policy** — 5xx then 200 succeeds on retry; 4xx only hits
  the server once (no retry); connect-error exhausts budget and
  raises `TransportServerDown`.
- Subprocess-contract log shape — `code=400`,
  `response_summary='Status 400: ...'`, `transport.client.nonzero_response`.

Suite: 624 → 637 (+13). All green.

## Alfred Learnings

- **Gotcha** — monkey-patching `httpx.AsyncClient` via
  `monkeypatch.setattr(client_mod.httpx, "AsyncClient", ...)` recurses
  infinitely if the replacement callable itself instantiates
  `httpx.AsyncClient(...)` — the module reference is already patched.
  Fix: capture `httpx.AsyncClient` into a local before the monkey-patch
  runs, and call that captured reference. Worth adding to the test
  patterns doc if we do this pattern again.
- **Pattern validated** — `httpx.MockTransport` plugged into a
  real `AsyncClient` is a cleaner test harness than intercepting at
  the request/response boundary. The test handlers are plain
  `Request → Response` callables and the assertions work against
  real request objects (url, method, headers, content).
- **Pattern validated** — structlog's `ConsoleRenderer` writes to
  stdout, which pytest's `capsys` fixture captures cleanly. `caplog`
  captures stdlib logging records, which the renderer bypasses — use
  `capsys` for structlog-rendered assertions.
