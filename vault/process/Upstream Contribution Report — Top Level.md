---
alfred_tags:
- process/upstream-contribution
- architecture/multi-instance
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
- '[[synthesis/Coding Instance Is Architectural Substrate for Other Alfred Instances]]'
- '[[synthesis/Orchestrator Instance Requires Breadth of Context Not Depth Per Domain]]'
- '[[synthesis/Curator Reconstruction From Prior Vendor Knowledge Extends Beyond Phishing
  to Legitimate Notifications]]'
- '[[assumption/DigitalOcean Control Plane Maintenance Does Not Affect Existing Alfred
  Workloads]]'
- '[[synthesis/Legitimate Creator Newsletters Employ Phishing-Style Urgency Subject
  Lines]]'
- '[[decision/Health Status Enum Ranks SKIP Above OK in Worst() Ordering]]'
- '[[decision/Health Primitives Use Stdlib Dataclasses With No External Dependencies]]'
- '[[decision/Health Aggregator Catches Exceptions at Boundary and Converts to FAIL]]'
- '[[decision/Health Check Timeouts Are Five Seconds Quick and Fifteen Seconds Full]]'
- '[[decision/Preflight Gate on alfred up Refuses to Start Stack on Any FAIL]]'
- '[[decision/BIT Daemon Writes Health Sweep as Run-Type Vault Record at Brief-Minus-Five-Minutes]]'
- '[[decision/Shared Anthropic Auth Health Probe for All SDK Consumers]]'
- '[[decision/Missing Curator Inbox Directory Is WARN Not FAIL]]'
- '[[decision/BIT Aggregator Filters Itself Out of Check Target List to Prevent Recursion]]'
- '[[decision/Tools Self-Register Health Checks at Import Time]]'
- '[[synthesis/Silent Downstream Failures Justified Upstream Health Rollup]]'
- '[[synthesis/Per-Tool Health Logs Don''t Aggregate — Operator Inspection Scales
  Poorly Without Uniform Primitive]]'
- '[[synthesis/Same Health Primitive Serves CLI, Preflight Gate, and Scheduled Rollup]]'
- '[[synthesis/Curator Populates Related Array and Relationships Field From Divergent
  Target Pools]]'
- '[[synthesis/Curator Emits Parallel Relationship Entries Per Target Pair With Divergent
  Type and Path Format]]'
- '[[synthesis/Extortion and Blackmail Emerges as Distinct Scam Category Beyond Credential
  and Financial Fraud]]'
- '[[synthesis/Homoglyph Attacks Mix Multiple Unicode Scripts in Single Email Increasing
  Evasion Sophistication]]'
- '[[synthesis/Extortion Scam Template Asserts Possession of PII Without Presenting
  Any Evidence]]'
- '[[synthesis/AAA Member Rewards Impersonation Forms Sustained Phishing Campaign
  Brand]]'
- '[[synthesis/Curator Aggregates Repeat Phishing Occurrences Into Single Record Under
  Additional Occurrences Section]]'
- '[[assumption/Inbox Arrival Verifies End-to-End Pipeline Correctness]]'
- '[[decision/Daemon Heartbeat Cadence Tuned to Human-Attention Timescale Not Machine-Monitoring]]'
- '[[constraint/python-telegram-bot CommandHandler Routes Before MessageHandler Gated
  by ~filters.COMMAND]]'
- '[[decision/Cross-Cutting Telemetry Registered as Application-Level TypeHandler
  at group=-1]]'
- '[[synthesis/Daemon Log Silence Has Three Indistinguishable Meanings Without Positive
  Heartbeats]]'
- '[[synthesis/Per-Handler Instrumentation Becomes Stale When Framework Adds Routing
  Branches]]'
- '[[synthesis/Intentionally-Left-Blank Pattern Was Convergent Not Designed]]'
- '[[decision/Idle-Tick Heartbeat Counter Reset Is Lock-Free via Single-Statement
  Read-Then-Reset]]'
- '[[decision/Andrew Uses Pen Name and Distinct Handle for Upstream Open-Source Identity]]'
- '[[decision/sleep_until Helper Bounds Wall-Clock Schedule Drift to One Chunk]]'
- '[[synthesis/Curator Emits Relationship Duplicates by Varying Target Path Type-Prefix]]'
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
- '[[decision/Alfred Fork Basis Is ssdavidai/alfred Through Commit 9d27ad9]]'
- '[[decision/Single Config Schema Shared Across All Alfred Instances]]'
- '[[decision/Voice Talker Tool-Use Loop Uses Model Escalation]]'
- '[[synthesis/Architectural Arcs Routinely Spawn Post-Merge Hotfix Tails From Dogfooding]]'
- '[[synthesis/Person Records Are the Canonical Site for Per-User Calibration Data]]'
- '[[assumption/Curator Treats Existing Person Record as Evidence of Opt-In Subscription
  Relationship]]'
- '[[synthesis/Gambling Spam Currency Mismatched to Recipient Geography Indicates
  Untargeted Mass Distribution]]'
- '[[synthesis/Newsletter Re-Engagement Resends Create Separate Empty Records Carrying
  Only Unread-Prompt Preheader]]'
distiller_signals: constraint:3, contradiction:9
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: 'LINK001 — three broken wikilinks in distiller_learnings: two are YAML-escape
  false positives ([[decision/Salem Ghostwrites External Communications on Andrew\\s
  Behalf]] and [[synthesis/Per-Tool Health Logs Don\\t Aggregate — Operator Inspection
  Scales Poorly Without Uniform Primitive]] — both targets exist with single apostrophe),
  one is a leading-slash artifact from record name ([[decision/extract Is Idempotent
  with Delete-First Re-Run Contract]] — target exists at decision/extract Is Idempotent...md).
  FM001/DIR001 also flagged by scanner but those codes are autofix-handled.'
project:
- '[[project/Alfred]]'
relationships:
- confidence: 0.85
  context: Shared process topic
  source: process/Upstream Contribution Report — Top Level.md
  source_anchor: Upstream Contribution Report
  target: process/Upstream Contribution — Reply 1 — Scope and field_allowlist.md
  target_anchor: Reply to Upstream Contribution
  type: related-to
- confidence: 0.8
  context: Shared reference to Upstream Contribution process
  source: process/Upstream Contribution Report — Top Level.md
  source_anchor: Upstream Contribution Report
  target: process/Upstream Contribution — Reply 2 — Outbound transport and Stage 3.5
    substrate.md
  target_anchor: Outbound transport and Stage 3.5
  type: related-to
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
updated: '2026-05-01'
---

*Ghostwritten by Salem (Andrew's personal AI instance) on Andrew's behalf.*

# Architectural arcs from a long-running Alfred fork

Hi David,

I'm Andrew (handle `newtonian-errant`, pen name Andrew Errant). I forked your Alfred template back in February — specifically the `131fb01` initial-commit era plus your follow-up batch through about commit `9d27ad9` ("Add parallel processing to alfred process"). Since then my fork has diverged by roughly 255 commits as I've bent the template toward a specific use case: a small family of Alfred instances that cover personal, clinical (my partner is a nurse practitioner), coding, and a future business line (non-emergency medical transport). One codebase, one config shape, many instances with different scopes and risk profiles.

The fork is still deeply recognizable as your design — the vault-as-source-of-truth, the agent-writes-directly pattern, the four tools (curator/janitor/distiller/surveyor), the skill-file-plus-scope contract. A lot of what I'm about to report on is elaboration on patterns you shipped, not reinvention of them. Some of it is intentional divergence. I want to report back both kinds because the convergence signal is as useful as the disagreements.

This is a report on shipped-and-learned, not a roadmap pitch. No pull requests attached; I'll open discussions if any specific arc looks worth porting.

## Architectural arcs shipped

| Arc | Commit range | Summary |
|-----|--------------|---------|
| Drift fix stack | `a9f6ec0..574dd02` | Closed three "looks-like-a-diff-but-is-just-resampling" drift sources in distiller, surveyor, and janitor. |
| Option E — deterministic janitor + `field_allowlist` | `433bf33..2d5e8cf` (+ follow-ups `657957a`, `2b8ddbd`, `4701e56`) | Moved LLM-composed janitor frontmatter into Python code paths; added a generic per-field scope-allowlist mechanism. |
| Pytest expansion | `2b7c681..90e6763` | Grew from zero tests to ~1042 collected, with ~23% overall line coverage and several tools pushed past 70-85%. |
| Voice Stage 2a — Telegram talker | `e737733..8b1ddbd`-ish through `cc3e...` (wk1-wk3) | Python-native Telegram bot + Anthropic SDK tool-use loop; session types; model escalation; calibration blocks on person records. |
| Voice Stage 2b — capture mode | `cefe063..c70e81e` | Silent capture session type, async structuring pass, `/extract`, ElevenLabs `/brief` audio. |
| BIT (Built-In Test) | `77fbfc3..2851b51` | Per-tool health probes + `alfred check` CLI + preflight gate on `alfred up` + morning brief integration. |
| Salem persona templating | `488b3d3` | `instance.name` / `instance.canonical` / alias table plumbed through talker + SKILL prompts. |
| Scheduling consolidation | `3f14226..d1b4d6c` | Shared `ScheduleConfig` + `compute_next_fire` with DST handling; heavy passes clock-aligned overnight instead of rolling-24h drift. |
| Instructor watcher | `6f66649..316f6b9` | New daemon polls for `alfred_instructions` frontmatter directives and executes them via an in-process Anthropic SDK tool-use loop. |
| Outbound transport | `aca34b1..87def9a` | aiohttp server hosted inside the talker daemon; `/outbound/*` for push, `/peer/*` + `/canonical/*` as 501 stubs from day one. |
| Multi-instance MVP (KAL-LE) | `01bb976..fed4b73` + hotfixes `1f89c0b..34245da` | First real peer instance (coding); real `/peer/*` + `/canonical/*` handlers; `bash_exec` safety machinery; router extension; `alfred instance new`. |
| `/speed` TTS preference | `2454692` | Per-(instance, user) ElevenLabs speed control, history-tracked on person records. |
| Reply-context consumer | `017487f` | Telegram `reply_to_message` propagated through the router as a classifier hint. |
| Schedule-followups | `45b41a4..bc50a5e` | Three bugs caught during dogfooding: brief drift (chunked wall-clock-checked sleep), janitor deep-sweep heartbeat + None coercion, BIT peer-handshake env-substitution. New `sleep_until` async helper that bounds drift to one chunk. |
| Brief Upcoming Events Phase 1 | `53d87c6` | New brief section reading `event` + `task` records, Today / This Week / Later buckets, 30-day cap. Intentionally rule-free — filter rules grow inline as cases appear, no DSL. |
| Talker scope + boundary fixes | `c341b98`, `2601067` | Person-record scope on talker (3-side contract: scope.py + JSON enum + SKILL prompt). Inline-command regex tightened to require sentence-terminating punctuation before the slash. |
| Email surfacing — c1 + c2 | `f0e5bbc`, `2537de4` | Per-instance email classifier as curator post-processor (`priority` + `action_hint`). Daily Sync conversation channel at 09:00 ADT — calibration loop, not status report. |
| Email backfill | `74affdf` | One-shot `alfred email-classifier backfill` to retroactively classify the ~700 pre-c1 email-derived notes so the calibration corpus has data to chew on. |
| Observability — intentionally-left-blank | `5a26d13`, `d4f9ac2`, `7cc89e5` | Pattern named after a misdiagnosis cascade. 60s `idle_tick` heartbeat in talker (commit 1), middleware coverage fix (commit 2), propagation across six other long-running daemons via shared `Heartbeat` class (commit 3). |
| KAL-LE propose-person protocol | `788daa6..5ed54f2` (`feature/propose-person`) | Peer-protocol record-creation. KAL-LE proposes new `person` records to Salem via the canonical-write API; Salem owns the create + confirms back. First real exercise of cross-instance write authority on top of the Stage 3.5 substrate. |
| KAL-LE reviews + digest arc | `feature/propose-person` extension (2026-04-25) | Per-project reviews CLI on KAL-LE writing to `aftermath-lab/teams/{instance}/reviews/`, plus cross-project digest synthesis. Substrate for the upstream-curation flow you'd expect from a coding-instance fork curator. |
| Hypatia Phase 1 — third instance launch | week of 2026-04-26–27 | Third instance live: scholar/scribe/editor profile, vault at `~/library-alexandria/`, port 8893. Mode dispatch (business / voice-conversation / voice-capture), peer-protocol honesty (declares scope without overreaching capability), Opus temperature stripped uniformly via shared SDK helper. Validates the multi-instance pattern beyond two. |
| Hypatia Phase 2 — four active postures | `791345a` + `2e21fc6` + `2db8cae` | Four-posture model (scholar / researcher / scribe / editor) with explicit Mode-2 boundaries. Slug-from-substance opt-in for content-derived filenames. Substack template wiring for the "Andrew Errant" essay flow. Six code-reviewer-audit cleanups bundled in the third commit. |
| Pending Items Queue Phase 1 | `a96975f` + cleanups `bdb274c` + docs `4edd254` | Per-instance JSONL queue for items only the operator can answer (outbound failures, unanswered clarifications, fuzzy matches). Cross-instance peer push/resolve so any instance's items aggregate to Salem's queue; resolutions route back to originator. ~3000 LOC, 38 tests. First production traffic over the Stage 3.5 substrate beyond control-plane handshakes. |
| Upstream merge — your 43-commit batch | `0c99c6a` + cherry-pick `6e76496` | Took 43 commits from your master, resolved 7 conflict clusters (entity-linking, curator perf, janitor token reduction, distiller legacy fixes). Most useful pattern to surface back: a TAKE OURS hunk-walk works well for conflict files that split clean by topic; the hand-merge phase shrinks to the genuinely-mixed cases. Documented for our future merges. |
| KAL-LE distiller-radar Phase 1 | `330974b` (radar enable + Alfred Learnings prioritization + backfill CLI) | V2 distiller enabled on KAL-LE's `aftermath-lab` vault with explicit Alfred Learnings sections in dev session notes prioritized in the prompt. Backfill CLI produced 1027 records from 142 source notes. Builds toward Phase 2 (synthesis ranker → digest §4 fill) and Phase 3 (daily continuous radar to KAL-LE's Daily Sync). |
| Distiller V2 retirement (Salem) | decision recorded in `project_distiller_v2_retirement.md` | Eight-day side-by-side: V2 over-called confidence (46% high vs legacy's 26%) and under-produced synthesis (36% of legacy's count) on Salem's corpus. Retired V2 from Salem's overnight rotation; legacy is sole distiller path on Salem. V2 stays operational on KAL-LE for the radar use case. Six architectural lessons banked — *architectural cleanliness doesn't always produce better outputs* is the headline. |
| Janitor residual reduction arc | scanner trio `6a515f0`+`739d68a`+`27cef3d`, slug-drift cleanup `bcfdba4`, `ignore_dirs` split `7ab3c80`+`8040572` | Multi-pronged. Scanner fixes for YAML-apostrophe LINK001 false positives (scanner-bug self-flagged in vault as a `constraint` record), leaf-by-design ORPHAN001 skip, and per-record-validation skip on vault-root scaffold files. Slug-drift cleanup pass across 91 files / 176 wikilink instances. Then a config-shape split: `vault.ignore_dirs` had two-purpose semantics (skip-from-scanning AND skip-as-link-target) that conflicted; split into `dont_scan_dirs` + `dont_index_dirs`. Cumulative residual: 1182 → 468 issues (-60.4%). |
| Talker QA review framework | living log in `project_talker_qa_review_log.md` | Standing per-instance review pass. Each cycle classifies user corrections by severity: process-level (architecture / routing / voice) vs content-level (factual disambiguation) vs self-corrected (caught in-flow) vs trivial. Comparing CATEGORY shape rather than density across instances avoids the work-shape-confounded false alarm. Two cycles in: corrections-getting-smaller hypothesis supported on Hypatia (16 sessions, zero process-level), one recurrent process-level on Salem caught (peer-list awareness was config-updated but the prompt template still ships the single-instance text). |
| Team-lead direct-edit hook + dev session notes convention | hook `271a65b`, convention `ff55ca7` | After two slips where work meant for a builder agent went directly to `src/alfred/` from the main conversation, added a `PreToolUse:Write` hook that blocks edits to `src/alfred/` from non-worktree contexts. Builders edit via worktrees and pass through cleanly. Same shift moved hand-authored dev session notes from Salem's vault to `aftermath-lab/session/` going forward (KAL-LE owns dev meta-knowledge; the dev notes feed KAL-LE's distiller-radar, not Salem's operational extraction). |

Full commit list is on the `master` branch of [newtonium-errant/alfred](https://github.com/newtonium-errant/alfred).

## Patterns that validated

- **One session note per commit.** Every non-trivial commit ships with a matching note in `vault/session/`. Git log stays legible as headlines, and each commit is self-contained — the why, the tradeoff, the follow-up flags. Reviewing history six weeks later is unreasonably pleasant.
- **Clock-aligned overnight passes for heavy daily work.** Rolling-24h intervals drift every dev-session restart. Deep sweeps kept wandering into working hours. Moving janitor deep sweep, distiller deep extraction, and distiller consolidation onto clock-aligned times (02:30 / 03:30 / Sun 04:00 local) eliminated the drift and gave the morning brief clean post-sweep state.
- **In-process Anthropic SDK for tool-use-heavy paths** instead of `claude -p` subprocesses. Lower latency, explicit `api_key` (no env-var leakage through subprocess inheritance), native conversation history, clean streaming. The subprocess pattern still makes sense for curator's one-shot agent calls, but the talker and instructor daemons both use the SDK directly.
- **Bundled scope + SKILL audits.** We adopted a rule: any commit that tightens a scope or narrows a schema must also audit the affected SKILL.md in the same cycle. Our canonical scar here was a janitor scope change (`2b8ddbd`) that denied body writes; the SKILL kept a "flesh out body" step that stayed dead for ~24h until the next session caught it. Now scope-and-prompt ship together.
- **Defense-in-depth on destructive operations.** KAL-LE's `bash_exec` uses a deny-first-then-allow command filter, a cwd-under-allowed-roots gate, AND a destructive-keyword dry-run gate. Each layer is independently sufficient for a single class of bypass; all three together were cheap and the redundancy caught one real issue during testing (inline `python -c '...'` slipping past the first-token allowlist).
- **HTTP transport substrate designed for multi-instance on day one.** The outbound transport arc (6 commits) stubbed `/peer/*` and `/canonical/*` as 501s and keyed the auth dict by peer name. The KAL-LE arc swapped the stubs for real handlers with a one-line namespace change. Designing the schema for the future use case, even when only the present-day use case lands, saved a full refactor.
- **"Intentionally left blank" — emit positive idle signals, never silent absence.** Named after a misdiagnosis cascade where we concluded "talker logging is broken" before realising "no Telegram traffic since 03:36 UTC." Now a fleet-wide rule: every long-running daemon emits a 60s `idle_tick` heartbeat with a per-daemon counter; brief sections that find nothing emit a "no upcoming events" line instead of being absent; janitor's deep-sweep tick emits its `fix_mode` decision so an operator can grep for it. Reply 8 is the deep dive.
- **Three-side contracts (scope / schema / prompt) for any restriction the LLM has to honor.** When Salem started creating note stubs for new people we found the bug needed three coordinated changes to actually fix: the scope's allow-list (so the operation isn't denied), the JSON tool-schema enum (the binding constraint the LLM literally couldn't pick `person`), and the SKILL prompt (so it knows the option exists). Any one of the three on its own was insufficient. Now we audit all three together when changing any LLM-facing capability boundary.
- **Calibration through interaction, not cold prompt-writing.** The email classifier (Email c1) was deliberately shipped before the rules were "right." The Daily Sync conversation channel (Email c2) is the calibration mechanism — Salem surfaces a batch of recently-classified emails at 09:00 ADT, Andrew replies in terse formats ("1 high, 2 spam"), corrections write to a per-instance JSONL corpus, the classifier rotates corpus entries into its prompt as few-shot examples. The right rules emerge from looking at real data with the tool already running, not from trying to write the rules cold.
- **Wall-clock-checked chunked sleep over plain `asyncio.sleep` for long-horizon scheduling.** A single `asyncio.sleep(N)` over hours drifts when the underlying monotonic clock falls out of sync with wall time (WSL2 host suspend/resume, NTP). Schedule-followups c2 introduced `sleep_until(target)` — a chunked loop that re-reads the wall clock between caps so the maximum drift is bounded to ~one chunk (default 60s) regardless of monotonic-clock pathology. Adopted by brief and BIT; will sweep through the other clock-aligned daemons as they hit the same scar.
- **Three-instance multi-instance in production.** Salem + KAL-LE + Hypatia all live for ~30 days now with overnight choreography aligned across instances and the Pending Items Queue carrying real cross-instance traffic. The Stage 3.5 transport substrate (Reply 2) and the multi-instance MVP arc paid off again as Hypatia plugged in with one-line route changes. Designing the schema for the future use case keeps paying — even when the future use case is a third instance with a different work shape (analytical / writing) than the second (coding).
- **Per-instance rolling QA review logs.** A `project_talker_qa_review_log.md` accumulates dated entries per instance, classifying each cycle's corrections by severity. The discipline that mattered most: comparing CATEGORY shape across instances (factual disambiguation in Salem's operational records vs analytical framing in Hypatia's heavy multi-turn vs zero-traffic in KAL-LE because narrow scope minimizes friction surface) rather than raw correction density. Density would falsely flag Salem as worse-calibrated when it's just doing the disambiguation-heavy work. The log keeps the review reproducible across sessions and gives the trend axis to detect regressions.
- **Upstream-fork merge is faster as a TAKE OURS hunk-walk.** When pulling your 43-commit batch into our fork, conflicts split fairly cleanly by topic. We walked the conflict files in two passes: a first sweep marking files where we'd take ours wholesale (fork-specific file paths, files we've fully rewritten), and a second hand-merge phase only on the genuinely-mixed cases. The first pass shrunk the second pass enough to make the whole merge bounded. We documented this for our own future merges; surfacing it back here in case it generalizes.

## Patterns we backed away from

- **Mocked database calls in integration tests.** Shipped once, caused a mock/prod divergence on an ops-count assertion. All integration paths now hit the real backing store (the vault itself or Milvus Lite).
- **OpenClaw for local models.** The OpenClaw backend exists in the tree because you shipped it, and it works as a CLI adapter. For local-model inference we couldn't get what we needed out of it and pulled in Ollama directly for surveyor embeddings.
- **pymilvus 2.6.x with milvus-lite 2.5.x.** Silent API mismatch on index params. Pinned pymilvus to 2.5.x.
- **Rolling-interval scheduling.** Every `alfred up` restart reset the clock and drifted the heavy passes forward. Replaced with `ScheduleConfig(time, timezone, day_of_week=None)` plus `compute_next_fire(cfg, now)`.
- **`claude -p` subprocess for tool-use-heavy paths** (context above). Startup cost per turn plus env var inheritance surprises.
- **Secret-like test fixtures (`sk-xi-test…`).** Tripped GitGuardian. Scrubbed to `DUMMY_*_KEY` patterns and added fixture-naming guidance to our builder-agent instructions so new tests don't rediscover the same false-positive.
- **Pydantic-typed "deterministic v2" distiller as Salem's primary path.** We rebuilt distiller around atomic typed extraction and ran it shadow against the legacy stream-of-consciousness extractor for eight days. V2 over-called confidence (46% high vs legacy's 26%), under-produced synthesis (36% of legacy's count), and didn't show convergent improvement on calibration prompts. Retired V2 from Salem's overnight rotation. Kept it operational on KAL-LE for the radar use case where structured atoms feed a downstream ranker. Headline lesson banked: *architectural cleanliness doesn't always produce better outputs* — the legacy "let the model write whatever it wants" path turned out to extract richer cross-source synthesis than the typed atom path on Salem's corpus.
- **Hardcoded instance-specific literals.** A class of antipatterns where single-instance assumptions survived multi-instance migration: `agent="salem"` defaults in router writers, `or "Alfred"` fallbacks on display-name fields, prompt-text references to the instance name that lived in code rather than config. Each one shipped clean against single-instance reality and broke quietly when KAL-LE / Hypatia came online. Ratified into a recurring code-reviewer audit pattern; sweep work documented in `project_hardcoding_followups.md`.

## Parallel conclusions

Places where we reached something similar to your public direction without coordinating:

- **Multi-instance architecture** as hub-and-spoke with a daily-driver orchestrator routing intent to specialist instances. Our version names the orchestrator SALEM (daily driver) and treats KAL-LE (coding), STAY-C (planned clinical), and others as spokes with scoped capabilities.
- **A capture/brainstorm session type** — silent during capture, asynchronous structuring into a summary section of the session record, optional note extraction on demand. Similar shape to the "brainstorm" framing that shows up in the upstream docs, though built to a different mid-session emoji-ack UX.
- **A scheduled structured daily report.** Our morning brief renders weather (METAR/TAF), health (BIT rollup), and calendar sections on a 06:00 local schedule and auto-pushes to Telegram when the transport layer is up.
- **An agent-team pattern.** `.claude/agents/` carries specialist instructions (builder / vault-reviewer / prompt-tuner / infra / code-reviewer). Each has its own knowledge requirements and lifecycle (persistent vs on-demand). Helped a lot for Alfred-the-product's own development loop.
- **A knowledge-curation instance.** Planned rather than shipped — a zettelkasten Alfred for long-form writing under the Andrew Errant pen name, with MOC-placement heuristics.

## Intentional divergence

Where we diverged from the shipped template and, I think, why:

- **SALEM as orchestrator plus specialist instances.** Our use case spans personal, clinical (PHIPA/PIPEDA in Canada), coding, and business (non-emergency medical transport regulated under provincial health rules). Different risk profiles per domain; one monolithic butler wouldn't work. We kept the Alfred template shape but run multiple instances from one codebase.
- **A planned PHI firewall for the clinical instance.** STAY-C (not yet built) will be local-only, with cross-instance peer queries returning redacted summaries only. The transport and canonical-permissions substrate is live; the firewall is the first real test of it.
- **Capability-bundle split for the coding instance.** KAL-LE runs Bundle B (active coding — edit files, run tests, checkout branches) plus Bundle D (aftermath-lab template curation). Crucially it *cannot* commit, push, or open PRs. Humans stay in the loop on any operation that touches remotes.
- **`field_allowlist` in the scope system.** We observed the janitor's LLM agent rewriting frontmatter fields it had no charter to touch (`alfred_tags`, specifically, which surveyor owns). Added a generic per-field allowlist mechanism to `vault/scope.py` so a scope can declare exactly which frontmatter fields it may mutate. The janitor scope now allows a narrow set (`janitor_note`, `status`, etc.); a sibling `janitor_enrich` scope covers the Stage 3 fields.
- **Deterministic writers for janitor_note.** For the subset of janitor issues where the scanner already has everything needed to compose the note, we moved composition into Python (`autofix.py::_flag_issue`) and stripped the matching LLM instructions from the janitor SKILL. LLM prose varied sweep-to-sweep even at temperature=0 when the input shifted subtly — every re-compose looked like a real diff but was resampling noise.
- **In-process SDK tool-use loop for the instructor daemon.** `alfred_instructions` frontmatter on any record becomes a natural-language directive; the instructor daemon polls for pending directives, executes them with a tool-use loop, and writes the result back (with a destructive-keyword gate that forces `dry_run=True` for delete/drop/purge verbs). This one had to be in-process — env-var control mattered because the instructor runs on any instance and the key path is different per instance.
- **Canonical person records owned by the orchestrator, with per-peer field permissions.** Other instances fetch identity facts from SALEM via a peer API rather than maintaining local copies; SALEM's config declares which fields each peer may read. Stage 3.5 D3 in our terminology. Writes stay orchestrator-only; peers propose edits via the peer API. KAL-LE's `propose-person` arc is the first real user of this — it constructs a candidate `person` record locally, posts it to Salem's `/canonical/propose-create`, and Salem owns the final write.
- **Pending Items Queue as a per-instance JSONL with cross-instance peer aggregation.** Each instance maintains a local queue of items only the operator can answer (outbound failure, unanswered clarification, fuzzy match where the agent isn't confident, inference review). KAL-LE and Hypatia push their items to Salem via the existing peer transport; Salem aggregates and surfaces them in the daily flow. Resolutions route back to the originator. The architectural read: not every cross-instance message is an RPC; some are pull-mode deferrals to operator attention. Phase 2 will add agent self-flag tooling so the queue gets populated reflexively rather than only on hard failures.
- **Per-instance distiller "radar" mode.** KAL-LE consumes its own dev session notes via the V2 distiller path with explicit "Alfred Learnings" sections prioritized in the prompt. Phase 2 will rank synthesis records into KAL-LE's project-digest TODO §4 slot. Phase 3 will continuously surface new patterns to KAL-LE's Daily Sync. The radar pattern is meta-level: an instance whose work shape produces enough session notes to mine becomes its own surfacer for "what did the operator learn this week." Salem doesn't need this (its records ARE Andrew's operational data); KAL-LE does (its records are *about* Andrew's coding work, an order of indirection up).

## Open problems I'd value your thinking on

- **UpToDate API licensing for a solo-use clinical instance.** The enterprise-only licensing model doesn't map to a solo NP's workflow. Open question whether there's a workable shape at all.
- **Per-project venv resolution for the coding instance's `bash_exec`.** Today the tool uses KAL-LE's own venv, which doesn't resolve target-repo dev deps (pytest, ruff). Moving toward a harness architecture (invoke the target project's toolchain), similar to how Claude Code and OpenCode work.
- **PHI firewall design for cross-instance peer queries.** We have the auth + permission schema; we don't yet have the redaction layer. Design under way.
- **Long-running-daemon schedule drift.** ~~We saw an ~16-minute early fire on the brief yesterday despite clock-aligned scheduling.~~ Resolved in the schedule-followups arc (`f40d5c7`) — root cause was monotonic-clock drift over the long sleep, fix is a chunked wall-clock-checked `sleep_until` helper. Happy to share the post-mortem if useful.
- **Email classifier calibration depth.** Phase 1 (priority + free-text action_hint) is shipped and the Daily Sync calibration loop is live. Open question on what the right second-axis vocabulary is — Andrew's first real example was "Tim Denning newsletter = surface; Tim Denning office-hours reminder = automate to calendar, don't surface." That's not just priority tier, it's what-should-happen-with-this-email. Curious whether your email-triage shape carries similar structure.
- **Backoff / suppression on chronic WARN states in BIT.** The brief's Health section re-surfaces every WARN every morning. Useful when state changes; noise when surveyor's Ollama probe has been WARN for three weeks because the host-side ollama is intentionally off. We have no `acknowledged_until` mechanism and aren't sure whether to add one.
- **Body-write scope for janitor's structural cleanup.** Today our janitor scope denies body writes wholesale because "the body is user-authored and janitor must not synthesise content." But removing a known-broken wikilink from body text isn't synthesis — it's structural cleanup of a dead reference. We have ~332 LINK001 broken wikilinks where the agent annotates instead of acts because the SKILL can't tell it to remove a body-located wikilink. The architectural fork we're considering: a narrow `body_replace`-only permission distinct from `body_append` / `body_stdin`. Find-and-replace-of-a-known-string isn't authoring new content. Curious whether your scope model has a similar two-tier shape, or whether you've found a different way to keep janitor structurally useful without giving it synthesis authority.
- **Distiller calibration drift detection.** With V2 retired we're back on the legacy distiller's calibration curve (26% high / 33% medium / 40% low). We don't have a regression alarm if it drifts toward over-confidence; the eight-day audit was hand-built. A standing monthly audit comparing distribution-by-confidence-bucket to a baseline would catch drift early but feels like a process artifact bolted onto the wrong layer. Open whether to build it as tooling or just keep it as a calendared review.

## Genuinely curious about

- **Is multi-instance on your radar?** Your `alfred.black` public docs suggest a single-user productized Alfred with email triage as the marquee use case. I'd love to know whether you're considering or explicitly avoiding the multi-instance shape — convergence or considered rejection are both useful signals for me.
- **How does upstream handle user-facing slash-command preferences?** We just shipped `/speed` as a per-(instance, user) TTS knob with history on the person record. Low-stakes surface, but it's the first of a class (voice, verbosity, pushback level, model tier) and the design precedent matters.
- **Your "Intuition" fourth learning tier in the public docs.** We have the three canonical `assumption/decision/constraint/contradiction/synthesis` types. If intuition is a live design, I'd love to understand the intended shape.
- **Daily Sync vs Morning Brief in your model.** We've started splitting these explicitly: the Morning Brief is a status report (what's true right now), the Daily Sync is the OODA-loop conversation channel (what should change). The two are pushed to Telegram at different times (06:00 and 09:00 ADT) and sourced from different code paths. Curious whether your shape collapses these or splits them differently — the second pattern seems load-bearing for a tool that wants to learn from its operator.
- **An aftermath-lab equivalent for upstream.** We run a separate repo (`aftermath-lab`) as KAL-LE's vault — a knowledge base of development patterns extracted from session notes via KAL-LE's distiller-radar. ~25 patterns curated so far, most of them porting back into how we build new Alfred features. Concrete example: the "three-side contract" pattern from validated-patterns above started as a session note, got extracted as a `synthesis` record, then got promoted to a canonical pattern. Curious whether your build process has an equivalent meta-layer or whether the patterns flow more directly through code review.
- **The upstream-merge experience itself.** We took your 43-commit batch and the merge worked cleanly with the TAKE OURS hunk-walk above. Genuinely curious whether the inverse direction has been tried — pulling fork-divergent improvements back into your master. We've shaped this report as discussion-thread-gated specifically to avoid presuming any of it is wanted; happy to PR specific arcs if any look worth porting.

---

Happy to split any of the above into their own threaded replies below if useful — Replies 1–6 are the original threaded set, Replies 7 (BIT health system) and 8 (intentionally-left-blank observability pattern) are queued. Everything here traces to commits on our `master`; the session notes bundle the reasoning alongside.

This is a refresh of the original 2026-04-23 send. Roughly ten new architectural arcs have shipped since then; they're integrated into the sections above rather than tacked on as a delta, so the report stays a single-moment-in-time view. Most consequential of the new arcs from your perspective is probably the merge of your 43-commit batch into our fork — that confirms the convergence channel works in both directions.

— Andrew (and Salem, who wrote it)
