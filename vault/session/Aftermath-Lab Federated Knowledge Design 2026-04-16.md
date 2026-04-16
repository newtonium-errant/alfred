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

### Origin agent as the curator

The aftermath-lab origin becomes its own Claude Code session with its own CLAUDE.md or SKILL.md that instructs:

1. **Start**: `git fetch --all` to pull latest from all registered forks
2. **Scan**: read every fork's `teams/*/candidate-patterns/`, `teams/*/candidate-gotchas/`, and `teams/*/session-notes/`
3. **Evaluate**: for each candidate, ask "is this generalizable across projects, or project-specific?" and "has this pattern appeared in multiple forks independently?" (cross-project convergence is a strong promotion signal)
4. **Promote**: write the generalizable ones into canonical `stack/`, `principles/`, or `architecture/`, attributed to the source project
5. **Log**: write a curation session note in canonical `session-notes/` (origin's own session trail)
6. **Push**: commit and push to origin's master

The origin agent doesn't modify any fork's content — it only reads forks and writes to canonical. The forks don't modify canonical — they only read canonical and write to themselves. Clean separation of write authority.

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
mkdir -p teams/alfred/{session-notes,candidate-patterns,candidate-gotchas}
# Write situation.md and mission.md
# Commit and push
```

### Per-project: register the fork on the canonical checkout

```bash
cd ~/aftermath-lab
git remote add alfred https://github.com/newtonium-errant/aftermath-alfred.git
```

## Daily Session Workflow (per project)

```bash
# SESSION START — pull canonical updates (optional, recommended weekly)
cd ~/aftermath-alfred
git pull upstream master
git push origin master    # keep fork in sync with canonical

# SESSION WORK — write session notes, candidate patterns, gotchas
# into teams/alfred/ as discoveries happen

# SESSION END — commit and push to fork
git add teams/alfred/
git commit -m "Session notes for 2026-04-16"
git push origin master
```

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

## Cross-Fork Visibility (the real value)

The origin agent's unique perspective is that it sees patterns emerging across ALL projects simultaneously. If Alfred's team and RxFax's team independently discover the same gotcha, origin spots the convergence and promotes with high confidence — "this appeared in two independent projects, it's real."

This cross-project signal is something no individual fork can see on its own. It's the federated learning loop: each project learns locally, contributes to a shared pool, and the curator determines what's generalizable.

Structurally parallel to the voice calibration mechanism designed in the same session: each user calibrates Alfred locally, Alfred progressively learns per-user, corrections feed back into the model. Same bidirectional feedback loop, different level of abstraction (team knowledge vs user understanding).

## Open Questions

1. **Origin curation cadence.** Manual review weekly? Monthly? Automated Claude Code session that runs periodically? Start manual, automate when the volume justifies it.
2. **Should project forks include their own `.claude/agents/` overrides?** Currently the four stack agents live in canonical. A project fork might want to add project-specific agents alongside the canonical ones. Convention TBD.
3. **What happens when a project is "done"?** Archive the fork repo, leave it read-only for origin to reference? Delete? Keep indefinitely? Probably archive.
4. **How does the origin agent handle conflicting patterns from different forks?** Two projects discover contradictory approaches to the same problem. Origin needs a resolution process — maybe promote both as alternatives with context, or mark one as superseding the other with a reason.
5. **Should session notes in the fork be a subset of the project's session notes, or a separate write?** Current answer: separate write. The fork gets a public contribution note, the project's own vault gets the full private session note. Same session, two artifacts.

## Implementation Plan

1. **Shut down other Claude Code sessions using aftermath-lab** (pending — user doing this now)
2. **Mark aftermath-lab as a template repo** — one `gh api` call
3. **Create `aftermath-alfred` from the template** — `gh repo create --template`
4. **Add `teams/alfred/` directory** with initial `situation.md` and `mission.md`
5. **Register the fork as a remote on canonical** — `git remote add alfred ...`
6. **Try the workflow for one session** — write a real session note and a candidate pattern, commit and push, verify it lands on the fork
7. **Try the curation flow once** — from the canonical checkout, `git fetch alfred`, read the candidate pattern, promote it to `stack/` via PR
8. **Expand to other projects** when the workflow is validated

Steps 2-5 are ~15 minutes of setup. Steps 6-7 are "try it once and see how it feels." Step 8 is future.

## Alfred Learnings

### Patterns Validated

- **Template repositories beat forks for same-account spawning.** GitHub's fork feature lives under a different account by design. Template repos give the "start from canonical" experience under the same account with the same permissions. Worth knowing for any future project-from-template scenario.
- **The `teams/` directory convention makes generic curation possible.** Because every fork puts contributions in the same place, the origin agent's scan logic is project-agnostic. "Read `teams/*/candidate-patterns/`" works for any fork without configuration. This is a simple convention with outsized value — it's the reason the curation flow can be automated later without project-specific logic.
- **Cross-project convergence is the highest-confidence signal for pattern promotion.** When two independent project teams discover the same pattern, it's almost certainly worth promoting to canonical. The origin agent should weight this signal heavily. This is the federated-learning insight applied to software development knowledge.

### New Gotchas

- **"Fork" means different things in different contexts.** GitHub's "fork" feature is specifically for cross-account repo copying. "Forking" as a concept (starting a new thing from a canonical thing) maps better to GitHub's "template repository" feature for same-account use. The terminology mismatch will confuse anyone reading docs or running commands — worth noting explicitly in any setup instructions.
- **`git pull upstream` can produce merge conflicts** if the fork has modified canonical files (which it shouldn't, per convention, but might accidentally). The session-start pull should probably be `git pull upstream master --ff-only` to fail fast rather than silently merge, so any accidental canonical-file modification surfaces immediately.
