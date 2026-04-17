---
type: session
status: completed
name: "Talker Wiring Complete"
created: 2026-04-17
description: "Voice Stage 2a-wk1 final commit — wire the talker tool into the top-level CLI, orchestrator, pyproject extras, and CLAUDE.md. Closes the 5-commit wk1 track; talker is now a first-class Alfred tool reachable via `alfred talker` and auto-startable via `alfred up`."
tags: [voice, telegram, talker, session-note, wk1-final]
---

# Talker Wiring Complete — 2026-04-17

## Context

Voice Stage 2a-wk1 is a 5-commit track that stands up the `talker` tool — a Telegram bot that lets the user voice/text chat with Alfred, with transcripts and vault actions written back to the vault as `session/` records. This session is commit 5, the final wiring pass.

Prior wk1 commits (in order):

| Hash | Title | What it shipped |
|------|-------|-----------------|
| `e737733` | Add talker scope and talker_types_only rule | `vault/scope.py` gains a `talker` scope; new `talker_types_only` rule reuses the existing distiller-style type gate. Pure infrastructure — no tool wired to it yet. |
| `59cbf56` | Scaffold talker module (config, state, utils, placeholder skill) | Minimum viable package: `src/alfred/telegram/{__init__.py, config.py, state.py, utils.py}` plus a placeholder `src/alfred/_bundled/skills/vault-talker/SKILL.md`. Loads config, loads state, configures logging. No runtime yet. |
| `29ce6b6` | Implement talker session lifecycle and Anthropic tool-use loop | `session.py` (open/append/close, startup sweep, gap-timeout check, vault-path-aware frontmatter), `conversation.py` (the Anthropic tool-use loop with vault tool bridge). The brain of the bot — handles message → LLM → vault-op → reply. |
| `57dbea6` | Add Telegram bot, Groq transcription, and talker daemon | `bot.py` (PTB v21 handlers, allowlist gate, voice+text dispatch), `transcribe.py` (Groq Whisper via `httpx`), `daemon.py` (lifecycle, sweeper task, SIGTERM/SIGINT handling, startup close-on-restart). Daemon is now runnable via `python -m alfred.telegram.daemon`-ish plumbing, but no CLI entry point. |
| `<this commit>` | Wire talker tool into CLI and orchestrator | Adds the `alfred talker {watch,status,end,history}` CLI, registers `talker` in `orchestrator.TOOL_RUNNERS`, gates auto-start on the `telegram:` config section, adds a `[voice]` pyproject extra, and drops a Talker row into the CLAUDE.md tools table. |

## This commit — what changed

### `src/alfred/cli.py`

- New `cmd_talker(args)` dispatcher mirroring the per-tool pattern (curator/distiller). Subcommands:
  - `watch` — calls `alfred.telegram.daemon.run(raw, skills_dir_str, suppress_stdout=False)` and `sys.exit(code)`. The daemon's own signal handling + manual PTB lifecycle does the rest.
  - `status` — loads `StateManager`, prints active chat_ids (started_at, last_message_at, turn_count) and count of closed sessions. `--json` supported.
  - `end <chat_id>` — calls `session.close_session(..., reason="cli_manual", ...)` for the given chat_id; prints the vault record path; errors if no active session for that chat_id. `--json` supported.
  - `history [--limit N]` — prints the last N closed sessions from state. `--json` supported.
- `--json` flag triggers `suppress_stdout=True` in `_setup_logging_from_config` so structured log lines don't leak into parseable output. Same contract the vault CLI uses.
- Parser registration sits alongside `mail`, following the janitor/distiller subparser pattern.
- Also added a "--- Talker ---" section to `cmd_status` (only shown when `telegram:` is in config, mirroring the orchestrator's auto-start gate) so `alfred status` reports active/closed session counts alongside the other tools.

### `src/alfred/orchestrator.py`

- New `_run_talker(raw, skills_dir, suppress_stdout)` — thin `asyncio.run(daemon.run(...))` wrapper that `sys.exit(code)` on non-zero (so the `78` missing-config exit hits the existing "don't retry" branch for free).
- `TOOL_RUNNERS["talker"] = _run_talker`.
- Auto-start gate: `if "telegram" in raw: tools.append("talker")` — sits after the surveyor/mail/brief gates. Users without a bot don't get a restart-looping daemon.
- Talker falls into the 3-arg branch of `start_process` (not the `("surveyor", "mail", "brief")` tuple), so it receives `(raw, skills_dir_str, suppress_stdout)` — matching the talker daemon's signature because its system prompt is loaded from the bundled `vault-talker/SKILL.md`.

### `pyproject.toml`

- New `[voice]` optional extra: `python-telegram-bot>=21.0`, `anthropic>=0.40`.
- Added `"alfred-vault[voice]"` to the `all` extra so `pip install -e ".[all]"` picks it up alongside surveyor + temporal.
- Base dependencies untouched — `httpx` was already there (Groq transcription uses it).

### `config.yaml` (local, gitignored)

- Added a `telegram:` section mirroring `config.yaml.example`. Placeholders (`${TELEGRAM_BOT_TOKEN}`, `${ANTHROPIC_API_KEY}`, `${GROQ_API_KEY}`) resolve at runtime from `.env`.
- `config.yaml` is gitignored (confirmed via `.gitignore`) so this change is intentionally NOT in the commit — it's for the local machine only. The canonical example already has the section from commit 2.

### `CLAUDE.md`

- Added `| **Talker** | Telegram voice/text chat with Alfred, vault-grounded |` to the tools table.
- Updated the lead sentence from "four AI-powered tools" to "five AI-powered tools". (The brief and mail tools aren't in the table — that's the pre-existing state; this commit doesn't change it.)

## Smoke checks (all passed)

1. `alfred talker --help` — parser registered, lists `{watch,status,end,history}`.
2. `alfred talker status` — prints "Active sessions: none / Closed sessions: 0" against the empty talker_state.json.
3. `alfred talker watch --help` — help text shows.
4. `python -c "from alfred.orchestrator import TOOL_RUNNERS; print('talker' in TOOL_RUNNERS)"` — prints `True`.
5. `pip install -e ".[voice]"` — clean install (anthropic + python-telegram-bot already present from commits 3-4).
6. `alfred status` — shows the new `--- Talker ---` section with active/closed session counts (0/0).

Bonus verified: `alfred talker status --json` emits clean JSON (no log leakage into stdout).

## What ships at end of wk1

Alfred now has a fifth tool. With a valid `telegram:` section in `config.yaml` plus tokens in `.env`:

- `alfred up` auto-starts the talker alongside curator/janitor/distiller (and surveyor/mail/brief if those sections are present).
- `alfred talker watch` runs the bot in the foreground for dev/debug.
- Voice messages hit Groq Whisper for STT, text or transcript goes through the Anthropic tool-use loop, vault mutations go through the talker scope, and when a 30-minute gap closes the session the transcript + vault_ops land as a `session/` record.

The wk1 bar was "bot receives a message, replies, writes a session record when the user stops talking." That bar is met.

## What's next (wk2 preview)

- **End-to-end live test.** The bot has never spoken to the real Telegram API with real tokens. First wk2 action is `alfred talker watch`, send "hello" from the allowlisted account, and verify: message received → LLM call → reply delivered → (after 30min gap or `alfred talker end <chat_id>`) → `vault/session/Voice Session — …md` written with correct frontmatter and rendered transcript.
- **SKILL.md content.** Commit 2 shipped a placeholder. Prompt-tuner owns the real content — calibration, vault-grounding rules, primary-user conventions.
- **Voice replies.** Stage 2a-wk2 adds ElevenLabs TTS so Alfred replies with voice, not text. The bot module has a clear seam for this — `bot.py`'s `reply_text` is the only downstream call; swap in `reply_voice` behind a config flag.
- **Session-record quality review.** Vault-reviewer needs to sample the first few closed sessions and check frontmatter, tag conventions, outputs wikilinks, participants field.
- **Layer-3 dedup across voice sessions.** Once sessions start landing, Alfred's existing dedup infrastructure (from prior Layer-1/2 work) should treat them like any other source record — tests needed.

## Alfred Learnings

- **PTB v21 manual lifecycle works.** `Application.run_polling()` wants to own the event loop (installs signal handlers, closes the loop at exit) which collides with our own SIGTERM handler + async sweeper. Manual `initialize() → start() → updater.start_polling() → wait on event → stop/shutdown` is the right pattern when integrating PTB with an existing asyncio app.
- **Exit-code 78 = "don't retry" is a real shared contract.** Curator/janitor/distiller's orchestrator already routes code 78 through the `_MISSING_DEPS_EXIT` branch. The talker daemon reuses the same code (`_MISSING_CONFIG_EXIT = 78`) so missing tokens / empty allowlist naturally hit the no-restart branch — no new orchestrator logic needed. New tools with config-class failure modes should keep reusing 78.
- **Auto-start gating lives on config-section presence, not on a boolean flag.** The pattern `if "<section>" in raw: tools.append("<tool>")` is now applied by surveyor, mail, brief, and talker. Cleaner than a per-tool `enabled:` flag — absence == don't start.
- **JSON-emitting CLIs must gate log handlers.** The vault CLI already does this (`suppress_stdout=True`). Repeated for talker's `--json` subcommands. Worth flagging in the builder agent notes: **any new CLI subcommand that emits JSON on stdout needs to pass `suppress_stdout=True` to `_setup_logging_from_config`** — otherwise `structlog` handlers silently corrupt the JSON stream. This is a documentation candidate for `CLAUDE.md`.
- **`config.yaml` is gitignored; `config.yaml.example` is not.** When adding new tool config, the example file is the canonical committed source; the user's real `config.yaml` is a local artifact. Per the session-note rubric: additions to `config.yaml` go in the working tree for the user's local use but are not staged. This was caught by reading `.gitignore` before staging — worth a builder-agent reminder.
- **3-arg vs 2-arg runner split is load-bearing.** `start_process` in orchestrator.py chooses the signature based on `tool in ("surveyor", "mail", "brief")`. New tools that need `skills_dir` (curator, janitor, distiller, now talker) must stay OUT of that tuple. The talker needs `skills_dir` because its system prompt lives in `src/alfred/_bundled/skills/vault-talker/SKILL.md`. Straightforward, but it's the kind of two-line switch that's easy to get wrong.

## Handoff

Talker is code-complete for wk1. Next human action: run `alfred talker watch` against the real bot token and send a message. Expected flow: message arrives, LLM reply returns, 30-minute gap fires a close, session record lands in `vault/session/Voice Session — …md`.

If the first live test surfaces any surprises, they go to the builder agent for code fixes, to the prompt-tuner for SKILL.md iteration, or to infra if it's a tunnel/tokens problem.
