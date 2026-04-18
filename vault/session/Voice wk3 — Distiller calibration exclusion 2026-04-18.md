---
type: session
status: completed
name: Voice wk3 — Distiller calibration exclusion
created: 2026-04-18
description: Commit 4 of 8 in Voice Stage 2a-wk3 — extend the distiller's strip pattern to exclude ALFRED:DYNAMIC and ALFRED:CALIBRATION blocks from both the parsed body and the stripped_body_length length check.
intent: Close the feedback loop before commit 7's writer starts producing calibration content at close time — the distiller must never re-learn Alfred's own self-model as vault learnings.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Calibration IO 2026-04-18]]'
tags:
- voice
- talker
- wk3
- distiller
- calibration
---

# Voice wk3 — Distiller calibration exclusion

## Intent

Commit 3 put real content into Andrew's calibration block. The distiller runs daily across every vault record; without a strip pattern, it would read that block, treat Alfred's self-model as source material, and produce `synthesis`/`assumption` records about Andrew's communication style — which Alfred wrote itself. That's a textbook feedback loop: the model's own output feeds its input, learnings drift away from ground truth, and the calibration block itself gets summarised into new learnings that the next distiller pass would re-summarise again.

Commit 4 extends `alfred.distiller.parser` so both marker-fenced block types (`ALFRED:DYNAMIC` — already used for machine-generated briefings — and the new `ALFRED:CALIBRATION`) are stripped alongside the pre-existing `KEN:DYNAMIC` pattern.

## Work Completed

- `src/alfred/distiller/parser.py`:
  - Added `ALFRED_DYNAMIC_RE` and `ALFRED_CALIBRATION_RE` module-level precompiled regexes (DOTALL, non-greedy, same shape as `KEN_DYNAMIC_RE`).
  - New `_strip_excluded_blocks(text)` helper applies all three strips in one pass. Centralising the list is deliberate — the same list must be applied to both `parse_file` (body returned in `VaultRecord`) and `stripped_body_length` (used to gate empty-record checks). Diverging these would leave one surface still ingesting calibration content.
  - `parse_file` now applies the strip to `post.content` before returning. Wikilink extraction still runs on the raw text (pre-strip) on purpose — if a calibration block references `[[project/A]]`, that's still a genuine "this record links to A" semantic.
  - `stripped_body_length` uses the helper, preserving the existing embed / heading strip steps.
- Tests: new `tests/test_distiller_parser.py` (11 tests):
  - `_strip_excluded_blocks` individually and collectively for all three block types.
  - `parse_file` strips calibration and dynamic blocks from the body but preserves frontmatter, wikilinks, and surrounding body text.
  - `stripped_body_length` counts zero when body is only excluded blocks; counts real content when mixed; still handles `KEN:DYNAMIC` (regression guard).

71 tests pass (60 after commit 3 + 11 new).

## Outcome

The distiller is now safe from commit 7's upcoming writes. Even if commit 7 produces a large, verbose calibration block, none of it will leak back into the `assumption` / `decision` / `constraint` / `contradiction` / `synthesis` record types.

## Alfred Learnings

- **Pattern validated**: the centralised `_strip_excluded_blocks` helper collapses three copies of the same `re.sub` chain into one list-of-patterns pipeline. Adding a fourth marker type in wk4+ is now a one-line change on a single function rather than editing every call site.
- **Pattern validated**: wikilinks extracted from the raw pre-strip text preserve the semantic "this record references this target" regardless of whether the reference lived in a dynamic or static block. Stripping wikilinks along with the block would have been a silent correctness bug (e.g., the calibration block referencing `[[project/Alfred]]` would drop that backlink from the graph).
- **Gotcha**: `stripped_body_length` and `parse_file` must stay in sync on what they strip. I nearly stripped only in `parse_file` — which would have left a record whose entire body was a calibration block reading as "non-empty" to the distiller's gate logic, defeating the point of the strip on records that have nothing else. Added a regression test `test_stripped_body_length_excludes_alfred_calibration_from_count` that pins this invariant.
