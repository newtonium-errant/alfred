---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, scope, infrastructure]
---

# Option E - Commit 1: Scope `field_allowlist` Infrastructure

## Scope

First of the six-commit Option E sequence. Plan Part 1: introduce a
generic `field_allowlist` permission type in `vault/scope.py` that lets
a scope limit which frontmatter fields it may write. No scope actually
uses it yet — commit 5 flips janitor onto it with a narrow allowlist.

## What Changed

`src/alfred/vault/scope.py`:

- `check_scope(...)` gains a `fields: list[str] | None = None` kwarg.
- New branch: when `permission == "field_allowlist"`, look up the
  per-scope allowlist at `rules[f"{operation}_fields_allowlist"]` and
  require every entry in `fields` to be in the allowlist. Fails closed
  when `fields is None` so callers can't bypass the check by omission.
- `SCOPE_RULES` type hint widened to `dict[str, dict[str, bool | str | set[str]]]`
  so allowlists can live alongside bool/str permissions.

`src/alfred/vault/cli.py::cmd_edit`:

- Compute `fields = list(set_fields.keys()) + list(append_fields.keys())`
  before the `check_scope` call and pass through.
- Body-only writes (`--body-append`, `--body-stdin`) deliberately NOT
  included in `fields`. That is the known body-write loophole (open
  question #3) we're tracking but not closing in this commit.

## Why This Matters

The field_allowlist rule is the mechanism that will narrow the janitor
scope in commit 5. Shipping it as pure infrastructure first means:

- Commits 1-4 don't change scope behavior. Daemons keep working
  unchanged.
- Commit 5's flip is a one-line permissions change plus the allowlist
  set. Easier to review, easier to revert.
- Other scopes (distiller, surveyor) can adopt the same rule later
  without re-plumbing.

## Smoke Test

Inline Python:

- `check_scope('janitor', 'edit', fields=['janitor_note'])` passes —
  janitor still has `edit=True` so fields arg is ignored.
- Synthetic `SCOPE_RULES['test']` with `edit='field_allowlist'` and
  `edit_fields_allowlist={'foo','bar'}`:
  - `fields=['foo']` → passes
  - `fields=['foo','bar']` → passes
  - `fields=['baz']` → raises ScopeError listing allowlist + rejected
  - `fields=['foo','baz']` → raises ScopeError
  - `fields=None` → raises ScopeError (fails closed)
- `check_scope('curator', 'edit', fields=['anything'])` still passes
  (curator has `edit=True`).

All paths behaved as planned. No other callsites needed updating —
`cmd_edit` is the only frontmatter-writing path; `cmd_create` already
uses `frontmatter` for type-gated rules, and `cmd_move`/`cmd_delete`
don't touch fields.

## Alfred Learnings

- **Fail-closed default for new permission types.** When adding a new
  permission type that depends on caller-supplied data (the `fields`
  list), raising on `None` prevents accidental bypass if a future
  call site forgets to pass the new argument. The only cost is a
  clearer error message when scopes get wired to the new rule.
- **Resolves open question:** plan Part 6 Q3 — body-write loophole is
  deferred, not ignored; we explicitly don't include body writes in
  `fields` so the allowlist doesn't pretend to cover something it
  doesn't.

## Commit

- Code: (this commit)
- Session note: (this file)
