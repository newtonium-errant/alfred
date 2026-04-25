---
alfred_tags:
- process/upstream-contribution
- system-architecture
- automation
created: '2026-04-21'
distiller_learnings:
- '[[decision/Peer Protocol v1 Is HTTP REST JSON Localhost-Only]]'
- '[[decision/Per-Instance Port Convention Stepped by Ten]]'
- '[[decision/KAL-LE Scope Denies Move and Delete — Curation Is Additive]]'
- '[[decision/Instance-Specific Record Types Registered Outside Base KNOWN_TYPES]]'
- '[[decision/bash_exec Evaluates Denylist Before Allowlist]]'
- '[[decision/bash_exec Uses shlex.split With subprocess_exec Never shell=True]]'
- '[[decision/bash_exec Git Allowlist Restricted to Read-Only Subcommands]]'
- '[[decision/bash_exec Destructive-Keyword Dry-Run Gate Overrides Caller Flag]]'
- '[[decision/KAL-LE Cannot Perform Remote Git Operations — Bundle B Plus D Split]]'
- '[[decision/bash_exec Audit Log Excludes Command Output]]'
- '[[constraint/bash_exec cwd Must Resolve Inside Approved Repository Roots]]'
- '[[constraint/bash_exec Enforces 300-Second Timeout and 10KB Output Truncation]]'
- '[[synthesis/Inline-Code Interpreter Flags Are Attack Vectors Independent of Interpreter
  Allowlist]]'
- '[[decision/Canonical Record Reads Are Default-Deny With Field-Level Permissions
  and Audit]]'
- '[[decision/Peer Client Dispatch Uses Correlation IDs Written to Per-Peer Inbox]]'
- '[[decision/Upstream Contribution Uses Discussion Threads Gated on Per-Arc Interest]]'
- '[[decision/Frame Upstream Report as Shipped-and-Learned Not Roadmap Pitch]]'
- '[[decision/Salem Ghostwrites External Communications on Andrew''s Behalf]]'
- '[[assumption/Convergence Signal As Valuable as Divergence When Reporting to Upstream]]'
- '[[synthesis/Core Alfred Design Patterns Held Across 255 Commits of Fork Divergence]]'
- '[[synthesis/Fork Use Case Spans Four Risk Tiers From Single Template]]'
- '[[decision/Field-Level Allowlist Is Primary Scope Enforcement Mechanism Not SKILL
  Guardrails]]'
- '[[decision/Split Janitor Into Autofix and Enrich Scopes With Separate Allowlists]]'
- '[[decision/check_scope Fails Closed When Field List Is Omitted]]'
- '[[decision/Body-Write Permission Gated Separately From Frontmatter Allowlist on
  Janitor Scope]]'
- '[[synthesis/LLM Agents Interpret Boolean Scope Flags as Broad License Absent Explicit
  Field Restrictions]]'
- '[[assumption/Field-Level Allowlist Sufficient Without Restructuring Agent Invocation
  to Hide Full Records]]'
- '[[decision/Scope Contract Enforced by Executable Smoke Test Not Documentation]]'
- '[[decision/Use aiohttp Over FastAPI for Transport Server]]'
- '[[decision/Host Transport HTTP Server Inside Talker Daemon Event Loop]]'
- '[[decision/Register Peer and Canonical Routes as 501 Stubs From Day One]]'
- '[[decision/Morning Brief Dispatches Directly Rather Than Through Scheduler]]'
- '[[decision/Single Egress Route for All Outbound Messages]]'
- '[[decision/Scheduler Uses dedupe_key to Survive Restart Mid-Fire]]'
- '[[constraint/Telegram 4096-Character Per-Message Limit]]'
- '[[constraint/Telegram Per-Chat Rate Limit Requires Inter-Message Throttle]]'
- '[[decision/Reuse MAIL_WEBHOOK_TOKEN Env-Injection Pattern for Transport Auth]]'
- '[[synthesis/Stubbing Future Route Namespaces Pays Off Within Days When Next Arc
  Is Queued]]'
- '[[synthesis/Independent Cadences Should Not Share a Dispatcher Even When They Share
  a Transport]]'
- '[[assumption/30-Second Vault Poll Interval Sufficient for Task Reminder Precision]]'
- '[[synthesis/Curator Produces Case-Variant Duplicate Records on Case-Sensitive Filesystem]]'
- '[[synthesis/Regulatory Notification Subject Lines Provide No Service Identification
  for Triage]]'
- '[[assumption/Curator Speculates Transaction Content Beyond Source Evidence]]'
- '[[decision/Instructor Uses In-Process Anthropic SDK Not Subprocess claude -p]]'
- '[[decision/Instructor Destructive Ops Protected by Two Independent Layers]]'
- '[[decision/Instructor Processes One Directive Per Record Per Cycle]]'
- '[[decision/Instructor v1 Limits Directives to Single-Record Scope]]'
- '[[synthesis/Backend Execution Pattern Determines Agent Dispatch Mechanism]]'
- '[[synthesis/Instructor SKILL Templating Enables Per-Instance Self-Identification]]'
- '[[constraint/Subprocess Env Inheritance Leaks ANTHROPIC_API_KEY Into claude -p]]'
- '[[decision/Instructor Audit Regex Co-Located With Its Writer]]'
- '[[assumption/Destructive-Keyword Regex Covers Dangerous Instructor Operations]]'
- '[[decision/Capture Session Type Silences LLM Turns to Preserve Brainstorming Flow]]'
- '[[decision/Telegram Per-Message Emoji Reaction as Silent-Mode Receipt Signal]]'
- '[[decision/Capture Note Extraction Is Opt-In via /extract Not Automatic on /end]]'
- '[[decision/Maximum Eight Derived Notes Per Capture Session]]'
- '[[decision/Calibration Fires at Capture Session Close Using Structured Summary]]'
- '[[decision/Structured Summary Output Wrapped in ALFRED:DYNAMIC Markers Above Raw
  Transcript]]'
- '[[assumption/Calibration-Relevant User Signals Surface in Silent Capture Sessions]]'
- '[[synthesis/Turn-by-Turn LLM Session Shape Is Wrong for Continuous Brainstorming]]'
- '[[synthesis/Silent LLM Modes Require Per-Message Delivery Receipts to Preserve
  User Confidence]]'
- '[[decision/Deterministic Prefix Short-Circuits LLM Classifier for Session Routing]]'
- '[[decision/Brief Audio Delivery Falls Back to Text and Document Upload on API or
  Size Failure]]'
- '[[decision//extract Is Idempotent with Delete-First Re-Run Contract]]'
- '[[decision/Wall-Clock Scheduling Replaces Rolling 24h Intervals for Heavy Daily
  Passes]]'
- '[[decision/Shared Schedule Helper Instead of a Central Scheduler Daemon]]'
- '[[decision/Retain Rolling Intervals for Cheap and Event-Responsive Work]]'
- '[[decision/Legacy *_interval_hours Fields Kept as Backward-Compat Fallbacks]]'
- '[[decision/First-Boot Seeds last_consolidation=now to Prevent Immediate Fire on
  Restart]]'
- '[[constraint/Morning Brief at 06:00 Requires Clean Post-Sweep Post-Enrichment Vault
  State]]'
- '[[assumption/Asyncio.sleep Drift Under Load Causes Morning Brief Early Fire]]'
- '[[synthesis/Development Restart Cadence Silently Shifts Rolling Schedules Into
  Working Hours]]'
- '[[synthesis/Filesystem-Watcher Daemons Are Correctly Shaped Without Wall-Clock
  Scheduling]]'
- '[[synthesis/Curator Summary Body Contains Garbled CJK Character Substitutions]]'
- '[[synthesis/Phishing Template Placeholder Failures Reveal Automated Bulk Generation]]'
- '[[synthesis/Phishing Uses Fabricated Generic Service Names Alongside Real Brand
  Impersonation]]'
distiller_signals: constraint:2, contradiction:1
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: LINK001 — scanner false positives on distiller_learnings YAML-escaped
  apostrophes; wikilink targets exist as decision/Salem Ghostwrites External Communications
  on Andrew Behalf and decision/extract Is Idempotent with Delete-First Re-Run Contract.
  FM001/DIR001 deterministic — file is type:note in process/ dir, missing name field;
  awaiting autofix.
project:
- '[[project/Alfred]]'
relationships:
- confidence: 0.7
  context: Both mention Upstream Contribution stages.
  source: process/Upstream Contribution — Reply 2 — Outbound transport and Stage 3.5
    substrate.md
  source_anchor: Stage 3.5 substrate
  target: process/Upstream Contribution — Reply 4 — KAL-LE multi-instance MVP.md
  target_anchor: KAL-LE multi-instance MVP
  type: related-to
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
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
