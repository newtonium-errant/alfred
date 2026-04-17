---
type: session
status: completed
name: Talker Session and Tool Bridge
created: 2026-04-17
description: Commit 3 of 5 on the voice stage 2a-wk1 build. Implements the
  talker's session lifecycle module and the Anthropic tool-use loop that
  bridges the model to the vault via scope-enforced operations. Purely
  logic-layer work — no Telegram, no CLI wiring (those are commits 4 and 5).
intent: Land the two modules that make the talker testable without Telegram
  or a live API, and flag decisions that deviate from or tighten the spec
  for the next session-lead review.
participants:
  - '[[person/Andrew Newton]]'
project:
  - '[[project/Alfred]]'
related:
  - '[[session/Voice Chat and Calibration Design 2026-04-15]]'
tags:
  - voice
  - telegram
  - build
---

# Talker Session and Tool Bridge — 2026-04-17

## Summary

Commit 3 of the Voice Stage 2a-wk1 build: the session lifecycle module and
the Anthropic tool-use loop. Both modules are pure Python logic — commit 4
will wire them to Telegram, commit 5 will wire them to the CLI/orchestrator.

Two files added, nothing modified:
- `src/alfred/telegram/session.py` — `Session` dataclass, `open_session`,
  `append_turn`, `append_vault_op`, `resolve_on_startup`, `check_timeouts`,
  `close_session`, plus pure frontmatter/body builders
- `src/alfred/telegram/conversation.py` — `VAULT_TOOLS` schemas,
  `_execute_tool` vault bridge, `run_turn` tool-use loop

Anthropic SDK (`anthropic==0.96.0`) installed into `.venv` — not yet added
to `pyproject.toml`. Commit 5 will wire the `[voice]` extra.

All 4 smoke checks passed end-to-end against a temp vault dir:
1. imports clean, anthropic importable
2. `open_session` → `append_turn` × 3 → `close_session` writes a
   `session/Voice Session — …` record with the expected frontmatter
   (`type: session`, `status: completed`, `participants`, `telegram`
   subsection with `chat_id`, `message_count`, etc.) and transcript body
3. `_execute_tool` with `vault_read` bridges into `ops.vault_read` and
   returns the expected JSON; a missing-file error surfaces as
   `{"error": "..."}` JSON rather than raising
4. `run_turn` with a mocked Anthropic client returns the expected text,
   persists both the user and assistant turns, passes exactly 4 tools and
   2 cacheable system blocks (each with `cache_control: ephemeral`) to
   `messages.create`

## Plan deviations and non-obvious choices

### `_execute_tool` signature — extra `vault_path` parameter

The spec had `_execute_tool(tool_name, tool_input, vault_path, state,
session)`. That's what I implemented — vault path is a string (not a
`Path`) because the top-level `run_turn` reads it from `config.vault.path`
and doesn't pre-cast. If that bothers a reviewer, the fix is a one-liner.

### `run_turn` signature — matches spec exactly

Matches spec. Imports `anthropic` at module level as required, so the
module import does fail if the SDK is missing — which is exactly what
commit 5's `[voice]` extra will cover.

### Safety cap → explanatory assistant turn

Spec said "log + break + return an error string." I do all three, and
also append the error as a synthetic assistant turn to the transcript so
the session record (if the conversation then times out and closes) makes
it visible that the talker bailed out. Judgment call — more traceable than
returning bare text.

### `resolve_on_startup` vault-path dependency

The `close_session` signature requires a `vault_path_root`, but at daemon
boot we don't have the session-specific vault path stashed anywhere (each
active session dict in state is just transcript + metadata). I added a
convention: the daemon (commit 4) should stash `_vault_path_root`,
`_user_vault_path`, and `_stt_model_used` onto each active-session dict
when it creates the session. `resolve_on_startup` reads them if present,
skips (with an info log) if absent, so an old state file from before this
convention lands cleanly without exploding.

**This is a forward contract for commit 4** — the bot handler must stash
those three keys onto the session dict at open time.

### `_json_default` for vault_read results

First smoke run tripped on `Object of type date is not JSON serializable`
because frontmatter contains a `created` `date`. Added a `_json_default`
hook that ISO-formats `date`/`datetime` and flattens sets. Applies only to
tool-result serialization, not to state-file writes (state is filtered to
JSON-safe types upstream).

### `outputs` populated from `vault_ops`

Per the explicit correction in the spec (and confirmed against builder-
review feedback), `outputs` in the session frontmatter is the list of
`[[path]]` wikilinks from `session.vault_ops`, not always empty. The
`telegram.vault_operations` subsection mirrors the same data in full-record
form (op + path + ts) for when the `outputs` wikilinks lose the context.

### Unique record name

Two sessions closing within the same minute would otherwise collide
(`Voice Session — 2026-04-17 2128` twice). Appended the session UUID's
short form (`cd2bde19`) to the record name. Keeps them distinct and
still skimmable.

### Tool enum narrower than scope

`VAULT_TOOLS["vault_create"]["type"]` enum is `{task, note, decision,
event}`. The `talker` scope in `vault/scope.py` allows a wider set
(`TALKER_CREATE_TYPES` includes `session, conversation, assumption,
synthesis` too). This is intentional — wk1 prompt keeps the LLM surface
narrow, but `scope.check_scope` is still the gate and can widen cleanly
as the prompt matures in wk2/wk3 without touching the schema.

## Alfred Learnings

### Patterns Validated

- **Prompt-caching pattern: two breakpoints (system, context).** The
  claude-api skill's recommended agent pattern — frozen SKILL-style prompt
  first, semi-static vault context second, both with
  `cache_control: ephemeral` — maps cleanly onto the talker. Each session
  pays one cache write on the first turn and reads on turns 2+. Worth
  remembering: when a tool has both a frozen prompt and a per-session
  context snapshot, two breakpoints is the right shape.
- **Error-as-tool-output for agent-recoverable failures.** Returning
  `{"error": "..."}` JSON from `_execute_tool` instead of raising lets
  Claude observe the error inside its tool-use loop and recover in the
  same turn (pick a different record, ask a clarifying question, try
  `vault_search` instead of `vault_read`). Much more robust than surfacing
  the error to the user and starting over.

### New Gotchas

- **`frontmatter.load` returns `date` objects, not ISO strings.** Anything
  that JSON-dumps a `vault_read` result without a custom `default=` hook
  will blow up on `created: 2026-04-01` and similar fields. Flagging so
  future vault-bridging code across tools either filters frontmatter or
  uses a JSON-default hook. Candidate for `vault/ops.py`: a `json_safe`
  helper that returns already-stringified values from `vault_read`,
  removing the burden from every caller.

### Corrections

- **Session frontmatter `outputs` is not always empty.** The design doc
  section 4 originally showed it empty; a builder review flagged that the
  field should be populated from the session's vault ops. Confirmed in
  this commit. The voice-design session note itself (2026-04-15) may need
  a small edit to reflect the corrected schema.

## Next

Commit 4: `src/alfred/telegram/bot.py` (python-telegram-bot handlers, voice
download), `daemon.py` (watcher + periodic `check_timeouts`), and
`transcribe.py` (STT). Commit 4 must also stash `_vault_path_root`,
`_user_vault_path`, `_stt_model_used` onto active-session dicts at open
time so `resolve_on_startup` can recover orphaned sessions after a
daemon restart.

Commit 5: `cmd_talker` in `src/alfred/cli.py`, orchestrator registration in
`TOOL_RUNNERS`, `[voice]` extra in `pyproject.toml` pulling in
`anthropic`, `python-telegram-bot`, and the chosen STT client.
