---
type: session
status: completed
name: Daily Sync calibration parser fixes
created: 2026-04-24
description: Ship c1-c5 to the Daily Sync calibration reply loop — tokenizer (no sentence-period splits), "Same/Ditto" chaining, applied-rule echo, sender fallback, (sender, subject-pattern) grouping. Andrew's live 5-item Borrowell reply went from 2/5 accepted to 5/5 accepted.
intent: Close the 60% parse-failure rate on list-style calibration replies Andrew observed in a live /calibrate session, and replace the developer-facing parse-error dump with user-facing messaging.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Email c2 + polish-audit closes 2026-04-22]]'
tags:
- daily-sync
- calibration
- parser
- ux
- email
---

# Daily Sync calibration parser fixes

## Intent

Live /calibrate session 2026-04-24 01:27 ADT surfaced a compound bug: Andrew sent 5 corrections on 5 Borrowell items, Salem applied 2, dumped *"Couldn't parse: warnings only., 2. Same as above., 3. Same."* to the user as if that were a useful response. Decomposition showed three mechanical bugs + one UX issue + one data-shape issue (4 near-duplicate items presented as 4 separate asks).

## Work Completed

Five commits on master, one per c:

- `c775946` — Daily Sync calibration c1: tokenize reply on numbered-list boundaries, not periods (`assembler.py`, +48). Two-tier list-shape detection (shape-gate + item-boundary regex) so prose like "2 questions came up, 1 for you" doesn't false-positive-list-shape. Fixes the 13-char parse-fail class.
- `c6d20c6` — Daily Sync calibration c2: "Same" / "Same as above" chaining (`assembler.py`, +147). Inherits tier/ok/reject from prior (or explicit "#N") item. Case-insensitive; "ditto" + "^" carrot variants supported. Trailing content after dash/colon/em-dash preserved in `note` field for downstream (item-4 embedded question stays captured).
- `a57f5df` — Daily Sync calibration c3: echo applied rule, user-facing parse errors (`reply_dispatch.py`, +148/-19). Per-item summary `Item N: <sender> — "<subject>" -> TIER (applied to K records)` instead of opaque count. Parse-failure messaging shifts from developer dump to user-facing with hint about "Same" shortcut.
- `ed12132` — Daily Sync calibration c4: sender display falls back to From header / domain (`email_section.py`, +117/-7). Fallback chain: resolved person record → From display name → domain → "(unknown)". Fixes the "(unknown) — Borrowell Credit Score Update" display paradox. No person records inferred; display-only.
- `dc7aae8` — Daily Sync calibration c5: group items by (sender, subject pattern) (`email_section.py` + `reply_dispatch.py` + `assembler.py`, +415/-107). Subject normalization strips trailing dates, numbers, month+year markers. Near-duplicate recurring emails collapse to one calibration ask; correction fan-outs to all cluster members. `_resolve_correction` signature extended to return `list[CorpusEntry]` (internal callers only).

## Validation

End-to-end check on Andrew's exact 2026-04-24 01:32 live reply text:

```
corrections: 5   unparsed: []
  item 1: tier=spam  note="not interested in their routine messages..."
  item 2: tier=spam  (chained from 1)
  item 3: tier=spam  (chained)
  item 4: tier=spam  note="also, why was this ranked lower..."
  item 5: ok=True
```

Before: 2/5 accepted, 3/5 in `unparsed`. After: 5/5 accepted, embedded question preserved in note for future handling.

## Outcome

The /calibrate feedback loop now works on natural list-reply style. Andrew can teach the classifier without having to restate intent per item or avoid periods in item bodies. c5's grouping also reduces daily Sync noise for recurring vendor mail — 4 Borrowell pings collapse to 1 calibration ask with the correction fan-out applied to all 4 records.

The embedded-question case (item 4's "why was this ranked lower?") is preserved but not yet answered — task #6 (embedded-question handling) deferred until post-dogfood in a few days when more real data accumulates.

## Alfred Learnings

- **Pattern validated**: the "c-series" discipline (one commit per logical change, numbered in order, with commit message referencing the series) makes arc-level validation easy. Each c is independently bisectable.
- **Pattern validated**: end-to-end validation against the exact user-reported input (Andrew's verbatim 5-item reply) caught that item 4's embedded question would have been lost if c2 didn't preserve trailing content.
- **Gotcha**: Builder estimated c2 at ~30 lines; actual was 147. Size overrun was justified (two-tier shape detection to avoid prose false-positives) but flagged. For parser work, triple the naive LOC estimate.
- **Anti-pattern avoided**: original plan called for per-commit session notes paired with ship. Skipped them in the heat of the session; caught by Andrew in end-of-day audit. Rule clarified in `feedback_session_notes_per_commit.md` item 7 — catch-up notes are acceptable when commits ship faster than notes.
