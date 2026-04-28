---
alfred_tags:
- software/development
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
distiller_signals: decision:1, constraint:1
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: 'LINK001 — scanner false positives in distiller_learnings: YAML-escaped
  apostrophe artifact in [[decision/Salem Ghostwrites External Communications on Andrews
  Behalf]] (target exists with single apostrophe), and leading-slash artifact in [[decision//extract
  Is Idempotent with Delete-First Re-Run Contract]] (target exists at decision/extract
  Is Idempotent...md). Same pattern documented in process/Upstream Contribution Report
  — Top Level. FM001/DIR001 are autofix-handled.'
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

# Reply 4 — Multi-instance MVP: KAL-LE

**Problem shape.** We wanted to prove the multi-instance architecture end-to-end with a low-stakes specialist before tackling the higher-stakes clinical instance (STAY-C, which needs a PHI firewall). KAL-LE is a coding-focused Alfred running against an aftermath-lab vault (our internal dev-knowledge repo). Salem (the daily-driver orchestrator) classifies a user message's intent and forwards coding turns to KAL-LE via the peer API; KAL-LE handles them; the response relays back through Salem with a `[KAL-LE]` prefix.

**Solution shape.**

The peer protocol is HTTP REST, JSON, localhost-only in v1. Auth is a bearer token per peer pair in `transport.auth.tokens`. Each instance has its own config (`config.kalle.yaml`), its own state and log directories (`/home/andrew/.alfred/kalle/`), its own Telegram bot token, and its own port (convention: SALEM 5005, KAL-LE 5015, STAY-C 5025, …).

Four sub-systems landed in the 11-commit arc:

1. **Config plumbing.** `--config` widened to all subcommands. `pid_path` and `instance.skill_bundle` so each instance can point at its own orchestrator state and own SKILL file. (c1)
2. **Canonical records on SALEM.** A permissions config declares which fields each peer may read from SALEM's person records. Default-deny, audit every read. (c2 + c3)
3. **Client + server.** Real `/peer/*` + `/canonical/*` handlers swap in for the 501 stubs from the transport arc. Client dispatch uses correlation IDs written to a per-peer inbox. (c3 + c4)
4. **SKILL bundle + scope.** A `kalle` scope with `edit: True, move: False, delete: False` (curation is additive — Andrew removes canonical content, nobody else). A `KALLE_CREATE_TYPES` set adds `pattern` and `principle` as KAL-LE-only record types without polluting the base `KNOWN_TYPES`. Enforcement via `kalle_types_only` rule, same shape as `talker_types_only`. (c5)

**The `bash_exec` tool (c6).** KAL-LE needs to run tests and editors on target repos. We wrote this as the safety-critical commit — 76 new tests, every denylist item has a dedicated assertion.

Invariants:

- **Deny-first ordering.** Denylist runs before allowlist so an allowlisted head token can't mask a denylisted tail.
- **`shlex.split` + `subprocess_exec`, never `shell=True`.** Shell metacharacters pass as literal argv, no expansion. Covered with tests asserting `$(whoami)` is literally `$(whoami)` when seen by the subprocess.
- **First-token allowlist:** pytest, npm, yarn, jest, mypy, ruff, black, eslint, tsc, python, python3, node, grep, rg, find, ls, cat, head, tail, wc, diff, file, stat, sort, uniq, awk, sed, git. Git requires a subcommand from a read-only set (`status, diff, log, show, blame, branch, checkout, switch, ls-files, ls-tree, cat-file, rev-parse`).
- **Denylist substrings** (case-insensitive): all git mutation verbs, `rm -rf`, `chmod`, `sudo`, `curl`, `wget`, `ssh`, `pip install`, `npm install`, `| sh`, `| bash`, `bash -c`, `sh -c`, `eval`, `exec`, `python -c`, `python3 -c`, `node -e`. The last few came out of testing — inline-code flags on interpreters are an attack vector regardless of which interpreter is allowlisted.
- **cwd gate.** `Path.expanduser().resolve()` then `is_relative_to` one of `{aftermath-lab, aftermath-alfred, aftermath-rrts, alfred}`. Symlinks caught after resolve. `/`, `$HOME`, `/tmp`, `..` escapes reject.
- **Destructive-keyword dry-run gate.** `rm -r`, `rm -f`, `truncate `, `mv `, `cp -r` force `dry_run=True` regardless of caller's flag. Belt-and-braces against the denylist.
- **300s timeout.** 10 KB per-stream truncation. Audit log in JSONL (command, cwd, exit_code, duration, session, reason — no stdout/stderr content).

Crucially: **no `git push`, no `git commit`, no PR opening.** KAL-LE can edit, test, and branch-switch. Humans run the remote-affecting operations. This isn't a temporary constraint — it's the Bundle B + Bundle D capability split we decided on up-front.

**Operator action between builder-done and live-validation.** BotFather to create the new bot, generate four tokens (`TELEGRAM_KALLE_BOT_TOKEN`, `ALFRED_KALLE_TRANSPORT_TOKEN`, `ALFRED_KALLE_PEER_TOKEN`, `ALFRED_SALEM_PEER_TOKEN`), `alfred instance new kalle` to scaffold, then `alfred --config config.kalle.yaml up --only talker,transport,instructor`. The `alfred instance new` CLI (c8) is there specifically so future instances don't rediscover the dance.

**Tradeoffs / what we rejected.**

- **Shared API key vs per-instance keys.** Shared for MVP; will split if dogfood shows rate-limit cross-talk.
- **Thread segregation for KAL-LE responses.** Rejected; `[KAL-LE]` prefix on inline responses is enough signal for a solo user.
- **Dynamic peer registry.** Rejected indefinitely. Config-driven. Revisit past ~10 instances.
- **Tool-level routing** (Salem decides per tool call whether to forward). Deferred as possible v2. Message-level routing handles the 80% case cleanly.
- **Canonical record on-disk caching on the peer side.** Rejected — 60s in-memory TTL only, fetch on expiry. Keeps SALEM the single source of truth for identity.

**Commit range.** `01bb976..fed4b73` (11 commits) + hotfixes `1f89c0b..34245da` (4 commits). The hotfixes caught: self-target guard in the peer dispatcher (Salem-to-Salem was a real mis-fire), Salem SKILL addendum for peer-routing awareness, router classifier cue tuning, and wiring `bash_exec` into the conversation tool-use dispatch (c6 shipped the executor; c4 hotfix wired it into the turn loop).

Would love to hear how this echoes (or doesn't) in your thinking — especially the Bundle-B+D capability split.
