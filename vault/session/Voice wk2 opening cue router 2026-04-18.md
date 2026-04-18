---
type: session
status: completed
name: Voice wk2 opening cue router
created: 2026-04-18
description: Commit 3 of 5 — introduce the opening-cue router that classifies the first message of a session and optionally flags continuation of a prior session.
intent: Land the router as a standalone module so commit 4 can wire it into bot.handle_message with a clean import and a pre-tested classification surface.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk2 session types module 2026-04-18]]'
- '[[session/Voice wk2 session record fields 2026-04-18]]'
tags:
- voice
- talker
- wk2
---

# Voice wk2 — opening cue router

## What changed

New `src/alfred/telegram/router.py`:

- `RouterDecision(session_type, model, continues_from, reasoning)` frozen dataclass — the router's output surface.
- `classify_opening_cue(client, first_message, recent_sessions)` — async entry point. Makes one `messages.create` call against `ROUTER_MODEL` (Sonnet, pinned).
- Inline `_ROUTER_PROMPT` (plan open question #6). Short, explicit, JSON-only response format with the 5 type buckets and continuation instructions.
- JSON parsing with graceful fallback: on API error, parse failure, unknown type, or hallucinated continuation, the router returns a safe `note` / Sonnet / no-continuation decision. User-visible behaviour stays wk1-equivalent whenever the router is unreliable.
- Hallucination guard: `continues_from` is only honoured when the returned path is present in the `recent_sessions` list. The router can invent plausible paths; state is the source of truth.
- Article-with-no-prior stays as `article` / Opus with `continues_from = None` (plan open question #8 — intent trumps absence).

## Tests

`tests/telegram/test_router.py` (4 tests):
1. Clear task cue → `task` / Sonnet / no continuation, and uses `ROUTER_MODEL` for the router call.
2. Continuation validation — known path honoured, hallucinated path dropped, type stays `article` with `continues_from = None`.
3. Non-JSON output → parse fallback to `note`.
4. Raised exception from the fake client → API fallback to `note`.

All 4 use the `FakeAnthropicClient` from `conftest.py` — no network calls.

pytest: 28/28 passing.

## Contracts

- Router emits JSON with fields `session_type` / `continues_from` / `reasoning`. Any drift here breaks commit 4's caller.
- `recent_sessions` is the closed_sessions list from `StateManager` — shape is already defined by commit 2 (`record_path`, `session_type`, `started_at`, etc.).

## Alfred Learnings

- **Pattern validated**: split "extract text from response", "parse JSON", "build decision" into three tiny functions. Each has one failure mode, each is individually testable, and the main async entry point reads top-to-bottom as prose.
- **Anti-pattern avoided**: no regex-extract fallback for JSON-with-prose. If the model starts emitting prose, we want the log to say "parse_failed" so we see it, not silent partial success. Regex extraction would hide that drift.
- **Pattern validated**: temperature=0.2 on classification prompts. Zero was too sticky in spot-checks (picked `note` for everything); 0.2 is enough to escape that local minimum without introducing noise. Worth keeping this number in mind for any future classification LLM call.
