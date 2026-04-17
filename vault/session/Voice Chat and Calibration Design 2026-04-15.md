---
alfred_tags:
- software/alfred
- design/voice
- design/calibration
created: '2026-04-15'
description: Canonical design doc for Alfred's voice chat integration and the
  bidirectional user-calibration mechanism. Captures use-case priorities, tech
  stack decisions (Claude + ElevenLabs + Telegram, no OpenAI), staged build
  plan from async capture to streaming real-time to wearable glasses, the
  profile-doc-as-vault-artifact calibration design, and a research summary of
  Hey Cyan smart glasses including the community SDK on GitHub.
intent: Persist the full voice + calibration design discussion as a working
  document so future sessions can implement from it without re-deriving the
  decisions
name: Voice Chat and Calibration Design
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Layer 3 Janitor Triage Queue 2026-04-15]]'
- '[[session/Per-Tool Log Routing Refactor 2026-04-15]]'
status: completed
tags:
- design
- voice
- calibration
- roadmap
type: session
---

# Voice Chat and Calibration Design — 2026-04-15

## Intent

Alfred currently has rich vault-backed cognitive infrastructure (curator, janitor, distiller, surveyor) but no native interaction surface beyond text editing and CLI commands. Voice chat closes that gap and unlocks four new use cases — journaling, task execution, conversational query, and dictation — each of which reuses Alfred's existing intelligence rather than requiring net-new cognitive work. The design here is the canonical artifact for that build, persisted so future sessions can implement directly from this doc rather than re-deriving the decisions in conversation.

The deeper bet is that voice plus a **bidirectional user-calibration mechanism** turns Alfred from a personal vault tool into a self-improving thinking partner that progressively models its primary user. That mechanism — a profile doc that Alfred writes to during voice sessions and the user edits directly in Obsidian — is the most architecturally important piece of this design and the reason the work is worth doing now rather than later.

## Use Case Priorities

In order, from highest to lowest priority:

### 1. Journaling and reflection (primary)

Multi-turn voice conversations where Alfred listens to the user think through something out loud, surfaces its current understanding periodically, asks clarifying questions when it notices inconsistency or ambiguity, and captures the conversation as a session record that the distiller processes for assumptions, decisions, constraints, contradictions, and syntheses.

This is the deepest use case but the one that needs the *least* new intelligence. It maps almost perfectly onto Alfred's existing architecture: voice-session-as-session-record → distiller-extracts-learnings → vault-becomes-the-conversational-memory. The talker tool just needs to provide voice I/O and a SKILL.md that knows when to interject.

### 2. Task execution (high)

Voice as a command surface: "Alfred, create a task to call Alliance Dental tomorrow" or "Alfred, merge those two PocketPills records." Latency-sensitive, needs confirmation loop, low cognitive overhead per interaction. Implemented as an intent-routing path in the talker that recognizes command-style utterances and calls the appropriate `alfred vault create/edit/delete` operation, with voice confirmation back.

### 3. Conversational query (medium)

"What's coming up this week?" or "What did I decide about the Ozempic refill?" Voice questions answered grounded in vault content. The talker holds vault search/read tools and uses them mid-conversation to answer factually, then summarizes verbally.

### 4. Dictation (low)

Pure voice-to-text capture into the inbox without a response loop. The simplest mode and the lowest priority, because every other mode subsumes it — if you can journal, you can dictate.

## Tech Stack Decisions

### Brain: Claude API

Already the rest of Alfred's stack. No reason to introduce a second LLM provider for the voice layer specifically. Every backend pattern, every SKILL.md convention, every prompt engineering muscle the project has built is Claude-shaped, and the talker tool inherits that consistency.

### Voice provider: ElevenLabs (no OpenAI)

ElevenLabs handles both TTS (their core product, excellent voices, conversational-tuned presets) and STT (their newer Scribe product). Single-vendor voice pipeline beyond Claude. Streaming available for Stage 3 when latency matters more.

**Explicitly rejected**: OpenAI Realtime API. While it's technically the fastest path to a streaming voice prototype, introducing OpenAI as a dependency in an otherwise Claude-first stack is the wrong direction for this project.

**STT options in preference order:**
1. **ElevenLabs Scribe** — keeps the voice stack single-vendor beyond Claude
2. **Groq Whisper** — fast and cheap, hosts open-source Whisper V3, non-OpenAI
3. **Local whisper.cpp / faster-whisper** — runs on current hardware, zero cloud cost, works offline. Good for Stage 1 if cloud STT is to be avoided early.

**Stage 3 streaming**: ElevenLabs Conversational AI with Claude as the underlying LLM. ElevenLabs handles the streaming STT + TTS + turn-taking pipeline, Claude handles the thinking layer via tool calling. Closest equivalent to "ChatGPT voice mode but the brain is Claude."

### Primary client: Telegram bot

This is the most counter-intuitive call in the design and worth explaining at length.

A custom PWA or native iOS app would give the most UX control but takes weeks to build and yields a worse experience than off-the-shelf Telegram. Telegram is the right Stage 1–3 client for this build because:

- **Zero client-side build.** Telegram already has polished iOS, desktop, and web apps with first-class voice message support, push notifications, transcripts, and waveform playback. Their UX team already solved the problems a custom client would need to solve from scratch.
- **Voice-native UX.** The `voice_note` message type handles recording, waveform display, playback speed control, and haptic feedback as built-ins. None of this needs to be designed or implemented.
- **Bot API is free and well-documented.** Telegram bots are the gold standard for back-and-forth conversational interfaces. Push notifications cost nothing. No walled garden.
- **Text + voice hybrid by default.** You can type when in a quiet space and speak when walking around. Same conversation thread, same record, zero extra work to support both modalities. This satisfies the "text or voice" preference directly.
- **Cross-device.** Desktop Telegram, iOS Telegram, web Telegram all show the same conversation. Start a session on phone, continue typing on laptop, read history on desktop. No state synchronization to build.
- **Webhook pipeline matches existing email infrastructure.** Cloudflare tunnel → n8n → Telegram bot webhook → Alfred inbox is structurally identical to the Outlook → n8n → email webhook → Alfred inbox flow that's been working for a month. The bot becomes another input pipeline alongside email.

**Trade-off**: conversation content lives in Telegram's cloud, and Alfred reaches the user through a third-party app. Telegram's encryption for regular chats isn't end-to-end but it's acceptable for this use case. If end-to-end matters later, swap Telegram for Signal (Signal CLI exists and works similarly).

**Alternatives considered and rejected**:
- iMessage — Apple blocks third-party bots
- WhatsApp Business — Twilio integration works but has per-message costs and more friction
- Discord — fine for text, awkward for one-on-one voice
- Custom PWA — multi-week build for worse UX than Telegram out of the box
- Native iOS app — even longer build, justified only when Telegram limits become a real ceiling

**iOS native app is the eventual ideal** for the most polished mobile experience, but only after Telegram has validated the design and identified what specifically a custom client should add.

### Bot implementation: Python-native with direct Claude API

**Decision: build the Python bot from the start** (Option B from the design discussion), not an n8n-first prototype. Rationale: no migration when Stage 2a arrives — the bot IS the talker's client from day one. The extra upfront work (~3-5 days vs ~1-2 days for n8n) is paid back immediately by zero migration tax.

**Library**: `python-telegram-bot` v20+ (async-native, fits Alfred's asyncio pattern). Handles text and voice messages with the same handler pattern — voice just adds a transcription step before the same conversation pipeline. Module structure:

```
src/alfred/telegram/
  __init__.py
  bot.py          # Telegram bot setup, message handlers, voice download
  conversation.py # Claude API conversation loop with tool_use
  transcribe.py   # Voice → text via Whisper/Scribe
  config.py       # Bot token, STT provider, model selection
  session.py      # Session type routing, history management, session record writing
```

**Claude API pattern**: direct Anthropic SDK with `tool_use`, NOT the `claude -p` subprocess pattern used by curator/janitor/distiller. Conversation is the primary interaction (not vault mutation), so the direct API is more natural and lower-latency. Claude sees the conversation history, has access to vault operations via tool_use (search, read, create, edit), and responds in the same API call. One round-trip per turn (two if there's a tool call).

```python
response = client.messages.create(
    model=session.model,              # sonnet or opus, per session type
    system=system_prompt,             # calibration + vault context + conversation rules
    messages=conversation_history,    # growing list of user + assistant turns
    tools=[vault_search, vault_read, vault_create, vault_edit],
)
```

### Per-instance talker

Each Alfred instance runs its own talker tool. Same code, per-instance config. The talker's modes (grounded, generative, brainstorm-capture) and session types are configured per instance. The Knowledge Alfred instance enables generative mode for fiction and non-fiction writing; NP's eventual instance might have grounded only with a different SKILL.md tone. The main Alfred starts with grounded only.

This fits the existing per-tool config pattern exactly. No new architecture needed for multi-tenancy — it's already how curator, janitor, etc. are configured per instance.

## Session Management

Sessions are **typed, resumable, and model-aware**. The opening cue does triple duty: identifies the session type, finds a previous session if continuing, and selects the right model. This is the core interaction design for the talker.

### Session types

Each type carries defaults for model, history scope, continuation behavior, and push-back frequency:

| Type | Default model | History scope | Continues previous? | Push-back | Example cues |
|---|---|---|---|---|---|
| `note` | Sonnet | sliding window (last N turns) | no | low (1/10) | "Quick note", "Remind me to..." |
| `task` | Sonnet | minimal (one-shot or 2-3 turns) | no | none | "Create a task to call Dr. Bailey" |
| `journal` | Sonnet → Opus on depth | full session | optionally, by reference | 4/10 | "I want to think through the dental situation" |
| `article` | Opus | full + previous session loaded | yes, by default | 3/10 | "Let's continue the last article" |
| `brainstorm` | Sonnet capture, Opus format | full | optionally | 4/10 (minimal interjections) | "Brainstorm session about Q2 logistics" |

These are starting-point defaults. Over time, the calibration mechanism learns which types consistently need which model and adjusts recommendations.

### Opening-cue router

When a message arrives, the talker's first job is to classify the opening cue. This is a lightweight Sonnet API call that reads the opening message and returns a routing decision:

```json
{
  "session_type": "article",
  "continue_from": "session/Article Draft - Multi-Instance Architecture 2026-04-12",
  "model": "opus",
  "reasoning": "User said 'continue the last article' — searching for most recent article session"
}
```

The router:
1. Classifies the session type from the natural-language cue
2. If the type supports continuation AND the cue implies it ("let's continue...", "pick up where we left off..."), searches the vault for the most recent matching session record
3. Selects the model based on the type's default (overridable by user cue — "quick article note" → Sonnet even for article type)
4. Loads the appropriate context: previous session history (if continuing), vault summary, calibration profile

Cost: one extra Sonnet API call at session start (~0.5s, minimal tokens). Worth it for correct routing. The router itself can be a simple structured-output prompt — it doesn't need tools, just classification.

### Model selection and mid-session escalation

**Starting model** is determined by the session type's default. General note-taking starts on Sonnet for speed. Articles start on Opus for depth.

**Mid-session escalation** supports BOTH explicit and implicit detection:

- **Explicit**: user types `/opus`, `/sonnet`, "use the bigger model", "switch to Opus." Talker switches immediately. Unambiguous.
- **Implicit**: the talker detects it's giving shallow responses (short answers, hedging, not connecting dots across vault context) and offers: "This is getting complex — want me to switch to Opus for more depth?" User confirms or declines. Same bidirectional pattern as calibration.

Both coexist: explicit always works, implicit is a learned behavior that improves over time.

**Model-selection calibration** is recorded in the `<!-- ALFRED:CALIBRATION -->` section:

```markdown
### Model Preferences (learned)
- article sessions: default Opus (escalated 9/10 times in first month)
- note sessions: Sonnet is sufficient (never escalated)
- journal sessions: start Sonnet, offer Opus after turn 5 (escalated ~40%)
  _Updated 2026-05-15 — Alfred recommended changing article default to Opus_
```

This means model selection self-tunes the same way confirmation frequency does — Alfred observes the user's actual patterns and recommends adjustments when confidence is high enough.

### Session boundaries

**Start**: a new session begins when the user sends a message after a gap (no explicit `/start` needed). The opening-cue router classifies the type and loads context.

**End**: two mechanisms, both active:
- **Explicit**: user sends `/end` or "end session." Session record is written immediately.
- **Implicit**: 30-minute gap with no messages. Session record is written automatically. The next message starts a fresh session (or continues a previous one if the cue says so).

The implicit gap means you never have to remember to close a session — they expire naturally. The explicit command gives you control when you want the session note written NOW (e.g., before switching contexts).

### Session records in the vault

Each session writes a `session/` record at close with:
- `session_type` in frontmatter (note/task/journal/article/brainstorm)
- `model_used` — which model(s) were used, including any escalation events
- `continues_from` — wikilink to the previous session if this was a continuation
- Full conversation transcript as the body
- Any vault operations performed during the session (tasks created, records edited) linked in `related`

The distiller processes these on its normal cadence, extracting learnings just like any other session note.

## Modes

Three modes, all powered by the same talker tool, distinguished by SKILL.md prompts and per-session flags.

### Grounded mode (MVP — ship first)

Vault search aggressive. Alfred cross-references the user's previous sessions, decisions, and entity records. Push-back fires on inconsistencies. Best for journaling, query, and task execution.

**Push-back calibration: 4 out of 10.** Alfred surfaces its current understanding every ~4 turns OR when it detects a contradiction with session history or linked vault records. Not interrogative, not silent — thoughtful-friend frequency.

The push-back is **bidirectional**: Alfred says "I'm hearing X, is that right?" and the user can confirm, correct, or realize their own thinking has shifted. Corrections become assumption/synthesis records, propagating to future sessions.

### Generative mode (deferred — Knowledge Alfred instance only)

Vault search disabled or read-only. Alfred is free to imagine, propose, remix. "Yes and" energy. Best for creative work — fiction drafts, story ideation, essay brainstorming, exploring hypotheticals where vault grounding would inhibit imagination.

**Will not exist in the main Alfred instance.** Belongs to the **Knowledge Alfred** instance — the planned instance that handles all writing work, both fiction and non-fiction. Knowledge Alfred is one of the five instances flagged in `project_multi_instance_design.md` memory and is the canonical home for generative voice mode. Stage 4a is gated on the multi-instance architecture (Stage 3.5) being built first, since Knowledge Alfred is a separate Alfred instance with its own vault, talker config, and calibration profile.

NP's instance probably doesn't get generative mode either — her instance is for her operational work, not creative writing.

### Brainstorm-capture mode (Stage 2b)

Long-form continuous voice with minimal interruption, then a batch formatting pass that produces a structured markdown note. Different from journaling because the user is doing most of the talking and Alfred is mostly capturing — closer to dictation with intelligent post-processing.

Flow:
1. Wake / activation: "start a brainstorm session about X"
2. Capture: Alfred records and live-transcribes; minimal interjections at the 4/10 push-back level (mostly clarifying questions like "want me to link this to project/Q2?")
3. Wrap: user signals end of session
4. Format: batch LLM pass produces structured note with headers, bullets, linked entities, extracted task candidates
5. Commit: note lands in vault, distiller runs on its normal cadence
6. Brief-back: Alfred speaks a short summary ("3-page note written, 4 task candidates extracted, linked to project/Q2")

This mode is the **flagship glasses use case** later but works on phone/Telegram today. Doesn't need streaming infrastructure because the conversation isn't real-time turn-taking — it's capture + post-processing.

## Bidirectional Calibration via Person-Record Calibration Sections

The most architecturally important piece of this design.

### The mechanism

Alfred maintains its understanding of the primary user as a **delimited section inside the user's existing `person/` record**, not as a new entity type. The section is wrapped in `<!-- ALFRED:CALIBRATION -->` ... `<!-- END ALFRED:CALIBRATION -->` markers — the same pattern Alfred already uses for dynamic content (`<!-- ALFRED:DYNAMIC -->` per `vault/CLAUDE.md`). The talker writes only inside the markers; the user can edit anywhere in the file, including inside the markers.

The record's frontmatter carries an `alfred_calibration: true` flag so the talker can fast-filter "which person records have calibration sections" without reading every file.

**Two-way editable memory**: Alfred writes, user edits, Alfred reads the user's edits, behavior propagates. Most AI assistant calibration systems either keep the calibration opaque (the user can't see it), let the user view but not edit it, or let the user edit but don't react to the edits. Vault-backed and git-tracked, all three problems dissolve.

### Why a person-record section instead of a new `profile/` type

Calibration is fundamentally **Alfred's behavioral model of how to interact with a person**, not facts about the person. Person records hold facts (name, role, contact, history). Calibration is meta about those facts and lives alongside them in the same record. Three reasons this beats a new type:

1. **Zero schema changes.** `KNOWN_TYPES`, `TYPE_DIRECTORY`, the curator's per-type rules, the janitor's per-type validators all stay untouched. New types have a real cost in this project; avoiding one when the existing structure works is the right call.
2. **Single source of truth per person.** Facts and calibration live in one record. Open `person/Andrew Newton.md` in Obsidian and you see the whole picture.
3. **Reuses an existing pattern.** The `<!-- ALFRED:DYNAMIC -->` delimited-section convention is already documented in `vault/CLAUDE.md`. Calibration sections are a sibling instance of the same pattern. Reusing established patterns is free.

The pattern generalises: any record where Alfred wants to maintain a behavioral model can carry a calibration section. `org/PocketPills.md` could eventually have one ("preferred channel: email; response latency: 48h"). Out of scope for phase 1 but worth knowing the door is open.

### Flagging primary users at the instance level

Each Alfred instance declares its primary user(s) in `config.yaml` under the talker section:

```yaml
talker:
  primary_users:
    - "person/Andrew Newton"
    # For a future couple's instance:
    # - "person/NP"
```

The talker reads this list at startup. For each named primary user, it loads the corresponding person record's calibration section as grounding context for voice sessions. Single-user, dual-user (couples), and small-team instances all work via the same config field — no code changes needed for multi-tenancy.

When voice identification arrives later (Stage 5+ feature), the talker can route to the right user's calibration based on who's speaking. Until then, the talker can ask "is this Andrew or NP?" at session start, or default to the first entry.

### Document shape

```markdown
---
type: person
name: Andrew Newton
alfred_calibration: true
updated: 2026-04-15
---

# Andrew Newton

## Facts

- Owner of Rural Route Transportation, owner of Struggle Bus brand
- Based in Nova Scotia
- Partner: NP

## Notes / History

(curator-written content from inbox, meetings, conversations)

## Alfred's Calibration

<!-- ALFRED:CALIBRATION -->

### Communication Style

- **Military-style comms**: terse, direct, high-signal/low-noise.
  _Confirmed 2026-03-01 · source: session/Alfred Setup and Email Integration 2026-03-26_
- **Prefers Option A/B/C framing** for non-trivial decisions, rather than open-ended exploration.
  _Inferred 2026-04-15 · source: session/[voice session]_ [needs confirmation]
- **Rejects excessive caveats and hedging.**
  _Corrected 2026-04-10 · replaced earlier "appreciates nuance"_

### Workflow Preferences

- One logical session per commit
- Every commit paired with a session note in `vault/session/`
- Surgical hunk-level staging when pre-existing dirty files are in scope
- Python-layer enforcement first, prompt-layer as belt-and-braces
  _Updated 2026-04-15 from voice session — previous belief: prompt-first_

### Current Priorities

- Shipping Alfred voice chat integration (Telegram first, glasses eventually)
- Multi-instance architecture (hub-and-spoke, 5 instances planned)
- Knowledge Alfred for all writing (fiction and non-fiction)

### What Alfred Is Still Unsure About

- [ ] How much push-back during voice journaling feels right (current setting: 4/10 — to be tuned)
- [ ] Whether to auto-create task records from voice intent or always confirm first
- [ ] Whether Knowledge Alfred should have any vault access at all, or be a clean-slate creative space

<!-- END ALFRED:CALIBRATION -->
```

### Distiller awareness

The distiller currently extracts learnings from session records. When the calibration-section pattern lands, the distiller needs to **skip content inside `<!-- ALFRED:CALIBRATION -->` markers** when processing person records — those are Alfred's own model, not user-authored claims to distill from. Same treatment as `<!-- ALFRED:DYNAMIC -->` blocks (assuming the distiller already handles those; if not, both need the skip-rule).

Small distiller change required when implementing this section. Flagged as a Stage 2a sub-task.

### Provenance and auditability

Three layers ensure the user can always see where a belief came from:

1. **Inline source markers** on each bullet — `_source: session/[name]_` points to the session that produced the claim
2. **Vault git history** — every edit to the person record is a commit in the vault's inner git repo. Full history available via `vault snapshot --log` or directly with git in `vault/.git`
3. **Optional changelog section** at the bottom of the calibration block — chronological list of significant belief shifts with their source sessions, for fast scanning without digging through git history

The user should never have to wonder "how did Alfred get this idea?" — the source is one click away.

### Update protocol during a voice session

When the talker detects a reflection-worthy moment:

1. **Surface understanding**: "I'm hearing that you want X. That's a shift from your profile, which currently says Y. Want me to update the profile to X?"
2. **User responds**: confirm, correct, or deflect ("let me think on that")
3. **On confirmation**: talker calls `alfred vault edit person/Andrew Newton.md` with the specific bullet change inside the calibration section, including source-session attribution
4. **On correction**: talker uses the corrected statement instead, still attributed
5. **On deflection**: talker leaves a `[needs confirmation]` entry in the "What Alfred Is Still Unsure About" subsection for next session

### Confirmation policy

The confirmation policy lives on a 1–5 scale where 1 is fully silent (Alfred writes whatever it infers, user reviews later in Obsidian) and 5 is fully explicit (Alfred asks before every single edit, no matter how minor). The policy isn't fixed — it's intentionally designed to evolve as Alfred's confidence in its model of the user grows.

**Starting setting: 3-4** while the calibration mechanism is being validated. This means Alfred surfaces its understanding for confirmation on most non-trivial claims but can silently append low-confidence `[needs confirmation]` items to the "What Alfred Is Still Unsure About" subsection for later review. More intrusive than the long-run target, but the early iterations need feedback to learn what to surface and when.

**Self-tuning over time.** Once Alfred has accumulated enough confirmed calibrations and observed the user's correction rate stay low for a sustained period, it should be able to **recommend lowering the validation frequency itself**. Something like: "I've successfully predicted your responses on the last 20 reflections without a correction. Want me to drop validation from 4 to 3 going forward?" The user accepts, declines, or counter-proposes. The new setting is recorded in the calibration section's metadata so it persists.

This makes the validation frequency itself part of the bidirectional calibration loop — Alfred learns not just the user's beliefs but also how much it can trust its own model of those beliefs. Meta-calibration. The user is in control either way; Alfred just makes recommendations when its confidence supports them.

The setting is tunable manually at any time by editing the calibration section's metadata in Obsidian. Alfred reads it at session start.

### Migrating the existing `vault/user-profile.md`

Two-stage plan. MVP collapses to single source of truth; long-term reintroduces the top-level file as a convenience view without duplication.

**MVP (implement with Stage 2a):** migrate the content of the existing untracked `vault/user-profile.md` into the `<!-- ALFRED:CALIBRATION -->` section of `person/Andrew Newton.md` and delete the standalone file. Single source of truth during the validation period. Every read and write goes through the person record. No duplication, no sync concerns.

**Long term (re-introduce after validation):** bring back `vault/user-profile.md` as a top-level convenience file that **transcludes** the calibration sections from the primary users named in `talker.primary_users`, using Obsidian's native embed syntax. Zero duplication — the transclusions are live views, not copies. The file looks something like:

```markdown
# User Profile — Aggregated View

_This file is a convenience view. Calibration content lives in the
`<!-- ALFRED:CALIBRATION -->` sections of the person records linked below.
Edit there for changes to take effect; this file will reflect them._

## Andrew Newton

![[person/Andrew Newton#Alfred's Calibration]]

## (other primary users — if any, for couple's or team instances)

![[person/NP#Alfred's Calibration]]
```

**Why do it this way rather than a generated file**: Obsidian's transclusion is native and free. No background regeneration job, no race condition between the canonical record and the aggregate file, no duplication to keep in sync. The top-level file is discoverable ("open user-profile.md to see Alfred's mental model") but remains a live view of the canonical data.

**When to re-introduce it**: once the calibration mechanism is validated and you find yourself navigating to `person/{user}.md` frequently to check Alfred's model of you. If you never miss the top-level file, don't bother adding it back. The MVP single-source-of-truth state is good enough to ship and operate against indefinitely.

### Pattern beyond user calibration

The calibration-section-in-existing-record pattern isn't limited to person records or user calibration. Future extensions:

- **`org/{name}.md` calibration sections** — "how Alfred handles interactions with this org" (preferred channel, response latency expectations, who at the org to address). Useful for personal-business interactions where consistent context matters.
- **`person/{anyone}.md` calibration sections** — for every person Alfred interacts with on the user's behalf, not just primary users. "Here's what Alfred believes about Dr. Bailey." Updated when journaling about appointments or meetings.
- **A self-model on `project/Alfred.md`** — Alfred's own behavioral self-model, edited by the user to change how Alfred sees itself. Meta but potentially powerful.

All extensions reuse the same `<!-- ALFRED:CALIBRATION -->` pattern. Out of scope for phase 1 but the door stays open.

## Session Persistence

Combo of two patterns, both supported simultaneously:

### Default (every session, automatic)

1. Each user turn + Alfred turn appended to a `session/` record as the conversation happens
2. Session record finalized when the conversation ends
3. Distiller runs on its normal schedule and extracts learnings — assumptions, decisions, constraints, contradictions, syntheses — exactly like it does for any other session note today

### Explicit (when the user asks for it, in-session)

The talker recognizes intent for explicit save operations and routes them through vault tool functions:

- "Save that to project Q2" → `alfred vault edit project/Q2 --body-append "..."` with relevant content from the last turn or two
- "Create a task to follow up with Dr. Bailey next week" → `alfred vault create task "Follow Up With Dr Bailey" --set due=...`
- "Make that a decision" → `alfred vault create decision/...` directly, rather than waiting for distiller inference
- "Link this to my chat with Alliance Dental on April 14th" → talker searches for the prior session, extracts wikilink, appends to current session's `related` field

These are additional tool functions available to the talker agent during a session. Both patterns coexist — explicit saves don't replace the automatic distiller pass, they augment it.

### Ambiguity handling

When the user says "save that" without specifying where, the talker asks **one clarifying question** rather than guessing. "Save to project/Q2 or create a new note?" Low-risk, respects user intent, avoids creating orphan records from misinterpreted voice commands. Tunable.

## Staged Build Plan

Note: Stage 1 (async-capture-only via n8n) was superseded by the decision to build the Python bot from the start (Option B). Stages 1 and 2a merge into a single build with weekly milestones. The table below reflects the current plan.

| Stage | Target | Client | Stack | Time estimate | Hardware-dependent |
|---|---|---|---|---|---|
| **2a-wk1** | Text + voice back-and-forth MVP (single session type, Sonnet, full history) | Telegram bot (Python-native) | `python-telegram-bot` + Anthropic SDK direct + Groq Whisper or ElevenLabs Scribe | ~1 week | No |
| **2a-wk2** | Session types + continuation + model routing (note/task/journal/article/brainstorm types, opening-cue router, previous-session loading) | Telegram bot | Same + vault session search | ~1 week | No |
| **2a-wk3** | Model escalation + calibration integration (explicit /opus + implicit detection, calibration loading, push-back mechanism, session-end calibration writes) | Telegram bot | Same + calibration section read/write | ~1 week | No |
| **2b** | Brainstorm-capture mode (long dictation + smart format + audio summary) | Telegram bot | Same talker, new SKILL.md mode | ~1 week after 2a | No |
| **3** | Real-time streaming conversation | Telegram bot + maybe web PWA | ElevenLabs Conversational AI with Claude brain | ~2 weeks | Yes — Mac Studio, fall 2026 |
| **3.5** | Multi-instance architecture (prerequisite for instance-specific talker modes) | — | Per-instance deploy pattern across the whole stack | Separate track, scope unknown | No, but big |
| **4a** | Generative mode (Knowledge Alfred instance only — handles all writing, fiction and non-fiction) | Telegram bot | Talker SKILL.md mode + per-instance config | Days, depends on 3.5 | No |
| **4b** | Hey Cyan glasses client | Cyan glasses + paired phone | HeyCyan community SDK via BLE → phone bridge → talker | ~2-3 weeks | Yes — Cyan glasses + SDK integration |

### Stage 1 — Async voice capture (~1 week)

- **Client**: Telegram bot. User sends voice messages to @AlfredBot. Bot is the inbox.
- **Pipeline**: bot webhook → Cloudflare tunnel → n8n → Whisper API call → transcript → vault inbox file with `source: voice` tag → curator picks it up like any inbox entry
- **Processing**: curator produces a session record, distiller runs on its normal schedule
- **Output**: no response path yet. Voice in, vault absorbs.

This stage validates the end-to-end pipeline without committing to talker tool scope. It's the "I can talk to Alfred and the words land in the vault" milestone. Foundation for everything else.

### Stage 2a — Turn-based grounded conversation (~2-3 weeks)

- **New tool**: `src/alfred/talker/` following the existing pattern (`config.py`, `daemon.py`, `state.py`, `backends/{cli,http,openclaw}.py`, and a SKILL.md)
- **Voice layer**: batch Whisper for STT, Claude API for the LLM via existing agent backend pattern, ElevenLabs for TTS
- **Latency**: ~5–15s per turn, walkie-talkie feel. Acceptable for journaling, task execution, and query modes.
- **Modes available**: grounded only. Generative is deferred to Knowledge Alfred (Stage 4a).
- **Push-back level**: 4/10 by default, configurable per-session
- **Confirmation policy**: starts at 3-4/5 during validation, with self-tuning over time as Alfred's confidence in its model grows
- **Session shape**: one continuous session per "conversation start". User starts a session, talks back-and-forth, ends the session. **Multi-session stitching is explicitly deferred to a future enhancement** (see "Future growth" below) — phase 1 keeps the model simple.
- **Calibration integration**: talker loads the calibration sections from `person/` records named in `talker.primary_users` config as grounding context at session start, writes to them during the session via the calibration update protocol

### Stage 2b — Brainstorm-capture mode (~1 week after 2a)

- Same talker tool, additional SKILL.md mode
- Different conversation flow (long-form capture + post-process formatting)
- Same Telegram client — no new client needed
- Output is a structured note in the vault, summarized verbally to the user at session end

### Stage 3 — Real-time streaming conversation (Mac Studio, fall 2026)

- Upgrade voice transport from batch to streaming
- **Provider**: ElevenLabs Conversational AI with Claude as the underlying LLM (decided in this design, can be revisited closer to the date)
- **Architectural rule**: Alfred-side code (talker SKILL.md, vault tool functions, session record shape, profile doc protocol) must be provider-agnostic. The streaming upgrade swaps the voice transport, not the application logic.
- **Key feature unlock**: barge-in and real-time understanding surfacing. Alfred can interject "wait, I want to reflect back something" mid-monologue without waiting for a natural turn boundary. Bidirectional push-back becomes more natural at this latency.

### Stage 3.5 — Multi-instance architecture (separate track)

Prerequisite for Stage 4a (generative mode in the Knowledge Alfred instance) and the eventual NP instance. Out of scope for this design doc but blocks the per-instance-mode features.

### Stage 4a — Generative mode in Knowledge Alfred instance (depends on 3.5)

- Same talker code, new SKILL.md mode
- Enabled in the Knowledge Alfred instance only — the planned instance for all writing work, fiction and non-fiction
- Main Alfred and NP's instance keep grounded mode only
- Days of work once multi-instance architecture (Stage 3.5) is in place

### Stage 4b — Hey Cyan glasses client (hardware-dependent)

See the "Hey Cyan smart glasses" section below for the research summary. Implementation outline:

- **Client**: glasses-side code using the [`ebowwa/HeyCyanSmartGlassesSDK`](https://github.com/ebowwa/HeyCyanSmartGlassesSDK) community SDK
- **Architecture**: glasses → BLE → phone bridge → tunnel → talker tool. The glasses are a thin client; the phone is the network gateway; Alfred runs unchanged on the home server.
- **Activation**: button press on the glasses (no wake word — see below). User taps to start recording, taps to end.
- **Interaction modes**:
  - Quick task execution: "Create a task to call Alliance Dental tomorrow." 2-second interaction, audio confirmation back.
  - Brainstorm-capture: glasses-friendly version of Stage 2b. User opens a session, talks for 5–30 minutes while doing something else, taps to end. Note arrives in the vault, audio summary plays back.
- **Confirmation UX**: short audio ack ("got it") via glasses speaker for quick interactions; full voice response for query and brainstorm-capture modes
- **Wake word as future enhancement**: see "Hey Cyan" section for why this is hard with Cyan specifically and what the alternatives are

## Hey Cyan Smart Glasses — Research Summary

Researched 2026-04-15 via heycyan.net and a GitHub search.

### What the product is

- **Hardware**: white-label budget smart glasses, ~$55–60 retail (per user reviews)
- **Distributor**: HeyCyan ships the companion app and is the consumer-facing brand; the underlying hardware is manufactured by an unidentified OEM and resold by multiple brands
- **Connectivity**: Bluetooth 5.3+ for primary pairing with a phone, WiFi for media file transfer (photos, videos, voice notes)
- **Battery**: 8–9 hours active use
- **Built-in features (per the app)**: voice-activated AI assistant, hands-free navigation, voice notes, real-time translation across 35+ languages, photo/video capture with WiFi sync
- **Output modality**: speaker for voice playback (likely open-ear or bone conduction; not specified). No in-lens display mentioned.
- **No hardware spec sheet** is published on heycyan.net — the official site is purely about the companion app

### Community SDK

This is the part that matters for Alfred integration:

- **Repository**: [`ebowwa/HeyCyanSmartGlassesSDK`](https://github.com/ebowwa/HeyCyanSmartGlassesSDK) on GitHub
- **Cross-platform**: iOS and Android
- **Transport**: Bluetooth Low Energy
- **Capabilities**: photo capture, video recording, audio recording, AI image generation, BLE scanning, connection management, device information retrieval, remote photo/video control
- **Activity**: active community engagement with issues opened in early 2026, indicating ongoing maintenance
- **License/status**: described as proprietary in some references; community-driven via this third-party repo. Not officially supported by HeyCyan but apparently functional.

### What the SDK supports vs what Alfred needs

| Alfred need | SDK support | Notes |
|---|---|---|
| Audio recording from glasses | Yes | Voice notes are a built-in primitive |
| BLE pairing and connection management | Yes | Standard SDK feature |
| File transfer to phone | Yes | Via WiFi sync per HeyCyan app |
| Wake word / always-on listening | **Not mentioned** | Probably unsupported; needs phone-side workaround |
| In-glasses display for HUD confirmation | **Not mentioned** | Hardware likely doesn't have it |
| Push-to-talk button | **Likely yes** (most budget glasses have a touch surface) | Needs verification on actual hardware |

### Architectural implications for Stage 4b

1. **Glasses are a thin client**, not a compute node. All intelligence runs on the phone or Alfred server.
2. **Wake word lives on the phone, not the glasses**. The user taps the glasses (or uses a phone wake word like openWakeWord) to start a session. Less magical than always-on glasses listening but acceptable for the use cases.
3. **Glasses SDK runs on the paired phone, not on Alfred directly**. So Stage 4b requires a phone-side companion app (or background service) that bridges between the SDK and Alfred's Telegram bot or talker tool's WebSocket. This is meaningfully more work than the glasses-stream-directly-to-Alfred fantasy implied by "smart glasses with wake word."
4. **Audio quality is unknown**. Budget glasses often have decent voice-note mics but struggle in noisy environments. STT accuracy may suffer outdoors or in cars.
5. **Voice notes are a built-in primitive**, so the simplest Stage 4b path is: tap glasses → record voice note → SDK syncs to phone → phone forwards to Telegram bot or directly to Alfred → talker processes → response plays back via glasses speaker. No wake word, no streaming, just voice messaging routed through wearable hardware.

### Alternative glasses to consider

Hey Cyan is the user's stated preference, but worth noting other options if the SDK proves limiting:

- **Brilliant Labs Frame** — open Lua-based SDK, developer-friendly, has a tiny in-lens display, more expensive
- **Meta Ray-Ban** — closed ecosystem, no third-party SDK currently, but Meta announced developer wearables toolkit for 2026
- **Even Realities G1** — newer entrant, has display, more expensive
- **DIY / Mentra dev platform** — for someone who wants to build their own glasses from kit

Hey Cyan is the right starting point because it's cheap, has a community SDK, and matches the budget-conscious build philosophy of Alfred. If the community SDK proves brittle, switching to Brilliant Labs Frame would be the natural upgrade.

## Multi-Instance Implications

Because each Alfred instance may have a different primary user, the talker tool deploys **per instance**. Four implications:

1. **Per-instance Telegram bot**. Each instance runs its own bot with its own token, its own webhook endpoint, its own user. The main Alfred talks to @AndrewAlfredBot; the Knowledge Alfred instance talks to @KnowledgeAlfredBot; NP's eventual instance talks to @NPAlfredBot. They never share state.

2. **Per-instance talker config**. Modes (grounded, generative, brainstorm-capture) are enabled or disabled per-instance via the tool's config section. The Knowledge Alfred instance enables generative for fiction and non-fiction writing; the main Alfred and NP's instance keep grounded only.

3. **Per-instance primary users via `talker.primary_users` config field**. The list names which `person/` records the talker should treat as its primary calibration targets. Single-user instances list one person; couples or small-team instances list multiple. The talker loads each named person record's calibration section as grounding context for voice sessions.

4. **Per-instance person record + calibration section**. Each instance's vault has its own `person/{user}.md` with its own `<!-- ALFRED:CALIBRATION -->` block. The main Alfred's calibration lives in `person/Andrew Newton.md` in the main vault. NP's calibration lives in NP's vault, not in Andrew's. Knowledge Alfred has its own vault and its own version of `person/Andrew Newton.md` with calibration tuned for creative collaboration rather than operational work. Cross-instance calibration sharing is out of scope and probably never wanted — calibration is private per user, per instance.

The voice work doesn't add multi-instance complexity. It just rides on the per-instance pattern that already exists for every other Alfred tool.

### A note on Knowledge Alfred specifically

Knowledge Alfred deserves a callout because it's the instance that uses every voice mode meaningfully:

- **Grounded mode** for non-fiction work where vault context (research notes, prior drafts, references) is the whole point
- **Generative mode** for fiction and brainstorming where vault grounding would inhibit creativity
- **Brainstorm-capture mode** for long-form ideation that becomes a structured note afterward

Knowledge Alfred's `person/Andrew Newton.md` calibration section will reflect Andrew's WRITING preferences, not his operational ones — different style cues, different push-back patterns, different Current Priorities. Same person, different facet of the user, different calibration. This is precisely why per-instance calibration matters: one Andrew is the operator of Rural Route Transportation, the other Andrew is the writer working on a novel. Alfred should know which one it's talking to.

## Future Growth (Explicitly Deferred Beyond Phase 1)

These are not on the immediate roadmap but are worth noting so future sessions know they're intentional gaps, not oversights:

- **Multi-session stitching for journaling.** Phase 1 ships with one continuous session = one session record. The future enhancement: a `continues_from` frontmatter field linking related session records together so a multi-day journaling thread can be treated as one logical conversation. Alfred would surface "this picks up where you left off in session/X yesterday" at the start of a continuation. Worth doing when journaling becomes a regular practice and the cost of fragmented threads becomes felt. Not blocking anything; small to add later.
- **Voice identification for multi-primary-user instances.** Phase 1 lists primary users in `talker.primary_users` config but doesn't auto-detect who's speaking. In a couple's instance, the talker can either ask "Andrew or NP?" at session start, or default to the first entry. Adding voice ID later (via ElevenLabs or a small local speaker-recognition model) would route automatically to the right calibration.
- **Calibration sections on `org/` and other entity types.** The pattern works for any record where Alfred maintains a behavioral model. Out of scope for phase 1; the mechanism allows it without new infrastructure.
- **Self-model for Alfred itself** via a calibration section on `project/Alfred.md`. Meta but potentially powerful.

## Open Questions Deferred to Build Time

These have obvious right answers that are easier to make when actually implementing rather than now:

1. **STT provider for Stage 1.** Three candidates (ElevenLabs Scribe, Groq Whisper, local whisper.cpp). Pick when actually building, based on what's cheapest and easiest at that moment.
2. **Default ElevenLabs voice for Alfred.** "Shimmer," "Rachel," or one of the conversational presets — pick when actually building, easy to swap.
3. **Phone-side companion app for Stage 4b glasses.** What language / framework? React Native? Native Swift? Just a Python script using a BLE library? Decide when actually building Stage 4b.
4. **Wake word fallback for Stage 4 if Cyan SDK can't support it.** openWakeWord on the paired phone is the obvious fallback. Decide when actually building.

## Sources and References

- [HeyCyan Smart Glasses Companion App — heycyan.net](https://heycyan.net/) — the official product site; companion app only, no hardware spec
- [`ebowwa/HeyCyanSmartGlassesSDK` — GitHub](https://github.com/ebowwa/HeyCyanSmartGlassesSDK) — community SDK for the glasses, BLE-based, iOS + Android
- [Smart Glasses Tech — Developer Platform](https://smartglassestech.com/developers.html) — broader smart glasses developer ecosystem reference
- [Brilliant Labs Frame](https://brilliant.xyz/) — alternative glasses with open SDK, mentioned as fallback if Cyan SDK proves limiting
- [Meta Wearables Device Access Toolkit](https://developers.meta.com/blog/introducing-meta-wearables-device-access-toolkit/) — Meta's 2026 developer platform announcement, context for the broader market direction
- ElevenLabs Conversational AI product — referenced as the Stage 3 streaming choice; product details to confirm closer to implementation
- Telegram Bot API — the proposed primary client for Stages 1–3; documentation at core.telegram.org

## Alfred Learnings

### Patterns Validated

- **Reuse the existing pipeline shape rather than building a new one.** The email pipeline (Outlook → n8n → webhook → tunnel → inbox) is structurally identical to what voice needs, and Telegram lets us slot in a new ingestion path without redesigning anything. This is the second time today that the existing email pipeline turned out to be the right starting point for a new feature (also true of Stage 1 voice capture). Pattern: when adding a new ingestion modality, ask "can I reshape the email pipeline to handle this?" before designing from scratch.
- **Per-tool config as a multi-instance lever.** The per-instance talker idea didn't require any new architecture because Alfred's tools are already configured per-instance. Modes-enabled-per-instance is just a config field. Worth remembering: per-instance variation of any tool's behavior is mostly free if the tool's behavior is config-driven.
- **The vault as bidirectional memory is unique to Alfred.** Most AI assistants either keep calibration opaque or let you view-but-not-edit it. Alfred's vault is text files in Obsidian, version-controlled, openable by any editor. That makes the profile-doc-as-calibration mechanism implementable in days rather than months because the file storage, editing UI, version history, and read/write APIs all already exist. This is a genuine architectural advantage worth leaning into for any future "user-editable AI state" feature.

### New Gotchas

- **"Smart glasses" varies wildly by hardware tier.** The fantasy of "ambient always-on glasses with wake word" is what high-end devices (Meta Ray-Ban, Brilliant Labs Frame) are aiming at, but budget glasses like Hey Cyan are closer to "Bluetooth voice memo recorders with a speaker." Plan for the hardware that actually exists and can be acquired now, not the demo videos.
- **OpenAI dependency creep is easy to introduce by accident.** I almost recommended OpenAI Realtime API as the Stage 3 choice because it's technically the fastest path. The user explicitly didn't want OpenAI. Lesson: when designing a stack, ask the user about provider preferences early — don't assume the technically-best path is the right path.

### Missing Knowledge

- **Hey Cyan hardware spec is not publicly documented.** The official site only describes the companion app. To plan Stage 4b accurately, we'll need to either acquire a unit and characterize it directly, or find a more detailed third-party review. Flagged for whoever picks up Stage 4b.
- **ElevenLabs Conversational AI exact pricing and feature set as of 2026 implementation date.** The product exists today but specifics will have shifted by the time we build Stage 3. Confirm closer to the date.
- **Telegram Bot API limits for sustained voice messaging.** For a personal-use bot the limits are generous, but if multi-user instances scale up, may need to verify rate limits on voice file uploads and processing latency. Not blocking for phase 1.
