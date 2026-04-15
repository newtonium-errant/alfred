---
alfred_tags:
- software/alfred
- software/curator
- prompt/fix
created: '2026-04-15'
description: Update curator SKILL.md STEP 2a.1 recovery procedure to match the new
  vault_create hard-refusal contract from commit 4e10af3. Old guidance told the agent
  to delete the just-created file, which is no longer accurate because vault_create
  raises before writing.
intent: Align the curator's prompt-level dedup guidance with the hardened Python-layer
  error contract so agents reading the skill get accurate recovery steps
name: Curator SKILL Hard Refusal Contract Update
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Harden Vault Dedup at Python Layer 2026-04-15]]'
- '[[session/Curator Dedup Hard-Stop Fix 2026-04-15]]'
status: completed
tags:
- prompt-fix
- curator
type: session
---

# Curator SKILL Hard Refusal Contract Update — 2026-04-15

## Intent

When `4e10af3` landed the Python-layer harden (`vault_create` raises `VaultError` with structured `details` before writing anything, instead of writing-then-warning), the curator SKILL.md was left with stale STEP 2a.1 guidance that still told the agent to "delete the just-created file" on a near-match response. The file is no longer created in the first place, so that step is fiction. This commit rewrites STEP 2a.1 and its supporting reinforcements to match the real error contract.

## What Changed

Single file: `src/alfred/_bundled/skills/vault-curator/SKILL.md`, net-neutral rewrite (+47 / -41).

Three regions updated for consistency:

### STEP 2a.1 — HARD REFUSAL recovery procedure

Now a 5-step procedure:

1. Parse the error JSON, extract `details.canonical_path` (the existing record's actual on-disk path in canonical casing).
2. **Do NOT attempt to delete anything** — the file was never written. Explicit retraction of the old "just-created file" language so agents with cached older versions of the skill don't stay confused.
3. For standing entities (`org/person/project/location/asset/account`) that carry an `aliases` field, append the attempted variant casing to the canonical record via `vault edit --append`. For activity records (`note/task/conversation/event`) that do not define `aliases`, skip the alias step and just reuse the canonical path.
4. Merge any new information from the incoming content into the canonical record via `vault edit --set / --append / --body-append`.
5. Continue downstream record creation referencing the canonical wikilink with the casing as it appears on disk.

Added a one-sentence framing above the procedure explaining that Stage 2 of the Python pipeline already handles this error transparently, and this prompt-level rule is belt-and-braces for any direct `alfred vault create` call the agent makes outside the pipeline.

### Worked PocketPills example

Rewritten to show the real CLI interaction:
- `alfred vault create org "PocketPills" ...` → exits non-zero with `{"error": "Near-match exists: ...", "details": {"canonical_path": "org/Pocketpills.md", "reason": "near_match", "attempted_path": "org/PocketPills.md"}}`
- Agent extracts `canonical_path = "org/Pocketpills.md"` from the error details
- Agent runs `alfred vault edit "org/Pocketpills.md" --append 'aliases="PocketPills"'`
- Agent proceeds with note/task creation referencing `[[org/Pocketpills]]`

No `vault delete` step anywhere in the example.

### Anti-pattern block and file-ops pointer

Both reinforcements updated:
- Anti-pattern entry: "Don't ignore `Near-match exists` warnings" → "Don't ignore `Near-match exists` errors — the create already refused, proceed to `vault edit` on `details.canonical_path`. No file was written, do NOT attempt to delete."
- File-ops pointer (under "Creating a new record"): replaced references to `warnings[]` soft-warning language with exit-non-zero + `details.reason` hard-error language.

Grep across the doc confirms zero remaining references to `warnings[]`, "warnings array", "HAS ALREADY BEEN WRITTEN", or "soft warning" in the context of `vault create`.

## What's NOT in This Commit

- **Scaffold template `aliases` field.** The prompt-tuner flagged that only `person.md` under `_bundled/scaffold/_templates/` currently seeds an empty `aliases: []` list. STEP 2a.1 tells agents to append variants to `aliases` for `org/project/location/asset/account`, but those templates don't pre-seed the field, so the first append may need to create it. Functionally this works (`vault_edit --append` creates missing list fields), but it's aesthetically inconsistent. Deferred as a separate small scaffold template pass — not blocking.
- **Code changes.** This is a pure prompt update. No changes to `vault/ops.py`, `curator/pipeline.py`, `vault/cli.py`, or any other Python.

## Alfred Learnings

### Patterns Validated

- **Code contract changes must be paired with prompt contract updates.** Commit `4e10af3` changed the error shape that `vault_create` produces, but the SKILL.md recovery guidance was written against the old shape and nothing forced a sync. The prompt stayed wrong for ~2 hours of session time. For code-and-prompt coordinated fixes, the commit message of the code change should explicitly list the prompt files that need matching updates, so the followup doesn't get lost. (I flagged this followup in `project_next_session.md` during `4e10af3`'s session — that worked as a memo, but an inline TODO in the SKILL.md or a linter check would be more robust.)
- **Explicit retraction of old guidance helps agents with cached prompts.** STEP 2a.1 step 2 now contains "The old version of this rule told you to `vault delete` the just-created file — that guidance is obsolete." This is a small but real win: if an agent has an older copy of the skill in its context window (e.g. across a compaction boundary, or between a read and a re-read), the retraction tells it what the new correct behavior is. Pattern worth repeating for other contract changes: leave a one-line "this used to say X; it does not anymore" when rewriting safety-critical procedures.
