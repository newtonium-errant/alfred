---
alfred_tags:
- software/alfred
- voice
- hotfix
created: '2026-04-18'
description: Opus 4.x deprecated the temperature parameter. Omit it from
  messages.create kwargs when session.model starts with claude-opus-. Sonnet
  and Haiku still accept temperature and continue to receive it.
intent: Unblock Opus escalations after first live article session 400'd on
  temperature=0.7 against claude-opus-4-7
name: Opus Temperature Hotfix
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Talker API Schema Hotfix 2026-04-18]]'
status: completed
tags:
- hotfix
- voice
- opus
type: session
---

# Opus Temperature Hotfix — 2026-04-18

## Intent

First live article session post-wk3 ship. Voice cue "let's go deeper on the bait-and-switch essay template" classified correctly as `article` + `claude-opus-4-7` (confirming both the router AND the Opus 4.7 alias is live). Main API call then 400'd: `'temperature' is deprecated for this model.`

## Root cause

Anthropic deprecated the `temperature` parameter on Opus 4.x models. Sonnet/Haiku still accept it.

Our `run_turn` unconditionally passed `temperature=config.anthropic.temperature`. Needed a per-model gate.

## What shipped

`conversation.py::run_turn` now builds `create_kwargs` as a dict and only adds `temperature` when `session.model` does NOT start with `claude-opus-`. Future-proof for any `claude-opus-*` variant.

4 regression tests in `tests/telegram/test_opus_temperature.py`:
- `test_opus_omits_temperature` (opus-4-7)
- `test_sonnet_includes_temperature`
- `test_haiku_includes_temperature`
- `test_opus_4_5_also_omits_temperature`

## Verification

135/135 tests pass. Daemons need restart to activate.

## Alfred Learnings

**Router + classification + Opus 4.7 alias all confirmed live** before this bug hit. `talker.router.decided model=claude-opus-4-7 session_type=article` — the wk3 pipeline worked end-to-end up to the SDK call. The bug is purely in how we call the SDK, not in wk3 logic.

**Model-specific parameter deprecations are the new category of SDK-layer fragility.** Expect more of these as Anthropic ships new models. The `create_kwargs` dict pattern generalises — for any future deprecation (max_tokens limits, system-block restrictions), gate the kwarg addition on a per-model check.

**Continuation issue flagged**: the log shows `continues=False` on the article session despite the user wanting to continue the prior note. This is correct per the wk2 plan — `note` type has `can_continue=False`, so even though the prior session was a note (and the user referenced it), continuation didn't fire. The user's request was classified as `article` (new session) which DOES continue by default, but the router looked for a prior ARTICLE to continue from and found none. So `continues=False` is accurate but surprising from the user's POV. Future work: either relax the router to cross-type-continue on explicit cue, or have Alfred read the prior note via vault_read inline. Tracked informally for wk4+.
