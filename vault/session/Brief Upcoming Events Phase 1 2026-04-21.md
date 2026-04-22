---
type: session
title: Brief — Upcoming Events section Phase 1
date: 2026-04-21
tags: [brief, axis-2, upcoming-events, vault, phase-1]
---

## Summary

Single commit `53d87c6` adds a forward-looking calendar slice to the
Morning Brief. Scans event records and task records, buckets them by
date relative to today, drops anything past a 30-day window. Phase 1
is intentionally rule-free — Andrew's directive: "build filter rules as
we go." Filter rules will grow inline as real-data patterns reveal what
counts as noise.

## Why

The brief currently reflects current state — health, weather, ops
retrospective. It tells Andrew **what's happening** but never **what's
coming up**. Email ingestion already pulls in records with future dates
(appointments, deadlines, renewals); they sit in the vault unsurfaced
until someone goes looking. This closes that gap with the smallest
possible surface area, giving Andrew a forward-looking section in the
06:00 ADT brief that names today/this-week/later items by reading the
vault directly.

## What changed

- New module `src/alfred/brief/upcoming_events.py` (~150 lines) — section
  renderer with `_iter_records`, `_collect_items`, `_bucket`,
  `_render_item`, and the public `render_upcoming_events_section(config,
  vault_path, today)` entry point.
- `src/alfred/brief/config.py` — `UpcomingEventsConfig` dataclass +
  parsing in `load_from_unified()`. Dataclass defaults match the YAML
  defaults so omitting the block is a no-op.
- `src/alfred/brief/daemon.py` — section appended after Operations.
  Updated the load-bearing `# Section order is load-bearing` comment to
  explain why Upcoming Events lives last (forward-looking; lower
  priority than now-state once readers have absorbed the rest).
- `config.yaml.example` — new `brief.upcoming_events` block documenting
  both keys with sensible default-preview comments.
- `tests/test_brief_upcoming_events.py` — 14 tests covering bucketing,
  cutoff, past exclusion, task with/without `due`, empty-state sentinel,
  `enabled=false` omission, location/description rendering, within-
  bucket sort order.

## Design decisions

- **Sources: `event` + `task`, NOT `remind_at`.** `remind_at` already
  drives the outbound transport scheduler. Surfacing the same record in
  both the brief AND a reminder push would duplicate user-visible
  output. Phase 2 candidate: dedup logic if/when this becomes noisy.
- **Buckets: Today / This Week / Later, 30-day cap.** The 30-day cap is
  the only filter on the input side. Anything further out gets dropped.
  The first place we'd loosen as we see real data.
- **Empty state is operator-visible.** Empty buckets are omitted. If all
  three are empty, the section emits "No upcoming events." rather than
  collapsing to nothing — silent crashes can't masquerade as quiet days.
- **`enabled: false` short-circuits cleanly.** The renderer returns ""
  as the omit signal; the daemon then drops the tuple entirely from the
  sections list. No empty header, no "section disabled" placeholder.
- **Frontmatter read inline via `frontmatter.load()`** rather than
  extending `vault_list()` API. `vault_list` returns only `name/path/
  type/status`; we need `date`/`due`/`location`/`description`. Inline
  read keeps the API change blast radius zero (one Phase-1 caller; not
  worth broadening a cross-tool helper). If perf becomes a problem at
  scale, swap to `vault_list("event") + vault_list("task")` two-pass.
- **No filter framework.** Andrew's explicit directive. No rule registry,
  no DSL, no priority classifier integration, no per-user calibration
  table. Rules grow inline in `upcoming_events.py` as cases appear.
  Each rule's commit message should cite the brief that surfaced the
  noise.
- **Section position: after Operations.** Forward-looking context is
  lower priority than now-state once the reader has absorbed health/
  weather/ops summaries.

## Phase-2 candidates surfaced (filed but not built)

Filed in `project_brief_upcoming_events_phase2.md` for inline addition
as cases appear. In expected likelihood order:

1. **Status filter on tasks** — `cancelled` / `done` tasks with future
   `due` will currently appear. Likely first to bite.
2. **Dedup with `remind_at`** — overlap with reminder pipeline becomes
   noise as both mature.
3. **Wikilink rendering** — names render as plain text; could become
   `[[event/Name]]` for click-through navigation in Obsidian.
4. **Vault-walk perf** — `_iter_records` walks the whole vault each
   brief; future scaling concern.

## Alfred Learnings

- **Pattern validated — pre-spec a Phase 1 with the user before
  spawning the builder.** I sketched the bucket boundaries, source set,
  empty-state behavior, config shape, and section position in chat
  before spawning, then asked for "green light on spec." Andrew nodded
  once; the builder shipped without scope drift. Compare to spawning
  with a vague "implement upcoming events" — would have produced
  something, then needed a redo on at least 2 of the 6 spec axes.
- **Pattern validated — explicit "no framework, rules inline" stance.**
  The original 2026-04-21 sketch in `project_brief_upcoming_events.md`
  proposed 7 filter axes (priority tier, source weighting, time-
  sensitivity, dedup, recurrence, per-user calibration, learning loop).
  Andrew's "build as we go" directive cut that to zero. The Phase 1
  ship is ~150 lines instead of an estimable 800+ for a flexible filter
  framework that would have been wrong on first contact with real data.
- **Pattern — flag follow-ups at filing time, not at discovery time.**
  The builder volunteered four Phase-2 candidates in the report rather
  than ignoring them. Each is now memo'd with a "shape" so the next
  session can implement without rediscovering. Costs ~3 min at filing,
  saves ~30 min per future session.
- **Inline-frontmatter-read works fine at current scale.** No need to
  extend `vault_list()` for one caller. The "smaller blast radius"
  reasoning (one Phase-1 caller, no cross-tool API change) generalizes
  — when in doubt, do the local thing first; broaden the API only when
  a second caller appears.

## Next

Phase 1 is in production starting tomorrow's 06:00 ADT brief. Real-data
review pass should happen on the first 1-2 briefs to catch the most
obvious Phase-2 candidate (probably the status filter on cancelled/done
tasks). Otherwise the next priority is whatever Andrew picks from the
roadmap — STAY-C Phase 1 on Axis 1, or another Axis 2 item (email
surfacing, person-record scope on talker, etc.).
