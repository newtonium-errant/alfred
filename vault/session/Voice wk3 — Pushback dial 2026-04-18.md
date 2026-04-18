---
type: session
status: completed
name: Voice wk3 — Pushback dial
created: 2026-04-18
description: Commit 1 of 8 in Voice Stage 2a-wk3 — rename pushback_frequency to integer pushback_level and wire it into the turn loop as a fourth cache-control system block.
intent: Give every session type a deterministic per-level prompt directive the model can execute against, and lay down the cache-control slot calibration (commit 2) will sit in.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
- '[[session/Voice wk2 session types module 2026-04-18]]'
tags:
- voice
- talker
- wk3
- pushback
---

# Voice wk3 — Pushback dial

## Intent

Wk2 stored a string `pushback_frequency` (`none|low|medium|high`) on every session type but nothing consumed it — the prompt never told the model how hard to push back, so every session behaved the same regardless of type. Wk3 commit 1 converts the field to an int 0-5, adds a per-level directive lookup table in `conversation.py`, and appends the directive as the fourth cache-control system text block after the system prompt, vault context, and (in commit 2) calibration block.

## Work Completed

- `src/alfred/telegram/session_types.py`: renamed `pushback_frequency: str` → `pushback_level: int` on `SessionTypeDefaults`. Values: task=0, note=1, article=3, journal=4, brainstorm=4 (per plan).
- `src/alfred/telegram/conversation.py`:
  - New `_PUSHBACK_DIRECTIVES: Final[dict[int, str]]` table with six distinct directives (levels 0-5). Level 5 is reserved for the confrontational "devil's-advocate" mode that wk3 doesn't validate yet but is documented so the config surface is complete.
  - `_pushback_directive(level)` falls back to level 3 (mid-intensity) on out-of-range input, not level 0 or 5 — a config typo mustn't silently lobotomise the assistant or make it hostile.
  - `_build_system_blocks` now accepts optional `calibration_str` + `pushback_level` kwargs and emits the blocks in canonical cache order: system → vault context → calibration → pushback. The `calibration_str` plumbing is wired but unused until commit 2.
  - `run_turn` now accepts `calibration_str` and `pushback_level` kwargs and threads them through. Both default to `None` so pre-wk3 call sites stay byte-identical.
- `src/alfred/telegram/bot.py`: `_open_session_with_stash` now accepts `pushback_level` and stashes it as `_pushback_level` on the active dict. `_open_routed_session` pulls the level from `session_types.defaults_for(decision.session_type).pushback_level`. `handle_message` re-reads the active dict on each turn and threads the stashed level into `run_turn`.
- `src/alfred/telegram/session.py`: `close_session` / `_build_session_frontmatter` accept `pushback_level` and emit it as `telegram.pushback_level` on the session record. `check_timeouts` / `resolve_on_startup` pass the stashed value through so timeout-closed sessions keep the annotation.
- Tests:
  - `tests/telegram/test_session_types.py::test_pushback_level_defaults_by_type` — per-type values + range check + fallback.
  - `tests/telegram/test_pushback.py` (new, 8 tests): directive rendering per level, unknown-level fallback, four-block cache ordering (none-pushback, all-four), `run_turn` threading, session-open stashing, routed-open stashing.
  - `tests/telegram/test_session_frontmatter.py::test_telegram_pushback_level_in_record` — frontmatter field presence + `None` behaviour for wk2 records.

48 tests pass (38 wk2 baseline + 10 new).

## Outcome

Pushback is now a first-class session property: router decides session type → type defines level → level renders directive → directive lands as a cache-control system block → session record preserves the value for post-hoc correlation. Commit 2 will slot the calibration block between vault context and pushback without disturbing this wiring.

## Alfred Learnings

- **Pattern validated**: when a prompt has multiple stable prefixes, the cache block ordering must be locked by test. Added `test_build_system_blocks_cache_order_with_all_four_blocks` before commit 2 lands so calibration can't accidentally move ahead of pushback and invalidate the session-stable suffix.
- **Pattern validated**: out-of-range fallbacks should pick a neutral middle, not an extreme. A config typo for `pushback_level: 99` falling through to level 5 would be a user-visible regression the test wouldn't catch; falling through to level 3 degrades gracefully.
- **Anti-pattern avoided**: I nearly bundled the `run_turn` session.model bug fix into commit 1 (the change is right next to the new `pushback_level` kwarg). Reverted — that fix belongs in commit 5 where the plan spec'd it, so the bug's fingerprint stays grep-able to a single commit.
- **Gotcha**: `check_timeouts` and `resolve_on_startup` both close sessions without a config handle, so every new `close_session` kwarg has to be plumbed through both of those call sites — three paths total. Missed any of them and timeout-closed sessions would lose the new field silently.
