---
alfred_tags:
- software/aftermath-lab
- design/knowledge-sharing
created: '2026-04-16'
description: Design doc for improving aftermath-lab's knowledge flow from one-way
  (canonical → projects) to bidirectional (projects contribute back via template-repo
  forks with a teams/ directory convention and an origin agent that curates across
  all forks)
intent: Persist the federated knowledge sharing design for aftermath-lab so future
  sessions can implement from it
name: Aftermath-Lab Federated Knowledge Design
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Voice Chat and Calibration Design 2026-04-15]]'
status: completed
tags:
- design
- aftermath-lab
- knowledge-sharing
- roadmap
type: session
---

# Aftermath-Lab Federated Knowledge Design — 2026-04-16

## Intent

Aftermath-lab (`github.com/newtonium-errant/aftermath-lab`) is a shared development knowledge base containing proven patterns, anti-patterns, and conventions extracted from production projects. It has a stack-specialist agent team (frontend, n8n-backend, qa-ux, supabase-db) and pattern docs organized by technology.

Currently, knowledge flows ONE way: projects consult aftermath-lab's patterns before building. Local teams accumulate their own knowledge — session notes, new patterns, confirmed anti-patterns, gotchas — but that knowledge is trapped in each project's local workspace with no mechanism to flow back to canonical or sideways to other projects.

This design makes the flow bidirectional: each project gets its own private repo (forked from aftermath-lab via GitHub's template-repository feature) where it writes contributions, and the aftermath-lab origin gains the ability to read across all project forks, curate, and promote generalizable patterns back to canonical.

## Current State of Aftermath-Lab

- **GitHub**: `github.com/newtonium-errant/aftermath-lab`, single `master` branch
- **Structure**:
  - `CLAUDE.md` — top-level knowledge-base rules and knowledge map
  - `.claude/agents/` — four stack-specialist agents: `frontend.md`, `n8n-backend.md`, `qa-ux.md`, `supabase-db.md`
  - `stack/` — pattern docs by technology (n8n, supabase, frontend, auth), each with patterns.md, anti-patterns.md, gotchas.md
  - `principles/` — development.md, team-operations.md, qa-process.md
  - `architecture/` — data-flow.md, deployment.md, testing.md
- **Projects that consult it**: Alfred (RRTS), RxFax, potentially future projects using the same stack
- **The connection between projects and aftermath-lab is consultative, not derived**: each project has its own agent team (e.g. Alfred has builder/vault-reviewer/prompt-tuner/infra/code-reviewer, which are NOT instances of aftermath-lab's four stack agents). The agents consult aftermath-lab docs when doing stack work, but they're separate teams with separate instruction files.

## Core Principle: Reasoning as Institutional Memory

Every decision in this system — promote, decline, defer, override, archive — carries a short summary of WHY it was made. Not a full essay; just enough for a future agent, arriving with zero context about today's conversation, to understand the reasoning and judge whether it still holds.

This matters because the agents making and consuming these decisions will change across sessions. Today's Coding Alfred will not be the same context window as next month's Coding Alfred. A local team's Claude Code session ends and starts fresh. The only thing that persists across those boundaries is the written record. If the reasoning isn't in the record, it's lost — and future agents either repeat the same evaluation from scratch (wasteful) or make a different decision without knowing why the first one was made (dangerous).

The principle applies everywhere in Alfred, not just aftermath-lab:
- **Voice calibration**: each bullet in the `<!-- ALFRED:CALIBRATION -->` section has an attributed source session and, for corrections, a note about what the previous belief was and why it changed
- **Layer 3 triage tasks**: the `candidates` list and the triage ID trace back to the specific DUP001 that triggered the decision
- **Aftermath-lab reviews**: the review records capture both origin's reasoning and local team's reasoning, permanently linked to the archived pattern if declined
- **Distiller learnings**: assumptions, decisions, constraints, contradictions, syntheses all carry evidence chains and source references

The standard is: **a future agent reading this record should be able to understand what was decided, why, what alternatives were considered, and whether the reasoning still applies given what it knows now.** Short is fine. Absent is not.

Critically: **decisions are not permanent.** A future agent presented with new evidence — a second project discovering the same pattern, a change in the stack, a correction from the user — can and should revisit prior decisions. The reasoning record isn't a lock; it's a starting point. "Here's what was decided, here's why. Has anything changed?" If yes, the agent writes a new review record linked to the old one, explaining what new evidence prompted the revision. The chain of reasoning grows over time, and each link makes the next agent's decision more informed rather than starting from zero.

This is the same self-correcting loop as the voice calibration mechanism: Alfred's model of the user starts wrong, gets corrected, improves. Aftermath-lab's canonical knowledge starts incomplete, gets enriched by project contributions, improves. Both are designed to change their minds when presented with more evidence — and to record why they changed, so the next change is even better informed.

## Key Design Decisions

### Template repository, not GitHub forks

GitHub's native fork feature requires the fork to live under a DIFFERENT account from the original. Since all repos live under `newtonium-errant`, true forks aren't practical. Instead, aftermath-lab is marked as a **template repository** (a GitHub setting), and each project creates a new repo FROM that template via `gh repo create --template`. The new repo starts as a snapshot of aftermath-lab's content, lives under the same account, and is independently owned.

The template-spawned repos are called "forks" conceptually throughout this doc because that's what they function as — each one starts from canonical, diverges with project-specific content, and can contribute back. The GitHub feature doing the work is `is_template`, not the fork button.

### `teams/<project>/` directory convention

Every project fork adds its contributions under a `teams/<project>/` directory at the repo root. Canonical content (`stack/`, `principles/`, `architecture/`, `.claude/agents/`) stays untouched in the fork unless the project is specifically making a change to PR back to canonical.

Directory structure inside each fork's `teams/<project>/`:

```
teams/<project>/
  situation.md              # what this project is (military-style context doc)
  mission.md                # current objectives
  session-notes/            # public session notes from this project's work
    2026-04-15-layer-3.md
    2026-04-15-voice-design.md
  candidate-patterns/       # patterns discovered, ready for origin to evaluate
    per-tool-log-routing.md
    profile-section-as-calibration.md
  candidate-gotchas/        # gotchas confirmed, ready for origin to evaluate
    alfred-down-orphans-children.md
    setup-logging-hardcoded.md
  reviews/                  # neutral communications channel (origin ↔ team)
    2026-04-16-origin-promote-log-routing.md
    2026-04-17-team-context-log-routing.md
  archived/                 # declined candidates, linked to their review conversations
    declined-orphan-cleanup.md
```

The `teams/` convention is the same across all forks. This means the origin agent's curation logic is generic: "for each registered fork, read `teams/*/candidate-patterns/*.md` and evaluate." No project-specific code in the curation path.

### Two knowledge flows

**Flow 1 — Canonical → Project (downstream).** Each project fork pulls canonical updates via `git pull upstream master`. If origin adds a new pattern to `stack/n8n/gotchas.md`, every fork that pulls receives it. Standard git; no custom tooling.

**Flow 2 — Projects → Canonical (upstream curation).** The aftermath-lab origin reads FROM all registered forks and decides what to incorporate into canonical. The mechanism:

1. Each fork is registered as a git remote on the canonical checkout:
   ```bash
   git remote add alfred https://github.com/newtonium-errant/aftermath-alfred.git
   git remote add rxfax https://github.com/newtonium-errant/aftermath-rxfax.git
   ```
2. `git fetch --all` pulls all forks' content without merging
3. `git show alfred/master:teams/alfred/candidate-patterns/per-tool-log-routing.md` reads any fork's content
4. `git diff master...alfred/master -- teams/` shows what the fork has added since it was spawned
5. Origin evaluates each candidate and either promotes to canonical, leaves at project level, or flags for review

Every operation is a standard git command. No custom tooling needed.

### Origin agent as Coding Alfred — the department head

The aftermath-lab origin is not a neutral librarian filing patterns. It IS **Coding Alfred** — instance #5 in the five-instance Alfred architecture, the department head of the coding domain. As a department head, it has:

- **Domain authority**: makes final decisions about what belongs in canonical, considering but not bound by local team reasoning
- **Strategic view**: sees all forks simultaneously, can spot cross-project convergence, and can evaluate patterns against the overall direction of the stack — not just whether they worked for one project
- **Write authority over canonical**: only origin promotes patterns to `stack/`, `principles/`, `architecture/`. Local teams contribute candidates; origin decides.

This mirrors the multi-instance hierarchy: each Alfred instance is a department head of its domain (Ops coordinates, Business owns RRTS decisions, Knowledge owns writing decisions, Medical owns clinical decisions, Coding owns stack/engineering decisions). Recommendations flow up from project teams; decisions flow down from the department head. Input is valued and considered, but the department head's broader view wins when there's a conflict.

### The neutral communications channel: `reviews/`

Curation decisions need transparent reasoning — both the origin's reasoning for promoting or declining, and the local team's context that might affect the decision. Without this, local teams see patterns appear (or not) in canonical and have to reverse-engineer why. That's the same opacity problem we identified with Alfred's voice calibration: if decisions aren't transparent, the affected party can't course-correct.

**The `reviews/` directory** is the neutral communications channel between origin and each fork. Both parties write to it. Conversations are structured markdown records in git — permanent, versioned, auditable.

In each fork:
```
teams/<project>/reviews/
  2026-04-16-origin-promote-log-routing.md     ← origin writes its recommendation + reasoning
  2026-04-17-team-context-log-routing.md       ← team responds with context or pushback
```

In canonical aftermath-lab:
```
reviews/
  2026-04-16-promoted-log-routing-from-alfred.md     ← permanent decision record with both sides' reasoning
  2026-04-18-declined-orphan-cleanup-from-rxfax.md   ← and the reasoning for declining
```

A review record looks like:

```markdown
---
type: review
from: origin
to: teams/alfred
date: 2026-04-16
subject: per-tool-log-routing
decision: promote
confidence: high
---

## Promote "Per-Tool Log Routing" to Canonical

**Source**: teams/alfred/candidate-patterns/per-tool-log-routing.md
**Destination**: stack/n8n/per-tool-log-routing.md

### Why promote
This pattern addresses how CLI handlers route log events to per-tool files.
Both Alfred and RxFax discovered it independently — cross-project convergence
is the strongest promotion signal. The pattern is stack-level, not project-level.

### What I'd generalize
Remove Alfred-specific references. Generalize the helper signature. Note the
suppress_stdout caveat for JSON-emitting handlers.

### Request for team
Any context I'm missing? Anything Alfred-specific that should NOT be in the
canonical version?
```

The local team responds:

```markdown
---
type: review
from: teams/alfred
to: origin
date: 2026-04-17
subject: per-tool-log-routing
in-response-to: 2026-04-16-origin-promote-log-routing.md
---

## Context for Per-Tool Log Routing

The suppress_stdout caveat is load-bearing — without it, any CLI handler that
emits JSON on stdout breaks if a log handler leaks there. In Alfred this was
cmd_vault specifically, but it applies to any tool that uses structured stdout.

Also: the existing _setup_logging_from_config helper was the root cause — it
hardcodes alfred.log as the destination regardless of which tool calls it.
Worth noting in the canonical pattern as the anti-pattern to avoid.
```

Origin reads the response, incorporates the context into the canonical version, and writes a permanent decision record in canonical's `reviews/`:

```markdown
---
type: review-decision
from: origin
date: 2026-04-17
subject: per-tool-log-routing
decision: promoted
sources:
  - teams/alfred/candidate-patterns/per-tool-log-routing.md
  - teams/rxfax/candidate-gotchas/cli-log-misdirection.md
promoted-to: stack/n8n/per-tool-log-routing.md
---

## Decision: Promoted with team context incorporated

Cross-project convergence (Alfred + RxFax). Incorporated Alfred team's note
about suppress_stdout being load-bearing for JSON-emitting handlers, and the
anti-pattern of hardcoding log destinations in shared helpers. RxFax team
did not provide additional context within the review window.
```

### Curation flow (updated with dialogue)

The aftermath-lab origin (Coding Alfred) runs curation sessions with this flow:

1. **Fetch**: `git fetch --all` to pull latest from all registered forks
2. **Scan**: read every fork's `teams/*/candidate-patterns/`, `teams/*/candidate-gotchas/`, and `teams/*/session-notes/`
3. **Evaluate**: for each candidate, assess generalizability. Cross-project convergence (same pattern in multiple forks independently) is the strongest promotion signal.
4. **Write review records**: for each evaluated candidate, write a review record in the fork's `teams/<project>/reviews/` explaining the recommendation (promote, decline, defer, request-context) and the reasoning. Push to the fork's remote.
5. **Wait for response** (optional, configurable): give local teams a review window to respond with context or pushback. Could be immediate (origin decides now, team can respond retroactively) or deferred (origin waits for a response before acting). Phase 1: immediate decisions with retroactive response.
6. **Make the final decision**: origin reads any responses, incorporates relevant context, but retains authority to decide differently if the broader strategic view warrants it. The decision record in canonical's `reviews/` captures BOTH the local team's reasoning AND origin's reasoning, so the "why" is transparent even when origin overrides.
7. **Execute**: if promoting, write the canonical pattern into `stack/`, `principles/`, or `architecture/`, attributed to the source project(s). Write the permanent decision record in canonical's `reviews/`.
8. **Log**: write a curation session note in canonical's own session trail
9. **Push**: commit and push to origin's master

**Key authority principle**: origin considers local team reasoning seriously but is not bound by it. Origin has the cross-project strategic view, the domain expertise, and the authority to make decisions that individual project teams can't see the full picture for. A local team saying "don't promote this, it's project-specific" might be overruled by origin saying "actually, this appeared independently in three projects — you just can't see the other two from where you sit." The reasoning for the override is always recorded.

**Key transparency principle**: every decision — promote, decline, defer, override — has a review record with reasoning from both sides. No black-box curation. If a local team wants to understand why their pattern was declined (or why a pattern they didn't suggest was promoted), the answer is in `reviews/`.

**Key archival principle**: patterns that origin declines to promote are NOT deleted. The local team moves them from `candidate-patterns/` (or `candidate-gotchas/`) to an `archived/` directory within their `teams/<project>/` space, with a link to the review conversation that explains why it was declined. Archived patterns remain discoverable and searchable — they may be project-specific today but become promotable later if a second project independently discovers the same pattern (at which point origin has the cross-project convergence signal it lacked before). The archive also serves as institutional memory: "we considered this, here's why we decided not to generalize it, and here's the conversation that led to that decision."

Archive structure inside each fork:
```
teams/<project>/
  archived/
    declined-orphan-cleanup.md        ← moved from candidate-patterns/
    declined-mutex-lock-pattern.md
```

Each archived file carries a frontmatter link to its review conversation:
```markdown
---
archived: 2026-04-18
review: reviews/2026-04-17-origin-decline-orphan-cleanup.md
reason: project-specific — relies on Alfred's vault snapshot system which is not part of the standard stack
---
```

If a second project later discovers the same pattern independently, origin can revisit the archived version alongside the new candidate, see the prior reasoning, and decide whether the convergence now justifies promotion. The archive is a living record, not a graveyard.

### Sanitization: standard secret hygiene only

The forks are private repos under the same GitHub account. Personal data (names, paths, project details) is acceptable content — these aren't public. The only sanitization concern is standard secret hygiene:

- `.gitignore` for `.env`, `credentials.json`, `*.pem`, `*.key`, `config.local.*`, etc.
- GitHub's built-in secret scanning (runs on all repos, alerts on accidental key commits)
- No automated PII detection needed; no two-tier public/private session notes needed

If the repos ever go public or get shared outside the account, sanitization scope would expand. For now, private + secret scanning is sufficient.

## Setup Steps

### One-time: mark aftermath-lab as a template

```bash
gh api -X PATCH /repos/newtonium-errant/aftermath-lab -f is_template=true
```

### Per-project: create a fork from the template

```bash
# Create the new private repo from the template
gh repo create newtonium-errant/aftermath-alfred \
  --template newtonium-errant/aftermath-lab \
  --private \
  --description "Aftermath notes from the Alfred project"

# Clone locally
cd ~
git clone https://github.com/newtonium-errant/aftermath-alfred.git
cd aftermath-alfred

# Set up upstream remote for pulling canonical updates
git remote add upstream https://github.com/newtonium-errant/aftermath-lab.git

# Create the teams/ directory structure
mkdir -p teams/alfred/{session-notes,candidate-patterns,candidate-gotchas,reviews,archived}
# Write situation.md and mission.md
# Commit and push
```

### Per-project: register the fork on the canonical checkout

```bash
cd ~/aftermath-lab
git remote add alfred https://github.com/newtonium-errant/aftermath-alfred.git
```

## Daily Session Workflow (per project)

### Session start (every session, before new work)

```bash
# 1. Pull canonical updates
cd ~/aftermath-alfred
git pull upstream master
git push origin master    # keep fork in sync with canonical

# 2. Check teams/<project>/reviews/ for origin feedback
# 3. Consult stack/ and principles/ before coding
```

The upstream pull is a durable standing order (documented in Alfred's CLAUDE.md Team Lead Rules and in the fork's `teams/alfred/mission.md`). If the pull conflicts, resolve before starting — conflicts mean canonical files were accidentally modified in the fork.

### During session

Write candidate patterns and gotchas to `teams/<project>/candidate-patterns/` and `candidate-gotchas/` as discoveries happen. Include reasoning: why the pattern exists, what problem it solves, and whether you believe it's project-specific or generalizable. Origin makes the final call, but the team's assessment helps.

### Session end

1. **Review the session for aftermath-lab relevance.** Did we discover patterns, gotchas, or learnings the stack would care about?
2. **If relevant**: write candidates to their directories and a short session note (~20-30 lines, curated summary with pointers) to `teams/<project>/session-notes/`.
3. **If not relevant**: write an **ILB** (Intentionally Left Blank) entry to `teams/<project>/session-notes/`. This makes the review auditable — a future agent sees "this session was checked and nothing was relevant" rather than wondering whether anyone looked.
   ```markdown
   ---
   date: 2026-04-16
   project: alfred
   ilb: true
   ---
   ILB — Session reviewed, no aftermath-lab-relevant content.
   Work this session: [one-line summary so origin knows what was reviewed]
   ```
4. **Commit and push**:
   ```bash
   cd ~/aftermath-alfred
   git add teams/<project>/
   git commit -m "Session notes for <date>"
   git push origin master
   ```

The ILB convention ensures origin's health monitoring never confuses "team checked in with nothing to report" with "team didn't check in at all." An ILB entry resets the last-activity clock the same way a full contribution does.

## Contribution Back to Canonical (promotion flow)

When a candidate pattern is ready to be promoted — proven, generalizable, worth sharing:

```bash
cd ~/aftermath-alfred
git checkout -b promote-per-tool-log-routing

# Move from candidate to canonical location
git mv teams/alfred/candidate-patterns/per-tool-log-routing.md stack/n8n/per-tool-log-routing.md
# Edit to make project-agnostic
git commit -am "Promote per-tool log routing pattern from Alfred to canonical"
git push origin promote-per-tool-log-routing

# Open a PR from fork to origin
gh pr create \
  --repo newtonium-errant/aftermath-lab \
  --base master \
  --head newtonium-errant:promote-per-tool-log-routing \
  --title "Add per-tool log routing pattern from Alfred" \
  --body "Discovered during Alfred session 2026-04-15. Applies to any CLI tool with per-subcommand log files."
```

Origin reviews (self-review for personal use) and merges. Pattern becomes canonical. All forks get it on their next `git pull upstream master`.

## Cross-Fork Visibility

The origin agent's unique perspective is that it sees patterns emerging across ALL projects simultaneously. If Alfred's team and RxFax's team independently discover the same gotcha, origin spots the convergence and promotes with high confidence — "this appeared in two independent projects, it's real."

This cross-project signal is something no individual fork can see on its own. It's the federated learning loop: each project learns locally, contributes to a shared pool, and the curator determines what's generalizable.

Structurally parallel to the voice calibration mechanism designed in the same session: each user calibrates Alfred locally, Alfred progressively learns per-user, corrections feed back into the model. Same bidirectional feedback loop, different level of abstraction (team knowledge vs user understanding).

### Cross-team digests

Local teams currently can't see each other's contributions directly — each fork is an isolated repo. To bridge this without giving every fork read access to every other fork (which adds complexity and may leak project-specific context), **origin writes a periodic digest to canonical** that summarizes recent activity across all forks.

```
aftermath-lab/ (canonical)
  digests/
    2026-04-16.md
    2026-04-09.md
```

A digest summarizes each team's recent session notes, new candidate patterns/gotchas, and origin's review status:

```markdown
---
date: 2026-04-16
teams_active: [alfred, rxfax]
teams_silent: []
generated_by: origin
---

# Cross-Team Digest — 2026-04-16

## teams/alfred (Alfred — RRTS)
- **Session**: dedup hardening, surveyor drift root-cause, Layer 3 triage queue
- **New candidate pattern**: per-tool-log-routing
- **New candidate gotcha**: classification-temperature-drift
- **Origin status**: pending review

## teams/rxfax (RxFax)
- **Session**: invoice workflow refactor, Supabase migration 0047
- **New candidate pattern**: supabase-upsert-with-conflict
- **Origin status**: promoted to stack/supabase/patterns.md on 2026-04-14
```

Every fork receives the digest on its next `git pull upstream master`. In 30 seconds of reading, a local team knows what every other team has been working on and whether there's overlap. Benefits:

1. **Early pattern recognition** — a team sees another team's candidate and thinks "we hit something similar." The convergence signal appears before origin curates, accelerating the promotion pipeline.
2. **Avoiding duplicate work** — a team sees a candidate already submitted by another team and adds context to the existing one instead of writing their own version.
3. **Cross-pollination** — a pattern from one domain might inspire a solution in another, even across different project types.

The digest is origin exercising its department-head role: "here's what your peers are working on, in case it's relevant to you." Same authority, same neutral channel, same reasoning-first principle — broadcast rather than point-to-point.

**Cadence**: the digest is a natural byproduct of origin's curation session. When origin fetches and scans all forks, writing a 20-line digest at the end is ~2 minutes of extra work. The cadence matches the curation cadence.

### Team health monitoring

Origin tracks the **last activity date** for each registered fork. If a project hasn't checked in (pushed to its `teams/` directory) or answered an outstanding review request for more than a configurable window (default: two weeks), origin flags it in the digest:

```markdown
## Health Check

- **teams/rxfax**: last activity 2026-03-28 (19 days ago) ⚠️
  - Outstanding review request: 2026-03-30-origin-promote-upsert-pattern.md (unanswered 17 days)
  - Possible causes: project on hold, team not running aftermath session-end workflow, or system not working
- **teams/alfred**: last activity 2026-04-15 (1 day ago) ✓
```

The flag is deliberately neutral — "possible causes" includes legitimate ones (project is paused, team is on vacation, no relevant work happened) alongside systemic ones (the session-end ritual isn't being followed, the fork isn't being pushed to). Origin doesn't assume the worst; it surfaces the data and lets the user investigate.

**Why this matters**: the federated system only works if local teams actually contribute. If a team goes silent, the system degrades silently — no error message, no crash, just an absence of signal. The health check makes the absence visible. It's the same principle as the janitor heartbeat log (`daemon.triage_scan`) that makes "the helper didn't fire" diagnosable in 5 seconds: observability over silence.

If origin identifies a team that's been silent for an extended period and all review requests are unanswered, it can escalate in the digest: "teams/rxfax has been silent for 30 days with 2 unanswered reviews. Recommend checking whether the project is active and whether the session-end workflow is being followed." The user reads the digest and decides whether to investigate or accept the silence as intentional.

## Open Questions

1. **Origin curation cadence.** Manual review weekly? Monthly? Automated Claude Code session that runs periodically? Start manual, automate when the volume justifies it.
2. **Should project forks include their own `.claude/agents/` overrides?** Currently the four stack agents live in canonical. A project fork might want to add project-specific agents alongside the canonical ones. Convention TBD.
3. **What happens when a project is "done"?** Archive the fork repo, leave it read-only for origin to reference? Delete? Keep indefinitely? Probably archive.
4. **How does the origin agent handle conflicting patterns from different forks?** Two projects discover contradictory approaches to the same problem. Origin needs a resolution process — maybe promote both as alternatives with context, or mark one as superseding the other with a reason.
5. **Should session notes in the fork be a subset of the project's session notes, or a separate write?** Current answer: separate write. The fork gets a public contribution note, the project's own vault gets the full private session note. Same session, two artifacts.

## Implementation Plan

Steps 1–6 completed on 2026-04-16. Steps 7–8 are future.

1. ~~**Shut down other Claude Code sessions using aftermath-lab**~~ — done
2. ~~**Mark aftermath-lab as a template repo**~~ — done (`gh api -X PATCH ... -f is_template=true`)
3. ~~**Create `aftermath-alfred` from the template**~~ — done (`github.com/newtonium-errant/aftermath-alfred`, private)
4. ~~**Add `teams/alfred/` directory**~~ — done (situation.md, mission.md with standing orders + ILB convention, 5 subdirs: session-notes, candidate-patterns, candidate-gotchas, reviews, archived)
5. ~~**Register the fork as a remote on canonical**~~ — done (`git remote add alfred ...` on `~/aftermath-lab/`, verified with `git fetch alfred` + `git show alfred/master:teams/alfred/*`)
6. ~~**First real contribution**~~ — done (session note for 2026-04-15, two candidate patterns: per-tool-log-routing and classification-temperature-drift, verified visible from canonical via `git show`)
7. **Try the curation flow once** — from the canonical checkout, `git fetch alfred`, read a candidate pattern, write a review record, promote or decline via PR. This is the first exercise of origin's department-head role. Not done yet.
8. **Expand to other projects** when the workflow is validated. Next candidates: RxFax (if still active), future projects as they come online.
9. **Write the first digest** — after at least two forks exist, write the first cross-team digest to `digests/`. Requires step 8 for meaningful cross-team content.

## Alfred Learnings

### Patterns Validated

- **Template repositories beat forks for same-account spawning.** GitHub's fork feature lives under a different account by design. Template repos give the "start from canonical" experience under the same account with the same permissions. Worth knowing for any future project-from-template scenario.
- **The `teams/` directory convention makes generic curation possible.** Because every fork puts contributions in the same place, the origin agent's scan logic is project-agnostic. "Read `teams/*/candidate-patterns/`" works for any fork without configuration. This is a simple convention with outsized value — it's the reason the curation flow can be automated later without project-specific logic.
- **Cross-project convergence is the highest-confidence signal for pattern promotion.** When two independent project teams discover the same pattern, it's almost certainly worth promoting to canonical. The origin agent should weight this signal heavily. This is the federated-learning insight applied to software development knowledge.

### New Gotchas

- **"Fork" means different things in different contexts.** GitHub's "fork" feature is specifically for cross-account repo copying. "Forking" as a concept (starting a new thing from a canonical thing) maps better to GitHub's "template repository" feature for same-account use. The terminology mismatch will confuse anyone reading docs or running commands — worth noting explicitly in any setup instructions.
- **`git pull upstream` can produce merge conflicts** if the fork has modified canonical files (which it shouldn't, per convention, but might accidentally). The session-start pull should probably be `git pull upstream master --ff-only` to fail fast rather than silently merge, so any accidental canonical-file modification surfaces immediately.
