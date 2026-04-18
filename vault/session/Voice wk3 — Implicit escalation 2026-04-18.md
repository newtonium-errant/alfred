---
type: session
status: completed
name: Voice wk3 — Implicit escalation detection and /no_auto_escalate
created: 2026-04-18
description: Commit 6 of 8 in Voice Stage 2a-wk3 — add three-signal detection for "the user wants more thinking", append a /opus offer to the assistant reply when signals fire, and provide /no_auto_escalate to disable the offer for the session.
intent: Let Alfred notice when a Sonnet-session turn deserves Opus without requiring the user to remember which model they're on, while keeping the user fully in control of the switch.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Opus Sonnet commands and run_turn fix 2026-04-18]]'
tags:
- voice
- talker
- wk3
- escalation
---

# Voice wk3 — Implicit escalation detection

## Intent

Commit 5 gave Andrew the explicit switches. Commit 6 adds the implicit half: detect when a turn *looks like* Andrew wants more thinking (long introspective question, rephrase of a dissatisfying prior turn, literal "think harder"), and offer an Opus switch inline with the assistant reply. Cheap heuristics only — a false positive costs one extra line of text, and the user is always the one who actually presses `/opus` (or `/no_auto_escalate` to stop offering).

## Work Completed

- `src/alfred/telegram/conversation.py`:
  - `_ESCALATION_KEYWORDS` — four trigger phrases (`think harder`, `more depth`, `go deeper`, `dig into this`), case-insensitive substring match.
  - `_LONG_USER_MIN_CHARS` = 400, `_SHORT_ASSISTANT_MAX_CHARS` = 150 — the long-user/short-assistant signal.
  - `_REPHRASE_SIM_THRESHOLD` = 0.55 Jaccard over whitespace tokens, checked against the last three user turns.
  - `_ESCALATION_COOLDOWN_TURNS` = 5 — debounce so we don't append the offer on every qualifying turn after the first.
  - `_detect_escalation_signal(session, user_message, assistant_text) -> str | None` — returns the name of the first-firing signal so logs can track which one was most useful.
  - `_should_offer_escalation(active, session)` — cooldown / disable-flag / already-on-Opus guard.
  - Detection runs inside `run_turn` after assistant text extraction, before return. If fires + allowed, the `_ESCALATION_SUFFIX` is appended to the text, `_escalation_offered_at_turn` is stashed on the active dict, and `talker.model.escalate_offered` logs with the signal name.
- `src/alfred/telegram/bot.py`:
  - `on_no_auto_escalate` CommandHandler sets `_auto_escalate_disabled = True` on the active dict, logs `talker.model.auto_escalate_disabled`, replies tersely.
  - Registered as `/no_auto_escalate` (not `/no-auto-escalate`) — PTB's `CommandHandler` regex is `^[\da-z_]{1,32}$`, hyphens aren't legal.
  - `on_opus` now distinguishes accepted-offer from un-prompted switch. If `_escalation_offered_at_turn` is within the cooldown window at the time `/opus` arrives, it logs `talker.model.escalate_accepted` (with `offered_at_turn`) instead of `escalated`. Same `from`/`to`/`session_id` fields so aggregation works.
- Tests (`tests/telegram/test_implicit_escalation.py`, 17 new): per-signal positive + negative cases, Jaccard spot-check, cooldown/disable/on-Opus guards, end-to-end `run_turn` suffix behaviour, `/no_auto_escalate` registration.

95 tests pass (78 after commit 5 + 17 new).

## Outcome

Andrew's next Sonnet session that drifts into reflective territory will now offer an Opus switch inline. He can accept (one character: `/o` + tab-completion on most clients, or just type `/opus`), ignore (the offer decays after a 5-turn cooldown), or disable via `/no_auto_escalate` if he doesn't want any more offers this session.

## Alfred Learnings

- **Deviation from plan**: the plan called for `/no-auto-escalate` with hyphens. PTB's `CommandHandler` only accepts `[a-z0-9_]` — hyphens crash at handler-registration time with `ValueError: Command ... is not a valid bot command`. Implemented as `/no_auto_escalate`. This is a Telegram convention (their Bot API command spec is the same) so the constraint isn't PTB-specific; future slash commands with compound names must use underscores.
- **Pattern validated**: detection is a pure function that takes the session + message and returns an optional signal name, not a `bool`. Returning the signal name lets the logs answer "which signal fires most" without adding a separate telemetry surface. Cheap to pipe through.
- **Pattern validated**: separating "signal fires" from "should offer" (`_detect_escalation_signal` vs `_should_offer_escalation`) means we can log signal detection even when the offer is suppressed (by cooldown or disable flag). That log — rate of silent-suppressions vs offers actually made — is the data Andrew will need to decide whether to raise or lower the thresholds in wk4.
- **Gotcha**: the rephrase signal compares against prior user turns, but the current turn hasn't been appended to `session.transcript` at signal-evaluation time (`run_turn` calls `append_turn` before the API call, not after assistant response). Had to double-check which turns are in the transcript window; the tests pin the boundary.
- **Anti-pattern noted**: I almost put the detection call AFTER `append_turn(state, session, "assistant", ...)`, but then the just-appended assistant text would be the "last assistant" in a future rephrase check — skewing the signal. Ran it on the local `text` variable instead. The tests don't catch this (they build the session turn-by-turn), so the comment is load-bearing; future me must not move the detection call.
