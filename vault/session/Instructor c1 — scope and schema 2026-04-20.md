---
type: session
created: '2026-04-20'
name: Instructor c1 — scope and schema 2026-04-20
description: Commit 1 of the 6-commit alfred_instructions watcher rollout — new instructor scope in vault/scope.py and INSTRUCTION_FIELDS constant in vault/schema.py
intent: Land the permission + schema groundwork for the instructor daemon before the daemon module, CLI, or SKILL files go in
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related: []
tags:
  - instructor
  - scope
  - schema
  - alfred-instructions
status: completed
---

# Instructor c1 — scope and schema 2026-04-20

## Intent

Commit 1 of the 6-commit `alfred_instructions` watcher rollout. The
instructor daemon will watch every vault record for a pending
`alfred_instructions` frontmatter list, execute each directive under a
dedicated scope, and archive the result to `alfred_instructions_last`.

This commit ships only the permission + schema groundwork:
- `instructor` scope in `vault/scope.py`
- `INSTRUCTION_FIELDS` constant + `LIST_FIELDS` additions in `vault/schema.py`
- Tests covering both

The daemon module, CLI wiring, orchestrator registration, SKILL file,
and mutation-log integration land in commits 2–6.

## What shipped

### `instructor` scope (`src/alfred/vault/scope.py`)

Added to `SCOPE_RULES`:

```python
"instructor": {
    "read": True,
    "search": True,
    "list": True,
    "context": True,
    "create": True,
    "edit": True,
    "move": True,
    "delete": False,
    "allow_body_writes": True,
},
```

Position in the permission ladder:
- **Broader than janitor** — no frontmatter allowlist, body writes on,
  may create and move.
- **Narrower than talker** — delete is denied. Removing a record is
  always an explicit operator task; the watcher must never execute a
  destructive op on its own, even when a directive literally asks for
  it.

Uses the existing scope keys (`allow_body_writes`, operation booleans)
so `enforce_scope()` / `check_scope()` don't need any code changes to
handle the new scope.

### `INSTRUCTION_FIELDS` constant (`src/alfred/vault/schema.py`)

```python
INSTRUCTION_FIELDS: tuple[str, ...] = (
    "alfred_instructions",
    "alfred_instructions_last",
)
```

- `alfred_instructions` — pending queue. List of strings, each a
  directive.
- `alfred_instructions_last` — completed archive. List of
  `{text, executed_at, result}` dicts.

Both field names also added to `LIST_FIELDS` so the existing
frontmatter-list coercion treats them as lists when parsing records
with a single-string value (YAML would otherwise parse a one-entry list
as a scalar).

### Tests

- `tests/test_scope.py` — 9 new tests covering the instructor scope:
  read / search / list / context / edit (any field) / create (any
  type) / move / body writes allowed; delete denied.
- `tests/test_schema.py` — new file, 3 tests: `INSTRUCTION_FIELDS`
  importable, contains both field names in expected order,
  `LIST_FIELDS` includes both.

## Verification

Full `pytest tests/ -x`: **551 passed** in 22.65s. Baseline was 539;
this commit adds 12 new tests (9 scope + 3 schema).

## Deviations from spec

None. The scope keys matched existing shape, so no enforcer changes
were needed. `field_allowlist` is handled by the existing
`check_scope` logic — since the instructor scope sets `"edit": True`
(not `"field_allowlist"`), no allowlist key is required and none was
added.

## Guardrails honoured

- No `src/alfred/instructor/` module created — later commits.
- No orchestrator registration — later commits.
- No SKILL file — later commits.
- No backend prompt changes — later commits.
- No mutation-log changes — later commits.

## Alfred Learnings

- **Pattern validated — scope keys as single source of truth.** The
  `instructor` scope added zero lines to `check_scope()` itself
  because the existing key shape (`allow_body_writes`, operation
  booleans, special-permission strings) was expressive enough. The
  enforcer generalises over scopes rather than special-casing each
  one. Any new scope should fit into the existing key vocabulary; if
  it doesn't, that's a signal the vocabulary is wrong, not that the
  scope needs a bespoke branch.

- **Pattern validated — scope permission ladder as a design lens.**
  Placing `instructor` on the ladder (curator → janitor →
  instructor → talker) before writing the dict makes the
  allow/deny decisions fall out mechanically. The question "can
  instructor delete?" becomes "is deletion something the watcher
  should ever initiate without explicit operator intent?" — answer
  no, match janitor's position on delete, done.

- **Pattern validated — tuple for ordered schema constants.** Used
  `tuple[str, ...]` for `INSTRUCTION_FIELDS` rather than `set` or
  `list`. Tuples convey "these are the names, in this order, and
  this is immutable." `LIST_FIELDS` stays a set because ordering
  doesn't matter and membership-test is the only operation.

- **Gotcha confirmed — YAML single-entry list coercion.** If
  `alfred_instructions` has one directive and the user types it as a
  scalar (`alfred_instructions: "do the thing"`) instead of a list,
  python-frontmatter coerces to a string. Adding the field to
  `LIST_FIELDS` lets the existing normalization path promote it
  back to a single-entry list. This is why LIST_FIELDS exists in
  the first place; forgetting to add a new list-shaped field is a
  common bug source.
