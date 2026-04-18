---
alfred_tags:
- software/alfred
- voice
- hotfix
created: '2026-04-18'
description: Strip underscore-prefixed metadata keys (_ts, _kind) from transcript
  turns before sending to Anthropic's Messages API. The wk2 polish fix stamped
  these for session-record rendering; they leak through messages= and Anthropic
  400-rejects unknown fields.
intent: Unblock the talker after first post-wk3 live E2E hit 400 errors on every
  turn
name: Talker API Schema Hotfix
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Talker E2E Validation and Same-Day Fixes 2026-04-18]]'
status: completed
tags:
- hotfix
- voice
- regression
type: session
---

# Talker API Schema Hotfix — 2026-04-18

## Intent

Live E2E after wk3 ship produced `400: messages.0._ts: Extra inputs are not permitted` on every turn. Router + STT + classification all fired cleanly; the failure was on the main conversation API call.

## Root cause

Wk2 commit 5 (polish bug fixes, `4e681a6`) stamped `_ts` on every `append_turn` call to fix the transcript-timestamps bug. `_kind` stamping landed in the same commit for the voice_messages counter. Both were stored directly on the transcript turn dicts.

`run_turn` then passed `messages=session.transcript` verbatim to `client.messages.create`. Anthropic's Messages API strictly validates each turn's schema and rejects unknown fields — the 400 was its response to seeing `_ts`.

Wk1 smoke tests used a mocked Anthropic client that returned responses directly without validating input shape; the leak never surfaced until live API validation today.

## What shipped

New helper `_messages_for_api(transcript)` in `src/alfred/telegram/conversation.py` that strips any `_`-prefixed keys before sending. Applied at the one API call site in `run_turn`. Non-mutating — preserves the original transcript for session persistence and record rendering.

5 regression tests in `tests/telegram/test_api_message_schema.py` pin the contract: underscore-prefixed keys stripped, standard keys preserved, complex content-block shapes preserved, input never mutated, empty transcript handled.

## Verification

132/132 tests pass (131 baseline + 5 new - 4 collapsed into one module since they share the `_messages_for_api` target). Daemons need restart to activate.

## Alfred Learnings

**Mocked-client smoke tests don't catch Anthropic API schema violations.** The wk1 mock returned a canned response without validating input shape — so the `_ts` leak from wk2 never tripped a test. For any API-schema-sensitive code path, add at least one contract test (like `test_api_message_schema.py`) that pins the outbound payload shape separately from the mock response. The API's strict-field validation is the real contract, not the mock.

**Underscore-prefixed keys are the convention for "internal metadata" everywhere else in the codebase** (stashed fields on the active dict, hint fields on records). Applying that convention to transcript turns was natural but broke the API contract. Worth codifying: any dict that crosses the SDK boundary needs sanitization at the boundary.

**Per-commit bundled session notes (per `feedback_session_notes_per_commit.md`)** — this hotfix follows the corrected pattern: code + tests + note in one commit.
