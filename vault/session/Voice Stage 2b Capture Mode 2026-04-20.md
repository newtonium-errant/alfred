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
