---
type: session
date: 2026-04-17
status: complete
tags: [upstream-port, curator, backend, hermes, needs-validation]
---

# Upstream Port Batch C - Item 4: HermesBackend

## Scope

Port upstream `8e2673c`. Add a new backend option `hermes` alongside
`claude`, `zo`, `openclaw`. HermesBackend is HTTP-based, talks to a
persistent Background Hermes agent via its webui API, and supports
sessions that accumulate skills over time (no per-stage session clear
like OpenClaw).

**Shipped as a code path, not activated.** Daemons stay on whatever
backend was configured before (typically `claude` or `openclaw`). A
future switch to Hermes is a one-line `config.yaml` change.

## What Changed

- `src/alfred/curator/backends/hermes.py` (new, ~170 LOC):
  - `HermesBackend` implements our `BaseBackend.process(...)` signature
    (not upstream's `dispatch(prompt, context)`).
  - Uses `build_prompt()` like the Claude and Zo backends so the same
    skill text + vault context + inbox content gets assembled.
  - HTTP flow: `POST /api/chat/start` returns a `stream_id`, then
    `GET /api/chat/stream?stream_id=...` streams SSE events. We
    accumulate `token` or `text` deltas until `event: done`.
  - Typed error paths: `httpx.TimeoutException` → timeout summary;
    `httpx.HTTPStatusError` → status + body tail summary; any other
    exception → generic error summary. All return
    `BackendResult(success=False, ...)` — never raise.
  - URL precedence: explicit config.url > `HERMES_BG_URL` env >
    `http://hermes-bg:8787` docker default. Matches upstream.

- `src/alfred/curator/config.py`:
  - New `HermesBackendConfig` dataclass (url, timeout) registered in
    `_DATACLASS_MAP` and attached to `AgentConfig.hermes`.

- `src/alfred/curator/daemon.py`:
  - Import `HermesBackend` and add a `hermes` branch to
    `_create_backend()` that passes `vault_path` and `scope="curator"`.

## Interface Adaptation from Upstream

Upstream's HermesBackend has `dispatch(prompt, context)`. Our existing
backends have `process(inbox_content, skill_text, vault_context,
inbox_filename, vault_path)`. I kept our signature and used the shared
`build_prompt()` helper so HermesBackend plugs into the daemon without
a special-case code path. The semantic of "vault writes happen via the
`alfred vault` CLI" is preserved — the daemon still reads the mutation
log for `files_changed`.

## Not Activated

- Config defaults to `backend: "claude"` (unchanged).
- `config.yaml` not modified per team-lead constraint.
- Daemons stay down.
- Live HTTP validation deferred until the user chooses to switch.

When the user wants to try Hermes:
```yaml
agent:
  backend: "hermes"
  hermes:
    url: "http://hermes-bg:8787"   # or set HERMES_BG_URL env
    timeout: 300
```

## Smoke Tests

`/tmp/alfred_smoke/smoke_item4_hermes.py` — 5 checks, all pass:

```
OK: _create_backend returns HermesBackend with correct fields
OK: config round-trip preserves url+timeout
OK: default URL fallback chain works (explicit > env > docker)
OK: unknown backend rejected with ValueError
OK: existing backends (claude/zo/openclaw) still construct
```

## Risks

1. **Live HTTP never exercised.** The SSE parsing logic (token/text
   accumulator, `event: done` terminator) is a direct port from upstream
   that assumes a specific Hermes-webui protocol. If the real server
   emits something different (e.g. `event: message` with a JSON blob),
   the accumulator silently produces empty output. First real use
   should check that `response_text` is non-empty in the logs.
2. **Session-scoped persistence (`session_id=vault-curator`) is a
   semantic difference from OpenClaw.** Hermes accumulates skills across
   inbox files; OpenClaw clears every stage. Switching backends might
   produce different Stage 1 behaviour as Hermes "learns" from previous
   runs in-memory. Worth watching on the first few processed files
   after a backend switch.

## Alfred Learnings

- **Backend contract: `process()` in; `BackendResult` out; never raise.**
  Every backend in the tree catches all exceptions and surfaces them as
  `BackendResult(success=False, summary=...)`. Keeping that invariant
  means the daemon's per-file try/except can focus on logging +
  `mark_processed` fallback — it doesn't need per-backend knowledge.
- **SSE parsers want a structured-event terminator.** Relying on a
  specific `event: done` line is fragile; if the server drops the
  connection the `aiter_lines()` loop exits cleanly anyway. Both paths
  land us at the same `response_text.strip() or "No response"` summary.

## Commit

- Code: b27c046
- Session note: (this file)
