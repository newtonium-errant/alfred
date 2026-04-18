---
type: session
status: completed
name: Voice wk3 — /opus /sonnet commands and run_turn session.model fix
created: 2026-04-18
description: Commit 5 of 8 in Voice Stage 2a-wk3 — add explicit model-override commands and fix a wk2 bug where every run_turn silently ignored session.model.
intent: Give Andrew a one-token way to flip the active session between Opus and Sonnet, and close a silent correctness hole where the router's model choice never actually made it to the API call.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Calibration IO 2026-04-18]]'
tags:
- voice
- talker
- wk3
- commands
- bug-fix
---

# Voice wk3 — /opus /sonnet commands + run_turn session.model bug fix

## Intent

Commit 5 delivers two closely-coupled changes:

1. **Explicit model commands.** `/opus` and `/sonnet` flip the active session's model on the active dict. The next `run_turn` reads the new model and routes the API call accordingly. Commit 6 will piggy-back on this path for the implicit-escalation prompt.

2. **Regression fix on `run_turn`.** Wk2 had `run_turn` calling `client.messages.create(model=config.anthropic.model, ...)`. That meant every router-chosen model (article → Opus, anything else → Sonnet) got overridden by the config default on every turn after the first — so the router's model decision was effectively decorative. Without this fix, commits 6-8 would all be fighting the same hole.

The two changes have to land together: the commands are meaningless unless `run_turn` reads `session.model`, and the regression fix is hard to exercise cleanly without a UI switch to test against.

## Work Completed

- `src/alfred/telegram/bot.py`:
  - `_OPUS_MODEL` and `_SONNET_MODEL` module constants, centralising the model IDs so the commands, log events, and commit 8's scaffold all share one source of truth. Flipping Opus → `claude-opus-4-5` at the alias-404 fallback point is a single-line edit.
  - `_switch_model(state_mgr, chat_id, target, label) -> str | None` helper: returns a reply string, `None` when there's no active session to flip.
  - `on_opus` / `on_sonnet` `CommandHandler`s registered in `build_app`. Idempotent — flipping to the current model replies but doesn't re-log. Successful flips emit `talker.model.escalated` with `from` / `to` / `turn_index` / `trigger="explicit"` (same schema commit 6 will extend for implicit escalation).
- `src/alfred/telegram/conversation.py`:
  - **Bug fix**: `run_turn` now passes `model=session.model` to `messages.create`. The docstring calls out the regression explicitly so future readers don't reintroduce the bug.
- Tests (`tests/telegram/test_model_switch.py`, 7 new):
  - `_switch_model`: happy path, idempotent case, no-active-session case, persistence across a fresh `StateManager` load.
  - `run_turn`: **regression test** asserting `config != session.model` → API call uses `session.model`. Second test: two turns with a flip in between — per-turn model follows the session, not the config.
  - `build_app` registers `/opus` and `/sonnet` CommandHandlers.

78 tests pass (71 after commit 4 + 7 new).

## Outcome

Andrew can now type `/opus` mid-session to switch to Opus for harder thinking or `/sonnet` to drop back to Sonnet for cheaper follow-up turns. The router's model choice now actually takes effect on every turn. Commit 6 will use the same mechanism for the implicit escalation offer.

## Alfred Learnings

- **Pre-existing bug surfaced**: the `run_turn` session.model hole was introduced in wk1 (first cut of `run_turn`), masked by wk2 because every wk1 session ran on the config default anyway (no router yet). Wk2 opened the door for the bug to matter (router picks models) but didn't trip the test because the existing wk2 tests used the config default as the session model too. Adding a test that *deliberately* creates `config.model != session.model` would have caught this at wk2 landing time. Lesson: regression tests should construct the divergent state they're protecting against, not a case where both sides happen to agree.
- **Pattern validated**: centralising model IDs as module constants (`_OPUS_MODEL`, `_SONNET_MODEL`) rather than repeating string literals in five places. Commit 8 (model-selection calibration scaffold) can import the same constants rather than re-declaring them, and if the Opus alias 404s in production the fallback is a single-line change.
- **Anti-pattern noted**: I could have used a single `_switch_model(target=...)` with the target as argument of a unified handler; I kept the two `on_opus` / `on_sonnet` handlers explicit because PTB's `CommandHandler` binds one handler per command string anyway. Trying to DRY-up would have added a function-factory layer for no real saving.
- **Gotcha**: idempotent-flip detection has to read the active dict *before* calling `_switch_model` (not after), because `_switch_model` mutates it. Caught this while writing the test that asserts no log is emitted on a no-op flip.
