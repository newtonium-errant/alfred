---
alfred_tags:
- software/alfred
- software/curator
- bugfix/dedup
created: '2026-04-15'
description: Promote vault_create near-match from soft warning to hard refusal, make
  the live curator pipeline's Stage 2 entity resolution case-insensitive, and teach
  its VaultError handler the new error shape
intent: Close the class of case-variant duplicate bugs at the Python layer rather
  than relying on agent prompt discipline alone
name: Harden Vault Dedup at Python Layer
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Curator Dedup Hard-Stop Fix 2026-04-15]]'
- '[[session/Dedup Layers and Surveyor Tuning 2026-04-14]]'
status: completed
tags:
- dedup
- curator
- pipeline
- bugfix
type: session
---

# Harden Vault Dedup at Python Layer — 2026-04-15

## Intent

The earlier session today (commit `e6aa461`, session note "Curator Dedup Hard-Stop Fix") landed three fixes aimed at the soft-warning + prompt-hard-stop pattern. That work was correct but incomplete: the real case-variant failure originated in the **live** curator path — `src/alfred/curator/pipeline.py::_resolve_entities` — whose dedup check was case-sensitive and whose `VaultError` handler only recognized `"already exists"` collisions. This session hardens both layers so a case-variant duplicate literally cannot land in the vault, regardless of what the LLM asks for.

## What We Found

While auditing callers of `vault_create` for the harden change, the builder flagged a latent regression risk in `pipeline.py:_resolve_entities`. That led to a broader look at the file — and the realization that **`curator/pipeline.py::run_pipeline` is the live curator path**, imported by `curator/daemon.py:199`. It's a 4-stage pipeline:

- **Stage 1 (LLM, `prompts/stage1_analyze.md`)** — analyzes the inbox file, writes one note, returns an entity manifest as JSON
- **Stage 2 (pure Python, `_resolve_entities`)** — walks the manifest, calls `_entity_exists` to check if each entity is already in the vault, creates via `vault_create` otherwise
- **Stage 3 (pure Python, `_interlink`)** — wires wikilinks between the note and the resolved entities
- **Stage 4 (LLM, `prompts/stage4_enrich.md`)** — fills entity bodies and frontmatter

Stage 2 is where entity creation actually happens. The `_entity_exists` check at `pipeline.py:344` was a case-**sensitive** `Path.exists()`:

```python
candidate = vault_path / directory / f"{name}.md"
if candidate.exists():
    return f"{directory}/{name}.md"
return None
```

So if Stage 1's LLM returned `"name": "PocketPills"` and the vault had `org/Pocketpills.md`, `_entity_exists` returned None, Stage 2 fell through to `vault_create`, and a duplicate was born. The SKILL.md hardening we did earlier today was addressing the wrong path — STEP 2a.1 guides the agent on what to do when `vault_create` returns a warning, but Stage 2's Python code doesn't read SKILL.md and has its own error handling that only recognized `"already exists"` strings.

The earlier verification test (dropping an inbox file into `vault/inbox/`) passed only because Stage 1's LLM happened to return the manifest with lowercase "Pocketpills" spelling, which matched the canonical casing on first try. That path was the happy path — the adversarial case-variant path was never exercised. Good luck, not a verified fix.

## What Changed

Three atomic edits across two files (plus the one-file prompt update from earlier today that's still valuable as belt-and-braces for the agent-facing error):

### `src/alfred/vault/ops.py` — hard refusal in `vault_create`

`_check_near_match` now returns a `(canonical_path, message)` tuple instead of a plain warning string. In `vault_create`, the check runs **before any file write** — if it fires, it emits `log.error("vault_create.refused", ...)` and raises `VaultError` with a new structured `details` dict:

```python
VaultError(
    "Near-match exists: 'org/Pocketpills.md' ... Use vault_edit on the existing record instead of creating a duplicate.",
    details={
        "canonical_path": "org/Pocketpills.md",
        "reason": "near_match",
        "attempted_path": "org/pocketpills.md",
    },
)
```

`VaultError` itself grew an optional `details: dict | None` kwarg to carry structured metadata — the rest of `VaultError`'s callers are unaffected because it defaults to None. The directory-mismatch warning in `vault_create` stays as a soft warning (still writes) — only near-match becomes a hard refusal. The old soft-warning branch lower in the function was removed.

### `src/alfred/vault/cli.py` — surface `details` in the JSON error response

Two surgical edits: `_error()` helper gains an optional `details` kwarg and includes it as a top-level `"details"` key in the JSON output; `cmd_create`'s `except VaultError` passes `details=getattr(e, "details", None)` so the structured info reaches the caller:

```json
{
  "error": "Near-match exists: 'org/Pocketpills.md' ...",
  "details": {
    "canonical_path": "org/Pocketpills.md",
    "reason": "near_match",
    "attempted_path": "org/pocketpills.md"
  }
}
```

Exit code is non-zero. Other CLI handlers unchanged — only `cmd_create` currently populates `VaultError.details`.

### `src/alfred/curator/pipeline.py` — case-insensitive Stage 2 + near-match fall-through

`_entity_exists` now walks the type directory with `glob("*.md")` and compares stems via `.casefold()`. When a match is found it returns the **actual on-disk path**, preserving the canonical casing — so downstream stages always reference the real file, not the LLM's requested name. This is the primary defense: the near-match will almost never fire in Stage 2 because `_entity_exists` finds the canonical first.

`_resolve_entities`'s `except VaultError` branch now recognizes the new error shape:

```python
except VaultError as e:
    details = getattr(e, "details", None) or {}
    if details.get("reason") == "near_match" and details.get("canonical_path"):
        canonical_path = details["canonical_path"]
        resolved[entity_key] = canonical_path
        log.info("pipeline.s2_entity_near_match_reused", ...)
        continue
    # ... legacy "already exists" fallback retained for exact-match safety
```

This is the secondary defense: if a TOCTOU race between `_entity_exists` and `vault_create` ever slips a near-match through, Stage 2 reads the canonical path from the error `details` and uses that as the resolved path. No entity is dropped from `resolved{}` — Stage 3 still gets a valid wikilink target.

## Verification

### `_entity_exists` direct unit tests

All four cases pass against the live vault:

- `_entity_exists(vault, "org", "Pocketpills")` → `"org/Pocketpills.md"` (exact)
- `_entity_exists(vault, "org", "PocketPills")` → `"org/Pocketpills.md"` (the key case — uppercase-requested finds lowercase-canonical)
- `_entity_exists(vault, "org", "POCKETPILLS")` → `"org/Pocketpills.md"` (screaming caps still hits)
- `_entity_exists(vault, "org", "NonExistentOrg12345")` → `None`

### `_resolve_entities` adversarial test

Manually constructed manifest: `[{"type": "org", "name": "PocketPills", "description": "Test", "fields": {}}]`. `_normalize_name` does NOT lowercase for `org` type (only `person`), so the uppercase name actually reaches `_entity_exists`. Result: `resolved = {"org/PocketPills": "org/Pocketpills.md"}`. Files in `vault/org/*ocketpills*` before: `["Pocketpills.md"]`; after: `["Pocketpills.md"]` — zero new files. The Stage 2 primary defense held.

Also monkeypatched `_entity_exists` to return `None`, forcing the near-match `VaultError` path in `vault_create`. The secondary defense fired: `pipeline.s2_entity_near_match_reused` logged, `resolved` still mapped to `org/Pocketpills.md`, no duplicate written.

### End-to-end CLI hard refusal

```
$ ALFRED_VAULT_PATH=... ALFRED_VAULT_SCOPE=curator alfred vault create org "pocketpills" --set type=org --set name="pocketpills"
{"error": "Near-match exists: 'org/Pocketpills.md' ... Use vault_edit on the existing record instead of creating a duplicate.", "details": {"canonical_path": "org/Pocketpills.md", "reason": "near_match", "attempted_path": "org/pocketpills.md"}}
$ echo $?
1
```

No new file written to `vault/org/`. Exit 1. Structured details present.

## Alfred Learnings

### New Gotchas

- **Verification test needs to exercise the failure path, not the happy path.** This session's first verification (the synthetic inbox drop earlier today) "passed" only because Stage 1's LLM happened to normalize the entity name before Stage 2 saw it. The test never actually hit the case-variant branch it was meant to prove. Lesson: for dedup tests, either (a) unit-test the Python dedup function directly with adversarial inputs, or (b) monkeypatch the upstream stage so the adversarial input is guaranteed to reach the function under test. Shipping a fix on the strength of a happy-path test is a silent regression waiting to happen.
- **Pre-existing uncommitted work in the working tree can bleed into scope-focused commits.** When building `cli.py` for this commit, the builder's diff included ~30 lines of Layer 3 triage-id scaffolding that had been sitting in the working tree across multiple prior commits. It was unrelated to the harden work, depended on an untracked file (`src/alfred/janitor/triage.py`), and would have introduced a latent `ImportError` if committed. Resolution was to back up the full dirty file, `git checkout HEAD --` the path, re-apply only the two harden hunks via Edit, commit, then restore the backup so the Layer 3 work stays in the working tree for its own future commit. **Lesson for CLAUDE.md:** when a session touches a file that already has unrelated dirty state from a prior session, audit the diff before staging and surgically stage only the session's own hunks.

### Patterns Validated

- **Python-side dedup first, prompt-side as belt-and-braces.** Earlier today we fixed dedup at the prompt layer (SKILL.md STEP 2a.1), and it helped — but the live path turned out to be Python. Fixing `_entity_exists` at the Python layer closes the class of bug unconditionally: no LLM prompt can cause it now. The SKILL.md STEP 2a.1 rule still has value as a safety net for any CLI caller, but it's no longer load-bearing for Stage 2. The layering ordering should always be: code-level invariant first, prompt-level guidance second.
- **Structured error details are the right interface between a hardening gate and its callers.** Raising `VaultError(message, details={"reason": "near_match", "canonical_path": ...})` lets every caller recover intelligently without parsing message strings. The `_resolve_entities` branch that reads `details.reason == "near_match"` is cleaner than any substring match could be. When adding a new error class or condition to a shared gate, extend `details` rather than encoding info in the message.
- **Two-layer defense for class-of-bug fixes.** Case-insensitive `_entity_exists` is the primary defense (prevents the trigger). The hardened `vault_create` raise is the secondary defense (catches anything the primary misses, including TOCTOU races or future callers that skip the pre-check). Neither alone is sufficient: primary-only leaves new callers exposed; secondary-only lets happy-path callers drop entities on race. Both together is cheap and audit-clean.

### Corrections

- **The earlier session's framing ("STEP 2a.1 is the dedup fix") was incomplete.** That prompt change would not have prevented last night's actual failure because last night's failure happened in Stage 2 Python code before any agent prompt was consulted. The harden committed today is the real fix for the class of bug. The prompt change stays because it's still correct for any direct CLI caller (e.g. a future janitor merge path that calls `vault_create` through the agent subprocess).

### Missing Knowledge

- **The existence of the 4-stage pipeline itself.** CLAUDE.md describes the curator as "agent delegates work via `alfred vault` CLI," which is accurate for part of the flow but obscures the Python-side Stage 2/3 dedup and interlink work. The live flow is hybrid — Stage 1 and Stage 4 use the agent, Stage 2 and Stage 3 are pure Python. This is closer to the alfred.black named-stage architecture than the monolithic single-call picture the docs give. Candidate for a CLAUDE.md update: add a "curator pipeline stages" subsection under Architecture.
- **Unit-test harness for the curator pipeline stages.** Verification today was done by importing `_resolve_entities` and calling it manually from a shell one-liner against the live vault. That works but isn't repeatable and risks live-vault side effects. A fixture-based harness with a disposable temp vault for each stage is future work — flagged, not done.

## Follow-ups not done

1. **Update curator SKILL.md STEP 2a.1** to match the new error contract. The current prompt tells the agent to "delete the just-created file" on near-match, which is no longer accurate — `vault_create` now raises BEFORE writing. The prompt should instead tell the agent to read `details.canonical_path` from the error and pivot to `vault_edit`. Deferred because: (a) it's a prompt-tuner task, not builder, (b) the SKILL.md text is now belt-and-braces for Stage 2's primary defense and less load-bearing than before, and (c) it shouldn't block committing the code fix. Flagged for a future short prompt-tuner pass.
2. **The `src/alfred/janitor/triage.py` untracked file and the pre-existing cli.py/scope.py Layer 3 scaffolding** stay in the working tree, unchanged from session start. Those are from a prior session's unfinished Layer 3 work and were explicitly out-of-scope for the harden commit.
3. **The `alfred.black`-style "entity resolution as its own stage" design discussion** — originally queued as a design conversation, now moot: we already have a real Stage 2 in code. The discussion can instead be concrete: "is our current Stage 2 good enough, or does it need further refactoring?" Answer after today's harden: Stage 2 is now case-insensitive, has a structured fall-through path, and logs its decisions at info level. It's in good shape. A future improvement worth considering is adding an explicit per-entity `search_by_aliases` check that looks at the canonical record's `aliases` list — right now we only match on stem name, not alias. But that's a nice-to-have, not a bug.
