---
alfred_tags:
- process/upstream-contribution
- system-monitoring
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
- '[[decision/extract Is Idempotent with Delete-First Re-Run Contract]]'
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
- '[[decision/Health Check Aggregator Fans Out Concurrently via asyncio.gather]]'
- '[[decision/Missing Tool Config Section Returns SKIP From Health Check]]'
- '[[decision/BIT Aggregator Silently Skips Tool Modules Whose Optional Dependencies
  Are Absent]]'
- '[[decision/Morning Brief Re-Renders Latest BIT Record as a ## Health Section]]'
- '[[synthesis/Shared Auth Probe Reduces Secret-Logging Surface From N Tools to One]]'
- '[[synthesis/LLM Scope Drift Produces Plausible-Looking Diffs Indistinguishable
  From Intentional Edits]]'
- '[[synthesis/Cross-Tool Field Ownership Violations Surface Through Drift Investigations
  Not Live Monitoring]]'
- '[[decision/janitor_enrich Scope Denies Create Move and Delete Operations]]'
- '[[decision/Telegram Chunker Targets 3800 Characters Per Message]]'
- '[[decision/Outbound Server Enforces 250ms Inter-Message Floor]]'
- '[[decision/Transport Auth Schema Is Dict Keyed by Peer Name]]'
- '[[decision/ROUTE_NAMESPACES Constant Toggles 501 Stubs to Live Handlers]]'
- '[[synthesis/Telegram Operational Limits Surface Immediately on First Real Push
  Traffic]]'
- '[[assumption/Sharing Talker Event Loop Preferable to a Separate Transport Daemon]]'
- '[[decision/Peer-to-Peer Response Relay Prefixes Originating Instance Name]]'
- '[[decision/KAL-LE Creates Pattern and Principle Record Types Exclusively]]'
- '[[decision/Each Instance Owns Config State Logs and Telegram Bot Token]]'
- '[[synthesis/Per-Instance Type Enforcement Reuses talker_types_only Rule Shape]]'
- '[[decision/Safety-Critical Commits Require Per-Denylist-Item Test Assertion]]'
- '[[decision/bash_exec cwd Allowlist Restricted to Four Repository Roots]]'
- '[[decision/Bearer Token Per Peer Pair Stored in transport.auth.tokens]]'
- '[[decision/--config Flag Applies to All alfred Subcommands]]'
- '[[decision/Instructor Watcher Polls Vault at Sixty-Second Cadence]]'
- '[[decision/Instructor Watcher Hash-Gates on Full File Bytes to Skip Unchanged Records]]'
- '[[decision/Executed Instructor Directives Archive as Rolling Window of Five Entries]]'
- '[[decision/Blocked Destructive Directive Records Refusal Reason in Archive Entry]]'
- '[[synthesis/Subprocess Startup Cost Becomes Material for High-Frequency Daemon
  Backends]]'
- '[[synthesis/Reward Phishing Pairs Big-Box Retailer With Affiliated Premium Product
  Brand]]'
- '[[synthesis/Frontmatter-Only Allowlist Can Be Sidestepped Via Body Write Paths]]'
- '[[decision/Vault Edit CLI Owns Field Set Computation Before check_scope Delegation]]'
- '[[decision/ElevenLabs Speed Preference Scoped Per Instance Per User and Tracked
  on Person Records]]'
- '[[decision/Telegram reply_to_message Propagated Through Router as Classifier Hint]]'
- '[[synthesis/Three Vault-Tool Drift Sources Shared a Resampling-Disguised-As-Diff
  Pattern]]'
- '[[assumption/Fork Use Case Diversity Drives Stronger Upstream Signal Than Single-Instance
  Use]]'
- '[[synthesis/Live Handler Activation Avoids Server Refactor When Stubs Share File
  Auth and Config]]'
- '[[assumption/Uniform Egress Observation Required for Cross-Instance and BIT Visibility]]'
- '[[decision/KAL-LE Sequenced Before STAY-C to Validate Multi-Instance Architecture
  on Low-Stakes Specialist]]'
- '[[decision/Per-Instance pid_path and skill_bundle Fields Make Orchestrator State
  and SKILL File Instance-Local]]'
- '[[synthesis/Path.resolve Before is_relative_to Defeats Symlink Escape From cwd
  Allowlist]]'
- '[[assumption/E-Transfer Programmatic Sending Requires Three to Six Months Minimum
  Effort]]'
- '[[synthesis/LinkedIn Connection Notifications Fragment Into Per-Instance Records
  Despite Identical Content]]'
- '[[synthesis/Cloud Storage Phishing Campaign Rotates Sending Infrastructure Across
  Stable Template]]'
- '[[synthesis/Email Preheader Teaser Survives HTML-to-Text Conversion While Body
  Is Lost]]'
- '[[synthesis/Phishing Sender Local-Part Uses Generic Notification Word With Random
  Numeric Suffix]]'
- '[[synthesis/Curator Assigns Conflicting Confidence Scores to Duplicate Relationship
  Entries for Same Target]]'
- '[[synthesis/Patreon Creator Post Notifications Defeat HTML-to-Text Extraction Like
  Substack]]'
- '[[synthesis/Curator Assigns Maximum Confidence to Demonstrably False Relationship
  Context Claims]]'
distiller_signals: constraint:1, contradiction:2
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
project:
- '[[project/Alfred]]'
related_orgs:
- org/DigitalOcean.md
- org/Duolingo.md
- org/Blue Cross.md
- org/Anthropic.md
- org/Patreon Ireland Limited.md
related_persons:
- person/Magdalena Ponurska.md
- person/Benjamin Todd.md
- person/Kristine McNeil.md
- person/Brenna McGowan.md
- person/Jamie Sweetland.md
related_projects:
- project/Alfred.md
relationships:
- confidence: 0.9
  context: Upstream Contribution process
  source: process/Upstream Contribution — Reply 1 — Scope and field_allowlist.md
  source_anchor: Upstream Contribution scope
  target: process/Upstream Contribution — Reply 2 — Outbound transport and Stage 3.5
    substrate.md
  target_anchor: Outbound transport Upstream
  type: related-to
- confidence: 0.75
  context: Shared process topic
  source: process/Upstream Contribution — Reply 1 — Scope and field_allowlist.md
  source_anchor: Reply to Upstream Contribution
  target: process/Upstream Contribution — Reply 3 — Instructor watcher.md
  target_anchor: Instructor watcher
  type: related-to
- confidence: 0.8
  context: Both mention Upstream Contribution process.
  source: process/Upstream Contribution — Reply 1 — Scope and field_allowlist.md
  source_anchor: field_allowlist for Upstream Contribution
  target: process/Upstream Contribution — Reply 7 — BIT health check system.md
  target_anchor: BIT health checks in Upstream Contribution
  type: related-to
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
---

# Reply 1 — Scope system and the `field_allowlist` mechanism

**Problem shape.** The janitor tool has a legitimately broad mandate — it touches structural frontmatter across the vault during deep sweeps. But during a drift investigation we caught it rewriting `alfred_tags`, which surveyor owns. The agent hadn't been told it couldn't; the scope just said `"edit": True`, and the agent interpreted that latitude broadly. Scope creep that looked like a real diff but was an LLM deciding your fields were improvable.

**Solution shape.** We generalized the scope system to support per-field allowlists on any operation. `vault/scope.py` already had per-operation bools, plus a couple of special rules (`inbox_only`, `learn_types_only`). We added a `field_allowlist` permission value: when `check_scope` sees it, it looks up `{operation}_fields_allowlist` on the scope rules and requires every field the caller intends to write to be in the set.

```python
"janitor": {
    ...
    "edit": "field_allowlist",
    "edit_fields_allowlist": {
        "janitor_note",
        "type", "status",              # FM002/FM003 autofix
        "name", "subject",             # FM001 title
        "created",                     # FM001 mtime
        "related",                     # LINK002 autofix, DUP001 retargeting
        "tags",                        # FM004 scalar→list coercion
        "alfred_triage", "alfred_triage_kind", "alfred_triage_id",
        "candidates", "priority",
    },
    ...
}
```

The `alfred vault edit` CLI computes `fields = list(set_fields.keys()) + list(append_fields.keys())` before calling `check_scope`; `check_scope` fails closed when `fields is None`, so callers can't bypass by omission.

Janitor's legitimate Stage 3 enrichment (writing `description` / `role` / `email` etc. onto stub person and org records) needs a wider write surface than Stage 1/2 autofix. Rather than weaken the janitor allowlist, we split out a second scope, `janitor_enrich`, with its own allowlist for enrichment fields and with `create`/`move`/`delete` all denied. The Stage 3 enrichment pass runs under that scope; Stage 1/2 stays tight.

**Tradeoffs / what we rejected.**

- **SKILL-side "thou shalt not" guardrail** — cheapest to ship. Rejected as the primary mechanism because it relies on LLM compliance. Works as a belt alongside the scope braces, not a replacement.
- **Restructuring the agent invocation so the janitor sees issue metadata only, never the full record.** Bigger refactor; would have overlapped too much with the deterministic-writers work. Filed as a future option if field_allowlist proves insufficient.
- **Allowing the existing `edit: True` and just adding reviewer-side auditing.** The scope mechanism is the right layer to catch this — runtime enforcement, not post-hoc audit.

We also took the opportunity to close a sibling loophole: the `--body-append` / `--body-stdin` paths on `vault edit`. The frontmatter allowlist didn't cover body writes, which meant a Stage 1/2 janitor agent could theoretically sidestep by rewriting the entire body. Added an `allow_body_writes: False` flag on the janitor scope (commit `2b8ddbd`); `check_scope` rejects body writes early when the flag is set. Same-commit SKILL audit removed the "flesh out body" step from the janitor SKILL, per our scope-and-SKILL-bundled-audits rule.

**Commit range.** `433bf33..2d5e8cf` for the core Option E sequence (6 commits), plus follow-ups `657957a` (operator-directed merge scope, Q2), `2b8ddbd` (body-write loophole, Q3), `4701e56` (STUB001 fallback flag, Q6). Smoke test at `scripts/smoke_janitor_scope.sh` enforces the contract as a one-shot assertion.

Would love to hear how this echoes (or doesn't) in your thinking.
