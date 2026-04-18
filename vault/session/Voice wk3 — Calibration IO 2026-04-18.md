---
type: session
status: completed
name: Voice wk3 — Calibration IO
created: 2026-04-18
description: Commit 2 of 8 in Voice Stage 2a-wk3 — add the calibration block read path and inject it as the third cache-control system text block at session open.
intent: Land the snapshot/inject half of the calibration mechanism before the migration (commit 3) and close-time writer (commit 7), so the read end of the contract is in place before either side writes to it.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Pushback dial 2026-04-18]]'
tags:
- voice
- talker
- wk3
- calibration
---

# Voice wk3 — Calibration IO

## Intent

The calibration block is Alfred's running self-note about the user — communication style, workflow preferences, current priorities, open questions. Writing it into the system prompt on every turn would defeat prompt caching and also race with the close-time writer (commit 7), so the pattern is: read once at session open, stash on the active dict, thread into every turn's `run_turn` from there. Commit 2 lands the read half.

## Work Completed

- **New module** `src/alfred/telegram/calibration.py`:
  - `CALIBRATION_MARKER_START` / `CALIBRATION_MARKER_END` constants — centralised so commit 7's writer can't drift into a different spelling and silently duplicate blocks.
  - `CALIBRATION_RE` precompiled regex (DOTALL, non-greedy).
  - `read_calibration(vault_path, user_rel_path) -> str | None` — normalises `.md` suffix, returns `None` for missing file / missing block / empty block / read error. Never raises — a calibration read must not crash the bot on startup.
- `src/alfred/telegram/bot.py`:
  - `_open_routed_session` now calls `calibration.read_calibration(...)` after the router decision, stashes the result as `_calibration_snapshot` on the active dict.
  - `handle_message` re-reads the snapshot from the active dict on every turn and threads it into `run_turn(calibration_str=...)`. Parallel structure to the pushback dial — same read pattern, same stash key naming.
- Tests (`tests/telegram/test_calibration_io.py`, 11 new tests):
  - `read_calibration` happy path, suffix normalisation, missing file, empty rel_path, no block, empty block, multi-line block.
  - `_build_system_blocks` calibration position (between vault and pushback), None-skips.
  - `_open_routed_session` stashes the snapshot (both populated and `None` cases).
  - `run_turn` threads the calibration string into the third system block of the API call.

60 tests pass (48 after commit 1 + 12 new — 11 in the new file plus an extra `_build_system_blocks` check that overlaps the pushback ones without duplicating).

## Outcome

Calibration is now plumbed end-to-end on the read side. Commit 3 will populate the block on the actual `vault/person/Andrew Newton.md` record, commit 4 will teach the distiller to strip the block before extraction, and commit 7 will close the loop with the close-time writer.

## Alfred Learnings

- **Pattern validated**: co-locating the read + write ends of a marker-fenced block in one module is worth the later coupling. Alternative (readers in `calibration_read.py`, writers in `calibration_write.py`) would have given me two places to accidentally drift the marker string, which is the single load-bearing constant in the whole feature.
- **Pattern validated**: every new stashed-on-active-dict field (commit 1's `_pushback_level`, commit 2's `_calibration_snapshot`) gets a "re-read from active, thread into run_turn" pair in `handle_message`. Both commits used the same three-line shape, which means future commits' additions are mechanical.
- **Anti-pattern noted**: the easiest bug here would be to call `read_calibration` on every turn rather than once per session. Resisted by putting the call in `_open_routed_session` (which only runs on session open) and making `handle_message` read from the active dict. If the plan had said "thread the path into `run_turn` and read there", the per-turn cost would have been an IO hit that defeats the cache's entire premise. Worth calling out so commit 7's writer doesn't also slip into a per-turn shape.
