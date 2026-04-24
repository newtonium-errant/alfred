---
type: session
status: completed
name: Distiller rebuild Week 2 comparison harness
created: 2026-04-24
branch: rebuild/distiller
shadow: true
description: Ship c7 on the rebuild branch — scripts/distiller_rebuild_compare.py. Mechanical shadow-vs-legacy diff so Week 2 operator review is bounded to the pairs the machine can't adjudicate. Handles empty shadow gracefully (pre-flip state).
intent: Bound Andrew's Week 2 hand-rating load. When the flag flips and both pipelines run in parallel, the comparison script pre-filters agreements so the operator only reviews genuine disagreements and orphans.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Distiller rebuild Week 1 MVP 2026-04-24]]'
tags:
- distiller
- rebuild
- week-2
- comparison
- harness
- shadow
---

# Distiller rebuild Week 2 comparison harness

## Intent

**Branch: `rebuild/distiller`. Shadow-mode tooling — the script reads shadow + legacy trees and produces a diff; it does not run the pipelines itself.**

Week 1 MVP shipped the shadow pipeline. Week 2's original plan was 30-45 min of operator hand-comparison on ~20 records. c7 reduces that to: machine pre-filters agreements, operator only reviews disagreements + orphans. Runs offline (no LLM), read-only (no vault writes), gracefully handles the pre-Week-2-flip state (empty `data/shadow/distiller/`).

## Work Completed

One commit on `rebuild/distiller`:

- `38e9e6b` — Distiller rebuild c7: shadow-vs-legacy comparison harness (612 LOC, `scripts/distiller_rebuild_compare.py` new). Walks `data/shadow/distiller/<type>/*.md` and `vault/<type>/*.md`, matches records by slug-first-then-fuzzy-title (>0.85 ratio), produces agreement/disagreement/orphan buckets. Flags:
  - `--type assumption,decision` (default `assumption` matching `v2_types` default)
  - `--since <hours>` (default 48, mtime-filtered)
  - `--format md|json` (default `md`)
  - `--shadow-root` / `--vault-root` overrides (primarily for testing against fabricated fixtures)
  - Summary line to stderr: `AGREED=N DISAGREE=M ORPHAN_SHADOW=K ORPHAN_LEGACY=L` for scripting

## Validation

Three smoke runs performed:
1. `--help` output clean
2. Empty-shadow smoke: friendly `[info]` message + exit 0 (current pre-flip state)
3. Fabricated-fixture smoke (`/tmp/fake_shadow/` + `/tmp/fake_vault/` with 5 records each — perfect pair, confidence mismatch, fuzzy-title + divergent claim, one orphan each side): all four categories detected correctly. Output `AGREED=1 DISAGREE=2 ORPHAN_SHADOW=1 ORPHAN_LEGACY=1`.

## Outcome

Comparison harness ready. When Andrew flips `extraction.use_deterministic_v2: true` in Week 2:

1. Let 03:30 Halifax deep-extraction window fire both paths
2. Run `python scripts/distiller_rebuild_compare.py` (default to `--type assumption --since 48 --format md`)
3. Read the AGREED summary + inspect DISAGREEMENTS section (likely 3-5 records) and ORPHANS (likely small)
4. Hand-rate only those — probably <15 min operator time instead of 30-45

**Builder flagged scope creep:** 612 LOC vs the 250-LOC target / 300-LOC stop watermark. Breakdown: ~150 core logic, ~80 markdown formatter, ~80 JSON+argparse+dataclasses, ~130 docstrings/comments matching the rebuild branch's house style. No bloat; justified by thoroughness. Caught and reported per brief discipline rather than silent.

**Design decisions worth noting:**

- **Provenance field union**: legacy writer emits `based_on`, shadow writer emits `source_links`, some legacy records use `related` or scalar `source`. Script unions all four before diffing — stability across writer-shape drift.
- **Wikilink + type-prefix normalization**: `[[note/X]]`, `note/X`, and `X` collapse to the same value before set-diff. This was NOT in the spec but was the single biggest signal-vs-noise issue in fixture validation. Caught pre-commit.
- **List/scalar alias collapse**: `project: 'Alfred'` and `project: ['Alfred']` normalize to the same value. Also not in spec; also caught in smoke.
- **Claim extraction dual-path**: shadow puts claim in frontmatter; legacy puts claim in body `## Claim` section. Script reads frontmatter first, falls back to body.
- **Thresholds tunable via module-level constants**: `TITLE_MATCH_THRESHOLD = 0.85`, `CLAIM_SIMILARITY_THRESHOLD = 0.70`. Operator hints flagged in the builder report for likely Week 2 tuning needs (fragmented legacy claims may ratio lower than 0.70 even when semantically identical).

## Alfred Learnings

- **Pattern validated**: pre-build the evaluation harness before the evaluation window opens. c7 lands before Week 2 measurement starts; when flag flips, operator has tooling. Better than the pattern where we flip the flag and then realize we need tooling.
- **Pattern validated**: fabricated-fixture smoke caught two field-shape issues (wikilink formatting, list-vs-scalar) that the builder hadn't considered when reading the spec. `/tmp/fake_*` fixtures are a cheap-and-reliable dev loop when pytest is off the table.
- **Gotcha**: shadow writer and legacy writer serialize the same semantic content differently enough (wikilink formatting, list-vs-scalar defaults, frontmatter-vs-body placement of claim) that naive equality comparison would flag every pair as disagreeing. Normalization is load-bearing for the harness to be useful. Worth remembering for any future cross-writer comparison tooling.
- **Anti-pattern surfaced, not resolved**: legacy writer produces prose-padded claims ("The rationale underlying this assumption is that..."); shadow writer returns bare claims. Same semantics can ratio at 0.55. Flagged for Week 2 tuning — either drop threshold to 0.55, or strip prose padding before comparing. Choosing deliberately at Week 2 rather than guessing today.
