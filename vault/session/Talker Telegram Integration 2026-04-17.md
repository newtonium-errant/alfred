---
type: session
status: completed
name: Talker Telegram Integration
created: 2026-04-17
description: Commit 4 of 5 on the voice stage 2a-wk1 build. Adds the networked
  layer — Telegram bot, Groq Whisper STT, and the talker daemon — on top of
  the session lifecycle and tool-use loop from commit 3. No CLI or orchestrator
  wiring yet (that's commit 5).
intent: Land the three modules that make the talker a runnable daemon end-to-end,
  verify the forward contract from commit 3 is honoured, and document the PTB
  v21 lifecycle pattern for the next builder session.
participants:
  - '[[person/Andrew Newton]]'
project:
  - '[[project/Alfred]]'
related:
  - '[[session/Voice Chat and Calibration Design 2026-04-15]]'
  - '[[session/Talker Session and Tool Bridge 2026-04-17]]'
tags:
  - voice
  - telegram
  - build
---

# Talker Telegram Integration — 2026-04-17

## Summary

Commit 4 of the Voice Stage 2a-wk1 build: the networked layer. Three new files,
no existing files modified:

- `src/alfred/telegram/transcribe.py` — Groq Whisper STT via the OpenAI-compatible
  endpoint. Multipart POST, 30s timeout, empty-transcript guard.
- `src/alfred/telegram/bot.py` — `build_app` plus six handlers (`/start`, `/end`,
  `/status`, text, voice, shared `handle_message` pipeline). Per-chat asyncio
  locks serialize turns, allowlist silently drops unauthorized users.
- `src/alfred/telegram/daemon.py` — top-level `run(raw, skills_dir_str,
  suppress_stdout)` entry matching the 3-arg orchestrator contract. Loads config,
  runs the startup stale-session sweep, spins the PTB app + 60s gap-timeout
  sweeper, handles SIGTERM by closing all active sessions.

PTB v22.7 installed in the venv (the `>=21` constraint resolved to the latest
stable). `pyproject.toml` not yet updated — that's commit 5.

All five smoke checks pass:

1. `from alfred.telegram import bot, transcribe, daemon` — clean imports
2. `bot.build_app(...)` with a fake-but-plausible token returns a real
   `telegram.ext.Application` with 5 handlers registered
3. `transcribe.transcribe(b"", "audio/ogg", bad_key_cfg)` raises `TranscribeError`
   (Groq 401, error message surfaced cleanly)
4. `transcribe.transcribe(..., STTConfig(provider="elevenlabs"))` raises
   `NotImplementedError`
5. `daemon.run({"vault": {"path": ""}, "telegram": {}}, ...)` returns
   `_MISSING_CONFIG_EXIT` (78)

Forward contract from commit 3 honoured: a sixth check ran `_open_session_with_stash`
and asserted `_vault_path_root`, `_user_vault_path`, `_stt_model_used` are all
present on the active-session dict immediately after open. All three land with
the expected values, plus the per-turn counters `voice_messages` and
`text_messages` initialize to 0.

## Plan deviations and non-obvious choices

### PTB v22, not v21 — and manual lifecycle, not `run_polling`

The locked decision said "python-telegram-bot >=21". The latest on PyPI is v22.7,
which is what `pip install 'python-telegram-bot>=21'` resolved. No API changes
in v22 that affect our usage.

PTB's `Application.run_polling` assumes it owns the event loop (installs its own
SIGTERM handlers, calls `loop.close()` at exit via `close_loop=True`). That
collides with our own signal handlers and the sweeper task. I used the manual
`await app.initialize() → start() → updater.start_polling() → await
shutdown_event.wait() → stop chain` pattern instead. Teardown is guarded by
`initialised` / `started` / `polling` flags so a partial-init failure (bad
token → `InvalidToken` during `initialize()`) doesn't try to stop things that
never started.

**Alfred Learning below documents this.** Anyone else wiring a PTB daemon in
this repo should reach for the manual lifecycle first, not `run_polling`.

### Invalid-token / lifecycle failure → exit 78, not a traceback

Originally the top-level `try` had no `except`, so a bad token let the
`InvalidToken` exception propagate up past `finally`. That would look like a
daemon crash to the orchestrator and burn restart budget on a config-class
error. Added a top-level `except Exception`: log, set exit code to 78 if the
message mentions "token", else 1. Keeps the "don't restart" contract intact for
misconfiguration.

### `_MISSING_CONFIG_EXIT = 78`

Reused the orchestrator's `_MISSING_DEPS_EXIT` value rather than inventing a new
code. 78 already means "don't restart" in orchestrator.py's monitor loop, which
is the right behaviour for missing config too. Commit 5 can leave the orchestrator
mapping untouched.

### Voice/text counters stashed on state, not on the Session object

The task mentioned "attach these to the session object too if the session
record expects them." Reading `session.py._count_message_kinds` showed it walks
`session.transcript` looking for `_kind` metadata on each user turn — it never
reads per-session counters from the state dict. So the counters on the state
dict are purely for `/status` reporting and future review, not for the final
record. The record gets accurate counts from the transcript at close time.

Keeping them on the state dict (not the Session dataclass) avoids a schema
change to commit 3's `Session` — less churn, forward-compat.

### `build_vault_context` reuse

The task said "whichever helper exists; if no suitable helper, build a minimal
one inline." Curator already has a clean `build_vault_context` that walks the
vault and produces a type-grouped prompt listing. Reused it directly, wrapped
in a try/except so an import or walk failure falls back to a two-line inline
summary (type counts from top-level dirs). Either way the daemon boots.

### Voice counter bumped **after** session open, before `run_turn`

The counter increment happens inside the per-chat lock, right after the session
is opened/rehydrated but before `run_turn` is called. This way a crashed turn
doesn't double-count a retry, and the counter reflects "messages Alfred received"
not "turns that completed."

### `/start` says "Alfred", doesn't mention configuration status

Kept the greeting terse per the task — "Hi — this is Alfred. Send a voice note
or type a message." No version / config / debug info leaks. Unauthorized users
still get zero signal.

### Module-level logger with structlog

Consistent with the other telegram modules (`session.py`, `conversation.py`,
`state.py` all do `log = get_logger(__name__)`). Every inbound message logs
`talker.bot.inbound` with chat_id, user_id, kind, length/duration; every reply
logs `talker.bot.outbound` with the same + ok flag. Same logging discipline as
curator/janitor.

## Alfred Learnings

### Patterns Validated

- **Manual PTB lifecycle (`initialize → start → updater.start_polling → await
  event → teardown`) is the right pattern when you need to coexist with other
  asyncio tasks.** `run_polling` is convenient for standalone bots but
  incompatible with a daemon that also runs a signal handler and a background
  sweeper. Flags tracking `initialised/started/polling` make the finally block
  safe even when a stage partially fails.
- **Reuse `_MISSING_DEPS_EXIT = 78` for config-class failures.** The orchestrator
  already treats 78 as "don't restart." A bad bot token is the same user-action-
  required class as missing Python deps — returning 78 means commit 5 needs zero
  orchestrator changes to make the talker bail out cleanly on misconfiguration.
- **Stash forward-contract metadata on state dict immediately after `open_session`.**
  The three fields (`_vault_path_root`, `_user_vault_path`, `_stt_model_used`)
  let timeout-driven close paths work without a config handle. Keeping the stash
  in a single helper (`_open_session_with_stash`) co-located with the contract
  means a future change to the contract only touches one place.

### New Gotchas

- **PTB's `Application.run_polling(close_loop=True)` default closes the event
  loop that called it.** Easy to miss; the trace you get if you also have
  `loop.add_signal_handler` on the same loop is "RuntimeError: Event loop is
  closed" from whatever fires next. Manual lifecycle sidesteps it entirely.
- **PTB validates the bot token during `app.initialize()`, not at build time.**
  `Application.builder().token("fake").build()` succeeds; the token only hits
  the API when you initialize. Smoke-test-friendly at build_app level, but
  means daemon integration tests need either a real token or a mock transport.

### Missing Knowledge

- The voice-design note (2026-04-15) doesn't document the PTB lifecycle choice.
  Commit 5's session note should roll this up into the final Voice Stage 2a
  summary or the design note should be edited to capture it.

## Next

Commit 5 ties it all together:

- `cmd_talker` in `src/alfred/cli.py` — mirrors `cmd_curator`'s 3-arg
  shape (`raw, skills_dir_str, suppress_stdout`), calls
  `asyncio.run(daemon.run(...))`.
- Register `_run_talker` in `orchestrator.TOOL_RUNNERS` — the
  `raw, skills_dir_str, suppress_stdout` signature aligns with the existing
  CLI-backed tools, so it goes in the curator/janitor/distiller branch not the
  surveyor/mail/brief branch. Auto-start if `telegram` in `raw`.
- Add `[voice]` extra in `pyproject.toml` — `anthropic`, `python-telegram-bot>=21`.
  `httpx` is already a transitive dep via base install.
- Update `.env.example` and `config.yaml.example` only if something changed
  (nothing did this commit — both already have the fields).
