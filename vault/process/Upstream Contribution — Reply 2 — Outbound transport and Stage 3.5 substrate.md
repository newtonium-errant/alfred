---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 2 — Outbound transport, substrate for multi-instance

**Problem shape.** Our talker (Telegram bot) had zero outbound-push capability. "Remind me at 6pm" returned an honest "I can't push a message to you at a specific time." The morning brief generated the record on schedule but sat silently in the vault until the user went looking. We had tasks with due dates that nobody heard about. Worse — we knew multi-instance was next, and that needs inter-instance HTTP in the same process shape.

**Solution shape.** A new `src/alfred/transport/` module hosting an `aiohttp` server **inside the talker daemon's event loop**. No IPC hop; the scheduler polls the vault, the server accepts outbound sends, and the Telegram bot shares everything. Routes:

- `/outbound/send`, `/outbound/send_batch`, `/outbound/status/{id}`, `/health` — live in v1.
- `/peer/*`, `/canonical/*` — registered as 501 stubs from day one.

The stubs were the architecturally-load-bearing choice. When the KAL-LE arc swapped them for real peer handlers a day later, it was a one-line `ROUTE_NAMESPACES` change rather than a server refactor. Same file, same auth layer, same config schema.

Auth is a `transport.auth.tokens` dict keyed by peer name. v1 populates one entry (`local`); Stage 3.5 (multi-instance) adds per-peer tokens using the same schema. The orchestrator injects `ALFRED_TRANSPORT_HOST/PORT/TOKEN` into child tool subprocess env, matching the existing `MAIL_WEBHOOK_TOKEN` pattern.

The scheduler runs as an in-process async task alongside the bot's long-poller. 30s poll interval scanning `vault/task/**/*.md` for due `remind_at` fields. When one fires, it goes through the same `/outbound/send` endpoint with a `dedupe_key` so restart-mid-fire doesn't double-send.

**Consumers v1.**

- `remind_at` on tasks — scheduler dispatches.
- Morning brief — brief daemon dispatches post-write directly (not through the scheduler). Reason: brief timing is its own concern; making brief a consumer of the scheduler would have coupled two independent cadences.

**Tradeoffs / what we rejected.**

- **FastAPI.** Considered. Rejected for aiohttp because we wanted to share the talker's event loop cleanly without dragging in Starlette-shaped plumbing. aiohttp is smaller and async-native.
- **Separate transport daemon.** Rejected. Would have added a process boundary between the scheduler and the bot that the use case doesn't need.
- **Hardcoded brief delivery path inside brief.py.** Rejected — wanted one egress route so BIT and future cross-instance work could observe it uniformly.
- **Deferring `/peer/*` stub registration.** Rejected specifically because the second arc (KAL-LE) was queued. Two days later the stub pattern paid for itself.

**Commit range.** `aca34b1..87def9a` (6 commits). c1 config + auth scaffolding → c2 HTTP server + stubs → c3 client helper + exception hierarchy → c4 scheduler + `remind_at` schema + bundled talker-SKILL update → c5 brief auto-push + chunker → c6 orchestrator wiring + CLI + BIT probe + talker integration.

Worth flagging: we hit a Telegram chunking issue almost immediately (briefs exceeded 4096-char message limit). Added a paragraph-break chunker in c5 with a 3800-char target per chunk. Server-side 250ms inter-message floor to honor Telegram's per-chat rate limit. Both felt like load-bearing details you'd have run into on any real deployment.

Would love to hear how this echoes (or doesn't) in your thinking — particularly the inside-talker-process shape vs a sidecar.
