---
type: session
title: Talker — inline command boundary tightening
date: 2026-04-21
tags: [talker, telegram, regex, ux, bugfix]
---

## Summary

Single commit `2601067` tightens the talker's inline-command detection
to require a sentence-terminating punctuation character (`.,!?;:`)
before the slash, fixing prose false-positives like `"Goodbye /end"`.
Matched symmetrically across no-arg and with-arg detectors. 6 new
regression tests; 3 existing tests rewritten to preserve dispatch
intent under the tighter boundary.

## Why — bug shape was different than originally reported

Andrew's 2026-04-21 report said `"Good. /end"` routes to Claude instead
of closing the session. Builder repro proved the opposite: `"Good. /end"`
already fired correctly under the Phase 1 regex (shipped 2026-04-19
as `b248674`). The actual broken behavior was the *inverse* — the
Phase 1 regex `(?:^|\s)/(\w+)\s*$` matched ANY whitespace before the
slash, so prose ending with a slash-token false-positived as a command:

- `"the road came to a /end"` → fired `/end`
- `"Goodbye /end"` → fired `/end`
- `"Goodbye please /opus"` → fired `/opus`

These were never command intent — just prose. Phase 2 of the talker
inline-command work tightens the boundary to require either start-of-
message OR a sentence-terminating punctuation character.

## What changed

- `src/alfred/telegram/bot.py`
  - New constant `_INLINE_CMD_BOUNDARY = r"(?:^|[.!?;:,]\s+)"` — start of
    message OR one of `.!?;:,` followed by whitespace.
  - `_INLINE_CMD_RE` rebuilt around the boundary. Same shape applied to
    `_INLINE_CMD_WITH_ARG_RE` for `/extract <id>`, `/brief <id>`,
    `/speed <args>` so the fix is symmetric.
  - `_parse_short_id_arg` updated for the new group structure (with-arg
    regex now has 8 groups; coalesces correctly).
  - `_detect_inline_command` updated for group coalesce.
- `tests/telegram/test_inline_commands.py` — 6 new regression tests
  covering the four canonical cases (`"Good. /end"`, `"/end"`,
  `"the road came to a /end"`, `"Note: /extract abc"`) plus with-arg
  symmetry and case sensitivity.
- `tests/telegram/test_capture_extract.py` — 4 callsites updated from
  `"please /extract abc123"` → `"Note: /extract abc123"` (preserves the
  test's actual intent: dispatch wiring, not regex permissiveness).
- `tests/telegram/test_inline_commands.py` — `"please /opus"` →
  `"Yes please. /opus"`; `"ok /End"` → `"ok. /End"`.

## Design decisions

- **Keep prose-with-trailing-slash-token as prose, not commands.** The
  failure mode that motivated Phase 2 is users dictating voice messages
  that happen to end with a word starting with slash. Tightening the
  boundary is the right default; if a real use case for "command after
  whitespace-only" surfaces later, widen the regex on demand.
- **Same anchor on both detectors** (no-arg + with-arg). Future
  inline-eligible commands inherit the new boundary by default.
- **Existing tests rewritten, not deleted.** Each rewrite kept the test's
  original dispatch-wiring intent; only the input text changed to
  exercise the new boundary.

## Alfred Learnings

- **Gotcha — original symptom report was wrong-shaped.** Andrew reported
  `"Good. /end"` as the failing case; repro showed it actually worked.
  The true bug was prose false-positives. Worth extracting as a general
  rule: when investigating a reported bug, **build the repro before
  trusting the symptom narrative**. Took the builder ~5 minutes to
  realise the regex already handled the reported case — that 5 minutes
  saved scope creep into "ship the originally-described fix and miss
  the actual one." Already noted in
  `project_talker_inline_commands.md` as a future-investigation flag.
- **Pattern validated — symmetric anchor across related regexes.** Two
  regexes (no-arg + with-arg) sharing a boundary were tightened
  together. Asymmetric tightening would have left a known-broken path
  for arg-bearing commands; future inline-command additions inherit the
  hardened boundary by default.
- **Anti-pattern confirmed — overly-permissive regex defaults.**
  `(?:^|\s)/(\w+)` looks innocuous but accepts any whitespace
  separation. Any future "match a token after some context" regex
  should explicitly enumerate which contexts are valid (here:
  punctuation + whitespace), not "anything except letters."

## Next

The talker inline-command system is now hardened in both directions
(false-negatives fixed in Phase 1 b248674; false-positives fixed in
Phase 2 2601067). Don't reopen without a fresh repro showing a real
failure mode different from the two phases above. Memory entry
`project_talker_inline_commands.md` updated to reflect both phases
+ the bug-shape-surprise lesson.
