---
alfred_tags:
- software/alfred
- software/janitor
- feature/layer3
created: '2026-04-15'
description: Land the Layer 3 janitor triage queue — an advisory surface where the
  janitor agent surfaces ambiguous DUP001 dedup candidates as triage task records
  for human review, with deterministic triage IDs, scope-gated creates, and a soft+hard
  idempotency layer. No auto-merge loop.
intent: Give the janitor a way to surface "should these two records be merged?"
  decisions to the human without ever auto-merging, and without spamming the queue
  on successive sweeps when the same candidates reappear
name: Layer 3 Janitor Triage Queue
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Dedup Layers and Surveyor Tuning 2026-04-14]]'
- '[[session/Stop Surveyor Session Drift 2026-04-15]]'
- '[[session/Harden Vault Dedup at Python Layer 2026-04-15]]'
status: completed
tags:
- janitor
- triage
- layer3
- feature
type: session
---

# Layer 3 Janitor Triage Queue — 2026-04-15

## Intent

The janitor's structural scanner detects DUP001 (possible duplicate) issues that cannot be auto-fixed safely because they require human semantic judgment. Two records like `org/Acme Corp` and `org/Acme Corporation` might legitimately describe the same entity, or might not — auto-merging either way risks silent data loss. Before today, DUP001 just wrote a `janitor_note` flag on the candidates and hoped the human would notice.

Layer 3 surfaces these decisions as proper **triage task records**: regular `task/` files with a distinctive `alfred_triage: true` frontmatter flag. A human reads the task, inspects the candidates, decides, and either runs the operator-directed merge procedure manually or closes/deletes the task. There is **no auto-merge loop and no auto-resolve** — Layer 3 is purely advisory.

This commit lands a feature that was scaffolded in a prior session (across the working tree across multiple sessions, never committed), reviewed by team lead today against the two original tentative design decisions, polished, and shipped atomically.

## Design Decisions Confirmed

Two tentative decisions from the original Layer 3 design were on the deferred list pending team-lead reconfirmation. Both were validated by the review and remain in force:

### Decision 1 — Agent creates triage task records, Python tracks IDs

The janitor agent creates triage task records via `alfred vault create task ...` using its scope. **Python code does NOT create triage records directly.** Python's job in Layer 3 is:

1. Define the deterministic ID scheme (`compute_triage_id`) so the same candidate set always produces the same ID regardless of discovery order.
2. Expose existing open triage tasks to the agent via prompt context so the agent can skip already-queued items.
3. Track surfaced IDs in janitor state (`triage_ids_seen`) as a hard idempotency layer behind the soft prompt-side check.

Why agent-creates rather than Python-creates: the janitor was already an agent-writes-directly tool, and routing all vault writes through one path keeps the audit log coherent. Python is the deterministic side (ID computation, scope enforcement, state tracking). The agent is the natural-language side (deciding what kind of task title to write, which body fields to include).

### Decision 2 — Advisory-only, no auto-merge

The janitor never merges duplicates automatically. The Layer 3 "Default Triage Flow" in SKILL.md is the autonomous-sweep behavior; the existing "Operator-Directed Merge" procedure is gated behind explicit human approval (the sweep context must contain an operator merge instruction — a resolved triage task, an explicit action log entry, or a direct command). Several defenses in depth enforce this:

- `triage.py` module docstring is explicit: "No auto-merge, auto-resolve, or auto-edit of candidate records."
- The module exposes only pure functions — `compute_triage_id`, `collect_open_triage_tasks`, `format_open_triage_block`. Nothing in `triage.py` calls `vault_create`, `vault_edit`, `vault_move`, or `vault_delete`.
- SKILL.md's "What you MUST NOT do while a DUP001 triage task is pending or being created" enumerates the prohibited actions: no auto-merge, no edit/rename/retag of candidates, no delete, no `status` change on triage tasks (that field is human-only).

## What Shipped

Ten files in one atomic commit (~503 LOC).

### `src/alfred/janitor/triage.py` — new module (untracked → tracked)

The Layer 3 core. Defines the contract, the deterministic ID scheme, and the prompt-context helpers.

- `TRIAGE_KINDS` — `{"dedup", "orphan", "broken_link", "ambiguous_type"}`. Only `dedup` is implemented for Layer 3; the rest are reserved names for future kinds.
- `compute_triage_id(kind, candidates) -> str` — order-independent SHA256 truncated to 12 hex chars, prefixed with the kind. Same set of candidates in any permutation produces the same ID. Wikilink (`[[type/Name]]`) and bare path (`type/Name`, `type/Name.md`) forms both accepted; normalized via `_normalise_candidate`. Raises `ValueError` on empty kind or empty candidates.
- `collect_open_triage_tasks(vault_path) -> list[dict]` — walks `vault/task/`, returns triage tasks where `alfred_triage` is truthy AND `status == "open"`. Sorts by `(triage_id, path)` for stable prompt output. Skips parse failures with a log entry.
- `format_open_triage_block(tasks, seen_ids=None) -> str` — formats the list as a Markdown context block injected into the janitor agent prompt as `## Existing Open Triage Tasks`. With the optional `seen_ids` kwarg (the team-lead-approved wiring added late this session), it ALSO renders a parallel `## Triage IDs Already Surfaced (do not re-create)` block from `sorted(seen_ids - open_ids)`. The second block exists to remind the agent of historically-surfaced IDs whose tasks have been closed/deleted, so the human's "no, leave it" decision isn't re-litigated.

### `src/alfred/janitor/state.py` — `triage_ids_seen` set

Added a `set[str]` field `triage_ids_seen` to `JanitorState`, persisted as a sorted JSON list (deterministic on disk). New methods `has_seen_triage(triage_id)` and `mark_triage_seen(triage_id)`. Logged in `state.loaded` so observability includes the count. Atomic save via the existing `.tmp + os.replace` path.

### `src/alfred/janitor/daemon.py` — wiring + post-run-fix triage ID recording

- Imports `frontmatter`, `collect_open_triage_tasks`, `format_open_triage_block`, and the new private helper.
- `run_sweep` calls `collect_open_triage_tasks(vault_path)` once per sweep before the batch loop, then passes the rendered block (with `seen_ids=state.triage_ids_seen`) into `build_sweep_prompt` via the new `open_triage_block` kwarg.
- New helper `_record_triage_ids_from_created(created_paths, vault_path, state)` walks each batch's created paths, filters to `task/*.md` files with `alfred_triage: true` in frontmatter, extracts `alfred_triage_id`, and calls `state.mark_triage_seen()`. Wired into both the pipeline path and the legacy path. Empty-batch is a no-op for-loop. State is saved once at the natural end of `run_sweep`.

### `src/alfred/janitor/backends/__init__.py` — prompt builder kwarg

`build_sweep_prompt` gains an optional `open_triage_block: str = ""` kwarg. When non-empty, the rendered prompt includes a `\n{open_triage_block}\n---\n` section between the affected records and the fix instructions. When empty (legacy callers), the section is omitted entirely.

### `src/alfred/janitor/backends/{cli,http,openclaw}.py` — pass-through

Each backend's `run_fix` accepts `open_triage_block: str = ""` and forwards it to `build_sweep_prompt`. Three identical 9-line additions, one per backend.

### `src/alfred/vault/scope.py` — `triage_tasks_only` permission

The janitor's `create` permission flips from `False` to `"triage_tasks_only"`. `check_scope` gains an optional `frontmatter: dict | None = None` kwarg used by the new permission check. The check fires when `permission == "triage_tasks_only"`:

- If `record_type != "task"`, raise `ScopeError`.
- If the frontmatter dict is missing or `frontmatter.get("alfred_triage")` is falsy, raise `ScopeError`. Fails closed when the caller doesn't pass frontmatter at all.

This is the hard idempotency layer for "the agent cannot accidentally create a regular task." The flag must be present and truthy for the create to succeed.

### `src/alfred/vault/cli.py` — `cmd_create` passes frontmatter, new `triage-id` subcommand

- `cmd_create` reordered to parse `set_fields` before calling `check_scope`, then passes `frontmatter=set_fields` so the new scope rule sees the `alfred_triage` flag.
- New `cmd_triage_id` subcommand wraps `compute_triage_id` and emits `{"triage_id": ..., "kind": ..., "candidates": [...]}`. Wired into `build_vault_parser` and `handle_vault_command` dispatcher. The agent uses this from the SKILL.md instructions to compute the deterministic ID before checking the open-triage block.

### `src/alfred/_bundled/skills/vault-janitor/SKILL.md` — Default Triage Flow

The DUP001 handling section was rewritten from "default fix: NEVER merge automatically; flag with `janitor_note`" to a full "Default Triage Flow" with:

- A **machine-vs-human discriminator** rule: only emit triage when both candidates are entity types (`org/, person/, note/, project/, location/, account/, asset/, task/, event/, input/, conversation/, process/, run/`). Do NOT emit triage for learn types (`contradiction/, assumption/, decision/, constraint/, synthesis/`) — those carry legitimate human semantic pointers with their own confidence fields and aren't dedup candidates. Includes a **`KNOWN_TYPES` sync note** pointing at `src/alfred/vault/schema.py` as the source of truth so future schema changes know to update SKILL.md.
- A 4-step procedure: compute the deterministic ID via `alfred vault triage-id`, scan the rendered `## Existing Open Triage Tasks` block for an exact ID match, create the triage task only if no match, log as `FLAGGED`.
- A worked Acme Corp / Acme Corporation example with the exact CLI invocations.
- An explicit list of **what the agent MUST NOT do** while a DUP001 triage task is pending: no auto-merge, no edit/rename/retag of candidates, no delete, no `status` change.
- The pre-existing **Operator-Directed Merge** procedure is preserved unchanged but explicitly gated as an escalation path.
- Title separator unified on the dash form (`Triage - <name>`) for both filename and frontmatter `name`, replacing an earlier inconsistency where the filename used dash and the frontmatter used colon. Colons in filenames are problematic on some filesystems and the matching dash form keeps name and filename consistent for janitor frontmatter sweeps.

## Verification

No live daemon test (Layer 3 is incomplete in the working tree until this commit lands, and starting a janitor sweep against the live vault could produce real triage tasks before the commit was in HEAD). Verification is import-level + unit smoke tests:

**Import check:** all of `daemon._record_triage_ids_from_created`, `state.JanitorState`, `triage.{compute_triage_id, format_open_triage_block, collect_open_triage_tasks}`, `backends.build_sweep_prompt`, and `vault.scope.{check_scope, ScopeError}` import cleanly. No syntax errors, no circular imports.

**`compute_triage_id` smoke test:**
- Order-independence: `compute_triage_id("dedup", ["org/Acme Corp", "org/Acme Corporation"]) == compute_triage_id("dedup", ["org/Acme Corporation", "org/Acme Corp"])` → pass
- Wikilink normalization: `["[[org/Acme Corp]]", ...]` produces same ID as bare paths → pass
- Kind-in-hash: `compute_triage_id("orphan", same_candidates) != compute_triage_id("dedup", same_candidates)` → pass

**`check_scope` smoke test for `triage_tasks_only`:**
- `check_scope("janitor", "create", record_type="task", frontmatter={})` → raises `ScopeError` (missing `alfred_triage`)
- `check_scope("janitor", "create", record_type="task", frontmatter={"alfred_triage": True})` → returns cleanly
- `check_scope("janitor", "create", record_type="note", frontmatter={"alfred_triage": True})` → raises `ScopeError` (record type isn't `task`)

All three branches of the new permission check fire correctly.

## Followups Not in This Commit

1. **No automated test harness.** This project has no `tests/` directory, no pytest config, no CI. Bootstrapping a test harness is a bigger decision than Layer 3 polish — it requires picking pytest vs unittest, deciding on test layout, possibly adding dev deps. The smoke tests above are run by hand at commit time. Flagged as a separate followup; the team lead will decide whether to bootstrap pytest in a dedicated session.
2. **`writer._write_atomic` `mark_pending_write` race in the surveyor** — narrow, unobserved in practice, needs a small redesign to key on `(path, hash)` rather than `path`. Unrelated to Layer 3 but on the deferred list.
3. **`inbox/processed/` permanent surveyor exclusion** — policy decision, not a fix. Currently excluded; whether to re-include depends on whether some future consumer expects those emails indexed by the surveyor.
4. **Layer 4 (auto-merge loop)** is explicitly NOT planned. Layer 3 is the human-in-the-loop endpoint. If a future iteration wants auto-merge for high-confidence dedup pairs, it would need its own design pass — not a natural extension of this code.

## Alfred Learnings

### Patterns Validated

- **Deterministic IDs over content hashing for advisory queues.** `compute_triage_id` keys on `kind + sorted(normalised_candidates)`, not on the candidates' content. This means two records being modified between sweeps doesn't change the triage ID — the same dedup question keeps the same name, even if one of the candidates' bodies has been edited in the interim. That's the right behaviour: the question is "are these two records duplicates?", not "are these two specific snapshots duplicates?". Generalisable: when you're identifying a *question* rather than a *state*, hash the question's identity (the candidate paths) not the state (the file contents).
- **Soft + hard idempotency layering.** Layer 3 has three layers protecting against duplicate triage tasks: (1) the agent reads the rendered `## Existing Open Triage Tasks` block in its prompt and skips matching IDs; (2) the agent ALSO reads the parallel `## Triage IDs Already Surfaced` block to catch closed/deleted historicals; (3) the scope rule rejects malformed `vault create task` calls without `alfred_triage: true`. The first two are soft (prompt-level, can be ignored if the agent makes a mistake), the third is hard (Python scope enforcement, cannot be bypassed). Each layer catches a different failure mode. Worth doing whenever you have an idempotency-critical operation and the soft layer is where most of the work happens.
- **State-based hard idempotency must be wired or deleted.** During the team-lead review of the original Layer 3 scaffolding, I noticed that `JanitorState.mark_triage_seen` existed and `triage_ids_seen` was persisted, but **nothing in the code actually called `mark_triage_seen`**. Dead code. Two options: wire it (do what the docstring says) or delete it (rely on the disk-walk in `collect_open_triage_tasks`). Wiring it was the right call because the disk walk only sees open tasks, so closed/deleted historical IDs would be re-flagged on the next sweep — spam after the human had already decided. The wiring is small (~30 lines in `daemon.py`) and the `format_open_triage_block` extension exposes the historical IDs to the agent prompt. Generalisable: state fields that are persisted but never written are a silent design bug — either wire them or remove them, never leave them in limbo.

### New Gotchas

- **`_normalise_candidate` accepts wikilink, bare path, and `.md` suffix forms.** This is intentional — different callers (the SKILL.md examples, the `triage-id` CLI subcommand, the prompt-context block) use different conventions, and normalizing at hash time means all of them produce the same ID. But it's an invariant easy to break: if a future caller passes `"org/Acme Corp.md"` and the normalizer doesn't strip the suffix correctly, the hash changes silently. Worth noting in the docstring (already there) and in any future test pass.
- **`check_scope` fails closed on missing frontmatter.** The new `triage_tasks_only` permission requires `frontmatter` to be passed AND for it to contain `alfred_triage: true`. If a caller forgets to pass `frontmatter`, the check raises rather than allowing through. This is the right default (security-critical operations should never be permissive when their input is incomplete) but worth knowing if you're refactoring `check_scope` callers.

### Missing Knowledge

- **There is still no formal test harness.** Layer 3 ships with hand-run smoke tests instead of automated ones. Every future change to `compute_triage_id`, `check_scope`, or the `_record_triage_ids_from_created` walk would benefit from a fixture-based test that asserts the contract. Bootstrapping pytest is a small commit's worth of work and would catch regressions on the next refactor. Flagged for a dedicated session.
