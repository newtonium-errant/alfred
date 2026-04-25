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
  Communications on Andrew''s Behalf]] and [[decision/extract Is Idempotent with Delete-First
  Re-Run Contract]] both resolve correctly (scanner mis-parses YAML single-quote escaping
  and the leading-slash filename). FM001/DIR001 — scanner is expected to flag these
  deterministically; no janitor action.'
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

# Reply 5 — Scheduling consolidation

**Problem shape.** Every heavy daily pass — janitor deep sweep, distiller deep extraction, distiller consolidation — fired on a rolling-24h interval. Each `alfred up` restart during development reset the clock. Over two weeks the deep sweep drifted into working hours; one morning it kicked off a large LLM burn at 11:22 local while I was mid-conversation with Salem.

Worse, this interacted with the morning brief. Brief wants clean post-sweep, post-enrichment state at 06:00. With rolling scheduling, whether brief actually got that state depended on when the daemons had last been restarted.

**Solution shape.** A shared primitive: `src/alfred/common/schedule.py`.

```python
@dataclass
class ScheduleConfig:
    time: str              # "HH:MM"
    timezone: str          # e.g. "America/Halifax"
    day_of_week: str | None = None   # "Monday" … for weekly

def compute_next_fire(cfg: ScheduleConfig, now: datetime) -> datetime:
    ...
```

Wall-clock, DST-aware via `zoneinfo.ZoneInfo`. Tested against Halifax spring-forward (2026-03-08) and fall-back (2026-11-01). The API intentionally mirrors cron's "next wakeup" shape so daemons can poll for "is it time yet?" without a scheduler daemon.

Migration was four small commits:

- **brief** — zero behavior change, moved to the shared primitive as the reference case.
- **janitor** — `sweep.deep_sweep_schedule: "02:30" Halifax daily`.
- **distiller deep extraction** — `"03:30" Halifax daily`.
- **distiller consolidation** — `"04:00" Halifax Sundays` (weekly day-of-week gate is the same primitive, just with `day_of_week` set).

The overnight choreography now looks like:

```
02:30 — janitor deep sweep
03:30 — distiller deep extraction
04:00 — distiller consolidation (Sundays only)
05:55 — BIT preflight
06:00 — brief
```

30-60 minute gaps so each stage has clean state when the next starts. Brief at 06:00 sees post-sweep, post-enrichment, post-clustering vault state every morning, regardless of when the daemons were last restarted.

**What stayed rolling.** Cheap/event-responsive work kept its old cadence:

- Janitor structural sweep — 1h rolling.
- Distiller light scan — 1h rolling.
- Transport scheduler — 30s poll for `remind_at`.
- Instructor poll — 60s.
- Mail — 300s poll.

**Surveyor is out of scope.** It's a filesystem watcher with debounce polling; reacts to vault edits when they happen, no daily deep pass. Shaped correctly already.

**Tradeoffs / what we rejected.**

- **A separate scheduler daemon.** Rejected. Each daemon polls its own schedule with the shared helper. Adding a scheduler process just to own a cron-shaped API would have introduced a single-point-of-failure coordinator for independent work.
- **Hour-of-day-only configuration.** Rejected as too coarse — 02:30 vs 02:00 mattered for the stage gap.
- **Pruning the old `*_interval_hours` fields immediately.** Kept as backward-compat fallbacks; ignored when the `*_schedule` block is present. Will prune after a deprecation cycle.
- **First-boot firing.** Old behavior fired on boot then waited 24h. New behavior seeds `last_consolidation = now` so restarting at, say, 14:00 doesn't immediately fire consolidation. Contradicted the overnight-only intent.

**One known follow-up.** The morning brief fired ~16 minutes early today despite clock-aligned scheduling. Debugging under way — suspect `asyncio.sleep` drift under load. The clock-alignment math itself has good test coverage; the wait loop that consumes its output is the suspect.

**Commit range.** `3f14226..d1b4d6c` (5 commits).

Would love to hear how this echoes (or doesn't) in your thinking.
