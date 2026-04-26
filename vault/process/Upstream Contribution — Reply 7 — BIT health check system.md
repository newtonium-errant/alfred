---
alfred_tags:
- process/upstream-contribution
- system-monitoring
created: '2026-04-22'
distiller_learnings:
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
distiller_signals: assumption:2, constraint:1, contradiction:8
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: LINK001 — synthesis/Per-Tool Health Logs Dont Aggregate target exists
  with regular apostrophe; YAML-escaped apostrophes in distiller_learnings wikilinks
  defeat the scanner. FM001/DIR001 — file is type=note in process/ directory; autofix
  should relocate.
project:
- '[[project/Alfred]]'
relationships:
- confidence: 0.8
  context: Both discuss Upstream Contribution process.
  source: process/Upstream Contribution — Reply 7 — BIT health check system.md
  source_anchor: BIT health checks in Upstream Contribution
  target: process/Upstream Contribution — Reply 8 — Intentionally-left-blank observability
    pattern.md
  target_anchor: Observability patterns for Upstream Contribution
  type: related-to
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
---

*Ghostwritten by Salem (Andrew's personal AI instance) on Andrew's behalf.*

# Reply 7 — BIT (Built-In Test) health system

**Problem shape.** Alfred is a fleet of long-running async daemons with a vault as the source of truth. There was no single answer to the question "is everything healthy?" — every tool had its own log, its own state file, and its own private definition of "stuck." Operator inspection was a five-terminal exercise. Worse, the morning brief at 06:00 assumed clean post-sweep state, but if Ollama was down or a backend's auth had expired overnight, the brief landed in Telegram with quiet gaps and the failure surfaced an hour later when something downstream broke. We needed a uniform health primitive that every tool could opt into, an operator CLI that read it, a preflight gate that refused to start a broken stack, and a way to surface the rollup in the morning brief itself.

**Solution shape.** A four-layer system:

1. **Per-tool `health.py` registering with a shared aggregator.**
2. **`alfred check` CLI** that fans out, streams results line-by-line, and exits non-zero on any FAIL.
3. **`alfred up --preflight` gate** that runs the same sweep before spawning daemons; FAIL aborts.
4. **A scheduled BIT daemon** that writes the sweep as a `run`-type vault record at brief-minus-5min, plus a Morning Brief integration that re-renders the latest record as a `## Health` section.

The primitives are deliberately small: a `Status` enum (`OK / WARN / FAIL / SKIP`), a `CheckResult` (one probe), a `ToolHealth` (one tool's rollup), and a `HealthReport` (the whole sweep). All four are plain dataclasses in `src/alfred/health/types.py` with no dependencies beyond stdlib — so each tool's `health.py` can import them without circular-import risk.

```python
class Status(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    @classmethod
    def worst(cls, statuses: list["Status"]) -> "Status":
        order = {cls.FAIL: 3, cls.WARN: 2, cls.SKIP: 1, cls.OK: 0}
        return max(statuses, key=lambda s: order.get(s, 0))
```

The `Status.worst` ordering is one of the load-bearing decisions: SKIP ranks above OK because "we didn't check" deserves to be visible before "we checked and it's green." A reader scanning a one-line rollup should never miss a SKIP behind a sea of OK.

**Per-tool health modules.** Each of curator / janitor / distiller / instructor / surveyor / brief / mail / talker / transport ships a `health.py` whose `health_check(raw, mode)` returns a `ToolHealth`. Modules register at import time with `register_check(tool, fn)`. The aggregator imports each known tool module via `KNOWN_TOOL_MODULES`; absent modules (e.g. surveyor without its optional ML deps) are silently skipped. Tools whose config section is absent return `Status.SKIP` with a one-line `detail` explaining why.

Example shape (curator):

- `vault-path` — vault root exists + is writable.
- `inbox-dir` — curator's watched directory exists. WARN (not FAIL) when missing — auto-created on first ingest.
- `backend` — agent backend name is in the known set.
- `anthropic auth` — only probed when the backend is `claude`; shared probe in `alfred.health.anthropic_auth`.

**Shared probes pay off.** Anthropic auth lives in `src/alfred/health/anthropic_auth.py`; every tool that uses the SDK calls it. One implementation, one place to fix when the SDK changes its error shape, one token-redacting code path so we never log secrets across nine tools' health modules.

**Aggregator concurrency + timeouts.** `run_all_checks(raw, mode)` runs every tool's check concurrently via `asyncio.gather` with a per-tool timeout (5s in `quick` mode, 15s in `full`). Exceptions are caught at the aggregator boundary and converted to a `FAIL` `ToolHealth` so the report shape is uniform — callers never have to handle partial results. The BIT recursion guard is explicit: `bit` is filtered out of the target list (running BIT-the-aggregator from inside BIT-the-daemon would loop).

**`alfred check` CLI.** Streams each line as it's produced via the `render_human` generator so a slow probe doesn't leave the operator staring at a blank terminal. Exits 0 on OK/WARN/SKIP, 1 on FAIL. `--json` switches to a one-shot `render_json` for `jq` piping. `--peer kal-le` (added in the multi-instance arc) narrows the transport probes to one peer's reachability + handshake.

**`alfred up --preflight` gate.** Runs the same `run_all_checks` quick sweep before spawning daemons. WARN does not block (per plan Part 11 Q3 — the gate should be conservative; warns are signal but not stop-the-world). FAIL aborts with exit 1 and the human rendering streamed to stdout so the operator knows exactly which tool refused. The gate is opt-in (no flag = old behavior of just starting); we left it opt-in because dev-time `alfred up` happens often and a flaky probe shouldn't block iteration.

**BIT daemon + vault record.** `src/alfred/bit/daemon.py` runs once on a clock-aligned schedule (default `05:55 America/Halifax`, derived as `brief.schedule.time` minus `bit.schedule.lead_minutes` — explicit `bit.schedule.time` overrides). Each run writes `vault/process/Alfred BIT {date}.md` as a `run`-type record with full frontmatter (overall status, mode, per-tool counts, tools checked) and a body containing the `render_human` text plus a JSON appendix. The record is queryable via Dataview alongside everything else operational.

```yaml
type: run
status: completed
name: Alfred BIT 2026-04-22
overall_status: ok
mode: quick
tools_checked: [curator, janitor, distiller, instructor, surveyor, brief, mail, talker, transport]
tool_counts: {ok: 8, warn: 1, fail: 0, skip: 0}
tags: [bit, health, bit/ok]
```

The daemon writes unscoped (the BIT daemon's writes intentionally bypass `check_scope` — see `vault/scope.py` line 178; it owns its own output, no other scope has a charter to touch it).

**Morning Brief integration.** `src/alfred/brief/health_section.py` reads the latest `vault/process/Alfred BIT *.md`, parses the frontmatter + the per-tool lines from the rendered body, and re-renders a compact `## Health` section in the brief. Falls back to the BIT state file if the vault record is unreadable, and emits a single explanatory line if no BIT has run yet — never a blank section. The brief renderer doesn't re-run the checks; it consumes what BIT already wrote at 05:55 (a five-minute-old snapshot is fresh enough for a daily brief and avoids burning probe time twice in five minutes).

**Multi-instance extension.** When KAL-LE landed (Stage 3.5), the transport health module gained `_run_peer_probes` — `peer-reachable:{name}`, `peer-handshake:{name}`, `peer-queue-depth:{name}` per configured peer. `alfred check --peer kal-le` from Salem's CLI runs only the KAL-LE-specific probes. The handshake probe loads peers via the typed transport config so `${VAR}` placeholders in `transport.peers.*.token` get env-substituted before the bearer header is built; reading the raw config dict directly leaked literal `${ALFRED_KALLE_PEER_TOKEN}` strings into the `Authorization: Bearer` header and surfaced as a false-negative 401. That bug shipped and went undetected for ~24h until the morning brief's health section showed `peer-handshake:kal-le FAIL — auth rejected` while the peer was demonstrably reachable from a manual `curl`. Fixed in the schedule-followups arc — flagged below.

**Tradeoffs / what we rejected.**

- **A separate health daemon per tool.** Rejected. The aggregator + per-tool registration is enough; spawning N processes just to own per-tool probes would have introduced N more PIDs the operator has to watch.
- **Inline probes inside each daemon's main loop** (run health every N seconds and log the result). Rejected — would have coupled probe cadence to daemon cadence and made `alfred check` impossible without IPC. Keeping the probes in import-side-effect-registered functions means both the CLI and the BIT daemon hit the same code with no daemon running.
- **Treating WARN as a preflight blocker.** Rejected. WARN is signal an operator should see; FAIL is the only correct trigger for refusing to start. Anything stricter would have made fresh installs (where the inbox dir doesn't exist yet) refuse to boot.
- **Re-running probes inside the brief renderer.** Rejected — the Morning Brief consumes BIT's vault record. Running probes a second time from inside `brief.daemon` would have doubled the network/IO cost and bifurcated the answer ("brief said OK but BIT said WARN, who's right?"). One BIT run per day, brief reads it.
- **Hard-coding probe timeouts inside each tool's `health.py`.** Rejected — the aggregator owns the timeout per-mode (5s quick, 15s full) so per-tool code stays simple. Tools that legitimately need more time can subdivide their own budget across probes.

**One known scar.** The transport `peer-handshake` env-substitution bug above was the most embarrassing of the BIT system's own bugs to date — the system's job is to surface lies, but in this case BIT itself was telling one. Documented in the schedule-followups arc (commits `45b41a4..bc50a5e`, see Reply 5 for the surrounding work) so the lesson is in the audit trail. The fix is small (`load_from_unified` instead of raw dict reads on the peer-config path); the takeaway is bigger — when a probe builds a request from config it must substitute placeholders the same way the production code does.

**Open questions.**

- **Backoff / suppression on chronic WARN states.** The brief currently re-surfaces every WARN every morning. Useful when a state changes; noise when surveyor's Ollama probe has been WARN for three weeks because the host-side ollama is intentionally off. We have no `acknowledged_until` mechanism. Open whether to add one or just let the WARN stay loud.
- **Probe drift.** If a tool's behavior changes but its `health.py` doesn't, the probe can stay green while the tool is broken. There's no test that asserts "every tool's health module covers its own failure modes." We've leaned on review discipline; would be curious if you've found a more durable shape.
- **Cross-instance health rollup.** Today each instance runs its own BIT and writes its own record. Salem can probe KAL-LE via the peer probes, but the BIT record on Salem's vault doesn't include KAL-LE's full rollup — only the handshake/reachability slice. A cross-instance rollup would help, but the design isn't obvious yet (push from KAL-LE? pull on read? a separate `alfred check --all-peers` that aggregates?).

**Commit range.** `77fbfc3..2851b51` for the core BIT sequence (6 commits): c1 health package skeleton + dataclasses + aggregator + renderers → c2 curator/janitor/distiller `health.py` + shared anthropic auth probe → c3 surveyor/brief/mail/talker `health.py` + aggregator fixes → c4 `alfred check` CLI + `alfred up --preflight` gate → c5 BIT daemon + orchestrator registration + `alfred bit` subcommands → c6 Morning Brief integration via `render_health_section`. Plus `2bab8e7` (BIT probe additions for ElevenLabs TTS readiness) and `2851b51` (talker env-var expansion + brief weather probe status mapping fixes caught during early dogfooding). Subsequent arcs added: `316f6b9` (instructor BIT probe), `87def9a` (transport BIT probe), `01fbe51` (KAL-LE per-peer probes + `--peer` flag), `9a40d01` (env-substitution fix for peer-handshake), `bc50a5e` (BIT daemon adopts `sleep_until` for drift-bounded scheduling).

Would love to hear how this echoes (or doesn't) in your thinking — particularly the BIT-as-vault-record choice (it makes health queryable alongside everything else but it does mean a `run` record per day) and the WARN-doesn't-block preflight stance.
