---
type: session
title: Heartbeat coverage gap fix + email backfill + upstream contribution refresh
date: 2026-04-22
tags: [observability, heartbeat, email, classifier, backfill, upstream, ghostwriting, salem]
---

## Summary

Three commits + one orchestrated backfill run, all triggered by the misdiagnosis cascade that surfaced the `inbound_in_window` coverage gap. Diagnosed the gap, fixed it, ran the email backfill that the empty calibration corpus had been blocking, refreshed the upstream contribution draft to include the new arcs.

| Commit | Subject |
| ------ | ------- |
| `d4f9ac2` | Talker: move record_inbound to application middleware (coverage gap) |
| `74affdf` | Email c1.5: backfill command for existing email-derived notes |
| `cc6b0b9` | Gitignore manual config backups (config.yaml.bak.*) |
| `8cb0194` | Upstream contribution draft: refresh + Reply 7 + Reply 8 + attribution lines |
| `d569cc0` | Daily artifacts: 2 BIT runs + 3 voice sessions from 2026-04-22 |

Plus the actual backfill run (not a commit): 703 emails classified end-to-end in 30:19, zero errors.

## Why

A user-visible failure exposed three nested gaps:

1. Andrew sent `/calibration` (typo for `/calibrate`) at 22:55 ADT and got no response.
2. Initial diagnostic claimed "the message never reached Salem" based on `inbound_in_window=0` heartbeat readings — but Andrew's screenshot showed Telegram's `✓✓` delivery confirmation for both `/calibration` AND the corrected `/calibrate`. The `/calibrate` had produced two responses: "Daily Sync sample firing now…" + "Daily Sync sent, but no calibratable items in the vault yet."
3. The diagnostic was wrong because the heartbeat counter had a coverage gap — `record_inbound()` was wired into the text/voice handlers, missing unrecognised commands. AND the calibration corpus was empty because c1 only fires on new inbox events and the email pipeline broke 2026-04-11 (n8n upstream).

Three concurrent fixes:
- Move heartbeat instrumentation to PTB application middleware so every Update increments the counter regardless of routing.
- Build a backfill command that classifies existing email-derived notes lacking `priority`, then run it on the 703 candidates so the calibration loop has real data to surface.
- Refresh the upstream contribution draft (which Andrew had also asked me to take on) to include the new arcs and capture the observability pattern as its own reply.

## What changed

### `d4f9ac2` — Heartbeat coverage gap fix

- `src/alfred/telegram/bot.py` — added `_pre_record_inbound(update, ctx)` async wrapper around `heartbeat.record_inbound()` (try/except so a counter bug can't break message delivery). Registered as `app.add_handler(TypeHandler(Update, _pre_record_inbound), group=-1)` — the first handler in the application, runs before any routing. Removed the per-handler `record_inbound()` calls in `on_text` + `on_voice` to prevent double-counting.
- `tests/telegram/test_idle_tick.py` — added 6 integration-style tests building the real `bot.build_app` Application, driving `app.process_update(...)` with hand-built `Update` objects: plain text, recognised `/end`, **unrecognised `/calibration`** (the load-bearing case), edited message, callback query, single-fire double-count regression guard.

PTB doesn't have classical middleware. `TypeHandler(Update, callback)` matches every Update kind; `group=-1` puts it ahead of the default group=0 where real handlers live. PTB only fires one handler per group, so the negative group keeps the pre-pass off the routing critical path. Callback returns normally (no `ApplicationHandlerStop`), so per-handler routing fires unchanged.

### `74affdf` — Email c1.5 backfill command

- New `src/alfred/email_classifier/backfill.py` (~150 lines) — iterates note records via `is_email_inbox` heuristic, skips those with `priority` already set, calls the c1 classifier, writes `priority` + `action_hint` to frontmatter.
- New CLI subcommand: `alfred email-classifier backfill [--dry-run] [--limit N]`.
- Progress logging every 25 records; final summary log with classified / skipped / error counts.
- LLM failures logged + skipped + counted (don't abort the whole run).
- Tests in `tests/test_email_classifier/test_backfill.py`: skip-already-classified, dry-run-doesn't-write, limit caps correctly, email-only filter, LLM-failure-path, frontmatter shape correct.

### Backfill run results

`alfred email-classifier backfill` over the 703 candidates → **30:19 wall time, zero errors**.

| Tier | Count | % |
| ---- | ----- | - |
| **high** | 7 | 1% |
| **medium** | 138 | 20% |
| **low** | 455 | 64% |
| **spam** | 107 | 15% |

Action-hint top patterns: `archive` (396), `ignore` (108), `file:newsletter/Tim Denning` (21 — exactly the cold-prompt example), `file:receipts/apple` (9), `calendar` (8), various per-newsletter and per-finance folders.

The classifier picked up the Tim Denning newsletter pattern from the cold-prompt seed and applied it across 21 records — calibration intent matching design intent. It also auto-suggested newsletter folders (Magdalena Ponurska, Brenna McGowan, Mike Mandel, 80000Hours, New Means) and finance folders (paypal, scotiabank, apple-app-store) without being explicitly seeded. Emergent categorisation working.

### `cc6b0b9` — Gitignore for config backups

`config.yaml.bak.*` added to `.gitignore`. I create these snapshots before risky config edits (added 2026-04-22 for the email_classifier and daily_sync block additions). Same hygiene as `config.yaml` itself — they hold real config including any literal secrets that haven't been moved to env vars.

### `8cb0194` — Upstream contribution draft refresh

- Top-level message refreshed with post-2026-04-21 work: 7 new rows in `## Architectural arcs shipped` (Schedule-followups, Brief Upcoming Events Phase 1, Talker scope+boundary, Email c1+c2, Email backfill, Observability arc). 4 new bullets in `## Patterns that validated`. Open problems section updated — brief drift item struck through with the resolution commit.
- Reply 7 (BIT health check system) — was empty placeholder; now a full deep-dive (~10 KB) with Status enum semantics, per-tool modules, aggregator concurrency + timeouts, `alfred check` + preflight gate, BIT daemon vault record, Morning Brief integration, multi-instance peer probes, tradeoffs/rejected, the env-substitution scar, open questions.
- Reply 8 (intentionally-left-blank observability) — new (~9 KB). Misdiagnosis cascade, pre-pattern examples, talker idle_tick, 60s-vs-1Hz cadence math, the PTB middleware coverage gap, propagation across all watching daemons, per-daemon counter semantics (the load-bearing contract), mail's sync-thread exception, tradeoffs, test contract.
- Salem ghostwriting attribution line added to Reply 7 + Reply 8 (matching top-level convention) — initial agent pass missed them; caught in review and patched.

### `d569cc0` — Daily artifacts

2 BIT runs (2026-04-22 + 2026-04-23) + 3 voice session notes from 2026-04-22 (the `0313 ec1db330` capture session, `0313 142d0618` Jamie wishlist note session, `2051 cddb6d31` later session). All daemon-generated; committing keeps the audit trail intact.

## Design decisions

- **TypeHandler at group=-1, not a `MessageHandler` at group=-1.** TypeHandler(Update, ...) matches every update kind including callback queries, edited messages, etc. — exactly what the heartbeat needs. MessageHandler is narrower.
- **Backfill is sequential, not parallel.** ~3s per LLM call × 703 = ~30 min. Could parallelize but the c1 classifier uses a synchronous Anthropic SDK call; parallelizing would have meant restructuring the classifier itself, which is the opposite of "small commit." Sequential was fine.
- **Backfill skips already-classified records.** Safe to re-run, partial backfills can resume, no double-charging on API costs.
- **LLM failures don't abort the run.** Logged + skipped + counted. Backfill of 703 records would be unacceptably fragile if any single LLM hiccup killed the whole batch.
- **Reply 7 + Reply 8 each carry the Salem attribution line independently.** Top-level alone isn't sufficient because individual replies often get posted as separate threads/comments — each artifact shipping independently needs its own attribution.

## Alfred Learnings

- **Pattern validated — the "intentionally left blank" rule paid off the next day.** The heartbeat caught its own coverage gap. `/calibration` arrived and didn't increment the counter; the screenshot from Telegram + the `inbound_in_window=0` reading together pinpointed the gap (instead of "is the daemon broken?" — which a logging-silent system would have looked like). The pattern's value compounds: every layer that emits positive idle signals makes the next-layer-up debugging fast.
- **Anti-pattern confirmed — instrumenting at the handler layer instead of the application layer.** Per-handler counters drift when the framework adds routing branches (PTB's `CommandHandler` runs before `MessageHandler`-with-`~filters.COMMAND`). The general lesson: a heartbeat must instrument at the layer that sees every event by definition, not at the layer that handles each event type. PTB's `Application` is that layer; `TypeHandler(Update, ...)` is the hook. Same shape would apply to Flask/FastAPI middleware, aiohttp middlewares, etc.
- **Pattern validated — backfill commands as a deliberate first-class concept.** When a daemon's post-processor only fires on new events, the historical data lives in a different operational state than future data. A `<feature> backfill` CLI is the clean way to bridge them — explicit, idempotent (skip-already-done), dry-run-supported, progress-logged. Same shape would apply to: surveyor re-embedding after a model upgrade, distiller re-extraction after a SKILL change, etc.
- **Gotcha — silent dependency: c1 classifier needed the calibration corpus path matched between two config blocks.** `email_classifier.calibration_corpus_path` and `daily_sync.corpus.path` must point at the same JSONL file. The c2 builder added both fields to `config.yaml.example` but if a user adjusts one without the other, the few-shot rotation breaks silently (classifier reads from path A, Daily Sync writes to path B, neither errors). Worth adding a startup validation that the two paths agree, or unifying on a single `email_calibration_corpus.path` block. Filed for later.
- **Gotcha — Salem ghostwriting attribution is per-artifact, not per-package.** When I spawned the upstream-contribution refresh agent, I gave it "preserve voice from existing draft" — the agent matched voice well but missed the attribution line on Reply 7 + Reply 8 because the existing 1-6 replies had it implicitly via the top-level. Each file shipping independently needs its own attribution. Codified as `feedback_salem_ghostwriting_guidelines.md` so future external-comms specs include this in the prompt explicitly. Pre-loading the four ratified guidelines (shipped-and-learned, discussion-gated, attribution, convergence) prevents this drift class.

## Active follow-ups

- **`/calibration` alias** — explicitly NOT added. Andrew's stance: "We don't need to create alias for things before I get used to using the proper command." If muscle memory doesn't correct, revisit.
- **Email pipeline upstream restoration** — n8n on Railway, broken since 2026-04-11. At-home work (Railway UI is bad on mobile per `project_email_pipeline.md`). Backfill works around the gap; new emails won't arrive until the upstream is restored.
- **Capture burst-replay edge case** (`project_capture_burst_replay_edge.md`) — Andrew's natural-validation test pending. Surface at next session start if not validated.
- **Daily Sync calibration loop is now usable** with real backfilled data. First `/calibrate` from Telegram surfaces from the 703 newly-classified records.
- **Email c3-c6 still gated** on per-tier `/calibration_ok <tier>` toggles after Andrew validates accuracy via the calibration loop.

## Next

The chain "ship classifier → ship Daily Sync → backfill historical data → calibrate → activate surfacing tiers" is fully ready up through the calibrate step. From here, Andrew validates calibration accuracy through Telegram replies, flips per-tier confidence flags as accuracy proves out, and c3-c6 surface emails through brief / Obsidian / Telegram push as authorised.

Other queued arcs: STAY-C Phase 1 (ratified Stage 3.5 next instance), V.E.R.A. as roadmap-reorder candidate, integration audit (Andrew's enumeration), OpenClaw setup (at-home).
