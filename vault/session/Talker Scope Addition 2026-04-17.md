---
alfred_tags:
- software/alfred
- design/voice
- area/vault-scope
created: '2026-04-17'
description: First commit of the Stage 2a-wk1 voice rollout. Added a new `talker`
  scope and `talker_types_only` special rule to vault/scope.py so the upcoming
  Telegram bot has a permission boundary to plug into.
intent: Land the smallest possible additive scope change for the Telegram talker,
  ahead of any module/CLI/daemon work, so subsequent commits in the 5-commit plan
  can rely on the scope existing.
name: Talker Scope Addition
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
- '[[session/Voice Design Doc Revision 2026-04-15]]'
status: completed
tags:
- voice
- scope
- talker
- stage-2a
type: session
---

## Intent

Commit 1 of the approved 5-commit Stage 2a-wk1 plan. Goal: introduce the
`talker` scope and its `talker_types_only` create rule before any other
talker code lands, so later commits (module scaffold, session lifecycle,
bot, CLI wiring) have a stable permission contract to depend on.

## What shipped

- Added `"talker"` to `SCOPE_RULES` in `src/alfred/vault/scope.py`:
  read/search/list/context/edit allowed, create gated by
  `talker_types_only`, move/delete denied.
- Introduced module-level `TALKER_CREATE_TYPES` constant
  (`task`, `note`, `decision`, `event`, `session`, `conversation`,
  `assumption`, `synthesis`) so the rule handler and any future caller
  share one source of truth.
- Added the `talker_types_only` branch in `check_scope`, mirroring the
  shape of the existing `learn_types_only` rule.

One file changed, 29 insertions, no deletions.

## Verification

Smoke check (run via `python -c`) confirmed:

- `SCOPE_RULES["talker"]` exists with the spec'd permission map.
- All 8 allowed types pass `check_scope("talker", "create", record_type=t)`.
- `record_type="project"` and `record_type="input"` both raise
  `ScopeError`.
- `move` and `delete` raise `ScopeError`.
- `read`/`search`/`list`/`context`/`edit` pass through without raising.
- Existing `curator` and `distiller` create paths still pass — no
  regression in the other scopes.

`git diff` reviewed before commit; change is purely additive, no
unrelated edits.

## Alfred Learnings

No new gotchas, anti-patterns, or pattern validations from this commit.
The existing `learn_types_only` template was a clean fit; mirroring it
exactly was the right call and there's nothing here that needs to be
fed back into agent instructions or CLAUDE.md.
