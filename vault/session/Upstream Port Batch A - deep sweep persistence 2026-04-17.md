---
type: session
date: 2026-04-17
tags: [upstream-port, janitor, distiller, drift-stability, cost-control]
commits: [12fe4bc]
ports_from: [e510cbe]
---

# Upstream Port Batch A — Item 2: Persist deep-sweep timestamps + Stage 2 cap + prompt reshape

## What shipped

Three related changes in one commit (12fe4bc), all aimed at stopping runaway LLM cost on daemon restart.

1. **State persistence for `last_deep_sweep` / `last_deep_extraction`.**
   - `JanitorState.last_deep_sweep: str | None` — ISO timestamp of last fix-mode sweep.
   - `DistillerState.last_deep_extraction: str | None` — ISO timestamp of last deep extraction.
   - Both are now saved with the rest of state JSON and reloaded on `load()`.
   - `run_watch()` in both daemons reads the persisted value and sets `last_deep` to it (with a `ValueError/TypeError` fallback to epoch so a corrupted stamp degrades gracefully to old behaviour).
   - After a successful deep sweep/extraction, `state.last_deep_sweep/extraction = now.isoformat()` and `state.save()` is called so the stamp survives the next restart.

2. **Stage 2 LLM call cap.** New module constant `MAX_ISSUES_PER_SWEEP = 15` in `janitor/pipeline.py`. `_stage2_link_repair()` truncates `link_issues` to 15 after the unambiguous-Python-fix pass, logs `pipeline.s2_capped(total, processing)` when the cap bites. Unambiguous fixes still run for all issues (they're free — pure Python); only LLM-routed ambiguous cases are truncated.

3. **Stage 2 prompt reshape** in `src/alfred/_bundled/skills/vault-janitor/prompts/stage2_link_repair.md`.
   - Removed the `janitor_note` escape hatch (previously: "If no candidate matches, add a janitor_note"). Stage 1 owns janitor_note writes; Stage 2 writing them created new issues for the next scan.
   - Added explicit "If you are NOT SURE, do NOTHING. Reply with SKIP and nothing else."
   - Split Rules section: "READ CAREFULLY" heading, explicit Do-NOT list, reiterated "If unsure, do NOTHING. It is better to skip than to make a wrong fix."

## Why upstream did this

Root cause documented in e510cbe commit message: 21 daemon restarts in 3 days -> 968 janitor + 317 distiller wasted LLM calls -> over $100 spent on re-running the same deep sweep every restart. The epoch-default made every startup look like a fresh install. The unbounded Stage 2 amplified the damage — one sweep with 374 link issues = 374 LLM calls. The old prompt's janitor_note escape hatch was a feedback loop: LLM writes janitor_note to "document the issue"; structural scanner flags the janitor_note as drift; next deep sweep re-processes the same file. Stage 2 was paying to create Stage 1 work.

## Smoke tests

- **Round-trip persistence**: created temp JanitorState, set `last_deep_sweep` to `now().isoformat()`, saved, loaded into a fresh instance, confirmed value survived and `datetime.fromisoformat()` reparsed it cleanly. Same for DistillerState.
- **Skip decision**: simulated daemon restart 1h after a deep sweep with 24h interval. `hours_since_deep=1.0`, `would_skip=True`. Then simulated 30h later -> `would_run=True`. Confirms the stamp drives the decision correctly.
- **MAX_ISSUES_PER_SWEEP**: imported and asserted == 15.
- **Prompt reshape**: asserted "Reply with SKIP" and "If unsure, do NOTHING" strings present; asserted `janitor_note` appears only in the Stage-1-ownership disclaimer, not as an instruction to the LLM.

## Contracts affected

- **State file format**: JanitorState and DistillerState JSON both gain a new top-level `last_deep_sweep` / `last_deep_extraction` field. Missing key degrades to `None` (epoch behaviour), so old state files load fine. No migration needed.
- **Stage 2 prompt template**: template variables unchanged (`{file_path}`, `{broken_target}`, `{candidates}`, `{candidate_names}`, `{vault_cli_reference}` all preserved). The Python call site in `_stage2_link_repair()` still passes the same substitution dict; no code change required on that side. Prompt-tuner should know the `{candidate_names}` variable is now unused inside the rendered template (it was only referenced in the removed janitor_note command). Left in the format() call for safety — removing it would be a breaking change for anyone else formatting against this template.

## Alfred Learnings

- **Anti-pattern confirmed** — stateful daemon settings (interval trackers, rate limiters, cursor positions) must persist. Any `last_X_time = datetime.min` default in a run_watch loop is a cost bomb on restart; if the work is expensive, you pay again every single boot. Flag for CLAUDE.md: "if run_watch() tracks a timestamp, it MUST persist to state." Voice/talker and surveyor daemons should be audited for the same pattern.
- **Anti-pattern confirmed** — LLM-writing-fields-that-the-scanner-detects creates feedback loops. If Stage N writes output that Stage 1 flags, every deep sweep re-surfaces the same issue. Symmetric to the distiller MD5 loop from Item 1; this is now **two distinct tools** with the same class of bug, which meets the "pattern discovery = documentation trigger" rule in CLAUDE.md.
- **Pattern validated** — using `datetime.fromisoformat()` with a `ValueError/TypeError` catch to epoch is the right way to load persisted timestamps. Corrupted stamp => old behaviour (full sweep once), not a daemon crash loop.
- **Gotcha** — when reshaping an LLM prompt that includes removed instructions, verify template variable references inside the rendered text, not just the call site. A `.format()` with an unused var in the dict is harmless but a missing var in the text will KeyError at runtime.

## Next

Item 3: janitor perf — cap Stage 3 stub enrichment, track per-file enrichment attempts with stale-after-3 semantics, event-driven deep sweeps that skip the LLM call when no new issues exist since the last snapshot.
