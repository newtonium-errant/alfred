---
type: session
status: completed
name: Distiller rebuild research proposals preserved
created: 2026-04-24
description: Commit the two-team architectural research proposals (stabilize vs rebuild) to docs/proposals/ for durability. Captures ~6 hours of Plan-agent research effort that drove the rebuild direction.
intent: Move the research artifacts from /tmp (ephemeral) into the repo so future sessions can reference them, and the thinking behind the distiller rebuild decision is auditable.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Distiller rebuild Week 0 transition 2026-04-24]]'
- '[[session/Distiller rebuild Week 1 MVP 2026-04-24]]'
tags:
- rebuild
- architecture
- docs
- research
---

# Distiller rebuild research proposals preserved

## Intent

Two Plan agents produced independent architectural proposals (Team 1: stabilize-in-place, Team 2: deterministic-first rebuild) plus cross-critique debate responses during the 2026-04-24 foundation-research exercise. Proposals were ephemerally in `/tmp/alfred-research/`. Andrew asked them to be preserved in the repo for durability.

## Work Completed

One commit on master:

- `b93ce1e` — Docs: preserve distiller rebuild research proposals (+511, two files). Copied `/tmp/alfred-research/team1_stabilize.md` and `team2_rebuild.md` to `docs/proposals/distiller-rebuild-team1-stabilize.md` and `docs/proposals/distiller-rebuild-team2-rebuild.md`. No formatting changes. Both proposals land on master AND (via subsequent branching from this HEAD) on the `rebuild/distiller` branch.

## Outcome

Research artifacts durable. The rebuild decision, its failure-mode catalog (F1a/F1b/F1c parse-failure decomposition), and the converged Week 0 + Week 1 plan are all traceable from the repo going forward. Future sessions can reference the specific trade-offs without needing to re-spawn the research.

## Alfred Learnings

- **Pattern validated (see also `feedback_architectural_debate_pattern.md`)**: two Plan agents with opposite framings + cross-critique debate round produces sharper diagnosis than a single agent. Both teams reached the same F1a/F1b/F1c decomposition of the 1194 parse failures despite working independently — signal that the diagnosis is real, not an agent's preferred story.
- **Pattern validated**: preserve research artifacts in the repo when the research is load-bearing for the decision. `/tmp` is fine for the workshopping phase; `docs/proposals/` is where cost-of-rediscovery material belongs.
- **Gotcha**: `CLAUDE.md` says "NEVER create documentation files (*.md) or README files unless explicitly requested by the User." The research proposals are an explicitly-requested exception. Default-deny on docs creation remains the right posture for incidental work.
