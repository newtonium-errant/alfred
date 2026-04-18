---
type: session
status: completed
name: Voice wk2 polish bugs
created: 2026-04-18
description: Commit 5 of 5 — bundle three wk1 polish fixes (transcript timestamps, voice counter cleanup, stray mutation-log drop) into one commit.
intent: Clean up wk1 rough edges before the wk2 landing is considered complete. Team-lead bundled these into one polish commit.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk2 session types module 2026-04-18]]'
- '[[session/Voice wk2 session record fields 2026-04-18]]'
- '[[session/Voice wk2 opening cue router 2026-04-18]]'
- '[[session/Voice wk2 routed opening 2026-04-18]]'
tags:
- voice
- talker
- wk2
- polish
---

# Voice wk2 — polish bugs (commit 5)

## (a) Transcript timestamps

`src/alfred/telegram/session.py`:
- `append_turn` now stamps `_ts` (ISO 8601) on every turn.
- User turns additionally carry `_kind` (``"text"`` / ``"voice"``).
- Added `kind: str = "text"` parameter to `append_turn`.

**Result**: `_build_session_body` renders real per-turn HH:MM timestamps. Previously every turn rendered as the session-start time, which made a 30-minute conversation look like one minute.

## (b) Voice counter cleanup

Wk1 was double-tracking voice/text counts: once as state-dict counters (`voice_messages` / `text_messages` on the active session) AND again derived per-turn from `_kind` at close time. The two paths could disagree. Dropped the state-dict counters.

`src/alfred/telegram/conversation.py`:
- `run_turn` accepts `user_kind: str = "text"` and threads it into `append_turn(kind=...)`.

`src/alfred/telegram/bot.py`:
- `_open_session_with_stash` no longer calls `setdefault("voice_messages", 0)` / `("text_messages", 0)`.
- `handle_message` no longer increments the state-dict counters; instead it calls `run_turn(..., user_kind="voice" if voice else "text")`.

`_count_message_kinds` (session.py) is unchanged — it already derived voice/text from `_kind`, which is now always present on user turns.

## (c) Stray mutation-log call

`src/alfred/telegram/conversation.py`:
- Dropped both `mutation_log.log_mutation(session.session_id, ...)` calls.
- Dropped `mutation_log` from the vault imports.

**Bug**: `mutation_log.log_mutation` expects a JSONL *file path* as its first arg. Wk1 passed `session.session_id` — a UUID. That caused `open(<uuid>, "a")` to create a file literally named `286921d8-07d0-4735-8da9-2355decf2577` at the CWD (repo root) on the first voice session that called `vault_create`. Andrew noticed and flagged it for this batch.

**Why drop not fix**: the info was already tracked in `session.vault_ops` (via `append_vault_op` → session-record frontmatter). The `data/vault_audit.log` wiring (being threaded in separately) will pick up the same events. The `mutation_log` module is scoped to CLI-backend agent runs, not the talker's in-process tool loop — passing any path there would be incorrect semantics.

The stray file `286921d8-*` was already cleaned from the repo root before this commit.

## Tests

- `tests/telegram/test_session_body.py` (2 tests): `_ts` is stamped, body renders distinct timestamps.
- `tests/telegram/test_voice_counter.py` (3 tests): count function works from per-turn `_kind`, `run_turn` threads `user_kind`, `_open_session_with_stash` doesn't seed the redundant counters.
- `tests/telegram/test_no_orphan_mutation_log.py` (2 tests): call-signature guard, end-to-end `_execute_tool` leaves no stray UUID file at CWD.

pytest: 38/38 passing (31 prior + 7 new).

## Live E2E validation plan

Each polish fix is testable via a fresh live session after daemon restart:

1. **(a) timestamps**: send 3-4 voice/text messages over 5+ minutes. Close with `/end`. Read the session record — each turn should show its own HH:MM. Wk1 would collapse them all to the open-time.
2. **(b) voice counter**: mix voice + text messages in one session. Session record's `telegram.voice_messages` / `text_messages` should reflect actual mix; state file's active_sessions dict should have NO `voice_messages` / `text_messages` keys while the session is open.
3. **(c) stray file**: open a session, ask Alfred to create a task, watch `ls /home/andrew/alfred/ | grep -E '^[a-f0-9]{8}'` — empty before, empty after.

## Contracts

- `append_turn` is still signature-compatible with wk1 callers — `kind` defaults to `"text"` so any missed call site just loses the voice/text distinction, not correctness.
- `run_turn` is still signature-compatible — `user_kind` defaults to `"text"`. Bot layer explicitly threads the real value.

## Alfred Learnings

- **Bug class**: passing a UUID where a file path was expected produced a silent, but filesystem-visible, failure. The right pattern for a signature like this is to typecheck the arg (is it a path? does it exist?), not to just `open()` and hope. Not going to add the typecheck to mutation_log itself — caller was wrong — but the lesson is: when a function takes `path: str | Path`, think hard about whether the caller might hand over a UUID or other random string.
- **Anti-pattern confirmed**: redundant state tracking (state-dict counters + per-turn `_kind`) eventually drifts. Pick one source of truth and delete the other. In this case the per-turn data is more granular and useful for future analysis, so it wins.
- **Pattern validated**: bundling three small polish fixes behind one commit with three distinct test files kept the review surface small while still producing a clean isolated test per bug. Worth doing again for similar cleanups.
