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
status: in-progress
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

## Outcome

_(updated on commit 7)_

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
