---
type: session
status: completed
name: "Pytest coverage step (a) — baseline + top-5"
intent: "Add coverage reporting and write first tests for the 5 highest-value untested functions"
project: "[[project/Alfred]]"
created: 2026-04-19
tags: [testing, coverage, pytest, quality]
related:
  - "[[project/Alfred]]"
---

# Pytest coverage step (a) — baseline + top-5

Landing step (a) of the 3-part pytest expansion plan
(`project_pytest_expansion.md`). This is the measurement pass: add
`pytest-cov` to the `[dev]` extra, generate the first-ever baseline
coverage number, pick 5 high-value zero-coverage functions, and write
minimal first tests for each. Steps (b) and (c) are deferred — (b) is
the curator-pipeline deep dive, (c) is the orchestrator/state harness.

## Work Completed

### Tooling

- **`pyproject.toml`** — added `pytest-cov>=5.0` to the `[dev]` extra.
  Installed into `.venv` via `pip install -e ".[dev]"` (pulled in
  `coverage-7.13.5` + `pytest-cov-7.1.0`).
- **`.gitignore`** — added `htmlcov/`, `.coverage`, `.coverage.*`,
  `coverage.xml` so coverage artifacts don't get tracked.

### Baseline measurement

- **Before:** 14% overall (187 tests, 13,425 statements, 11,564
  uncovered).
- **After (this commit):** 15% overall (204 tests, 11,391 uncovered).
- Biggest zero-coverage surfaces surfaced: `distiller/pipeline.py`
  (496 stmts), `curator/pipeline.py` (324), `janitor/daemon.py` (256),
  `janitor/scanner.py` (257), `orchestrator.py` (288), and the whole
  `tui/` + `dashboard.py` + `temporal/` trees. These are the natural
  targets for step (b) and future work.

### Five first tests — one per target

| # | Test file | Target function | One-line rationale |
|---|-----------|-----------------|---------------------|
| 1 | `tests/test_curator_context.py` | `curator.context.extract_sender_email` | Email-pipeline parser; silent regex drift would disable every inbox sender-context lookup. |
| 2 | `tests/test_distiller_candidates.py` | `distiller.candidates.compute_score` | Gate for which records reach LLM extraction; weight drift changes what gets distilled with no alarm. |
| 3 | `tests/test_janitor_scanner.py` | `janitor.scanner._build_stem_index` | Powers wikilink resolution for broken-link checks; both bare-stem and path-qualified forms must resolve to the same file. |
| 4 | `tests/test_surveyor_writer.py` | `surveyor.writer.VaultWriter.write_alfred_tags` | Skip-if-equal drift guard; without the normalization early-return, every sweep churns the vault git history for no functional change. |
| 5 | `tests/test_vault_ops_near_match.py` | `vault.ops._check_near_match` | Dedup hard-gate inside `vault_create`; prevents `PocketPills` vs `Pocketpills` twin-record splits. |

All 17 new assertions pass; full suite is 204/204 green in 8.6s.

## Outcome

Coverage reporting is now first-class (`pytest --cov=alfred`), the
baseline number is on record (15%), and five drift-prone functions that
previously had zero assertions now have a contract pinned in place.
Step (b) can now proceed with a measurement feedback loop — any
curator-pipeline test-adds will have a visible % delta.

## Alfred Learnings

- **Pattern validated — coverage-first ordering.** Measuring before
  writing confirmed the plan's intuition: `curator/pipeline.py`,
  `distiller/pipeline.py`, and `janitor/daemon.py` are the three biggest
  silent gaps by statement count. Step (b)'s curator-focus is justified
  by the numbers, not just by the recent-churn priors.
- **Surprise — voice suite is punching above its weight.**
  `telegram/calibration.py` sits at 90% and `telegram/router.py` at 94%
  without any targeted coverage push. That tree is in good shape; we
  can redirect effort to the 0% tool trees.
- **Surprise — `janitor/merge.py` incidentally covered to 92%.** The
  existing `test_merge.py` pulls in most of the merge module as a side
  effect. Adding a direct test for `_check_near_match` on vault/ops did
  a similar side-bump.
- **Gotcha — `pytest-cov` needs `--cov=alfred` (package name), not a
  path.** The editable install registers the package as `alfred`, so
  `--cov=src/alfred/` silently measures nothing.
- **Missing knowledge — no `[tool.coverage]` config yet.** Current
  reports include every `src/alfred/*` file, but the `tui/`, `temporal/`
  opt-in extras, and `dashboard.py` drag the baseline down because they
  ship as part of the package even when their extras aren't installed.
  Worth filtering in step (b) or (c) so the number reflects the
  always-installed core.
- **No bugs surfaced by step (a).** The five target functions all
  behaved as documented; no follow-up flags. If step (b)'s deeper
  curator-pipeline coverage surfaces bugs, those go in a separate
  commit per the builder agent's no-scope-creep rule.
