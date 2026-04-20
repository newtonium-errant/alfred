---
type: session
created: '2026-04-20'
name: Voice Stage 2b Capture Mode 2026-04-20
description: Voice Stage 2b — brainstorm-capture mode (7-commit landing)
intent: Implement silent capture session type with async batch structuring, opt-in note extraction, and ElevenLabs TTS brief
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Voice Stage 2 Router Commits 2026-04-17]]'
tags:
  - voice
  - capture
  - telegram
  - stage-2b
status: completed
---

# Voice Stage 2b Capture Mode 2026-04-20

## Intent

Land the 7-commit Stage 2b sequence per the ratified plan (see
`project_voice_stage_2b.md`). Each commit lands code + tests + a rolling
update to this session note in one bundle per
`feedback_session_notes_per_commit.md`.

## Work Completed

### Commit 1 — Capture session type + router integration

- Added `capture` entry to `_DEFAULTS_TABLE` in
  `src/alfred/telegram/session_types.py` (Sonnet, `pushback_level=0`,
  `supports_continuation=False`).
- Added `_detect_capture_prefix()` + wired a deterministic short-circuit
  in `classify_opening_cue` so `capture:` dispatches capture WITHOUT an
  LLM call (load-bearing — a user-asserted prefix must never round-trip
  to a classifier).
- Updated `_ROUTER_PROMPT` to include the `capture` bullet with
  classification criteria ("let me brainstorm", "thinking out loud",
  "ramble").
- New tests: `tests/telegram/test_capture_session_type.py` (5 tests —
  defaults, `known_types()`, prefix short-circuit, case/whitespace
  tolerance, LLM-path classification).

### Commit 2 — Silent capture behaviour

- Added `CAPTURE_SENTINEL` module-level constant in `conversation.py`
  and a short-circuit in `run_turn` that fires when
  `session_type == "capture"`: appends the user turn, skips the LLM
  call, skips escalation detection, returns the sentinel.
- Added `session_type` kwarg to `run_turn` (wired through from
  `bot.handle_message` via the active dict's `_session_type`).
- Added `_post_capture_ack()` helper in `bot.py` using PTB 22.7's
  `Bot.set_message_reaction` API with `ReactionTypeEmoji("\N{HEAVY CHECK MARK}")`
  (= ✔). PTB 22.7 exposes the endpoint — no fallback to a text dot
  needed in the happy path.
- `handle_message` recognises the sentinel and posts the reaction
  instead of a text reply. Fallback: if `set_message_reaction` raises,
  emit a minimal "." text reply.
- Inline commands (`/end`, `/opus`, etc.) continue to fire during
  capture — the inline-command pre-check runs BEFORE the lock +
  `run_turn`, so the silent path is never entered for a command.
- New tests: `tests/telegram/test_silent_capture.py` (6 tests —
  transcript append + no LLM, regression on non-capture, sentinel
  bypass with canned responses, reaction emoji integration, fallback
  on reaction failure, /end during capture).

### Commit 3 — Async batch structuring pass

- New module `src/alfred/telegram/capture_batch.py` (~380 lines):
  - `StructuredSummary` dataclass (6 list fields: topics, decisions,
    open_questions, action_items, key_insights, raw_contradictions).
  - `run_batch_structuring()` — one Sonnet call with `tool_choice` pinned
    to `emit_structured_summary`, prompt caching on the system block.
  - `render_summary_markdown()` / `render_failure_markdown()` — produce
    the `## Structured Summary` block wrapped in `<!-- ALFRED:DYNAMIC -->`
    markers.
  - `write_summary_to_session_record()` — injects summary ABOVE the
    `# Transcript` heading via `vault_edit(body_rewriter=...)`. Sets
    `capture_structured: "true" | "failed"` frontmatter (string, not
    bool — leaves room for future "partial" state without schema
    migration). Idempotent: repeat runs replace the existing block.
  - `process_capture_session()` — top-level orchestrator. Runs batch
    pass, writes summary, sends follow-up Telegram message. Never
    raises: failures are logged + surfaced via `capture_structured:
    failed` flag + failure markdown.
- `bot.on_end`: after the session record is written, if
  `session_type == "capture"`, schedules the orchestrator via
  `asyncio.create_task` and returns a "capture processing…" reply
  immediately. The follow-up Telegram message (with `/extract` +
  `/brief` hints) arrives once Sonnet finishes.
- New tests: `tests/telegram/test_capture_batch_pass.py` (9 tests —
  happy path, missing tool_use raises, schema coercion, markdown
  rendering, idempotent writes, orchestrator happy/failure paths,
  transcript flattener).

### Commit 4 — Telegram `/extract <short-id>` command

- New module `src/alfred/telegram/capture_extract.py` (~330 lines):
  - `ExtractResult` dataclass (`created_paths` + `skipped_reason`).
  - `extract_notes_from_capture()` — resolve short-id via
    `closed_sessions`, load session record, run extraction LLM call
    (tool_choice=auto so the model can emit fewer notes), create
    note records via `vault_create`, update session frontmatter
    `derived_notes` with wikilinks.
  - Each note carries `created_by_capture: true`, `source_session:
    [[session/...]]`, `confidence_tier: high|medium` frontmatter.
    Body includes a source-quote blockquote + `_Source: [[...]]_`
    attribution.
  - Implicit chain: if the session record has no
    `## Structured Summary` block, run the batch pass first via a
    synthetic transcript reconstructed from the body
    `**Andrew** (hh:mm):` lines.
  - Idempotency: if the session already has `derived_notes`
    populated, return the existing list with
    `skipped_reason="already_extracted"` (no LLM call).
  - Max-notes defensive trim (DEFAULT_MAX_NOTES=8, configurable).
- New `on_extract` handler in `bot.py`:
  - Registered as `/extract` CommandHandler.
  - Inline-command dispatch extended: new
    `_INLINE_CMD_WITH_ARG_RE` matches `/extract <short-id>` at
    start-of-message AND end-of-line (end-of-line form requires a
    trailing arg so the no-arg path doesn't shadow it).
  - `_parse_short_id_arg()` tolerates both `ctx.args` (from
    CommandHandler) and inline regex extraction from the message
    text.
  - `on_extract` surfaces all skip reasons as distinct reply
    strings (no session / already extracted / no notes / llm
    error).
- `_INLINE_COMMANDS` gains `"extract"` + `"brief"` (brief handler
  lands in commit 5 but the set entry is a single point of
  truth).
- New tests: `tests/telegram/test_capture_extract.py` (11 tests —
  happy path, idempotency, missing session, max-notes cap, implicit
  structuring chain, inline detect, short-id parse, inline
  dispatch end-to-end).

### Commit 5 — ElevenLabs TTS + `/brief` command

- `pyproject.toml`: added `elevenlabs>=1.0` to `[voice]` extras. The
  in-tree implementation uses httpx directly (no SDK import), but
  pinning the SDK makes it available for future SDK-based swaps
  without another pip install.
- `src/alfred/telegram/config.py`: new `TtsConfig` dataclass
  (provider, api_key, model=`eleven_turbo_v2_5`, voice_id=`Rachel`,
  summary_word_target=300). `TalkerConfig.tts` defaults to `None`
  so the /brief handler can distinguish "not configured" from
  "configured with empty values". Section is optional in
  `load_from_unified`.
- New module `src/alfred/telegram/tts.py`:
  - `resolve_voice_id()` — friendly-name → canonical id (Rachel →
    `21m00Tcm4TlvDq8ikWAM`). Case-insensitive. Unknown names
    pass through as raw ids.
  - `synthesize()` — POSTs to
    `https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` via
    httpx with `xi-api-key` header. Raises `TtsError` on non-200
    or network failure, `TtsNotConfigured` on empty key.
  - `send_voice_to_telegram()` — PTB `send_voice` under 50 MB,
    `send_document` fallback above.
  - `compress_summary_for_tts()` — Sonnet call rendering the
    structured summary block as ~300 words of spoken prose.
- `on_brief` handler in `bot.py`:
  - Registered as `/brief` CommandHandler + inline handler.
  - Usage reply on missing short-id; "not configured" reply on
    absent tts section.
  - Implicit chain: if session record has no summary block, runs
    the batch pass before compressing.
  - Failure modes: TTS API down → text fallback reply with the
    compressed prose; upload failure → text fallback; send_voice
    >50 MB → auto-falls-back to send_document via the tts.py
    helper.
  - Per-session cost log line (`talker.brief.cost_estimate`) with
    approximate `$0.30/1M chars` Turbo v2.5 PAYG rate.
- `config.yaml.example`: new commented-out `telegram.tts` block
  with defaults.
- `.env.example`: new `ELEVENLABS_API_KEY` placeholder.
- New tests:
  - `tests/telegram/test_tts_brief.py` (14 tests — happy path,
    voice mapping, httpx mocking, compress, send_voice under/over
    cap, config shape).
  - `tests/telegram/test_tts_failure.py` (4 tests — not configured,
    API down fallback, missing session, implicit batch pass).

### Commit 6 — BIT probe additions for TTS

- Extended `src/alfred/telegram/health.py` with three new probes:
  - `tts-key` (static, <50ms): verifies `telegram.tts.api_key`
    present + env var resolved. SKIPs when tts section absent.
  - `capture-handler-registered` (functional, <50ms): import
    check on `capture_batch` and `capture_extract`.
  - `elevenlabs-auth` (remote_network, <2s): GET
    `https://api.elevenlabs.io/v1/user` with xi-api-key header.
    Only runs in `full` mode (pre-brief quick mode stays fast).
    SKIPs when tts section absent or key missing.
- All three SKIP gracefully when `telegram.tts` is absent; `/brief`
  is opt-in and its absence doesn't FAIL the rollup (though it DOES
  mark the rollup SKIP because `Status.worst` ranks SKIP > OK —
  the user correctly sees "we didn't check everything").
- Two existing tests in
  `tests/health/test_per_tool_telemetry.py` updated to reflect the
  new reality: `test_happy_path_ok` + `test_env_var_placeholders_are_expanded`
  now include a tts section so the rollup stays OK, and pass
  `mode="quick"` so the remote elevenlabs probe isn't attempted.
- New tests: `tests/telegram/test_health_tts_probes.py` (11 tests —
  each probe's happy/FAIL/SKIP paths, quick-mode skips remote
  probe, absent-tts doesn't FAIL rollup).

### Commit 7 — SKILL audit: vault-talker SKILL.md for capture mode

- Updated `src/alfred/_bundled/skills/vault-talker/SKILL.md`:
  - Frontmatter version bumped `1.0-wk1` → `1.1-wk2b`.
  - New `## Session types and capture mode` section after
    `## Session boundaries`.
  - Describes what the LLM sees on each of the three call paths
    the bot layer invokes it through during a capture session:
    (a) the batch structuring pass (`emit_structured_summary`
    tool, six buckets, empty lists legal), (b) `/extract`
    (`create_note` tool, up to 8 notes, quality over quantity),
    (c) `/brief` (compress structured summary to ~word-target
    prose, flowing paragraphs for spoken output).
  - Notes that during a capture session the LLM is NOT invoked
    turn-by-turn — the session is silent. The LLM only runs at
    the post-`/end` batch pass, `/extract`, and `/brief` steps.
  - Anti-patterns explicitly flagged: don't editorialise during
    the batch pass; don't synthesise across sessions during
    extraction; don't write eye-prose for /brief output.
  - Preserved `{{instance_name}}` / `{{instance_canonical}}`
    templating — nothing hardcoded to "Alfred" or "Salem".
- Per the CLAUDE.md scope-SKILL audit rule, this closes the new
  LLM-facing contracts introduced by commits 3 / 4 / 5 (the three
  tool_use schemas) with a matching skill-prompt update.
- No new tests — SKILL change is prompt-only. All 513 existing
  tests still pass; `test_instance_templating.py` verifies the
  `{{instance_name}}` substitution survives the edit.

## Outcome

All 7 commits landed successfully.

**Commit hashes:**
- c1 `cefe063` — capture session type + router integration (+5 tests)
- c2 `09e5f7b` — silent capture behaviour (+6 tests)
- c3 `68112e1` — async batch structuring pass (+9 tests)
- c4 `fe2d3ac` — Telegram `/extract <short-id>` command (+11 tests)
- c5 `b0c5345` — ElevenLabs TTS + `/brief` command (+18 tests)
- c6 `2bab8e7` — BIT probe additions for TTS (+11 tests)
- c7 `(this)` — SKILL audit for capture mode (no new tests)

**Test count:** 453 (baseline) → 513 (+60). Full suite green.

**BIT probes for /brief:**
- `tts-key` (static, SKIP when tts section absent)
- `capture-handler-registered` (functional, module import check)
- `elevenlabs-auth` (remote_network, full mode only, SKIP when tts absent)

**Session note:** this file, updated in place across all 7 commits
per `feedback_session_notes_per_commit.md`.

**Deviations from spec:**
- Used plain `\N{HEAVY CHECK MARK}` (✔) for the capture reaction emoji
  via PTB 22.7's `ReactionTypeEmoji`. PTB 22.7 supports
  `set_message_reaction` directly — no fallback dot path needed in
  the happy case (the fallback is still in place defensively).
- Used httpx directly for ElevenLabs REST calls rather than the
  `elevenlabs` SDK import. SDK is pinned in `[voice]` extras
  per spec but not imported at runtime. This keeps our import
  graph clean and tests can mock `httpx.AsyncClient.post` via
  `monkeypatch`.
- Two pre-existing talker health tests updated in place because
  the new optional probes changed the SKIP/OK rollup shape
  (`Status.worst` ranks SKIP > OK intentionally). Updates are
  minimal: pass a `tts:` section + `mode="quick"` so the rollup
  stays OK.

**Daemon restart required** to pick up the new handlers and
capture-mode routing in a live environment. Andrew to run
`alfred down && alfred up` once he's ready to validate.

## Alfred Learnings

_(updated on each commit)_

- **Pattern validated — deterministic prefix before classifier.** When a
  user explicitly prefixes their opening message with a type marker
  (`capture:`), short-circuit the classifier entirely. Round-tripping a
  user-asserted classification through an LLM risks mis-routes on
  borderline phrasings. Belongs BEFORE the LLM call in
  `classify_opening_cue`, not after (the post-classification fallback
  would already have paid a Sonnet token cost).

- **Pattern validated — sentinel string for "no reply" paths.**
  `run_turn` historically returned the assistant's text string. Adding a
  "don't reply" mode via `Optional[str]` or a tuple would force every
  existing caller to branch on the new shape. A module-level sentinel
  string (`CAPTURE_SENTINEL`) stays backwards compatible at the type
  level — callers that don't know about capture treat it as any other
  text — and the capture-aware caller does `if text == SENTINEL` to
  bypass the reply path. Cleanest upgrade.

- **Pattern validated — PTB 22.7 exposes `set_message_reaction` cleanly.**
  No version drift concern: the bot API endpoint `setMessageReaction`
  was added to PTB in 21.x and is unchanged in 22.7. The fallback path
  (text dot) stays in place defensively but does not fire in the happy
  path.

- **Pattern validated — `tool_choice={"type": "tool", "name": ...}`
  for schema-enforced outputs.** The batch structuring pass uses a
  single tool (`emit_structured_summary`) and pins `tool_choice` to it
  so the model MUST emit a tool_use block. Avoids the "model narrates
  instead of emitting" failure mode that plagues pure-text JSON
  extraction. Also cheaper — no wasted tokens on preamble text.

- **Pattern validated — frontmatter status as string, not bool.**
  `capture_structured: "true" | "failed"` leaves room for future
  states (`"partial"`, `"queued"`) without a schema migration. A bool
  would force a second field or a breaking change. The same argument
  applies to other multi-state flags we might add later.

- **Pattern validated — `<!-- ALFRED:DYNAMIC -->` markers for
  rewritable body blocks.** Mirrors the existing calibration-block
  protocol. The distiller and any downstream parser can strip these
  blocks uniformly via the marker pair. The `_insert_summary_above_transcript`
  helper uses the markers to make writes idempotent — repeat runs
  replace the block instead of stacking.

- **Pattern validated — idempotency via frontmatter list presence.**
  `/extract` checks the session record's `derived_notes` field; if it's
  non-empty, skip and return the existing list. No state duplication,
  no extra cache. The frontmatter list IS the state. Applies generally:
  any derived-output command should store its outputs in a frontmatter
  list and skip-if-present rather than storing a "has been run" bool
  separately.

- **Pattern validated — inline-command with-arg regex has priority
  over no-arg regex.** The existing `_INLINE_CMD_RE` matches
  `/command\s*$` (no trailing args). Without a separate with-arg
  matcher that fires FIRST, `please /extract abc123` would fall
  through to no-arg matching, which would fail because of the
  trailing arg. New `_INLINE_CMD_WITH_ARG_RE` is checked before the
  no-arg form so commands that take an argument (extract, brief)
  route correctly.

- **Pattern validated — httpx direct for third-party REST, SDK in
  extras only.** ElevenLabs ships a Python SDK but it pulls
  `pydantic`, `websockets`, and `requests` as transitive deps.
  Using httpx directly (our existing dep) keeps import graphs
  clean and cuts the surface that needs mocking. SDK is still
  listed in `[voice]` extras so users who want to swap to SDK-based
  calls later can do so without reinstalling.

- **Pattern validated — `TtsConfig | None` on the parent dataclass
  for optional sections.** When a config section is OPTIONAL (the
  feature degrades gracefully when absent), make the field
  `None`-defaulting on the parent rather than stubbing an empty
  dataclass. That way `config.tts is None` cleanly distinguishes
  "user didn't configure this" from "user configured with empty
  fields". `load_from_unified` only builds the dataclass when the
  section is present in the raw dict.

- **Gotcha confirmed — PTB `ctx.args` shape differs between
  CommandHandler and inline dispatch.** When PTB routes a message
  to a CommandHandler, `ctx.args` is a populated list of space-
  separated tokens. When the inline-dispatch path re-invokes the
  same handler, `ctx.args` is whatever the caller set (usually
  `None` or a `MagicMock`). `_parse_short_id_arg` defensively
  checks `isinstance(args, list)` and falls back to regex-parsing
  the raw message text.

- **Gotcha confirmed — `Status.worst` ranks SKIP above OK.**
  This is deliberate per `types.py` ("the user should see 'we
  didn't check everything' before they see a green rollup"), but
  it means adding optional probes that SKIP when their config
  section is absent will bubble up to mark the entire tool's
  status SKIP rather than OK. Pre-existing tests that asserted
  `result.status == Status.OK` must be updated when new optional
  probes are added — either pass a config that exercises them or
  assert per-probe status instead of the rollup.

- **Pattern validated — quick vs full mode for remote network
  probes.** The talker's remote elevenlabs-auth probe runs only
  in `full` mode (nightly BIT) — quick mode (pre-brief) stays
  under its latency budget by skipping the 2s ceiling call. This
  matches the broader pattern: cheap static checks always run;
  expensive network checks are full-mode-only.

- **Pattern validated — scope-SKILL audit at final commit.** Per
  CLAUDE.md's "scope-SKILL audit rule", any commit that introduces
  new LLM-facing contracts (new tool_use schemas, new prompts,
  new call paths) must be accompanied by a SKILL.md update
  documenting what the LLM is expected to do in each new path.
  Commits 3/4/5 each introduced a distinct LLM-call contract
  (batch structuring tool schema, create_note tool schema, brief
  compression prompt) — commit 7 lands the matching skill-prompt
  explanation in one place so prompt-tuner ownership of SKILL.md
  stays explicit (builder writes the code; prompt-tuner / builder
  together audit the skill).
