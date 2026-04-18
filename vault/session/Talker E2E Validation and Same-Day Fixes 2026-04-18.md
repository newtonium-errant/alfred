---
alfred_tags:
- software/alfred
- design/voice
created: '2026-04-18'
description: Stage 2a-wk1 end-to-end validation on live Telegram plus the
  AttributeError fix discovered during E2E. Full pipeline validated across
  5 turns (text, voice, query, create, /end). Three polish bugs found and
  captured for wk2 cleanup. ANTHROPIC_API_KEY subprocess isolation fix also
  landed and validated.
intent: Stamp Stage 2a-wk1 as shipped-and-validated with full E2E coverage
name: Talker E2E Validation and Same-Day Fixes
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
- '[[session/Talker Wiring Complete 2026-04-17]]'
- '[[session/Talker SKILL Full Draft 2026-04-17]]'
- '[[session/Isolate Anthropic Key From Subprocess 2026-04-18]]'
status: completed
tags:
- voice
- validation
- e2e
type: session
---

# Talker E2E Validation and Same-Day Fixes — 2026-04-18

## Intent

Stage 2a-wk1 was shipped 2026-04-17 but never validated against live Telegram. This session closes that gap. Two bugs surfaced during the E2E run and were fixed on the spot; three non-blocking polish bugs were logged for wk2.

## What shipped today on the talker path

1. **`19e7a3a` — Fix talker AttributeError: await AsyncAnthropic.messages.create()**. First live message produced `AttributeError: 'coroutine' object has no attribute 'content'`. The `run_turn` tool-use loop was missing the `await` on the async Anthropic SDK call. One-line fix. Smoke tests in wk1 missed it because the mocked client returned a sync object instead of a coroutine.

2. **`103a2ca` + `b94edcf` — Isolate `ANTHROPIC_API_KEY` from `claude -p` subprocesses**. Yesterday's talker work added `ANTHROPIC_API_KEY` to `.env` so the in-process Anthropic SDK could authenticate. Side effect: every subprocess inherited the var, including `claude -p` calls from the distiller/janitor/curator backends. `claude -p` switches from OAuth/Max-plan auth to API-credit billing whenever the env var is set. Users paying for Max were silently being charged against the API credit pool instead. New `alfred.subprocess_env.claude_subprocess_env()` helper strips the env var before subprocess spawn. Applied at four call sites.

## Validation — full E2E

After both fixes and a credit top-up ($100 grant paying down $9.34 of pre-existing API debt), a 25-turn live session validated every wk1 contract:

| Turn | Input | Tool | Latency | SKILL behavior |
|---|---|---|---|---|
| 1 | `Hello` | — | 5s | `Ready.` — terse, no preamble |
| 2 | `what projects do I have?` | `vault_search` | 5s | Correct: one active project |
| 3 | Voice (17s, creative brainstorm ask) | — | 5s | **Correctly deflected to Knowledge Alfred** per grounded-mode rule |
| 4 | `When was the last Alfred git commit` | — | 2s | **Honest scope limit** — no web/git, suggested `git log -1 --oneline` |
| 5 | `When was the last janitor sweep?` | `vault_search` | 6s | Vault-grounded: run records only show Morning Briefs |
| 6 | Voice (5s, task-create) | `vault_create` | 6s | Task created with inferred project link |
| 7 | `/end` | — | <1s | Session record written at `session/Voice Session — 2026-04-18 1638 286921d8.md` |

Session-record schema matched the wk1 design: `outputs` populated with the created task (the fix I flagged during plan review is working), `telegram.vault_operations` list with the create entry, transcript body renders tool_use/tool_result as compact summaries, Obsidian `![[related.base#All]]` embed trailing.

## Polish bugs logged (non-blocking, wk2 cleanup)

All three in `~/.claude/projects/-home-andrew-alfred/memory/project_talker_polish_bugs.md`:

1. Transcript body timestamps all show session-start time instead of per-turn `ts`.
2. `telegram.voice_messages` counter doesn't increment (recorded 0 despite 2 voice notes).
3. Talker mutation log landing at repo root with a bare UUID filename instead of `data/`.

None of these affect the vault's correctness or the talker's usefulness. They're rendering/counter/path polish.

## Operational side effects captured today

- **Subprocess isolation validated**: post-fix restart at 16:31 UTC, all 5 distiller consolidation types completed via Max plan OAuth with zero credit errors. Summary lengths 1665-3209 chars per type.
- **Janitor Option E + scope narrowing active**: janitor ran `structural_only=True` sweeps during the window; deep-sweep cadence (now persisted per upstream commit `e510cbe`) means the next fix-mode sweep is tomorrow ~00:39 UTC. Scope lock enforcement untested this window — needs either next deep sweep or manual `scripts/smoke_janitor_scope.sh` run to exercise.
- **Surveyor silent-writer bug still present**: alfred_tags additions observed on 5 records post-restart, but zero `writer.tags_*` log events. Correctness fine, observability broken. Tracked in `project_surveyor_silent_writer.md`.

## Alfred Learnings

**Async SDK test gap**: wk1 smoke tests for `conversation.py::run_turn` used a mock with sync return values. Real `AsyncAnthropic.messages.create()` returns a coroutine. The mock should mirror the real SDK's async shape. Add to the builder agent's checklist for any future async-SDK work.

**Env var leak pattern**: any credential added to `.env` for one tool is visible to every subprocess from every other tool. The `claude -p` billing switch was silent — no log, no error, just different invoice. When tools on the same stack use different auth paths (OAuth subprocess vs in-process API key), each subprocess call site has to explicitly scope the env. Worth a CLAUDE.md convention if it happens again.

**Credit balance vs spend limit**: Anthropic's "credit balance too low" error fires when prepaid credits are depleted, regardless of the monthly spend limit. User's Plans & Billing shows both separately — "Spend limits" is a ceiling, "Credit balance" is the drawable pool. Top up the pool, don't raise the ceiling.

**Max plan subscription is subprocess-only**: The Claude Max plan only covers `claude -p` (the Claude Code CLI using OAuth). Direct Anthropic Python SDK calls (like the talker) always hit API credits. No way around this without a billing-model change from Anthropic. Budget API credits separately for any SDK-based tool.
