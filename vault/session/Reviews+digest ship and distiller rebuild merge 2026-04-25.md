---
type: session
name: Reviews+digest ship and distiller rebuild merge 2026-04-25
date: '2026-04-25'
project:
- '[[project/Alfred]]'
status: final
tags:
- multi-instance
- distiller-rebuild
- kalle
- merge-arc
---

# Reviews + digest ship and distiller rebuild merge 2026-04-25

Long session covering two merged arcs: KAL-LE reviews+digest extension shipped via builder + prompt-tuner sequencing in a worktree, and the distiller rebuild branch merged to master with v2 going live in shadow mode. Filed three plans for post-merge work (drift mitigation, synthesis layer, KAL-LE distiller-as-radar) and a Pat (HypatiaErrantBot) bot identity created for the eventual H.Y.P.A.T.I.A. instance.

## Context

Resumed `feature/propose-person` from a prior session that crashed pre-Cursor-WSL-migration. Builder spawn was blocked on Claude Code 2.1.119 agent-registry reload — solved by session restart (per `feedback_claude_code_agent_reload`). Working tree clean at `8aafba8`; rebuild/distiller branch had 11 commits of Week 2 v2 work pending.

## What shipped

### KAL-LE reviews + digest arc (8 commits)

Builder spawned in worktree-isolation, shipped 5 commits ~2180 LOC; prompt-tuner SKILL update on top; two follow-ups for promotion-detection AND-gate (Andrew chose A+C: keyword regex + ADD-in-canonical-dir + "we looked, found nothing" empty-state) and recurrences unbounded (Andrew chose unbounded over windowed).

Final commits on master: `17a5825` `cbd5c38` `d0c9a73` `ee45fef` `db8c5ab` `e74d773` `5d0a9fe` `884d09b`.

What shipped: `alfred reviews {write,list,read,mark-addressed}` with `author: kal-le` discriminator that prevents collision with the 4 existing human-authored reviews; `alfred digest {write,preview}` deterministic Sunday 07:00 Halifax weekly writer with 5 sections; bash_exec extended to admit `alfred` first-token + two-level inner allowlist; SKILL.md advertises new surfaces.

### Distiller rebuild → master season

Restored Salem from feature/propose-person to rebuild/distiller via 5-step plan: fast-forward feature/propose-person to worktree tip → checkout rebuild/distiller and merge feature/propose-person → restart Salem on rebuild/distiller (distiller v2 booted clean) → ran first v2 vs legacy comparison → fast-forwarded master to rebuild/distiller (28 commits landed) → pushed to origin/master.

v2 went live in shadow mode (`use_deterministic_v2: true`), wrote 90 records to `data/shadow/distiller/` from today's first parallel-run pass (40 sources processed, run_id `0b74532f`). Legacy distiller continues writing to live vault in parallel.

### Daemon-generated work

Committed 12 vault/process files modified by today's manual deep sweep (20 fixes) + surveyor relabeling pass as `22b0aa2` "Vault: daemon-generated work — 2026-04-25 deep sweep + surveyor relabeling".

### Pat (H.Y.P.A.T.I.A.) bot identity

BotFather created `@HypatiaErrantBot` with HTTP API token. Saved in memory as `reference_hypatia_bot.md`. Pat added as 4th column to instance rollout ledger (mostly 🔧 needs config / ❓ decisions). No `config.pat.yaml` yet — bot identity stand-by ahead of instance launch.

## Decisions made

- **A+C for promotion detection** in digest section 2: keyword regex `\b(promot|canonical|curat)\b` + ADD-in-`{architecture,stack,principles}/` AND-gate, plus "Last detected: <X>" explicit-empty signal across sections 1/2/5
- **Unbounded recurrences** in digest section 5: drop window-bounded gating; topics with disagreement archives stay surfaced regardless of when they last fired
- **AIR MILES confidence**: legacy `low` correct; v2 `high` was over-confident on a marketing-email-derived "active state" assumption. 1 data point, watch for systematic pattern
- **Daily v2 vs legacy comparison** during parallel-run window — Andrew watches, doesn't pre-commit cutover until calibration confidence settles
- **Model B hybrid as post-merge arc** — atoms always-on (v2 cheap), weekly synthesis pass clusters atoms into essay records via surveyor's embedding stack; bidirectional `based_on` wikilinks; cluster-membership-gated re-runs (drift mitigation by design)
- **Drift mitigation Option 2 (body-hash gate, ~30 LOC) ratified**; Option 3 (audit-log mutation-source gate) deferred with coverage-gap rationale (audit log misses direct Obsidian edits)

## Plans filed for later

- `project_distiller_synthesis_layer.md` — Model B hybrid, 5 phases (P1 schema → P2 surveyor extension → P3 synthesis writer with revised legacy prompt → P4 cluster-stability gate → P5 iteration)
- `project_distiller_drift_mitigation.md` — Option 2 active, Option 3 contingent
- `project_kalle_distiller_radar.md` — KAL-LE distiller as surfacing engine, 4 phases + optional embedding-pattern-miner. Fills digest section 4's LLM-synthesis TODO.

## Bugs found / known issues

- **KAL-LE distiller crashes** — `FileState.__init__() got an unexpected keyword argument 'last_scanned'`. Config has no `distiller:` block; orchestrator starts distiller with defaults that conflict with rebuild branch's schema. Fix needed: skip distiller when no config block, or add `distiller.enabled: false`. Filed in rollout ledger.
- **KAL-LE missing inbox** — `/home/andrew/aftermath-lab/inbox` doesn't exist; curator errors. Small ticket.
- **Comparison harness mtime-contamination** — when janitor's deep sweep runs in same window, legacy records are mtime-bumped and the harness's `--since` filter inflates the "legacy orphans" count. Real today's legacy output was ~46 records; harness saw 198. Worth fixing in a future harness pass (source-link matching + last-distilled timestamp join instead of mtime).
- **v2 confidence over-rating watch** — single AIR MILES case observed; not yet a confirmed pattern. Filed in `feedback_distiller_v2_calibration.md`.
- **Source-link namespace mismatch** between v2 (`source/...`) and legacy (`process/...`) — small follow-up.

## Alfred Learnings

### New gotchas

- **Builder agent worktree was forked from base, not from feature tip.** When agent runs with `isolation: "worktree"`, the worktree is forked from the parent agent's HEAD at spawn time. If the parent agent has moved (we'd added 6 propose-person/vault/agents commits since the worktree was conceived), the worktree branch is BEHIND on the propose-person line. Required `git rebase feature/propose-person` from inside the worktree before `git merge --ff-only` could work. Worth noting for future multi-step builder arcs that span sessions.
- **Distiller config-yaml leakage between branches.** Andrew's working `config.yaml` had `use_deterministic_v2: true` from rebuild branch experimentation, but `feature/propose-person` (forked from older master) didn't have the matching dataclass field. Salem crashed on startup with `ExtractionConfig.__init__() got an unexpected keyword argument`. Pattern to watch: config.yaml is gitignored / lives in working tree only; switching branches keeps the config but may not match the code. When config has features the branch's code doesn't support, the daemon won't start. Fix-path: stay on a branch where code matches config (rebuild/distiller in this case), OR comment out the leaked feature.
- **`alfred janitor scan --deep` is not a CLI flag** — deep sweep is an internal daemon path triggered by schedule, not exposed via CLI. To force a deep sweep, edit `config.yaml` `deep_sweep_schedule.time` to a time ~15 min in the future and restart the daemon. State seeding logic at `janitor/daemon.py:456` makes this fire on first interval tick.
- **`git worktree remove` refuses removal when locked by an agent.** The Claude Code harness locks agent-isolation worktrees by parent PID, even after the agent has reported done — the lock persists until the agent is fully terminated. `git worktree remove -f -f` (double-force) overrides the lock, safe when work is fully merged. The ExitWorktree tool ONLY handles `EnterWorktree`-created worktrees, not agent-isolation worktrees.

### Patterns validated

- **Builder + prompt-tuner sequenced in same worktree.** Builder shipped code commits; prompt-tuner added SKILL update commit on top of builder's tip. Both fast-forwarded together in one branch operation. Clean and avoided the "scope/SKILL drift bundled-same-cycle" rule from CLAUDE.md.
- **`git branch -f master <ref>` for ref-only fast-forward** when working tree has uncommitted dirty files. Cleaner than `git checkout master && git merge --ff-only` because it doesn't disturb working tree. Used today to land 28 commits into master without disrupting Salem's running daemons.
- **Two-stage Andrew-decision arc on builder follow-ups** — surface the deviations the builder flagged, get a per-question yes/no, then dispatch a tiny follow-up to the builder agent. Avoided builder over-asking; Andrew got crisp framing of each tradeoff before committing.

### Anti-patterns confirmed

- **mtime as a re-extraction gate is unreliable** when other tools (janitor) bump mtime without semantic content change. Mtime → content-hash for the body is the right gate. (This is the ratified Option 2 in `project_distiller_drift_mitigation.md`.)
- **Full-agentic LLM extraction is fundamentally non-deterministic** — Layer (b) drift can't be fixed by gating, only by replacing the extractor itself with a Pydantic-validated single-call pipeline (which v2 does).
- **Title-fuzzy-match is the wrong primary identity for v2 vs legacy comparison** — they produce different artifact shapes (atom card vs essay record). The harness's 0-agreement was a schema-mismatch artifact, not a quality verdict. Per-source comparison via `source_links` field is more informative.

### Corrections

- **Rollout ledger had distiller marked ✅ for KAL-LE** — actually broken (FileState schema crash). Updated to ❌ with note.

### Missing knowledge / surprised by

- **Audit log captures Alfred-mediated mutations only.** Direct Obsidian edits by Andrew on a vault file bypass `mutation_log.py` and aren't in `data/vault_audit.log`. So a gate based purely on audit-log filtering would skip user edits. This is why Option 3 (mutation-source gate) was deferred — coverage gap makes it unsuitable as a standalone.
- **Surveyor's work counted in audit log dominates other tools' contribution** — last 500 audit entries showed surveyor=333 / distiller=130 / janitor=37. Surveyor writes `alfred_tags` and `relationships` fields constantly across vault, mtime-bumping records that other tools then notice as "changed."
- **v2 produces atomic claim cards by design, not essay records.** I had to look at actual record contents to realize v2 vs legacy aren't competing on the same artifact — they're producing fundamentally different shapes. The rebuild plan documented this ("non-agentic + Pydantic-validated") but the implication for "what the comparison harness shows" wasn't obvious until the per-source side-by-side read.
- **The 5-instance roster's column ordering on the rollout ledger is somewhat arbitrary** — added Pat after STAY-C. V.E.R.A. still doesn't have a column even though it's in the active-instances list. Could revisit when V.E.R.A. work begins.

## Roadmap state at session end

- Salem on master at `22b0aa2`, pushed
- v2 distiller live in shadow, daily comparison cadence committed
- Janitor schedule reverted to `02:30` ADT (overnight)
- KAL-LE running with two known issues (distiller crash, missing inbox)
- 4 plans filed for post-merge work (drift mitigation, synthesis layer, KAL-LE distiller-as-radar, rollout ledger updates)
- Pat bot identity created, awaiting instance launch
