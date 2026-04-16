---
alfred_tags:
- software/aftermath-lab
- software/alfred
- infrastructure/knowledge-sharing
created: '2026-04-16'
description: Designed and implemented the aftermath-lab federated knowledge sharing
  system. Marked aftermath-lab as a template repo, created aftermath-alfred as the
  first project fork, completed the first full curation cycle with two pattern
  promotions, and established the ILB convention, cross-team digests, team health
  monitoring, and the unrelated-histories setup SOP.
intent: Turn aftermath-lab from a one-way knowledge base into a bidirectional
  federated learning system with transparent curation and reasoning trails
name: Aftermath Federation Setup and First Curation
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Aftermath-Lab Federated Knowledge Design 2026-04-16]]'
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
- '[[session/Per-Tool Log Routing Refactor 2026-04-15]]'
- '[[session/Stop Surveyor Session Drift 2026-04-15]]'
status: completed
tags:
- aftermath-lab
- federation
- curation
- infrastructure
type: session
---

# Aftermath Federation Setup and First Curation — 2026-04-16

## Intent

Aftermath-lab (`github.com/newtonium-errant/aftermath-lab`) was a one-way knowledge base: projects consulted it, but never contributed back. This session designed, built, and validated a bidirectional federated knowledge system where project forks contribute candidate patterns and the aftermath-lab origin (Coding Alfred, department head of the coding domain) curates across all forks with transparent reasoning.

## What Shipped

### Design doc (committed to Alfred vault earlier this session)

`vault/session/Aftermath-Lab Federated Knowledge Design 2026-04-16.md` — the canonical design artifact covering template repos, teams/ directory convention, reviews/ neutral comms channel, archival principle, reasoning-as-institutional-memory core principle, cross-team digests, team health monitoring, ILB convention, the department-head authority model, and the full curation flow with dialogue.

### Infrastructure (across two GitHub repos)

**aftermath-lab (canonical)**:
- Marked as GitHub template repository (`is_template: true`)
- `digests/` directory created for future cross-team visibility
- `reviews/` directory created with two permanent decision records from the first curation
- `architecture/cli-logging.md` — first promoted pattern (from Alfred fork)
- `architecture/llm-gotchas.md` — first promoted gotcha (from Alfred fork), also first entry in a new LLM gotchas section (deliberate scope expansion by origin)

**aftermath-alfred (first project fork)**:
- Created from template (`github.com/newtonium-errant/aftermath-alfred`, private)
- Upstream remote configured, unrelated-histories merge completed (one-time SOP step)
- Registered as `alfred` remote on the canonical checkout
- `teams/alfred/` populated with:
  - `situation.md` and `mission.md` (with full session-start/during/end standing orders + ILB convention)
  - `session-notes/` — two session notes (2026-04-15 engineering marathon + 2026-04-16 federation setup)
  - `candidate-patterns/per-tool-log-routing.md` — submitted and promoted
  - `candidate-gotchas/classification-temperature-drift.md` — submitted and promoted
  - `reviews/` — two origin review records with reasoning and requests for team feedback
  - `archived/` — empty (nothing declined yet)

### First full curation cycle completed

The entire round-trip was validated:
1. Alfred team wrote candidates → pushed to fork
2. Origin (Coding Alfred) fetched → evaluated → wrote review records with reasoning to the fork
3. Origin promoted both candidates to canonical `architecture/` → wrote permanent decision records to canonical `reviews/`
4. Fork pulled promotions back via `git merge upstream/master`

Every step used standard git commands. No custom tooling.

### Standing orders added to CLAUDE.md

Alfred's CLAUDE.md Team Lead Rules now includes the aftermath-lab downstream sync as a session-start rule: pull upstream on `~/aftermath-alfred/`, check `teams/alfred/reviews/` for origin feedback, consult `stack/` and `principles/` before coding.

## Gotcha Discovered and Resolved

**Template-spawned repos have unrelated git histories.** The first `git pull upstream master` on aftermath-alfred failed with "refusing to merge unrelated histories" because GitHub's template feature creates a fresh initial commit (no shared ancestry with the template origin). The fix is `git merge upstream/master --allow-unrelated-histories` once, immediately after setup. Now baked into the per-project setup SOP as a CRITICAL step so future project forks never hit this during a normal session-start pull.

## Alfred Learnings

### Patterns Validated

- **The federated knowledge loop is the same shape as voice calibration.** Both are bidirectional feedback loops between a specialized instance and a canonical authority: local teams contribute knowledge upward, the authority curates and sends canonical patterns back down. The voice calibration mechanism (person record ↔ talker) and the aftermath-lab mechanism (project fork ↔ canonical origin) are implementations of the same meta-pattern at different levels of abstraction (user understanding vs team knowledge).
- **Department-head authority model with transparent reasoning makes curation work.** Origin considers local team reasoning but retains strategic authority to overrule when the broader cross-project view warrants it. The key: the override reasoning is always recorded. "Recommendations up, decisions down" — but with a permanent reasoning trail so the reasoning can be revisited with new evidence.
- **ILB (Intentionally Left Blank) closes the observability gap.** Without ILB, a team that doesn't contribute on a given session is indistinguishable from a team that didn't check in at all. ILB makes the absence intentional and auditable. Same principle as the janitor heartbeat log — observability over silence.

### New Gotchas

- **GitHub template repos produce unrelated histories.** Unlike true forks, template-spawned repos share no git ancestry. The first upstream merge requires `--allow-unrelated-histories`. After that, all future merges work normally. Now in the setup SOP.
- **Cross-repo PRs from template-spawned repos may not work the same as fork PRs.** Not fully tested yet — the first curation used direct commits to canonical rather than the full PR ceremony. Worth verifying the PR path when the second project fork is created.

### Missing Knowledge

- **The PR-based promotion flow hasn't been fully tested.** The design doc describes promotion via PR from fork to canonical, but the first curation used direct commits. The PR path may work differently for template-spawned repos than for true forks. Verify on the next promotion.
- **No automated curation tooling.** The first curation was manual (origin read candidates, wrote reviews, promoted). Automating the fetch → scan → digest → review-record-generation flow is future work. The manual process works and validates the design; automation comes when volume justifies it.
