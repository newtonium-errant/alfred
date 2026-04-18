---
alfred_tags:
- software/alfred
- subprocess
- billing
created: '2026-04-18'
description: Strip ANTHROPIC_API_KEY (and related credential env vars) from the
  environment passed to `claude -p` subprocess calls so the Claude Code CLI
  uses OAuth/Max-plan auth instead of silently switching to API-credit
  billing. The talker tool still sees the key via its in-process Anthropic
  SDK; no other change.
intent: Stop the distiller/janitor/curator agent backends from burning paid
  API credits instead of riding the user's Claude Max subscription
name: Isolate Anthropic Key From Subprocess
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Talker Session and Tool Bridge 2026-04-17]]'
status: completed
tags:
- subprocess
- billing
type: session
---

# Isolate Anthropic Key From Subprocess — 2026-04-18

## Intent

Yesterday's talker work (Voice Stage 2a-wk1) added `ANTHROPIC_API_KEY` to `.env` so the talker's direct Anthropic SDK calls can authenticate. Unintended side-effect: every subprocess `Alfred` spawns inherits that env var, and the `claude -p` CLI switches from OAuth/Max-plan auth to API-credit billing whenever that var is set. The user's distiller/janitor/curator LLM calls have been silently charging paid API credits instead of riding their Claude Max subscription, producing `Credit balance is too low` errors once the API balance depleted.

## What shipped

New module `src/alfred/subprocess_env.py` with a `claude_subprocess_env(overrides={})` helper that returns a copy of the current env with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` stripped, then applies the caller's overrides.

Four call sites updated to route through the helper:
- `src/alfred/curator/backends/cli.py`
- `src/alfred/distiller/backends/cli.py`
- `src/alfred/janitor/backends/cli.py`
- `src/alfred/temporal/activities.py`

The talker (`src/alfred/telegram/conversation.py`) uses the Anthropic Python SDK in-process, not `claude -p`. It continues to read `ANTHROPIC_API_KEY` from its process env — no change needed.

## Verification

- After daemon restart at 16:31 UTC: `claude -p` process running under the distiller's consolidation stage, authed via OAuth (`~/.claude/.credentials.json` refreshed).
- Zero `Credit balance is too low` errors post-restart.
- Talker still healthy, still polling Telegram via the Anthropic SDK with the key it reads from its own env.

## Alfred Learnings

**Env vars leak across subprocess boundaries by default.** Any credential added to `.env` for one tool is visible to every subprocess every other tool spawns. When two tools on the same stack use different billing/auth paths, this produces silent wrong-pool charging. Standard Python `subprocess.run(cmd, env={**os.environ, ...})` copies the full parent env; to scope a credential, the call site has to explicitly strip it. Worth documenting as a CLAUDE.md rule once we have a second occurrence.

**The failure mode here was observability-poor.** The `Credit balance is too low` errors looked like a user billing problem, not a bug. It took connecting yesterday's `.env` addition to today's error pattern to see that the two were related. Cross-feature env contamination is the general class.

**The fix is small but the discovery is the real work.** Four backends × one-line change apiece. Knowing you need the change is 90% of the effort.
