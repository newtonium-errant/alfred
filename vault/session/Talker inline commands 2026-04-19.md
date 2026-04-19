---
type: session
status: completed
name: "Talker inline commands"
intent: "Recognise /end, /opus, /sonnet, /no_auto_escalate, /status, /start when embedded in prose"
project: "[[project/Alfred]]"
created: 2026-04-19
tags: [talker, telegram, ux, commands]
---

# Talker inline commands

Ships the deferred fix from `project_talker_inline_commands.md`. Telegram's
`CommandHandler` only fires when the message text starts with `/command`;
real-world usage is "Good. /end" or "please /opus" — those land on the
text MessageHandler and get sent to Claude as prose, so the session never
closes and the model never flips. This commit adds a pre-check in
`handle_message` that recognises the command-at-end-of-line and
command-at-start-of-message shapes, dispatches to the same `on_*`
handlers as the CommandHandler path, and returns without wasting tokens
on Claude.

## Shape

- `_INLINE_COMMANDS`: set of command names eligible for inline detection.
  Covers all six CommandHandler-registered commands (`end`, `opus`,
  `sonnet`, `no_auto_escalate`, `status`, `start`).
- `_INLINE_CMD_RE`: `(?:^|\s)/(\w+)\s*$|^/(\w+)\b`. End-of-line variant
  requires whitespace or start-of-string before the slash (so `foo/end`
  doesn't trigger), and allows trailing whitespace. Start-of-message
  variant is belt + braces — PTB filters pure-command messages out of
  the text MessageHandler before they reach `handle_message`, so this
  branch mostly exists for test completeness.
- `_detect_inline_command(text)`: pure function, returns lower-cased
  command name or None. Only inspects `text.splitlines()[0]` — command
  detection is line-local so a second-line `/end` in a multi-paragraph
  message doesn't silently close the session.
- `_dispatch_inline_command(cmd, update, ctx)`: looks up the `on_*`
  handler in `_INLINE_HANDLERS` and calls it with the same `(update,
  ctx)` the CommandHandler path uses. No logic duplication — both paths
  share handlers byte-for-byte.
- `_INLINE_HANDLERS`: module-bottom dict populated after the `on_*`
  handlers are defined. Must stay in sync with `_INLINE_COMMANDS`.

`handle_message` runs the pre-check BEFORE acquiring the per-chat lock
and BEFORE session open/resume. Command intent is treated as the full
user intent — the prose around the command is not forwarded to Claude.

## Tests

`tests/telegram/test_inline_commands.py` — 19 tests:

Detector (pure function):
- End-of-line form.
- Trailing whitespace tolerated.
- Start-of-message form with prose after (`/opus please`).
- Case-insensitive (`/END`, `/End`).
- Mid-message slash token doesn't fire (`maybe I'll /end later`).
- Unknown commands ignored.
- Mid-word slash (`foo/end`) doesn't fire.
- Multi-line: only first line inspected.
- Multi-line: first-line command still fires even with prose below.
- All six supported commands recognised.
- Empty input returns None.
- Pure `/end` still detects (backwards compat).

Dispatch (MagicMock Update/Context):
- `/end` inline closes session, replies `session closed.`.
- `/opus` inline flips model to Opus.
- `/sonnet` inline flips model to Sonnet.
- Pure `/end` still works through the inline path.
- `maybe I'll /end later` stays as prose (run_turn stub called, session stays active).
- `/nonsense` treated as prose.
- `/END` and `/End` both fire (case insensitive).

Baseline before Q6: 160. After Q6: 168. After this commit: 187.

## Deviations from spec

- Added `/start` to the inline-command allowlist even though the spec
  only listed `/end`, `/opus`, `/sonnet`, `/no_auto_escalate`, `/status`.
  Rationale: `on_start` is registered on the CommandHandler too, and
  leaving it out would make the inline-command set subtly different
  from the CommandHandler set, which is a maintenance trap. The cost
  is zero (inline `/start` is a rare user action; the handler is a
  greeting reply).
- Dispatch reuses the existing `on_*` handlers directly rather than
  factoring out shared helpers. The spec mentioned either approach was
  acceptable ("reuse the existing logic — don't duplicate it"); the
  direct-dispatch path is simpler and makes every code path through
  these handlers identical regardless of entry mode.
- Test for `test_pure_command_still_works` exercises `/end` through the
  inline pre-check directly, because the PTB CommandHandler vs
  MessageHandler routing happens in PTB machinery that's hard to test
  without a live bot. The comment on that test notes the limitation.

## Memory update

Updated `project_talker_inline_commands.md` in memory (outside repo):
- `**Priority**: medium.` → `**Status**: SHIPPED 2026-04-19 (<hash>).`
- Removed "Fix deferred." from description.
- Left the detailed shape/test notes intact as a reference.

## Alfred Learnings

- **Pattern validated — PTB command routing is shape-specific.** Users
  expect slash-commands to work in any position in a message. PTB's
  strict start-of-string rule is a footgun in practice. Other Telegram
  bots (including several in the ecosystem) layer a regex pre-check on
  top of CommandHandler exactly like we just did. Worth calling out in
  any future talker-family docs.
- **Pattern validated — prose-around-command is waste.** The user's
  intent when they type "Good. /end" is the command, not a conversation.
  Running the prose through Claude would cost tokens, produce a reply
  that conflicts with the command's reply, and violate least
  astonishment. The pre-check treats the whole message as intent=command
  with no prose forwarding.
- **Gotcha — test `session_id` derivation.** `session.close_session`
  derives the vault record's short-id from `session_id.split("-")[0]`.
  Seeding two sessions in the same test with session_ids starting with
  the same prefix (`inline-test-7`, `inline-test-8`) generates colliding
  filenames. Per-chat prefix (`chat7-inline-test`) avoids it. Noted in
  case other close-path tests ever bump into this.
- **Gotcha — MagicMock'ing `conversation.run_turn`.** When stubbing
  `run_turn` to prevent real LLM calls in tests that test the pre-check's
  failure mode (prose passthrough), patch the module-level import
  (`conversation.run_turn = AsyncMock(...)`) rather than trying to
  monkey-patch the symbol inside `bot.handle_message`. `bot.handle_message`
  does `from . import conversation; conversation.run_turn(...)`, so the
  module-level rebinding is what gets picked up.
