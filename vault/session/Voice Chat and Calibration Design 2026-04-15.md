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

### Per-instance talker

Each Alfred instance runs its own talker tool. Same code, per-instance config. The talker's modes (grounded, generative, brainstorm-capture) are enabled or disabled per instance. The story-writer instance might enable generative mode; NP's instance might have grounded only with a different SKILL.md tone. The main Alfred starts with grounded only.

This fits the existing per-tool config pattern exactly. No new architecture needed for multi-tenancy — it's already how curator, janitor, etc. are configured per instance.

## Modes

Three modes, all powered by the same talker tool, distinguished by SKILL.md prompts and per-session flags.

### Grounded mode (MVP — ship first)

Vault search aggressive. Alfred cross-references the user's previous sessions, decisions, and entity records. Push-back fires on inconsistencies. Best for journaling, query, and task execution.

**Push-back calibration: 4 out of 10.** Alfred surfaces its current understanding every ~4 turns OR when it detects a contradiction with session history or linked vault records. Not interrogative, not silent — thoughtful-friend frequency.

The push-back is **bidirectional**: Alfred says "I'm hearing X, is that right?" and the user can confirm, correct, or realize their own thinking has shifted. Corrections become assumption/synthesis records, propagating to future sessions.

### Generative mode (deferred — instance-specific)

Vault search disabled or read-only. Alfred is free to imagine, propose, remix. "Yes and" energy. Best for story ideation, brainstorming creative work, exploring hypotheticals where vault grounding would inhibit creativity.

**Will not exist in the main Alfred instance.** Belongs to a future story-writer instance after multi-instance architecture is built. NP's instance probably also doesn't get this mode.

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

## Bidirectional Calibration via Profile Doc

The most architecturally important piece of this design.

### The mechanism

Alfred maintains its understanding of the primary user as a **first-class vault record** at `profile/Andrew Newton.md` (or equivalent per instance). The user can open this file in Obsidian and edit it directly. Alfred reads it as grounding context at the start of every voice session and writes to it during sessions when reflection-worthy moments occur.

**Two-way editable memory**: Alfred writes, user edits, Alfred reads the user's edits, behavior propagates. Most AI assistant calibration systems either keep the calibration opaque (the user can't see it), let the user view but not edit it, or let the user edit but don't react to the edits. Vault-backed and git-tracked, all three problems dissolve.

### Document shape

```markdown
---
type: profile
name: Andrew Newton
updated: 2026-04-15
alfred_calibration: true
---

# User Profile — Andrew Newton

## Communication Style

- **Military-style comms**: terse, direct, high-signal/low-noise.
  _Confirmed 2026-03-01 · source: session/Alfred Setup and Email Integration 2026-03-26_
- **Prefers Option A/B/C framing** for non-trivial decisions, rather than open-ended exploration.
  _Inferred 2026-04-15 · source: session/[voice session]_ [needs confirmation]
- **Rejects excessive caveats and hedging.**
  _Corrected 2026-04-10 · replaced earlier "appreciates nuance"_

## Roles and Responsibilities

- **Primary**: operator of Rural Route Transportation, owner of the Struggle Bus brand
- **Secondary**: builder of Alfred, personal knowledge/operational system
- **Partnership context**: NP is partner, may become a primary user of a separate Alfred instance

## Workflow Preferences

- One logical session per commit
- Every commit paired with a session note in `vault/session/`
- Surgical hunk-level staging when pre-existing dirty files are in scope
- Python-layer enforcement first, prompt-layer as belt-and-braces
  _Updated 2026-04-15 from voice session — previous belief: prompt-first_

## Current Priorities

- Shipping Alfred voice chat integration (Telegram first, glasses eventually)
- Multi-instance architecture (hub-and-spoke, 5 instances planned)
- Morning Brief module (RCAF-style briefing)

## What Alfred Is Still Unsure About

- [ ] How much push-back during voice journaling feels right (current setting: 4/10 — to be tuned)
- [ ] Whether to auto-create task records from voice intent or always confirm first
- [ ] Whether the story-writer instance should have any vault access at all
```

### Provenance and auditability

Three layers ensure the user can always see where a belief came from:

1. **Inline source markers** on each bullet — `_source: session/[name]_` points to the session that produced the claim
2. **Vault git history** — every edit to the profile is a commit in the vault's inner git repo. Full history available via `vault snapshot --log` or directly with git in `vault/.git`
3. **Optional changelog section** at the bottom of the profile — chronological list of significant belief shifts with their source sessions, for fast scanning without digging through git history

The user should never have to wonder "how did Alfred get this idea?" — the source is one click away.

### Update protocol during a voice session

When the talker detects a reflection-worthy moment:

1. **Surface understanding**: "I'm hearing that you want X. That's a shift from your profile, which currently says Y. Want me to update the profile to X?"
2. **User responds**: confirm, correct, or deflect ("let me think on that")
3. **On confirmation**: talker calls `alfred vault edit profile/Andrew Newton.md` with the specific bullet change, including source-session attribution
4. **On correction**: talker uses the corrected statement instead, still attributed
5. **On deflection**: talker leaves a `[needs confirmation]` entry in the "What Alfred Is Still Unsure About" section for next session

### Confirmation policy

Default policy: **explicit confirmation for profile edits, silent append for "[needs confirmation]" entries Alfred is unsure about**. Safer than silent writes for confident claims, faster than gating every edit, reviewable by the user later in Obsidian regardless. Tunable.

### Profile pattern beyond user calibration

The profile-doc-as-vault-artifact pattern isn't limited to user calibration. Future extensions:

- `profile/Alfred.md` — Alfred's self-model. What it believes about its own behavior, known weaknesses, current experimental modes. The user edits this to change Alfred's self-understanding. Meta but potentially powerful.
- `profile/{instance}.md` — per-instance profile so different Alfred instances behave differently
- `profile/{person}.md` — for every person Alfred knows about, not just the primary user. "Here's what Alfred believes about Dr. Bailey." Updated when journaling about meetings. Used as context when next interacting with that person. Powerful but out of scope for phase 1.

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

| Stage | Target | Client | Stack | Time estimate | Hardware-dependent |
|---|---|---|---|---|---|
| **1** | Async voice capture (foundation) | Telegram bot | n8n → Whisper (Scribe/Groq/local) → inbox → curator | ~1 week | No |
| **2a** | Turn-based grounded conversation (journaling, task exec, query) | Telegram bot | Talker tool + Claude + ElevenLabs STT/TTS batch | ~2-3 weeks | No |
| **2b** | Brainstorm-capture mode (long dictation + smart format + audio summary) | Telegram bot | Same talker, new SKILL.md mode | ~1 week after 2a | No |
| **3** | Real-time streaming conversation | Telegram bot + maybe web PWA | ElevenLabs Conversational AI with Claude brain | ~2 weeks | Yes — Mac Studio, fall 2026 |
| **3.5** | Multi-instance architecture (prerequisite for instance-specific talker modes) | — | Per-instance deploy pattern across the whole stack | Separate track, scope unknown | No, but big |
| **4a** | Generative mode (story-writer instance only) | Telegram bot | Talker SKILL.md mode + per-instance config | Days, depends on 3.5 | No |
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
- **Modes available**: grounded only. Generative is deferred.
- **Push-back level**: 4/10 by default, configurable per-session
- **Session shape**: one continuous session per "conversation start". User starts a session, talks back-and-forth, ends the session. Multi-session stitching is deferred to a future enhancement.
- **Profile doc integration**: talker loads `profile/Andrew Newton.md` as grounding context at session start, writes to it during the session via the calibration update protocol

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

Prerequisite for Stage 4a (generative mode in a story-writer instance) and the eventual NP instance. Out of scope for this design doc but blocks the per-instance-mode features.

### Stage 4a — Generative mode (instance-specific, depends on 3.5)

- Same talker code, new SKILL.md mode
- Enabled per-instance via config
- Story-writer instance gets it, main Alfred and NP instance probably don't

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

Because each Alfred instance may have a different primary user, the talker tool deploys **per instance**. Three implications:

1. **Per-instance Telegram bot**. Each instance runs its own bot with its own token, its own webhook endpoint, its own user. The main Alfred talks to @AndrewAlfredBot; NP's instance talks to @NPAlfredBot. They never share state.

2. **Per-instance talker config**. Modes (grounded, generative, brainstorm-capture) are enabled or disabled per-instance via the tool's config section. The story-writer instance enables generative; the main Alfred and NP's instance probably don't.

3. **Per-instance profile doc**. Each instance's vault has its own `profile/{user}.md`. The main Alfred's profile is `profile/Andrew Newton.md` in the main vault. NP's profile lives in NP's vault, not in Andrew's. Cross-instance profile sharing is out of scope and probably never wanted — calibration is private per user.

The voice work doesn't add multi-instance complexity. It just rides on the per-instance pattern that already exists for every other Alfred tool.

## Open Questions and Deferred Decisions

These are explicitly NOT decided in this doc and will be revisited closer to implementation:

1. **STT provider for Stage 1.** Three candidates (ElevenLabs Scribe, Groq Whisper, local whisper.cpp). Pick when actually building, based on what's cheapest and easiest at that moment.
2. **Default ElevenLabs voice for Alfred.** "Shimmer," "Rachel," or one of the conversational presets — pick when actually building, easy to swap.
3. **Whether profile edits require explicit confirmation or can be silent.** Default is explicit confirmation for confident claims, silent append for `[needs confirmation]` entries. Tunable based on actual voice session feel.
4. **Multi-session stitching for journaling.** Can a journaling session span multiple separate conversations across days? Simple answer for MVP: no, one conversation = one session record. Future enhancement: mark sessions as related via a `continues_from` frontmatter field.
5. **Phone-side companion app for Stage 4b glasses.** What language / framework? React Native? Native Swift? Just a Python script using a BLE library? Decide when actually building Stage 4b.
6. **Wake word fallback for Stage 4 if Cyan SDK can't support it.** openWakeWord on the paired phone is the obvious fallback. Decide when actually building.

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
