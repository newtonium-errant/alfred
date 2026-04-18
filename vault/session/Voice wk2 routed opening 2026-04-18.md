---
type: session
status: completed
name: Voice wk2 routed opening
created: 2026-04-18
description: Commit 4 of 5 — wire the opening-cue router into bot.handle_message so new sessions start with the right type, model, and (optionally) a continuation primer.
intent: Close the loop between router (commit 3) and session-record (commit 2) so an article cue genuinely opens on Opus, a task cue stays on Sonnet, and continuation references land in both state and the session record.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk2 opening cue router 2026-04-18]]'
- '[[session/Voice wk2 session record fields 2026-04-18]]'
tags:
- voice
- talker
- wk2
---

# Voice wk2 — routed opening

## What changed

`src/alfred/telegram/bot.py`:

- `_recent_sessions_for_router(state_mgr)` — reverses `closed_sessions` (oldest-first) and caps at 10. Feeds the router prompt.
- `_find_closed_session(state_mgr, record_path)` — locate a summary entry by record path; used to pull `message_count` / `ended_at` into the primer.
- `_open_routed_session(state_mgr, config, client, chat_id, first_message)` — new helper. Runs the router, opens a session on the decision's model, stashes `_session_type` / `_continues_from` on the active dict, and — if continuation is valid — appends a single assistant-style primer turn to the transcript referencing the prior record.
- `handle_message` now calls `_open_routed_session` both on first-message-ever AND on rehydrate failure (a corrupted active dict should feel like a fresh session to the user, not a silently-resumed stub).

`src/alfred/telegram/session.py`:

- **Bug fix**: `_persist()` now preserves stashed `_*` metadata. Wk1 got away with `set_active(chat_id, session.to_dict())` because nothing called `_persist` between stash and the first user turn. Wk2's continuation primer DOES call `_persist` (via `append_turn`) during opening, which was wiping the stashed fields. Fixed by merging `existing` active dict keys over `session.to_dict()` and re-applying anything starting with `_`.

## Tests

`tests/telegram/test_continuation.py` (3 tests):

1. Article continuation with a known prior → session on Opus, primer turn in transcript ("continuing from a prior article session (12 turns, ended 2026-04-17). Record: session/…"), active dict has `_session_type=article` + `_continues_from=[[session/…]]`.
2. Hallucinated continuation path → dropped by router, type stays `article`/Opus, transcript empty, `_continues_from=None`.
3. Plain note cue → Sonnet, `note` type, empty transcript, no primer.

pytest: 31/31 passing.

## Contracts

- `_continues_from` is stored as a wikilink string (`[[session/…]]`) on the active dict so it matches what the session record frontmatter will emit at close time.
- Primer format: `"[context: continuing from a prior <type> session (<N> turns, ended <YYYY-MM-DD>). Record: session/<name>. Ask before assuming — you may need to read the record first.]"` — one line, assistant role, not user. Shows up in the transcript above the first real user turn.

## Alfred Learnings

- **Pattern discovery**: wk1's `_persist` silently wiped orthogonal metadata because nothing ever wrote between the stash and the first user turn. Adding ONE new `append_turn` at session open triggered a latent data-loss bug. Lesson: stashed `_*` metadata must live outside the dataclass round-trip. Fixed in `_persist` once, benefits everything.
- **Anti-pattern avoided**: tempting to seed the full prior transcript from state. State only holds a summary (`message_count`, `ended_at`), not the transcript — and that's fine. The primer tells the model "there's a prior record, fetch it if you care." Cheaper and avoids doubling session memory on every continuation.
