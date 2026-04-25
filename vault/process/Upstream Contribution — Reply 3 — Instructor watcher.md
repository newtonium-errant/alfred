---
alfred_tags:
- process/upstream-contribution
- system-integration
- user-interaction
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
distiller_signals: none
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: 'LINK001 — scanner false positives: [[decision/Salem Ghostwrites External
  Communications on Andrew''s Behalf]] target exists (YAML doubled-apostrophe confuses
  scanner); [[decision//extract Is Idempotent with Delete-First Re-Run Contract]]
  has stray leading slash but resolves to existing decision/extract Is Idempotent
  with Delete-First Re-Run Contract.md — curator distiller_learnings write artifact.
  FM001/DIR001 are separate record-type/location issues (this is a note stored in
  process/ without a name field) awaiting operator triage.'
project:
- '[[project/Alfred]]'
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
---

# Reply 3 — Instructor watcher

**Problem shape.** Users want to drop natural-language directives onto records without opening a chat session. "Set tags to [instructor, smoke-test] and mark this active." "Archive the last three run records." The vault already has `alfred_instructions` as a frontmatter field (your design, kept). What was missing was the daemon that actually executes pending directives, plus the destructive-ops safety gate, plus the audit trail.

**Solution shape.** A new `instructor` tool alongside curator/janitor/distiller/surveyor. It polls the vault every 60s, hash-gating on full file bytes so unchanged records don't get rescanned. When it finds a pending directive, it dispatches an in-process Anthropic SDK tool-use loop with the `instructor` scope.

The contract on the record frontmatter:

```yaml
alfred_instructions:
  - "Set tags to ['instructor', 'smoke-test'] and set status to 'active'"
alfred_instructions_last:
  - text: "Set tags to…"
    executed_at: "2026-04-20T17:42:10Z"
    result: "tags → […], status → active"
alfred_instructions_error: null   # set only after max_retries
```

Executed directives move from `alfred_instructions` into `alfred_instructions_last` (rolling window of 5). An audit comment gets appended to the record body: `<!-- ALFRED:INSTRUCTION 2026-04-20T17:42:10Z "Set tags…" → tags →[…], status → active -->`. Rolling-5 window pruning is done against a regex on that comment format; the regex lives next to the writer so it can't silently drift.

**Destructive-keyword gate.** Before the tool-use loop starts, the directive text is scanned for `delete|remove|drop|purge|wipe|clear all`. If matched, the executor runs in `dry_run=True` mode with read-only tool access. The archive entry documents the refusal reason. Live-validated on 2026-04-20: `"Delete this record entirely"` returned dry-run only, no mutation, archive entry wrote the refusal.

**Why in-process SDK, not `claude -p`.** Three reasons that all came up at the same time:

1. The tool-use loop needs streaming and careful turn-by-turn dispatch.
2. The API key path varies per instance, and subprocess env inheritance had been biting us (we'd already landed a separate fix — `103a2ca` — to stop `ANTHROPIC_API_KEY` leaking into `claude -p` subprocesses). In-process skips the class entirely.
3. Startup cost per turn. The instructor fires often enough during smoke testing that subprocess setup was a noticeable fraction of runtime.

Curator still uses the subprocess agent-backend pattern. It's a one-shot call with a big prompt and no tool-use loop; the pattern fits it well.

**Tradeoffs / what we rejected.**

- **Executing all `alfred_instructions` at once per record.** Rejected — processed one at a time, with the executed directive moving to archive before the next starts. Clean audit trail and failure isolation.
- **Letting the tool-use loop write any field.** Deliberately ran under a new `instructor` scope with `delete` denied globally (even without the dry-run gate). The destructive-keyword gate sits inside the scope; two layers.
- **Cross-record operations in a single directive.** v1 is one directive per record. "Archive the last three run records" has to be three directives or an operator script. Filed as a future extension once real usage patterns shape it.

**Commit range.** `6f66649..316f6b9` (6 commits): scope + schema → config + state → watcher + `detect_pending()` → executor + daemon wiring → SKILL bundle with `{{instance_name}}`/`{{instance_canonical}}` templating → orchestrator + CLI + BIT probe.

The templating piece matters for multi-instance: the instructor SKILL.md has placeholders so KAL-LE's instructor and Salem's instructor identify themselves correctly in their tool-use reasoning.

Would love to hear how this echoes (or doesn't) in your thinking — particularly the in-process SDK choice vs your subprocess agent backend.
