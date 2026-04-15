---
alfred_tags:
- software/alfred
- design/voice
- design/calibration
created: '2026-04-15'
description: Revision changelog for the voice and calibration design doc landed earlier
  in commit 9765b8e. Captures four design refinements that came out of the user's
  follow-up answers to the outstanding-questions list, including the calibration
  section restructure (delimited section in person record instead of a new profile
  type), the Knowledge Alfred home for generative mode, the 3-4 starting confirmation
  policy with self-tuning, and the move of multi-session stitching from "open question"
  to "future growth"
intent: Persist the four design refinements that emerged after the canonical doc
  was first committed, without losing the structure of the original artifact
name: Voice Design Doc Revision
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
status: completed
tags:
- design
- voice
- calibration
- revision
type: session
---

# Voice Design Doc Revision — 2026-04-15

## Intent

The canonical voice + calibration design doc landed earlier today in commit `9765b8e`. Four follow-up answers from the user's review of the outstanding-questions list produced design refinements substantial enough to warrant updating the doc — but small enough that they fit cleanly into the existing structure rather than requiring a new artifact. This commit captures those revisions.

## What Changed

### 1. Calibration restructured: delimited section in person record, not a new type

**Was:** `profile/Andrew Newton.md` as a new entity type with `type: profile` frontmatter.

**Now:** A delimited `<!-- ALFRED:CALIBRATION -->` ... `<!-- END ALFRED:CALIBRATION -->` section inside the existing `person/Andrew Newton.md` record, marked by `alfred_calibration: true` in frontmatter. Reuses the existing `<!-- ALFRED:DYNAMIC -->` pattern from `vault/CLAUDE.md`. Zero schema changes.

**Why this is better:**
- Single source of truth per person — facts and Alfred's mental model live in the same record
- Reuses an established convention rather than adding a new one
- No new entries in `KNOWN_TYPES` / `TYPE_DIRECTORY`
- The pattern generalises cleanly: any record where Alfred wants to maintain a behavioral model can carry a calibration section (`org/{name}.md`, `project/Alfred.md` self-model, etc.)

**Primary user(s) flagged at the instance level** via a new `talker.primary_users` config field listing the `person/` records the talker should treat as calibration targets. Single-user, dual-user (couples), and small-team instances all work via the same field with no code changes.

**Distiller change required when implementing**: the distiller must skip content inside `<!-- ALFRED:CALIBRATION -->` blocks when extracting learnings from person records — that's Alfred's own model, not user-authored claims to distill from. Same treatment as `<!-- ALFRED:DYNAMIC -->` blocks. Flagged as a Stage 2a sub-task.

### 2. Generative mode home: Knowledge Alfred, not "story-writer instance"

**Was:** "Story-writer instance" as the home for generative mode.

**Now:** **Knowledge Alfred** — the planned instance for **all writing work, both fiction and non-fiction**. One of the five instances flagged in the existing multi-instance design memory. This is more general than "story writer" and matches how the user actually thinks about it.

Knowledge Alfred deserves a callout because it's the instance that uses every voice mode meaningfully:
- Grounded mode for non-fiction work where vault context (research notes, prior drafts, references) is the whole point
- Generative mode for fiction and brainstorming where vault grounding would inhibit creativity
- Brainstorm-capture mode for long-form ideation that becomes a structured note afterward

Knowledge Alfred's `person/Andrew Newton.md` calibration section will reflect Andrew's WRITING preferences specifically, not his operational ones — different style cues, different push-back patterns, different Current Priorities. Same person, different facet. This is precisely why per-instance calibration matters.

### 3. Confirmation policy: 3-4 starting point with self-tuning

**Was:** "Explicit confirmation for confident claims, silent append for `[needs confirmation]` entries. Tunable."

**Now:** A 1-5 dial where 1 is fully silent and 5 is fully explicit, **starting at 3-4** during validation. Plus the killer addition: **self-tuning over time**. Once Alfred has accumulated enough confirmed calibrations and observed a sustained low correction rate, it should be able to **recommend lowering the validation frequency itself**: "I've predicted your responses on the last 20 reflections without a correction. Want me to drop validation from 4 to 3 going forward?"

This makes the validation frequency itself part of the bidirectional calibration loop — Alfred learns not just the user's beliefs but also how much it can trust its own model of those beliefs. Meta-calibration. The user stays in control either way; Alfred just makes recommendations when its confidence supports them.

### 4. Multi-session stitching: explicit future growth, not open question

**Was:** Listed under "Open Questions and Deferred Decisions" as item 4 — "Multi-session stitching for journaling. Can a journaling session span multiple separate conversations across days?"

**Now:** Promoted to a new **"Future Growth (Explicitly Deferred Beyond Phase 1)"** section, with an explicit description of what the future enhancement looks like: a `continues_from` frontmatter field linking related session records into multi-day journaling threads, with Alfred surfacing "this picks up where you left off in session/X yesterday" at the start of a continuation. Worth doing when journaling becomes a regular practice and fragmented threads become felt.

The "Open Questions" section now contains only the legitimately-undecided build-time choices (STT provider, default voice, phone-side companion stack, wake word fallback). Multi-session stitching has a clear future shape; it's just deferred, not open.

## Files Changed

- `vault/session/Voice Chat and Calibration Design 2026-04-15.md` — substantial rewrite of the "Bidirectional Calibration" section, plus targeted edits to "Generative mode," "Stage 4a," "Multi-Instance Implications," "Confirmation policy," "Stage 2a sub-bullets," and the new "Future Growth" section
- `~/.claude/projects/-home-andrew-alfred/memory/project_voice_roadmap.md` — matched all four refinements in the memory entry so future sessions see the current shape

## Alfred Learnings

### Patterns Validated

- **Question lists drive design refinement.** I committed the design doc with a "go with my plan" mandate but explicitly listed five outstanding items the user hadn't confirmed. Three of them produced meaningful refinements when the user did look at them — the calibration restructure (which is materially better than the original), the Knowledge Alfred clarification (which prevents a confusing terminology drift), and the self-tuning confirmation policy (which is a genuine new design idea that emerged from the act of asking). The "list what's still undecided" practice is worth keeping for any design doc, even when the user has said "go ahead."
- **Reframing a question can produce a better answer.** The user asked "would it be better to create a new type, or just somewhere flag who the primary user(s) for that instance are?" That single question pushed me from "new type with one record per primary user" to "delimited section in existing record with primary users flagged in instance config." The reframing was more productive than the original framing — it let me spot that calibration is BEHAVIOR CONFIGURATION, not a new entity, and that "primary users" is an instance config concern, not a per-record one. Worth noticing: when you're stuck in a decision frame, questioning the frame itself can unlock the answer.
- **Self-tuning is a natural feature of bidirectional calibration.** I wouldn't have suggested the self-tuning confirmation policy on my own — the user added it. But once it's in the design, it's clearly correct: if Alfred is calibrating to the user, it should also be calibrating its own validation frequency. The mechanism is the same. Worth thinking about: any time Alfred has a "configurable parameter" that affects user experience, ask whether Alfred itself should be able to recommend tuning it based on observed outcomes. Probably yes, more often than not.
