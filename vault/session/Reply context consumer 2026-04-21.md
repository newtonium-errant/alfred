---
type: session
created: '2026-04-21'
name: Reply context consumer 2026-04-21
description: Consume Telegram's Message.reply_to_message so Salem understands reply-to-bot follow-ups
intent: Wire reply-to-bot context into the talker pipeline before the router and Anthropic turn run
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
tags:
  - talker
  - telegram
  - router
  - ux
status: completed
---

# Reply context consumer 2026-04-21

## Intent

When a Telegram user long-presses a bot message and hits "Reply," the
Bot API attaches the full parent message via `Message.reply_to_message`.
The talker was not consuming this: every turn was treated as standalone
input regardless of reply context. "book it" / "done" / "explain the
second failure" had no grounding in what the user was actually replying to.

## Work Completed

### 1. Reply-context helper — `bot.py::_build_reply_context_prefix`

Pure helper that takes a PTB `Message` (or None) and returns either
`None` (no prefix — photo-only reply, whitespace-only, missing object)
or a `[You are replying to ...]\n\n` prefix string. Key behaviours:

- **Attribution**: bot-authored parent → `"Salem's earlier message"`,
  non-bot parent → `"your earlier message"`. Multi-user chats are
  future work.
- **Truncation**: parent body capped at 500 chars with a trailing
  `... (truncated)` suffix so the prefix stays compact.
- **Timestamp**: `Message.date` normalised to UTC ISO with second
  precision (`2026-04-21T14:30:00+00:00`).
- **Caption fallback**: when the parent has no `text` (photo with a
  caption), the caption is quoted instead.
- **MagicMock-safe**: `isinstance(raw_text, str)` guard means existing
  tests that MagicMock the whole Update don't accidentally hit the
  reply path. Real PTB `Message.text` is `str | None` so the type
  check excludes nothing legitimate.

### 2. `handle_message` wiring

Early in the flow (before inline-command detection and the lock), build
the reply prefix and compute `effective_text = prefix + text`. Thread
`effective_text` into:

- `_open_routed_session` (so the router sees the prefix as the opening cue)
- `conversation.run_turn` (so Claude sees the prefix as the turn text)
- `_dispatch_peer_route` (so a peer-forward carries the same context)

Inline-command detection runs against the ORIGINAL text, not the
prefixed version — a `/end` reply still closes the session even though
the prefix would push the slash off the first line otherwise.

### 3. Router hint — `has_reply_context`

`classify_opening_cue(..., has_reply_context: bool = False)` threads the
signal through to the prompt as a templated `has_reply_context={true|false}`
token. The prompt text tells the classifier to prefer `continues_from` /
`note` when the flag is true (reply-to-bot is a strong "follow-up"
signal), while keeping `peer_route` valid and honouring explicit opening
cues like `capture:`.

Active-session fast-path: when an active session exists, the router is
never invoked (existing behaviour). A reply-to-bot in an open session
just feeds the prefix into `run_turn` directly. The hint only matters
when the router IS called (no active session or rehydrate failure).

### 4. SKILL addendum

One-paragraph mention in `vault-talker/SKILL.md` under "Session
boundaries": "Treat the quoted text as context for understanding the
follow-up … the prefix is machine-generated; don't echo it back."

### 5. Tests — `tests/telegram/test_reply_context.py`

19 new tests:

- 11 pure tests on `_build_reply_context_prefix` — short/long parents,
  None, bot vs user attribution, photo-only, caption fallback,
  embedded quotes, UTC normalisation, whitespace-only, multi-line
  reply preservation.
- 5 integration tests on `handle_message` — prefix reaches
  `run_turn.user_message`, no-reply produces no prefix, active-session
  skips router on reply, no-active-session reply passes
  `has_reply_context=True` to the router, non-reply doesn't set the hint.
- 3 router-signature tests — kwarg accepted, prompt contains
  `has_reply_context=true` / `=false` as expected.

## Alfred Learnings

### Patterns validated

- **Telegram's Bot API already provides reply context via
  `reply_to_message`.** We just weren't consuming it. python-telegram-bot
  exposes it as `update.message.reply_to_message` with the full parent
  body, sender, timestamp. No extra API roundtrip needed.
- **Reply-to-bot-message is a near-perfect "continuation" signal for the
  router.** If the user went to the trouble of long-pressing a bot
  message to reply to it, they're almost certainly continuing the
  thread of thought it represents — not opening a fresh session. The
  `has_reply_context` hint is a cheap, reliable way to tip the default.
- **Pattern of "annotating user text with machine-generated context
  prefix" is reusable.** The same shape (`[You are ...]\n\n<user text>`)
  will work for peer-routing (when Salem forwards to KAL-LE, include
  the attribution), for calendar-surfaced events ("you asked me to
  surface this: …"), and for any other flow where upstream context
  needs to reach the model without polluting the transcript with
  assistant turns.

### Anti-patterns confirmed

- **Test pollution via MagicMock auto-attribute creation.** When a
  production code path reads a new attribute that existing tests
  don't explicitly stub, MagicMock happily returns a magic-mocked
  value. The test-introduced `update.message.reply_to_message` was
  auto-created as a MagicMock (truthy, non-None), which made my first
  run of the helper produce MagicMock-quoted prefixes for every
  pre-existing test. Fix: tighten the helper with
  `isinstance(..., str)` guards. This matches the real PTB contract
  and fails-safe on test harnesses. Pattern: **when consuming new
  fields off an auto-mocked object, type-check the fields, don't just
  truthiness-check them.**

### Missing knowledge

- **Pre-existing flaky test**: `tests/test_transport_client.py::test_failure_log_has_subprocess_contract_fields`
  passes in isolation but fails when the full suite runs — appears to
  be structlog / httpx logger state leakage from other tests.
  Reproduces on HEAD without any of my changes (1022 passed, 1 failed
  before; 1041 passed, 1 failed after). Not caused by this commit; logging
  for a future triage pass.

## Outcome

- 20 new tests (1022 → 1041 passing, the pre-existing 1 failure
  unchanged).
- Reply to any bot message now triggers a machine-generated context
  prefix that flows through inline-command detection (against original
  text), router classification (with `has_reply_context=True`),
  Anthropic turn (prefix visible to the model), and peer-routing
  (prefix forwarded to the peer).
- Non-reply messages: zero behaviour change. Helper returns None, the
  same `text` variable flows through as before.
</content>
</invoke>