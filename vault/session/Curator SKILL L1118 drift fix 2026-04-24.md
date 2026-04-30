---
type: session
status: completed
name: Curator SKILL L1118 drift fix
created: 2026-04-24
description: |-
  Fix the SKILL drift caught by `scripts/smoke_curator_scope.py` on its first
  run — the "Moving a record" example showed a non-inbox path move that
  curator's `move: inbox_only` scope rejects. Prompt-tuner task.
intent: Close the SKILL/scope mismatch so the smoke script exits 0 and the 33→32 accepted-invocation count reflects all-clean state. Prove that the smoke pattern catches real drift at commit time.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Distiller rebuild Week 0 transition 2026-04-24]]'
tags:
- curator
- skill
- scope
- prompt-tuner
- drift
---

# Curator SKILL L1118 drift fix

## Intent

The Week 0 curator smoke script `scripts/smoke_curator_scope.py` (shipped `5ada988`) surfaced a real SKILL/scope mismatch on its very first run: the "Moving a record" example at `src/alfred/_bundled/skills/vault-curator/SKILL.md` L1118 showed `alfred vault move "note/Old Name.md" "note/New Name.md"` — but curator scope has `move: inbox_only`. The paragraph below the example also said "DO NOT use `vault move` on inbox files", contradicting itself. Prompt-tuner's job to fix the SKILL, not the smoke script.

## Work Completed

One commit on master:

- `1914e71` — Curator SKILL: fix L1118 move example (smoke drift) (+6/-4 on `src/alfred/_bundled/skills/vault-curator/SKILL.md` L1116-L1120). Option B from the prompt-tuner's three options: remove the illegal shell example, replace with scope-boundary prose. Rationale: the SKILL already states in 3+ other places that the daemon auto-moves inbox files and the curator must NOT move inbox files — so a positive example would contradict the rest of the SKILL; option A (inbox-internal move example) would reinforce the wrong mental model; option C (delete section entirely) would leave no guidance for the "I want to rename a non-inbox record" case. Option B answers that: flag it, don't move it.

## Validation

Pre-fix: `scripts/smoke_curator_scope.py` exits 1 with one violation at L1118. Post-fix: exits 0, 32/32 invocations accepted, 0 rejects. Count dropped 33→32 (the illegal example was removed).

## Outcome

SKILL/scope contract in sync. The curator smoke script will now catch any future drift (both SKILL instructions added that forbidden scope would reject, AND scope tightenings that leave existing SKILL steps dead). Justifies the smoke-as-pre-commit pattern end-to-end: the script caught drift on its first run, giving operator a fix-today opportunity vs the 24h dead-step window that caused the Q3 2026-04-19 incident.

## Alfred Learnings

- **Pattern validated**: smoke-script-at-commit-time wins. The Q3 2026-04-19 24h dead-step window was the motivation; this is the first real catch. Ship similar smoke scripts for any SKILL-scope pair we keep (curator now, distiller/janitor soon if kept agentic — though rebuild plan retires their SKILLs).
- **Pattern validated (prompt-tuner workflow)**: three-option consideration before picking option B. Prompt-tuner read surrounding context in the SKILL (3+ places saying "don't move inbox files") before deciding. Good discipline — mechanical fix would have been option A (swap example to inbox path) but that contradicts the rest of the SKILL.
- **Gotcha**: the drift likely landed during one of the scope-narrowing commits (probably `2d5e8cf` Janitor scope narrowing or adjacent). CLAUDE.md's team-lead rule "scope/schema-narrowing commits trigger a SKILL audit in the same cycle" is the right post-mortem; this case was caught in hindsight by smoke.
