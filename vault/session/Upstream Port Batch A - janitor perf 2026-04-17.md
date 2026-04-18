---
type: session
date: 2026-04-17
tags: [upstream-port, janitor, cost-control, drift-stability]
commits: [a27e431]
ports_from: [1a4a77b]
---

# Upstream Port Batch A — Item 3: Janitor perf (stub cap + staleness + event-driven deep sweeps)

## What shipped

One code commit (a27e431) covering three related cost-control changes on the janitor:

### 1. Stage 3 stub-enrichment cap

New `SweepConfig` fields in `janitor/config.py`:
- `max_stubs_per_sweep: int = 10`
- `max_enrichment_attempts: int = 3`

`_stage3_enrich()` in `janitor/pipeline.py` now:
- Filters out files flagged `enrichment_stale` (unless no state is passed).
- Sorts candidates by `(last_scanned, -linked_count)` descending — newest, best-connected stubs first. Rationale: more wikilinks = more context in the prompt = higher enrichment success rate.
- Truncates to `max_stubs_per_sweep`, logs `pipeline.s3_capped(total, processing)` when the cap fires.

### 2. Per-file enrichment staleness

`FileState` (in `janitor/state.py`) gains three fields, all persisted:
- `enrichment_attempts: int = 0`
- `last_enrichment_attempt: str = ""`
- `enrichment_stale: bool = False`

`JanitorState` gains three helpers:
- `record_enrichment_attempt(rel_path, max_attempts)` — increments counter, flips `enrichment_stale = True` once the counter reaches `max_attempts`.
- `reset_enrichment_staleness(rel_path)` — zeroes the counter and unflags. Called when a content hash change is detected.
- `is_enrichment_stale(rel_path)` — predicate used by the filter at the top of `_stage3_enrich`.

Stage 3 calls `record_enrichment_attempt` both on successful LLM invocations and on upstream `vault_read` failures — the latter means a permanently-broken file stops pinning Stage 3 capacity instead of dying silently on every sweep.

### 3. Event-driven deep sweeps

`JanitorState.previous_sweep_issues: dict[str, list[str]]` stores the last deep sweep's issue set, keyed by `rel_path`, persisted across restarts.

`JanitorState.get_new_issues(current)` returns `{path: [novel_codes]}` — for each current file, the issue codes NOT present in the previous snapshot. `save_sweep_issues(current)` replaces the stored snapshot.

`run_watch()` in `janitor/daemon.py` was rewritten around the deep-sweep branch:
- Pre-run a cheap `run_structural_scan` to build `current_issue_map`.
- Walk `state.files` computing the current on-disk hash; any file where `cur_md5 != stored_md5` is a changed file. Each such file also gets `reset_enrichment_staleness()` so edits make a stub enrichable again.
- Compute `new_issues = state.get_new_issues(current_issue_map)`.
- If `new_issues` empty AND `changed_files` empty → log `daemon.deep_sweep_skipped` and skip the fix pipeline entirely. Otherwise → normal `run_sweep(..., fix_mode=True)`.
- Either way, `save_sweep_issues(current_issue_map)` runs (so the first non-skip sweep has a baseline to diff against) and `last_deep = now`, `state.last_deep_sweep = now.isoformat()`, `state.save()` all fire.
- `last_deep` bumping is load-bearing: without it we would re-run the structural scan every `interval_seconds` tick for "nothing new" vaults. Now we only do the cheap scan once per `deep_interval_hours`.

`run_pipeline` and `_stage3_enrich` both take a new optional `state: JanitorState | None = None` argument. Optional so pipeline can still be unit-tested without a state instance. Wired in from `daemon.run_sweep`.

## Smoke tests

- `JanitorConfig().sweep.max_stubs_per_sweep == 10` and `max_enrichment_attempts == 3`: confirmed.
- Enrichment staleness lifecycle: created temp state, seeded a FileState, called `record_enrichment_attempt` three times — not stale after 2, stale after 3. Saved, reloaded, staleness persisted. Called `reset_enrichment_staleness`, counter back to 0 and flag cleared.
- Event-driven helpers: saved snapshot `{a: [BROKEN_WIKILINK], b: [STUB_RECORD]}`, called `get_new_issues({a: [BROKEN_WIKILINK], c: [ORPHANED_RECORD]})`, returned `{c: [ORPHANED_RECORD]}` (a has no novel codes; c is a new file with a new code). Identical snapshot returned `{}`.
- Full save JSON inspection: `previous_sweep_issues`, `enrichment_attempts`, `enrichment_stale`, `last_enrichment_attempt` all serialize as expected.
- `alfred status` command loaded real state (1097 janitor-tracked files, 20 sweeps) without error — confirms the new JSON-schema additions are backward-compatible with existing state files (missing keys default via `raw.get(..., default)`).

## Contracts affected

- **State file format**: JanitorState JSON gains `previous_sweep_issues` (dict) and three new fields inside each `files` entry (`enrichment_attempts`, `last_enrichment_attempt`, `enrichment_stale`). Old state files load fine — `dict.get()` fallbacks handle missing keys. No migration needed.
- **Pipeline signature**: `run_pipeline(issues, config, session_path, state=None)` and `_stage3_enrich(stub_issues, config, session_path, state=None)` both gain an optional `state` kwarg. Any external caller of run_pipeline (none in-tree besides daemon.run_sweep) needs to pass state if they want the new behaviour.
- **Daemon log events**: added `daemon.deep_sweep_check` (fires every deep-interval tick), `daemon.deep_sweep_skipped` (fires when nothing is new), extended `daemon.deep_sweep` with `new_issue_files` / `changed_files` keys. Downstream log consumers (if any) should accommodate.

## Alfred Learnings

- **Pattern validated** — the "state-as-memory" pattern for LLM-calling daemons. Every expensive operation needs either a persisted timestamp (Item 2's last_deep_sweep) or a content-diff memory (this item's previous_sweep_issues) so restarts are cheap. Once the pattern is in place, cost-control features like caps become trivial additions.
- **Pattern validated** — optional `state: T | None = None` kwargs let the test surface stay simple. Stage 3's filter/sort/cap logic is all conditional on state being passed, so `_stage3_enrich` still runs fine in a unit test that just supplies stub_issues + config. Followed the same pattern for run_pipeline.
- **Gotcha** — when a deep sweep gets skipped, you still need to snapshot the current issue set AND bump `last_deep`. The first is so the next sweep has a baseline; the second is so run_watch doesn't re-run the structural scan every interval_seconds tick. Missing either one of these would re-introduce the original cost bug from the other end.
- **Gotcha** — `FileState` gaining new fields requires matching keys in both the `load()` raw dict reconstruction AND the `save()` dict construction. Missed the save side once during the port; `dataclasses.asdict(fs)` would have caught it by emitting all fields automatically. Left the explicit dict construction for now because the existing file doesn't use asdict and consistency matters — but a future cleanup could replace both sides with `asdict()` / `**data`.
- **Anti-pattern confirmed (again)** — unbounded LLM loops over user-growing lists. Stage 2 cap (Item 2) was one example; Stage 3 stub cap (this item) is another. Every "for X in some_structural_scan_output: call_llm()" needs a cap. Should land as a CLAUDE.md rule if it happens a third time — we're now at two, one more and it's documentation-worthy.

## Rollout notes

- Defaults match upstream: 10 stubs/sweep, 3 attempts, event-driven. User can override via `janitor.sweep.max_stubs_per_sweep` and `janitor.sweep.max_enrichment_attempts` in config.yaml.
- First deep sweep after upgrade will run normally (previous_sweep_issues empty → new_issues = everything → not skipped). Subsequent sweeps see the baseline and start skipping when nothing changes.
- Files already enriched 3+ times before this patch have `enrichment_attempts=0` in legacy state, so they get a fresh allowance. That's intentional — resetting the counter on upgrade lets us see which stubs are actually stale under the new regime rather than inheriting pre-upgrade history.

## Batch A complete

- Item 1: a80ecbe (code) + 2171e08 (note) — distiller MD5 refresh + defaults + surveyor dim mismatch.
- Item 2: 12fe4bc (code) + 2d1f9bb (note) — deep-sweep timestamp persistence + S2 cap + S2 prompt reshape.
- Item 3: a27e431 (code) + this note — S3 stub cap + enrichment staleness + event-driven deep sweeps.

Awaiting review before Batch B.
