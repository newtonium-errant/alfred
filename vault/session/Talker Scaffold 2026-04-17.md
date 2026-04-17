---
alfred_tags:
- software/alfred
- design/voice
- area/telegram
created: '2026-04-17'
description: Second commit of the Stage 2a-wk1 voice rollout. Scaffolded
  src/alfred/telegram/ with the non-networked pieces (config, state, utils,
  placeholder SKILL.md) plus the config.yaml.example and .env.example entries
  for the new telegram section. No daemon, bot, conversation, or transcribe
  code yet — those land in later commits.
intent: Get the module skeleton, config schema, and state persistence in
  place so subsequent commits can add the networked layers (session lifecycle,
  Claude conversation loop, Groq Whisper STT, python-telegram-bot application,
  and the orchestrator/CLI wiring) against a stable foundation.
name: Talker Scaffold
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Talker Scope Addition 2026-04-17]]'
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
status: completed
tags:
- voice
- telegram
- talker
- stage-2a
- scaffold
type: session
---

## Intent

Commit 2 of the approved 5-commit Stage 2a-wk1 plan. The scope addition
from commit 1 (`e737733`) gave the talker a permission boundary; this
commit gives it a module home, a config schema, and a state store. All
dependency-heavy pieces (python-telegram-bot, Anthropic SDK, Groq API,
asyncio daemon loop) are deferred so this commit stays reviewable and
doesn't tangle the config/state contract with the network code.

## What shipped

- `src/alfred/telegram/__init__.py` — empty package marker.
- `src/alfred/telegram/config.py` — `TalkerConfig` dataclass tree
  (bot_token, allowed_users, primary_users, nested anthropic/stt/session/
  vault/logging). `load_from_unified(raw)` mirrors the curator shape:
  `_substitute_env` for `${VAR}` placeholders, recursive `_build` from
  a `_DATACLASS_MAP`, and the same unified-`logging.dir` →
  per-tool-`logging.file` mapping curator uses.
- `src/alfred/telegram/utils.py` — `setup_logging()` + `get_logger()`,
  copied from `curator/utils.py` minus the `file_hash` helper (talker
  doesn't hash inbox files).
- `src/alfred/telegram/state.py` — JSON-backed `StateManager`, atomic
  tmp+rename writes. Schema per the design plan:
  `{version, active_sessions: {chat_id: {...}}, closed_sessions: [...]}`.
  `append_closed` trims to `MAX_CLOSED = 50` so the file can't grow
  unboundedly. Public API: `load`, `save`, `get_active`, `set_active`,
  `pop_active`, `append_closed`.
- `src/alfred/_bundled/skills/vault-talker/SKILL.md` — placeholder
  noting that the prompt-tuner owns this file and it will be filled
  out before wk1 ships.
- `config.yaml.example` — new `telegram:` section with all three
  subsections (anthropic, stt, session) plus `primary_users` seeded
  with `person/Andrew Newton`. Env-var placeholders left literal
  (`${TELEGRAM_BOT_TOKEN}`, `${ANTHROPIC_API_KEY}`, `${GROQ_API_KEY}`).
- `.env.example` — stubs for the three new env vars with comments
  pointing at @BotFather, console.anthropic.com, and console.groq.com.
  Real `.env` not touched.

Seven files changed, 347 insertions, no deletions. Diff stat:

```
 .env.example                                     |  12 ++
 config.yaml.example                              |  24 ++++
 src/alfred/_bundled/skills/vault-talker/SKILL.md |  14 +++
 src/alfred/telegram/__init__.py                  |   1 +
 src/alfred/telegram/config.py                    | 135 ++++++++
 src/alfred/telegram/state.py                     | 114 ++++++
 src/alfred/telegram/utils.py                     |  47 +++
```

## Verification

Smoke check 1 — config load from the updated example:

```
python -c "from alfred.telegram.config import load_from_unified; \
  import yaml; print(load_from_unified(yaml.safe_load(open('config.yaml.example'))))"
```

Loads cleanly. Unresolved `${VAR}` placeholders are preserved verbatim
because the env vars aren't set in that shell, which matches the curator
behavior.

Smoke check 2 — state round-trip:

- Fresh `StateManager` → `load()` on a nonexistent file yields the empty
  schema.
- `set_active(123, session)` + `save()` + reload in a new manager +
  `get_active(123)` returns the identical session dict.
- `str(chat_id)` and `int(chat_id)` both address the same session
  (normalised via `str()` internally).
- `pop_active(123)` returns the session and clears it from state.
- `append_closed({...})` × 60 trims to the last 50 — `closed_sessions[0]['i']`
  is 10, `[-1]['i']` is 59.

Both checks passed.

## Pattern deviations

- **State schema diverges from curator's.** Curator's `State` is
  `ProcessedEntry`-keyed by inbox filename; talker needs active vs
  closed session lists. I kept curator's atomic-write pattern and
  `StateManager` class name but replaced the internal dataclasses with
  a plain dict that matches the documented JSON schema. Public API is
  the chat-id-scoped surface the plan called out
  (`get/set/pop_active`, `append_closed`). This is the right call —
  forcing talker's schema into `ProcessedEntry` shape would've been
  strictly worse.
- **Vault subsection is trimmed.** The talker only needs `path` and
  `ignore_dirs` from the unified vault config, so `load_from_unified`
  strips `inbox_dir`, `processed_dir`, and `ignore_files` before
  building `VaultConfig`. Same defensive pattern curator uses for
  `ignore_files`.
- **No `file_hash` helper in utils.py.** Curator hashes inbox files to
  detect unchanged work; talker doesn't have that need. Left out
  rather than dead-coded in.

## Alfred Learnings

No meaningful learnings from this commit. The curator module was a
clean reference template; every deviation from it was obvious in
context (schema shape, trimmed vault config, no file hashing) and
doesn't point at anything that needs to be added to agent instructions
or CLAUDE.md. If later commits hit unexpected friction from the
curator-shaped patterns not quite fitting the talker's needs, that
will be worth documenting — but nothing to flag yet.
