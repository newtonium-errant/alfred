---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, scope, security]
---

# Option E - Commit 5: Scope Lock

## Scope

Fifth of the six-commit Option E sequence. Plan Part 5: narrow the
janitor edit scope from "anything goes" to a tight field allowlist,
and split Stage 3 enrichment into its own `janitor_enrich` scope so
the Stage 1/2 allowlist can stay minimal. Resolves plan open
question Q1 — Q1 asked whether Stage 3 should share the janitor
allowlist or get its own, and we picked the separate-scope route.

## What Changed

`src/alfred/vault/scope.py`:

- `SCOPE_RULES["janitor"]["edit"]` flipped from `True` to
  `"field_allowlist"`.
- Added `edit_fields_allowlist` to the janitor rules covering the
  fields Stage 1 autofix and Stage 2 link repair legitimately write:
  `janitor_note`, `type`, `status`, `name`, `subject`, `created`,
  `related`, `tags`, `alfred_triage`, `alfred_triage_kind`,
  `alfred_triage_id`, `candidates`, `priority`. Nothing else.
- Added a new `"janitor_enrich"` scope for Stage 3. Rules: `read`,
  `search`, `list`, `context` = True; `create`, `move`, `delete` =
  False; `edit` = `"field_allowlist"` with allowlist covering the
  enrichment fields — `description`, `role`, `org`, `email`,
  `org_type`, `website`, `phone`, `aliases`, `related`, `tags`.

`src/alfred/janitor/pipeline.py`:

- `_call_llm` gained a `scope: str = "janitor"` kwarg that feeds
  into the subprocess env's `ALFRED_VAULT_SCOPE`. Default preserves
  existing Stage 2 behavior.
- `_stage3_enrich` now calls `_call_llm(..., scope="janitor_enrich")`
  so the enrichment LLM sees the wider-but-still-bounded allowlist.

## Why This Matters

Before this commit, any LLM invocation under the janitor scope could
write arbitrary frontmatter fields — including fields owned by other
tools (`alfred_tags` from surveyor, `distiller_signals` from
distiller, user-authored fields like the body content). The only
thing keeping the janitor honest was the prompt. With a misbehaving
or jailbroken agent, the prompt is not a security boundary.

Now the boundary is in Python: `check_scope()` rejects writes outside
the allowlist with a clean JSON error the caller can surface. The
Stage 1/2 allowlist is tight enough that you can read it and see
exactly what the janitor is entitled to touch.

Stage 3 needed its own scope because description-writing and
role-writing are fundamentally different operations from janitor-note
stamping. Lumping them into one allowlist would have lost the
signal that they're different capabilities.

## Smoke Tests

All 8 tests from the plan passed:

1. `SCOPE_RULES['janitor']['edit']` returns `"field_allowlist"`.
2. `'janitor_enrich' in SCOPE_RULES` is True.
3. `check_scope('janitor','edit',fields=['janitor_note'])` passes
   (in-allowlist).
4. `check_scope('janitor','edit',fields=['alfred_tags'])` raises
   `ScopeError` with message listing the allowlist and naming
   `alfred_tags` as rejected.
5. `ALFRED_VAULT_SCOPE=janitor alfred vault edit ... --set
   'alfred_tags=[...]'` exits 1 with JSON error mentioning
   "Scope 'janitor'".
6. `ALFRED_VAULT_SCOPE=janitor alfred vault edit ... --set
   'janitor_note="..."'` exits 0 with success JSON.
7. `ALFRED_VAULT_SCOPE=janitor_enrich alfred vault edit ... --set
   'description="..."'` exits 0 with success JSON.
8. Test edits on `person/Andrew Newton.md` (description and
   janitor_note) were reverted via follow-up CLI edits; `diff`
   against pre-test backup produced no output. Vault is clean.

## Alfred Learnings

- **Scope narrowing is a Python decision, not a prompt decision.**
  Before commit 5, the janitor's scope was effectively "trust the
  prompt". After commit 5, the scope is enforced in
  `check_scope()` with a fail-closed allowlist. The prompt is a
  hint; Python is the gate.
- **Stage-split scopes beat one-big-allowlist.** When Stage 3 needs
  to write `description`, giving the whole janitor scope that
  permission exposes Stage 1/2 to the same risk. Separate scopes
  per stage keeps each as tight as it can be.
- **Scope rules are the cross-agent contract.** If a future
  prompt-tuner change asks the Stage 2 LLM to write a new field,
  the first step is to update the allowlist — not to hope the field
  is covered. The error message lists the allowlist, so a scope
  denial should be self-diagnosing.

## Commit

- Code: 2d5e8cf (this commit)
- Session note: (this file)
