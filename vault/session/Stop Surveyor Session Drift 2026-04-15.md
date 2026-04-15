---
alfred_tags:
- software/alfred
- software/surveyor
- bugfix/drift
created: '2026-04-15'
description: Stop the surveyor daemon from continuously re-labeling committed session
  notes. Root cause was non-zero LLM temperature in the labeler plus no session/inbox
  exclusion in ignore_dirs, plus stale Milvus+state rows from the prior config that
  needed purging on startup.
intent: Kill the within-session re-drift loop that was making every commit produce
  new dirty session notes within minutes
name: Stop Surveyor Session Drift
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Repo Hygiene and Session-Start Rule 2026-04-15]]'
- '[[session/Harden Vault Dedup at Python Layer 2026-04-15]]'
- '[[session/Dedup Layers and Surveyor Tuning 2026-04-14]]'
status: completed
tags:
- surveyor
- drift
- bugfix
type: session
---

# Stop Surveyor Session Drift — 2026-04-15

## Intent

The earlier session today landed a CLAUDE.md rule about "session-start dirty-tree audits" to catch recurring uncommitted work. Within minutes of that commit, six freshly-committed session notes were already dirty again in `git status`. The audit rule catches cross-session drift but clearly wasn't going to fix a continuous loop — something was actively re-editing committed files. This session found and killed the loop.

## Misdiagnosis first, then real root cause

I initially attributed the drift to the janitor, because "janitor edits vault files" is the obvious mental model from CLAUDE.md. The Plan agent I dispatched to research a fix found the actual culprit almost immediately: **the surveyor**, not the janitor, is the only code path in Alfred that writes `alfred_tags` and `relationships` — those are the only fields drifting on every sweep. The janitor's `edit` scope covers other frontmatter fields, but `alfred_tags`/`relationships` are written exclusively by `src/alfred/surveyor/writer.py::VaultWriter.write_alfred_tags` and `write_relationships`.

The surveyor's re-label logic in `daemon.py` flags every "changed" cluster and re-labels its members every tick. A single modified vault file mutates its cluster's signature → the cluster is flagged changed → the labeler is called again → every member in that cluster (including session notes that happen to share semantic neighbourhood with a recently-modified note) gets re-labeled and re-written. And because `labeler.py` was calling the LLM with `temperature = openrouter_cfg.temperature` (non-zero in the current config), repeated calls produced different tag sets each time — so even when the cluster membership hadn't meaningfully changed, the tags flapped and the writer dutifully persisted each new set.

The surveyor had no `session` or `inbox` entry in its `ignore_dirs` list. Every session note was fair game for labeling, and every inbox-processed email was also being re-labeled every tick.

## What shipped

Five files, one atomic commit.

### `src/alfred/surveyor/config.py` — default `ignore_dirs` expanded

Added `session` and `inbox` to `VaultConfig.ignore_dirs` default. Session notes are narrative singletons — they don't cluster meaningfully and the machine-tag labeling produces noise on them. `inbox/processed/` holds curator-consumed emails that the surveyor shouldn't re-tag either.

### `src/alfred/surveyor/labeler.py` — force deterministic labeling

Hardcoded `self.temperature = 0.0` in the labeler constructor, ignoring the `openrouter.temperature` config value. Non-zero temperature was causing identical-cluster-different-tags drift across sweeps. The labeler's job is classification, and classification wants determinism, not creativity. Kept the config field alive for other callers (if any) but the labeler now explicitly overrides with a comment explaining why.

### `src/alfred/surveyor/writer.py` — trailing newline fix

`content = frontmatter.dumps(post) + "\n"` in `_write_atomic`. Correctness fix — files should end with a final newline, and the surveyor's atomic writes were stripping it, which showed up in every diff as `\ No newline at end of file` hunks.

### `src/alfred/surveyor/daemon.py` — purge stale rows + defensive filter

**This was the biggest surprise.** Adding `session`/`inbox` to `ignore_dirs` was NOT sufficient by itself. The builder restarted the surveyor after the config change, expected clean session notes, and instead saw fresh `writer.tags_written` events on session paths within a minute. Root cause: `embedder.get_all_embeddings()` returns every row in Milvus, and `PipelineState.files` was already populated with session/inbox paths from the prior config. The stale rows kept appearing in cluster memberships and driving re-labeling — the new `ignore_dirs` only controlled what got ADDED, not what was already indexed.

Fix:
- Added `_is_ignored(rel_path)` helper and `_purge_ignored_paths()` method that runs at the top of `Daemon.run()`. It walks `state.files` and Milvus, drops every row whose path is under the (now-expanded) `ignore_dirs`, and logs a `daemon.purged_ignored_paths` event with counts.
- Added a belt-and-braces `_is_ignored(path)` filter inside `_cluster_and_label`'s cluster-member build loop, so even if a stale row slips past the purge (race, crash, manual state edit), it cannot drive a writeback.

Live purge numbers on first boot after the fix: **518 Milvus rows removed, 520 state rows removed.** The `PipelineState.files` count dropped from 1491 to 971 — confirming that ~35% of tracked rows were session/inbox content that shouldn't have been there.

### `config.yaml.example` — keep the example in sync

Appended `"session", "inbox"` to the example `vault.ignore_dirs` list so new installs get the right defaults. The user's actual `config.yaml` (gitignored) also had its `vault.ignore_dirs` override updated locally — it had an explicit list that didn't include `session`, which would have masked the code default change if left alone.

## Verification

1. Six dirty session notes discarded via `git checkout -- vault/session/` (first pass).
2. Surveyor daemon restarted. `daemon.purged_ignored_paths milvus_removed=518 state_removed=520` fired at startup.
3. Touched `vault/note/Pocketpills Ozempic Order Preparation 2026-04-13.md` to force a diff tick.
4. Surveyor processed: `daemon.processing_diff diff='Diff(new=0, changed=152, deleted=0)'` — large changed count because the cluster-signature shift from the resurrected file set.
5. Waited 150 seconds.
6. `git status vault/session/` — empty. Zero dirty session notes.
7. `grep writer.*session/ data/alfred.log` since 17:00Z — zero hits. (Before the fix, this grep was hitting every few minutes.)
8. `grep writer.*inbox/ data/alfred.log` since 17:00Z — zero hits. Inbox drift also closed.
9. Surveyor still writing legitimately to `account/`, `decision/`, `note/` — its real job is unaffected.

## What's explicitly NOT in this commit

- **Layer 3 janitor triage queue** — 9 dirty files + untracked `triage.py` still deferred pending a dedicated review session per `project_next_session.md` memory.
- **Four drift-adjacent surveyor issues** the builder flagged during the fix, all out-of-scope for a one-commit drift fix:
  1. `writer.relationships_written` emits duplicate `added=1` entries when the labeler returns near-duplicate rels in one call — dedup check is `target`-only. Wasteful but not drift-causing.
  2. `writer._write_atomic` registers `mark_pending_write` keyed on path, not hash — if two back-to-back ticks write the same file with different contents, the second write's expected_md5 overwrites the first and a pending watcher event may ignore a real user edit. Narrow race.
  3. `LOOP_INTERVAL = 5.0` in `daemon.py` is more aggressive than the watcher's 30-second debounce; debounced batches re-trigger the full cluster/label pipeline every 5 seconds until nothing is debounced. Should probably match the debounce interval.
  4. Whether `inbox/processed/` should be permanently excluded or whether some other tooling expects those emails indexed — the fix excludes them but a future consumer might want them back.

## Alfred Learnings

### New Gotchas

- **"Obvious suspect" bias in debugging.** When frontmatter fields drift in committed files, check which daemon has write scope on those specific fields before naming a suspect. I blamed the janitor because it's the thing I know edits frontmatter. The Plan agent I dispatched checked `grep "alfred_tags\|relationships" src/alfred/*/writer.py` and found the surveyor as the *only* writer for those fields in seconds. Rule: when diagnosing drift, grep the codebase for the specific field name before reaching for the mental model.
- **Config changes on stateful daemons need a migration step.** Expanding `ignore_dirs` didn't fix the drift on the first restart because the existing Milvus embeddings and state-file rows for session/inbox paths were still live. The fix had to include a boot-time purge that walks both data stores and drops newly-ignored paths. Generalisable: whenever a daemon config change restricts the set of tracked resources, the daemon needs explicit purge logic for pre-existing state that falls outside the new scope, otherwise the change silently fails.
- **Non-zero LLM temperature on a classification task is a drift factory.** The surveyor's labeler was using `openrouter.temperature` (probably `0.7` or similar) for what is a deterministic-classification problem. Every sweep produced different tags for the same input. `temperature=0` for classification, `temperature>0` only for generative tasks where variance is desirable. Worth a rule in CLAUDE.md or an agent instruction.

### Patterns Validated

- **Plan-agent-before-builder when the diagnosis is uncertain.** I sent a Plan agent to research the fix before routing implementation to the builder. The Plan agent found the misdiagnosis (surveyor, not janitor), recommended a specific option (4a+4c+newline), and structured the decision document. If I had routed directly to the builder with my initial janitor framing, they would either have pushed back after their own investigation (wasted tokens) or — worse — made a change to the janitor that silently did nothing and left the drift in place. Decision doc first, code second.
- **Belt-and-braces defensive filters are cheap insurance.** The builder added an `_is_ignored` filter inside `_cluster_and_label`'s membership build loop even after the purge logic handled the happy path. That filter has a near-zero runtime cost and catches any stale row that slips past the purge in the future (race conditions, crash-recovery scenarios, manual state edits). Worth doing when the check is O(1) and the failure mode is "silent re-drift."

### Corrections

- The earlier session note "Repo Hygiene and Session-Start Rule 2026-04-15" proposed the session-start audit rule as the fix for "recurring uncommitted work." That rule is still useful for cross-session drift, but it does NOT fix the continuous within-session loop caused by the surveyor. The real root cause was the surveyor non-determinism + missing purge, and the audit rule is at best a symptom-management tool for a bug that now has a proper fix. Corrected in this commit.
- The earlier session note also blamed the janitor for the drift. That attribution was wrong and is corrected here: the janitor is innocent; `alfred_tags` and `relationships` drift is entirely surveyor-owned.

### Missing Knowledge

- **The surveyor's full pipeline shape wasn't documented anywhere I could find.** The builder had to grep through `daemon.py`, `embedder.py`, `clusterer.py`, `labeler.py`, `writer.py`, and `state.py` to reconstruct the tick cycle: watcher picks up changes → embedder computes new vectors → clusterer re-clusters → `_cluster_and_label` flags changed clusters → labeler classifies → writer persists. This should live in CLAUDE.md's Surveyor Pipeline section. Candidate for a small documentation commit.
- **There's no integration-test harness for the surveyor.** Verification today was done by touching a real vault file, waiting 150 seconds, and grepping `alfred.log`. A proper test would stub the LLM call to return a deterministic tag set, run a tick against a disposable temp vault, and assert that session/inbox paths stay clean. Flagged for future work, not blocking.
