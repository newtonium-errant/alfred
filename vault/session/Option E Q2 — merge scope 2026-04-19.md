---
type: session
date: 2026-04-19
status: complete
tags: [option-e, janitor, scope, merge, dup001]
---

# Option E Q2 — Operator-Directed Merge Scope

## Scope

Second of two deferred Option E follow-ups from yesterday's coordinated
pair. Q2 addresses the conflict between the tight janitor
`field_allowlist` landed in commit 5 and the DUP001 Operator-Directed
Merge procedure (SKILL.md §3), which rewrites wikilink-bearing
frontmatter fields (`org`, `client`, `parent`, `project`, `related`,
`relationships`, `assigned`, etc.) across many files when merging two
entity records. Under the narrow allowlist, every retargeting edit
would fail ScopeError.

## Decision: Option (c) — Deterministic Python

Three options were considered yesterday:

- **(a) Widen the janitor allowlist** to cover every wikilink field.
  Weakens scope for the 99% autonomous case. Rejected.
- **(b) Separate `janitor_merge` scope** with a broader allowlist,
  invoked only during the merge procedure.
- **(c) Deterministic Python merge** — add `janitor.merge.merge_entities`
  that does the retargeting in code; LLM only picks the pair.

**Picked (c).** Rationale:

1. **Scope plumbing is subprocess-boundary fixed.** `ALFRED_VAULT_SCOPE`
   is set once per `_call_llm` invocation (see
   `src/alfred/janitor/pipeline.py:106`). The agent cannot switch
   scope mid-batch. Option (b) would require a **separate pipeline
   stage** invoking the LLM under `janitor_merge` only for approved
   merges — architectural weight for an escalation edge case.
2. **Option E philosophy.** LLM for judgment (pick the winner),
   deterministic Python for mechanical work (rewrite N wikilinks).
   Merge retargeting is 100% mechanical once winner/loser are chosen.
3. **Atomicity.** A Python merge succeeds or fails atomically per
   step. An LLM iterating `vault edit` across dozens of files can
   partially fail and leave the vault in a half-merged state.
4. **LoC fits.** The new module is ~260 lines including docstrings.
   Well under the "stop and reconsider option (b)" threshold.

## What Changed

`src/alfred/janitor/merge.py` (NEW, ~260 LoC):

- `merge_entities(vault_path, winner, loser, *, session_path=None)`:
  1. Resolve + validate both records exist (via `vault_read`).
  2. Copy unique frontmatter fields from loser → winner (fields
     present on loser but absent or empty on winner). `type`, `name`,
     `subject`, `created` are immutable on the winner.
  3. Append loser's body to winner with a `<!-- merged from ... -->`
     provenance marker.
  4. Vault-wide: find every inbound wikilink via `vault_search`,
     rewrite each (case-insensitive match, winner's exact casing in
     replacement). Frontmatter scalars + lists + body prose all
     covered. Uses the existing `body_rewriter` kwarg on `vault_edit`.
  5. Delete the loser via `vault_delete`.
- `MergeResult` dataclass surfaces `winner_path`, `loser_path`,
  `retargeted_files`, `fields_merged`, `body_appended`, `loser_deleted`.
- `MergeError` for the missing-record / same-record cases.
- Calls `vault_ops` **directly** (bypassing the CLI scope gate) —
  the merge is a privileged operation that the operator has already
  approved, and no agent scope should grant these permissions. Every
  write still flows through `log_mutation(session_path, ...)` so the
  audit log captures every edit + delete.

No changes to `scope.py` — option (c) doesn't need a `janitor_merge`
scope. The helper is a Python-only entry point.

No changes to `vault/cli.py` — no new subcommand yet. Operator
invocation path is `python -c "from alfred.janitor.merge import
merge_entities; merge_entities(...)"` for now. A CLI shim can land
separately if/when the operator flow gets regular enough to deserve
one.

## Tests

`tests/test_merge.py` (NEW, 12 tests):

- `test_merge_retargets_frontmatter_single_value_links` — scalar `org:` retargets
- `test_merge_retargets_frontmatter_list_links` — `related: [...]` + `client:` retarget
- `test_merge_retargets_body_wikilinks` — prose `[[org/Loser]]` retargets
- `test_merge_deletes_loser_record` — loser file removed, winner survives
- `test_merge_absorbs_unique_winner_fields` — loser's `phone` copies to winner; winner's `website` preserved
- `test_merge_preserves_winner_identity_fields` — `type`/`name`/`created` immutable on winner
- `test_merge_appends_loser_body_with_provenance_marker` — `<!-- merged from ... -->` present
- `test_merge_logs_mutations_to_session_file` — every edit + delete lands in the audit trail
- `test_merge_refuses_same_winner_and_loser` — MergeError
- `test_merge_refuses_missing_loser` — MergeError
- `test_merge_refuses_missing_winner` — MergeError
- `test_merge_accepts_bracketed_and_suffixed_forms` — `[[org/Foo]]`, `org/Foo.md`, `org/Foo` all work

Fixture: temp vault with `org/Pocketpills` (winner) + `org/PocketPills`
(loser) + three downstream records (`person/Alice`, `project/Rx
Refill`, `note/Order Prep`) that link to the loser via `org:`,
`client:`, `related:`, and body prose. Mirrors the real 2026-04-13
PocketPills merge referenced in SKILL.md §3.DUP001.

Full suite after Q2: **160 passed** (148 after Q3 + 12 new).

## SKILL.md Follow-up (prompt-tuner territory)

The current SKILL.md §3.DUP001 "Operator-Directed Merge" procedure
describes steps 1–6 as if the janitor LLM runs them. With option (c)
this is no longer accurate — the merge is a Python call, not an
agent procedure. The SKILL should either:

- delete the step-by-step procedure and replace with "Operator will
  invoke `alfred.janitor.merge.merge_entities` — the janitor agent
  does not run merges directly", OR
- keep the procedure as a reference for the operator while clarifying
  the LLM's role is only the triage task (already covered in §3.DUP001
  Default Triage Flow).

Flagging for prompt-tuner; not blocking this commit. The SKILL text
remains **internally consistent** today — it explicitly says "Use
this procedure ONLY when the operator has already approved the merge
... Do NOT run it autonomously." Option (c) just makes "operator
approves" mean "operator calls Python" rather than "operator tells
the LLM to run it". Code ships; doc cleanup is a prompt-tuner pass.

## Alfred Learnings

- **Scope plumbing is a binding constraint on LLM-side option design.**
  `ALFRED_VAULT_SCOPE` is a subprocess env var, fixed at
  `_call_llm` invocation time. Any design that says "the agent
  switches scope mid-batch" is wrong. Designs that need wider scope
  for specific work must either (a) run a separate subprocess with
  a different env, or (b) move the work to Python where scope is
  moot. This came up in Q2; it will come up again. Mid-batch scope
  escalation is not a thing.
- **The `vault_ops` layer has no scope enforcement.** All scope
  gating lives in `vault/cli.py`. Python callers that bypass the
  CLI bypass scope — which is what we want for privileged operations
  like `merge_entities`, but it's a **capability**, not a bug. New
  contributors should know: scope is a CLI concern, not a data-layer
  concern.
- **Deterministic beats narrowly-scoped LLM for mechanical rewrites.**
  Merge retargeting has zero judgment (once winner chosen, rewrite
  every inbound link). Making an LLM do it would have required a
  wider scope, a separate pipeline stage, and a prompt explaining
  how to follow-link-sweep. Option (c) collapsed all three into
  `re.sub`.
- **`vault_edit` body_rewriter closure caveat.** The
  `body_rewriter` kwarg expects `Callable[[str], str]`. When
  iterating per-file, don't capture the `new_body` by reference in
  a list comprehension — each closure must capture loser/winner
  targets by value so per-file rewriters stay independent. Used an
  explicit `_make_rewriter(lt, wt)` factory to make this unambiguous.

## Commit

- Code + tests + this session note: (this commit)
- Previous: Option E Q3 body-write loophole (2b8ddbd)
