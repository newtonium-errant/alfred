---
alfred_tags:
- software/alfred
- software/curator
- bugfix/dedup
created: '2026-04-15'
description: Root-cause and fix the curator case-variant dedup failure exposed by
  the 2026-04-15 PocketPills test, covering logging sink, SKILL.md hard-stop rule,
  and inbox .lock re-processing leak
intent: Make the curator's existing near-match safety net actually enforce dedup,
  and close the side channel where .lock sidecars were re-processed as inbox entries
name: Curator Dedup Hard-Stop Fix
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Dedup Layers and Surveyor Tuning 2026-04-14]]'
- '[[session/Catch-Up Commit Housekeeping 2026-04-15]]'
status: completed
tags:
- dedup
- curator
- bugfix
type: session
---

# Curator Dedup Hard-Stop Fix — 2026-04-15

## Intent

The 2026-04-15 PocketPills case-variant test (dropped overnight) produced duplicate records across `org/`, `note/`, and `task/`, proving last session's dedup layers were not actually enforcing anything at the failure boundary. This session was the RCA + targeted fix.

## What We Found

An earlier RCA (held in-conversation) isolated three independent bugs rather than one:

1. **`vault_create.near_match` warning had no log sink.** `src/alfred/cli.py::cmd_vault` was the only `cmd_*` handler that did not call a logging-setup helper before dispatching. The check in `src/alfred/vault/ops.py::_check_near_match` fires correctly and puts the warning string into the JSON response's `warnings[]` array, but `log.warning("vault_create.near_match", ...)` went to an unconfigured structlog sink and produced zero output in any log file. So we had no audit trail for how often the safety net tripped — just a silent pass-through.

2. **Curator SKILL.md did not treat `warnings[]` as blocking.** STEP 2a's dedup guidance (added last session) told the agent to search first and reuse, but nothing in the prompt told the agent what to do when `vault create` came back with a near-match warning. The agent saw the warning, ignored it, and moved on. Worse, the warning is emitted _after_ the new file is already written to disk — the agent has to both stop AND delete the just-created duplicate, a subtlety not covered anywhere.

3. **`.lock` sidecar files re-processed as fresh inbox entries.** `src/alfred/curator/daemon.py::_claim_file` writes a `{inbox_file}.lock` sidecar next to each inbox file during processing as a cross-process lock. Neither `InboxHandler._handle()` nor `InboxWatcher.full_scan()` filtered on suffix, so on the next scan the `.lock` was picked up as a new file and fed through the full curator pipeline a second time. The 03:54:14 PocketPills test ran normally, then at 03:55:17 the watcher grabbed the sibling `.lock` and re-invoked the agent against the same content — producing a parallel set of lowercase-variant note+task duplicates on top of the first pass's uppercase ones.

## Timeline correction from the RCA

The problem framing started with "something corrupted `org/PocketPills.md` last night," but the audit log and surviving files told a different story. Both `org/PocketPills.md` (uppercase) and `org/Pocketpills.md` (lowercase) were created in the _same Apr 13 curator run_, 23 seconds apart, from a single `care@pocketpills.ca` email. The manual merge yesterday consolidated to the lowercase canonical. The Apr 15 test then created NEW case-variant note and task duplicates on top of an already-cleaned-up org. So the test exposed the dedup failure at the entity-resolution level, not at the org level — the org was already fine.

## What Changed

Three minimal, single-scope edits:

- **`src/alfred/cli.py`** — `cmd_vault` now loads unified config, resolves the log dir, and calls `setup_logging(level, log_file="./data/vault.log", suppress_stdout=True)` before dispatching. `suppress_stdout=True` is load-bearing: the vault CLI emits JSON on stdout that the agent parses, so any log handler leaking to stdout would break the contract. Wrapped in `try/except` so logging setup failure cannot break the vault CLI contract. New log sink: `data/vault.log`, atomically appended from any daemon's subprocess calls.

- **`src/alfred/_bundled/skills/vault-curator/SKILL.md`** — new **STEP 2a.1: HARD STOP** section immediately after the existing STEP 2a dedup guidance. Covers the subtlety that `vault create` writes the file before emitting the warning, so the recovery is: STOP → `vault delete` the just-created file → extract canonical path from the warning → `vault edit --append aliases=...` on the canonical → reference canonical downstream. Includes a worked PocketPills example showing the full sequence. The rule is reinforced three more times — in the File Operations Guide pointer, in the anti-patterns block, and in the worked example — because one mention of STEP 2a was empirically not enough to change agent behavior.

- **`src/alfred/curator/watcher.py`** — two-line filter: `InboxHandler._handle()` and `InboxWatcher.full_scan()` both skip any file with `suffix == ".lock"`. Option A (filter at the scanner) chosen over Option B (redesign the locking primitive) because the `.lock` sidecars are correctly cleaned up by `_release_file()` in the daemon's `finally` block — the only failure mode is the scanner seeing them, which is now closed.

## Verification

Restarted the daemon (pid 22849) to pick up the watcher fix, then dropped a synthetic test file `email-test-20260415-dedup-verification.md` into `vault/inbox/` referencing "PocketPills" (uppercase) deliberately. Curator processed it in one pass (no `.lock` re-trigger). Outcome:

- Audit log (2026-04-15T14:07:09): one `create` (`note/Pocketpills Dedup Verification Test 2026-04-15.md`, lowercase canonical casing) + one `modify` on `org/Pocketpills.md`. No attempted uppercase create, no delete/recreate cycle, no parallel pass.
- `org/Pocketpills.md` `aliases` now contains `PocketPills`, `Pocket Pills`, `pocketpills` — the agent correctly merged the incoming uppercase spelling as an alias on the canonical record instead of creating a new file.
- No `vault_create.near_match` entry fired during this run, because the agent's STEP 2a search caught the existing record before `vault_create` was ever called. That is the ideal outcome — the safety net is still there, we just didn't need it.
- `data/vault.log` exists and contains the builder's earlier standalone verification warning, proving the sink works; during the real run the safety net was preempted by the search path as designed.

## Cleanup

Last night's contamination removed:

- `note/PocketPills Prescription Refill Reminder 2026-04-15.md` — deleted
- `task/Refill Prescription at PocketPills.md` — deleted
- `note/Pocketpills Prescription Refill Reminder 2026-04-15.md` — kept (canonical), task wikilink appended to `related`
- `note/Pocketpills Dedup Verification Test 2026-04-15.md` — deleted (test artifact)
- `task/Refill Prescription at Pocketpills.md` — kept (canonical)
- `account/PocketPills Pharmacy Account.md` — untouched, no case variant exists, correct brand casing
- `org/Pocketpills.md` — untouched, still carries the LINK001 `janitor_note`

The `.lock` sidecar in `vault/inbox/processed/email-live-20260415-034500-PocketPills-Refill-Reminder-CaseVariant-Test.md.lock` was left in place — it's in the processed tree, which the scanner never reads.

## Alfred Learnings

### New Gotchas

- **Any `alfred vault` subprocess that emits structured output on stdout MUST configure logging with `suppress_stdout=True`.** `_setup_logging_from_config` defaults to adding a stdout handler, which silently breaks the JSON-over-stdout contract of the vault CLI. `cmd_vault` is the only stdout-sensitive handler today; if any future CLI subcommand returns structured data on stdout, it needs the same treatment. Candidate CLAUDE.md line.
- **Curator artifacts must be invisible to the next scan pass.** This is the second time a curator-produced sidecar has leaked back into the scanner's input list (first was mutation-log session files, now `.lock` files). The pattern is: the curator creates a side-file during processing, the file sits next to the real inbox entry, and the next scan treats it as new work. The class of bug is structural, not incidental — worth flagging in `.claude/agents/builder.md` as a review checklist item for any future curator side-artifact.

### Patterns Validated

- **Single-prompt dedup guidance wasn't sufficient.** STEP 2a was added last session and looked fine on paper, but the agent still produced duplicates. The fix that worked was restating the rule _four_ times across the SKILL.md (main rule body, worked example, file-ops pointer, anti-pattern block). Alfred.black's named-stage curator pipeline (analyse → entity resolution → interlink → enrich) is looking increasingly like the right structural answer — separating entity resolution into its own prompt with one job would make dedup enforcement unmissable instead of something the agent has to remember across a long single-shot prompt. Not built yet, flagged for design.
- **Warnings in a JSON response are not hard gates.** The near-match check in `ops.py` was designed as a soft warning that the caller "verifies before continuing." When the caller is an LLM agent under a time budget, "verify before continuing" is equivalent to "proceed." Defense in depth would be to make `_check_near_match` return a hard refusal (non-zero exit, canonical path in the error) so the duplicate literally cannot be written. Deferred — the prompt-level fix is empirically sufficient for now, and the code-level change has a bigger behavioral contract.
- **Daemon restart was required to pick up `watcher.py` changes.** `SKILL.md` is re-read on each agent dispatch (importlib.resources in editable install), so prompt changes do not need a restart. Code changes to the daemon modules DO. Worth noting for operational runbooks.

### Corrections

- The RCA's initial framing of "how did `org/PocketPills.md` disappear overnight" was wrong. The file was manually merged during yesterday's cleanup (outside the vault CLI, which is why `vault_audit.log` had no record of it). The Apr 15 test landed on an already-cleaned-up org and broke new ground on note/task case-variants. Future RCAs on overnight contamination should explicitly ask "was there a manual cleanup between the last audit entry and now?" before chasing a phantom bug.

### Missing Knowledge

- **No regression test harness for curator dedup.** The verification here was a one-shot synthetic inbox drop + manual grep of the vault. A proper fixture-based test that stages a known "existing canonical + incoming case-variant" scenario against a disposable vault would let us assert the dedup contract automatically. Not built — flagged as future work.
- **alfred.black (the upstream commercial product)** describes a named "entity resolution" stage in its curator pipeline. The public docs don't specify the implementation, but having a dedicated single-purpose LLM pass for canonical-naming decisions — with no other responsibilities — is a design pattern worth adopting even in a clone. Saved to memory this session; design discussion deferred.
