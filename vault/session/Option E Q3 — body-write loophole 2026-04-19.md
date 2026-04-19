---
type: session
date: 2026-04-19
status: complete
tags: [option-e, janitor, scope, security]
---

# Option E Q3 — Body-Write Loophole

## Scope

First of two deferred Option E follow-ups from yesterday's coordinated
pair. Q3 closes a gap in the scope lock landed in commit 5: the
janitor `field_allowlist` rule gated `set_fields` / `append_fields`
keys on frontmatter but NOT `body_append` or `body_stdin`. A sweep-path
agent could have rewritten entire record bodies to bypass the
frontmatter restrictions.

## What Changed

`src/alfred/vault/scope.py`:

- Added `allow_body_writes: bool` to every scope in `SCOPE_RULES`:
  - `curator` → True (inbox → record body writes)
  - `janitor` → **False** (the Q3 gate itself)
  - `janitor_enrich` → True (Stage 3 description appends)
  - `distiller` → True
  - `surveyor` → True (defensive default; surveyor doesn't body-write today)
  - `talker` → True (notes / sessions / conversations synthesized from voice)
- `check_scope` gained a `body_write: bool = False` kwarg. When True
  AND the scope carries `allow_body_writes: False`, it raises
  `ScopeError` with a body-specific message. Checked BEFORE the
  operation-level permission so callers attempting a forbidden body
  write get a clean "may not write record body content" error rather
  than a misleading allowlist one. Default False preserves backwards
  compatibility for every existing `check_scope` call site.

`src/alfred/vault/cli.py`:

- `cmd_edit` now computes `body_write_requested = bool(args.body_stdin
  or args.body_append)` and passes it into `check_scope`.
- When the caller supplies no frontmatter fields (pure body write),
  `cmd_edit` now passes `fields=[]` rather than building an empty list
  and tripping the allowlist's fail-closed branch. The body_write gate
  still applies and denies janitor body writes as expected.
- `cmd_create` also passes `body_write=bool(args.body_stdin)` so a
  jailbroken janitor can't slip a body through `vault create`. Inert
  for the expected triage-task-create happy path (no `--body-stdin`).

## Tests

8 new tests in `tests/test_scope.py` covering the matrix:

1. `test_janitor_scope_denies_body_append` — body_append under janitor raises
2. `test_janitor_scope_denies_body_replace` — body_write=True still denies even with allowlisted frontmatter fields
3. `test_janitor_enrich_allows_body_append` — Stage 3 description append still works
4. `test_talker_allows_body_append` — voice bot body synthesis still works
5. `test_curator_allows_body_append` — inbox → record body writes still work
6. `test_janitor_frontmatter_only_works` — baseline: allowlisted frontmatter edit with body_write=False still passes
7. `test_curator_create_allows_body_write` — curator create-with-body is the core flow
8. `test_janitor_create_denies_body_write` — janitor triage task creation can't slip a body through create

Full suite: **148 passed** (140 baseline + 8 new Q3 tests). No
regressions.

## Why This Matters

Q3 was explicitly flagged in yesterday's scope lock as an open gap
(see commit 5 code comment: "body-only writes ... intentionally NOT
routed through the field allowlist for now — body-write loophole is a
known gap tracked as open question #3"). The gap was real: the
allowlist rejects `janitor_note` arbitrary values but a misbehaving
janitor could have replaced the entire body of any record via
`alfred vault edit --body-append`. That would turn a "janitor can only
touch janitor_note" contract into a "janitor can rewrite anything
below the frontmatter fence" reality.

The fix is independent of the body's content — it's a gate on the
**capability**, not a content check. Stage 1/2 janitor flows
genuinely don't need to write body content (the only body-touching
stage is Stage 3 enrichment, which is a separate scope). Closing the
gate costs nothing for the expected happy path and removes a
jailbreak surface.

## Alfred Learnings

- **Scope gates compose, don't overlap.** The field allowlist gates
  frontmatter. `allow_body_writes` gates body content. Each is
  single-purpose and orthogonal. Trying to fold body-write into the
  allowlist ("body" as a pseudo-field) was considered and rejected —
  the body isn't a field, and the error messages would have been
  misleading.
- **Default True for `allow_body_writes` was load-bearing.** Setting
  the new key on every scope explicitly makes the intent visible
  ("yes, curator is supposed to write bodies — that's the core
  curator flow") but keeping the default True in `.get()` means any
  future scope that forgets the key inherits the permissive default.
  For a body-write capability that's a feature: new scopes don't
  accidentally lock themselves out. For a write capability you WANT
  locked down, explicit is better — which is why janitor sets it
  False by name rather than relying on any default.
- **Gate order matters for error messages.** Placing the body_write
  check before the operation permission check means a denied body
  write produces "may not write record body content" rather than the
  generic allowlist rejection. Self-diagnosing errors beat self-
  diagnosing stack traces.
- **Stage 3 enrichment was already correctly scoped.** When yesterday's
  scope-lock split `janitor` from `janitor_enrich`, it accidentally
  made Q3 cheaper — the janitor_enrich scope inherits
  `allow_body_writes: True` and Stage 3 continues to work unchanged.
  If Stage 3 had stayed in the janitor scope we'd have needed a
  per-stage carve-out. The split paid for itself.

## Commit

- Code + tests + this session note: (this commit)
- Previous: 07da7d0 (voice doc consolidation)
