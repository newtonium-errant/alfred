---
type: session
name: Transport c4 — Scheduler + remind_at schema + talker SKILL update
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
  - scheduler
  - skill
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c4 — Scheduler + remind_at schema + talker SKILL update

## What shipped

This commit bundles the three sides of one cross-agent contract per
CLAUDE.md's "scope+schema narrowing commits trigger a SKILL audit in
the same cycle" rule.

### Scheduler (code)

`src/alfred/transport/scheduler.py` — in-process async task that runs
inside the talker daemon:

- `find_due_reminders(vault_path, now, stale_max_minutes)` — pure
  function. Returns `(due, stale)`. Filters to `type == "task"`,
  `status in {todo, active}`, parses `remind_at`, skips records where
  `reminded_at >= remind_at`. Stale window → stale list.
- `format_reminder(entry)` — `"Reminder: {title} (due {due})"` with
  `reminder_text` verbatim override per ratified recommendation 3.
- `clear_remind_at_and_stamp(entry, now)` — drops `remind_at`,
  stamps `reminded_at`, appends
  `<!-- ALFRED:REMINDER fired_at=... remind_at=... -->` to body.
- `run(config, state, send_fn, vault_path, user_id, shutdown_event)`
  — main loop. Wakes every `poll_interval_seconds`, runs one `_tick`
  which fires due reminders, dead-letters stale ones, then drains
  `state.pop_due(now)` for server-scheduled sends.

### Schema (data contract)

`src/alfred/vault/schema.py` — new `REMINDER_FIELDS` tuple
documenting `remind_at` / `reminded_at` / `reminder_text` as optional
frontmatter fields on task records. Module docstring explains the
re-arming rule (new `remind_at > reminded_at` → fires again).

### Scope (authorisation)

No change needed. Talker scope already has `"edit": True` — broad
permit. Test added (`test_talker_scope_permits_task_edits`) to pin
this so if a future Option-E style tightening lands, the test fails
and flags the cross-contract drift.

### SKILL (agent instructions)

`src/alfred/_bundled/skills/vault-talker/SKILL.md` — added
`## Setting reminders` section covering:

- Natural-language time parsing with timezone resolution against
  `person/Andrew Newton.md`'s `timezone` field.
- Update-existing-task preference (search first, create second).
- Fields to set (`remind_at`, optional `reminder_text`), fields NOT
  to set (`reminded_at` — scheduler owns it).
- Never-in-the-past rule; re-arming pattern.
- One short confirmation sentence.

Also added `remind_at` to the `task` row of the record-type table so
the existence of the field is discoverable from the overview.

## Tests

`tests/test_transport_scheduler.py` — 18 tests:

- 7 for `find_due_reminders` — past/future/already-reminded/re-arm/
  wrong-status/stale-split/no-task-dir.
- 3 for `format_reminder` — reminder_text override, due present/absent.
- 2 for `clear_remind_at_and_stamp` — field mutation + body audit,
  idempotent same-timestamp repeat.
- 3 for `_tick` end-to-end — fires+dead-letters, pending-queue drain,
  retain-on-send-failure.
- 3 cross-agent safety net — schema constant exists, scope permits
  edit, SKILL contains `Setting reminders` section.

Suite: 637 → 655 (+18). All green.

## Alfred Learnings

- **Pattern validated** — CLAUDE.md's "ship scope+schema+SKILL
  together" rule is now encoded as three test-level assertions in
  one test file. The `test_talker_skill_has_setting_reminders_section`
  / `test_talker_scope_permits_task_edits` /
  `test_schema_exposes_reminder_fields` trio forms a drift trip-wire:
  if any one of them fails, the contract has drifted.
- **Pattern validated** — ISO timestamp tolerance logic
  (`_parse_iso`) is now duplicated in `state.py` and `scheduler.py`.
  Acceptable here (different contexts, small surface), but worth
  consolidating into `utils.py` if a third consumer appears. Flagged
  for a follow-up refactor.
- **Anti-pattern avoided** — resisted the urge to hard-code the user
  ID inside the scheduler. The caller passes `user_id` explicitly
  (in c6 the talker daemon reads it from `telegram.allowed_users[0]`
  and passes it in). Single-user v1, but the seam for multi-user is
  already in place.
- **Decision recorded** — `reminder_text` field verbatim overrides
  the template; the scheduler uses it as-is without any formatting.
  If a user types curly quotes or emojis, those come through
  unchanged to Telegram. Deliberate choice.
