---
alfred_tags:
- software/alfred
- process/hygiene
created: '2026-04-15'
description: Catch up recurring uncommitted drift (surveyor labeler code-fence fix,
  six janitor session-note edits), add outer-repo .gitignore rules so vault inner-repo
  content stops appearing as untracked noise, and add a durable CLAUDE.md rule that
  every session must begin with a dirty-tree audit
intent: Stop the recurring pattern of uncommitted work accumulating across sessions
name: Repo Hygiene and Session-Start Rule
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Harden Vault Dedup at Python Layer 2026-04-15]]'
- '[[session/Catch-Up Commit Housekeeping 2026-04-15]]'
status: completed
tags:
- hygiene
- process
- gitignore
type: session
---

# Repo Hygiene and Session-Start Rule — 2026-04-15

## Intent

While wrapping up the dedup-harden commit earlier in this same calendar day, Andrew noted that uncommitted work keeps accumulating across sessions — we discover something mid-session, fix the immediate thing, and leave unrelated dirty files untouched for a future session that never gets prioritised. This commit lands the four smallest items on the backlog and installs a process rule to prevent the pattern from recurring.

## What Shipped

### Outer repo `.gitignore` — stop the vault-inner-repo noise

Added explicit exclusion for everything under `vault/` except `session/` and `process/`, which are the only two paths the outer alfred repo tracks. The rest of `vault/*` lives in the nested vault git repo used by the snapshot system, and adding any of it to the outer repo would double-track and defeat snapshots.

```
/vault/*
!/vault/session/
!/vault/process/
```

Before this change, `git status` on the outer repo showed 22 untracked `vault/*` directories on every run. That noise trained everyone to stop reading `git status` output carefully, which is exactly how unrelated real dirty code (e.g. the Layer 3 triage scaffolding) sat uncommitted across multiple sessions without being noticed. With the exclusion in place, `git status` only shows genuinely actionable outer-repo state, and real uncommitted work becomes immediately visible.

### `src/alfred/surveyor/labeler.py` — markdown code-fence tolerance

Added a small `_strip_code_fences` helper that extracts JSON content from responses wrapped in markdown code blocks. Applied it to both `_build_tags` and `_build_relationships` in the `Labeler` class before passing the text to `json.loads`. Also appended an explicit "Return the JSON array directly with no markdown code fences" line to the labeler's system prompt so compliant models stop wrapping in the first place.

This is defensive — the prompt asks for a raw JSON array, but some models wrap responses in ```json fences anyway, and the old code path raised `JSONDecodeError` and dropped the entire relationship batch. The helper handles the common cases (language-tagged fences, unlabeled fences, raw passthrough, surrounding prose, unterminated fences) and strips cleanly. Zero-risk change — if there's no fence, the text passes through unchanged.

Origin: this was dirty on top of commit `6996baa` ("Harden dedup prevention, surveyor labeler, and subprocess observability"), meaning additional labeler work happened after that commit and was never landed. Exact origin session is unknown, but the code reads as an obvious defensive fix for a real failure mode.

### Six vault session notes — janitor sweep drift catch-up

```
vault/session/Alfred Setup and Email Integration 2026-03-26.md      (+12 / -0)
vault/session/Catch-Up Commit Housekeeping 2026-04-15.md            (+30 / -0)
vault/session/Dedup Layers and Surveyor Tuning 2026-04-14.md         (+4 / -0)
vault/session/Email Pipeline and Knowledge Management 2026-04-02.md  (+9 / -3)
vault/session/Ollama Local LLM and System Buildout 2026-04-08.md     (+5 / -2)
vault/session/System Hardening and Agent Team 2026-04-14.md          (+9 / -2)
```

These are janitor-produced frontmatter edits (alfred_tags, relationships, janitor_note fields, description refinements) that have been accumulating across sessions as the janitor does its normal sweep work. None of them are content rewrites; they're frontmatter maintenance the janitor is supposed to do and that nothing has been committing.

### New CLAUDE.md rule — session-start dirty-tree audit

Added two bullets to the Team Lead Rules section:

1. **Session start requires a dirty-tree audit.** Classify every dirty and untracked outer-repo path as (a) commit now, (b) discard, (c) explicitly deferred with a reason. Deferred items must be flagged in a memory entry or session note so the next session doesn't rediscover them cold.
2. **Surgical staging when pre-existing dirty files are in scope.** If a session touches a file that already has unrelated prior-session drift, back up the file, revert to HEAD, re-apply only this session's hunks, commit, then restore the backup. Scope bleed is worse than git gymnastics.

Both rules grew directly out of this morning's experience — we spent real time untangling pre-existing Layer 3 scaffolding from the harden commit's `cli.py` hunks, and the recurring-drift problem had to be spotted by the user after two "scope-clean" commits had already happened on top of a dirty tree.

## What's still uncommitted after this pass

Intentionally deferred (documented in `project_next_session.md` memory):

1. **Layer 3 janitor triage queue** — 9 dirty files + 1 untracked file (`src/alfred/janitor/triage.py`, 241 lines). Coherent feature that needs its own focused review session against the two tentative decisions from the prior pickup note. Explicitly not committable piecemeal because the files import from each other. Memory entry updated to include Layer 3's current state as "in working tree, awaiting dedicated review session."

Nothing else is uncommitted in outer-repo scope. Inner vault repo has its own lifecycle via the snapshot system and is not the outer repo's concern.

## Alfred Learnings

### New Gotchas

- **Untracked-noise gitignore exclusion is a force multiplier.** The 22 `vault/*` untracked entries in every `git status` were actively harmful — they trained readers to stop parsing the status output, which let real uncommitted work hide. The fix is three lines of gitignore. Pattern for elsewhere: if a repo has persistent untracked noise from a known-external source, exclude it explicitly so actionable output stays visible.
- **"Already dirty" ≠ "dirty by this session."** A file can be modified but not by the work this session intends to commit. Stage by hunk, not by file, whenever a pre-existing dirty path is in scope. The restore-from-backup workaround (back up, revert to HEAD, re-apply session hunks, commit, restore backup) is a clean way to get hunk-level staging in a non-interactive environment where `git add -p` doesn't work.

### Patterns Validated

- **Session-start audit as durable discipline.** This session uncovered three separate classes of uncommitted drift (Layer 3 feature, labeler defensive fix, janitor session-note edits) that were all sitting in the tree because nobody had done a status audit at session start. Adding the rule to CLAUDE.md makes the discipline explicit and repeatable; hopefully the next session starts with a quick audit and doesn't accumulate a new backlog on top of the cleaned one.
- **Bundling related small hygiene changes into one commit is the right cadence.** Three separate commits for "gitignore," "labeler," "session note drift," and "CLAUDE.md rule" would be over-granular. They all share the same theme — repo hygiene — and landing them together with one session note captures the whole context in one place. The dedup-harden commits earlier today were rightly separate because they had distinct root-cause narratives; these do not.

### Corrections

- The earlier session note "Catch-Up Commit Housekeeping 2026-04-15" said the working tree was "clean except for the inner vault git repo's untracked content." That was inaccurate — there were 200+ LOC of Layer 3 scaffolding, a labeler defensive fix, and six session-note drift edits in the tree at the time. The inaccuracy wasn't deliberate; it was the same recurring pattern of not actually auditing `git status` output because the vault-inner-repo noise had made it unreadable. Corrected now.

### Missing Knowledge

- **Layer 3 janitor triage queue design checkpoint.** The untracked `triage.py` and its supporting files represent a half-built feature whose tentative design decisions haven't been reconfirmed this session. Before committing that work, the two decisions from `project_next_session.md` need explicit sign-off: (1) agent-creates-triage-tasks with Python tracking of `alfred_triage_id`, and (2) advisory-only (no auto-merge loop). Flagged for next session.
