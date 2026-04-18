---
type: session
status: completed
name: Voice wk3 — Calibration migration
created: 2026-04-18
description: Commit 3 of 8 in Voice Stage 2a-wk3 — populate the calibration block on Andrew's person record from the claude-memory files, delete the stale user-profile.md scaffold.
intent: Make the commit 2 read path see actual data so the first wk3 voice session has a real calibration prefix, and retire the orphaned user-profile.md that duplicated the intent in a shape nothing reads.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs:
- '[[person/Andrew Newton]]'
related:
- '[[session/Voice wk3 — Calibration IO 2026-04-18]]'
tags:
- voice
- talker
- wk3
- calibration
- migration
---

# Voice wk3 — Calibration migration

## Intent

Commit 2 added the reader and the cache slot, but the calibration block had to actually exist somewhere before the bot could inject it. Commit 3 is vault-edit-only — no code, no tests — and moves the content by reading the four relevant claude-memory files (`user_andrew.md`, `feedback_save_session_notes.md`, `feedback_session_notes_per_commit.md`, `feedback_use_aftermath_team.md`), extracting the user-profile-shaped content, and composing it into the canonical subsections on Andrew's person record.

The old `vault/user-profile.md` scaffold is deleted — it was a template the user never filled in, it wasn't a standard vault type (carried a `FM001` janitor note flagging it), and nothing read from it. The calibration block on the person record supersedes it.

## Work Completed

- `vault/person/Andrew Newton.md`:
  - Added `alfred_calibration: true` to frontmatter so the distiller (commit 4) and future tools can detect calibration-bearing records by flag instead of regex.
  - Inserted an `<!-- ALFRED:CALIBRATION --> ... <!-- END ALFRED:CALIBRATION -->` block between the title heading and the `person.base` embed sections. Subsections:
    - **Communication Style** — RCAF cadence, proactive-save preference (`_source: memory/user_andrew.md_`, `memory/feedback_save_session_notes.md`).
    - **Workflow Preferences** — WSL2 hardware, session-notes-per-commit, aftermath-lab consultation (`memory/user_andrew.md`, `memory/feedback_session_notes_per_commit.md`, `memory/feedback_use_aftermath_team.md`).
    - **Current Priorities** — RRTS, NP practice / Medical Alfred, Alfred multi-instance, RxFax (all from `memory/user_andrew.md`).
    - **What Alfred Is Still Unsure About** — two open questions Alfred hasn't confirmed yet (no source — these are Alfred's own uncertainty).
    - **Model Preferences (learned)** — empty; populated by commit 8's `propose_default_flip` over time.
  - Each populated bullet carries an `_source: memory/<file>_` italic attribution per the team-lead decision on open question #2.
- `vault/user-profile.md`: deleted.
- No code touched. `pytest`: 60 tests pass (unchanged from commit 2).

## Outcome

The first wk3 voice session Andrew starts will now see a real calibration prefix in the system prompt. The content is deliberately conservative — only items explicitly stated in the claude-memory files were migrated, plus two explicit-uncertainty items so commit 7's writer has something to update at close time.

## Alfred Learnings

- **Pattern validated**: migrating profile content into a fenced marker block on the existing person record, rather than a new top-level file, means existing person.base Dataview queries and wikilink-backlinks continue to work unchanged. Would have been tempting to create `person/Andrew Newton — Calibration.md` as a standalone record; that would have doubled the janitor's work (backlink audit, type schema exception) and made the close-time writer need to search/select across two files.
- **Pattern validated**: `alfred_calibration: true` as a frontmatter flag is cheaper to query than a body-regex scan. Distiller commit (4) strips on regex anyway, but future tools that want "all records with calibration" can use the flag without reading bodies.
- **Gotcha surfaced**: the existing `janitor_note: LINK001` on Andrew's record references `org/Rural Route Transportation` which doesn't exist. Left untouched because it's out of wk3 scope, but flagging here — a follow-up should either create that org record or retarget the link.
- **Missing knowledge candidate**: there's no documented convention for what "What Alfred Is Still Unsure About" should contain at initial migration. I picked two real open items (edit-vs-preference conflict behaviour, session-type classification in practice) rather than leaving it empty, so commit 7's writer has a shape to append onto. Worth documenting in the voice design doc at the next revision.
