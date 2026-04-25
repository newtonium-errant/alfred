---
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
janitor_note: LINK001 — decision/Salem Ghostwrites External Communications on Andrews
  Behalf and decision//extract Is Idempotent with Delete-First Re-Run Contract exist
  with regular apostrophe; YAML-escaped apostrophes in distiller_learnings wikilinks
  defeat the scanner. session/... is a literal placeholder, no resolvable target.
  FM001/DIR001 — file is type=note in process/ directory; autofix should relocate.
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

# Reply 6 — Voice Stage 2b: capture mode

**Problem shape.** Telegram voice message + Groq Whisper + Claude turn-by-turn (our Stage 2a) is good for journaling and task execution. It's wrong for long continuous brainstorming. Every voice note triggers a full LLM turn, which means every note comes back with an assistant reply — breaks the flow when you're just thinking out loud for ten minutes. The right shape is "capture silently, give me a structured output on demand."

**Solution shape.** A new `capture` session type alongside the existing `note / task / journal / article / brainstorm` set.

- **`pushback_level=0`** (silent — the model never proactively challenges).
- **`supports_continuation=False`** (each capture session is its own record).
- **Router entry:** deterministic `capture:` prefix short-circuits to capture without hitting the LLM classifier. Natural-language cues ("let me brainstorm", "thinking out loud", "ramble") route via the classifier.

**Mid-session behaviour.** `conversation.run_turn` short-circuits when `session_type == "capture"`: appends the user turn, skips the LLM call, skips escalation detection, returns a `CAPTURE_SENTINEL`. The bot recognizes the sentinel and posts a per-message emoji reaction (✔) via Telegram's `set_message_reaction` API instead of a text reply. No assistant turn, just a receipt. User keeps talking.

Slash commands (`/opus`, `/end`, `/status`, `/brief`, `/extract`) still fire during capture. `/opus` in capture applies to the post-`/end` batch pass.

**On `/end`.** Session close triggers an async `asyncio.create_task` batch structuring pass. One Sonnet call, tool-use enforced, returns:

```python
{
    "topics": list[str],          # max 8
    "decisions": list[str],
    "open_questions": list[str],
    "action_items": list[str],
    "key_insights": list[str],    # high-confidence only
    "raw_contradictions": list[str],
}
```

Rendered as markdown under a `## Structured Summary` section wrapped in `<!-- ALFRED:DYNAMIC -->` markers, injected *above* the raw transcript in the session record. `capture_structured: true` frontmatter flag. `/end` returns fast; the structured output arrives as a follow-up Telegram message.

**`/extract <short-id>`.** Opt-in note derivation from a captured session. Max 8 derived notes per capture (distiller dedupes downstream). Each: `created_by_capture: true`, `source_session: [[session/...]]`, `confidence_tier: high|medium`. Session's `derived_notes` list populated. Idempotent: re-running after success replies "Already extracted N notes. Delete first to re-run."

**`/brief <short-id>`.** Compress the structured summary to ~300 words prose via Sonnet, then ElevenLabs Turbo v2.5 TTS, delivered as a Telegram voice note. Default voice Rachel. Failure fallbacks: text reply if the API is down, document upload if audio exceeds 50 MB.

**Calibration still fires at session close** even for capture — fed by the structured summary rather than by turn-by-turn transcript. This was the one non-obvious decision: the user's calibration-relevant signals (direction changes, sensitivities, preferences) show up in capture sessions same as anywhere else.

**Tradeoffs / what we rejected.**

- **One-shot "(capturing)" ack** vs per-message reaction. Rejected the one-shot because it's ambiguous when the model's behavior is silence — the user can't tell the message landed. Per-message reaction is cheap signal.
- **Auto-extract on `/end`.** Rejected. User should decide whether this ramble is worth N derived notes. `/extract` is explicit.
- **LLM during capture for "useful interruptions"** (gotcha catching, redirect prompts). Rejected — breaks the flow this mode exists to preserve. If the user wants that shape, they pick `journal` or `brainstorm`.
- **Building the `alfred_instructions` watcher in the same arc.** Rejected as scope creep; shipped separately (see Reply 3).

**Cost profile (10-min capture session).** STT (Groq Whisper) ~$0.007, Sonnet batch pass ~$0.03 (Opus ~$0.16), ElevenLabs TTS Turbo v2.5 ~$0.30-0.50. Monthly at daily use ~$9-15, TTS dominant.

**Commit range.** `cefe063..c70e81e` (7 commits, bundled session note across all of them per our session-notes-per-commit rule).

Would love to hear how this echoes (or doesn't) in your thinking — especially the silent-during-capture-then-summarize shape vs real-time assistant-interjection during brainstorming.
