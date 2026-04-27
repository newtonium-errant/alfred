---
type: session
name: Hypatia Phase 1 ship and QA standard ratification 2026-04-26-27
date: '2026-04-27'
project:
- '[[project/Alfred]]'
status: final
tags:
- multi-instance
- hypatia
- qa-standard
- phase-1
---

# Hypatia Phase 1 ship and QA standard ratification 2026-04-26-27

Long arc spanning 2 days: scaffolded Hypatia (5th instance, scholar/scribe/editor) from zero to operational MVP with voice modes + business drafting + per-instance session-save + Opus-uniform temperature handling. Validated end-to-end via Telegram. Ratified two new standing team procedures (QA review pass + iteration loop + live-boot smoke). Filed 4 deferred Phase 2 follow-ups. Ten commits to master; all pushed.

## Context

Started with bot identity created (`@HypatiaErrantBot`) but no `config.pat.yaml`, no vault, no SKILL. Andrew sketched scope earlier in the session: business document drafting (Hypatia drafts, Andrew approves) + voice modes (conversation as scribe; capture as silent recorder) + creative copy-edit deferred to Phase 2. Vault location decided as `~/library-alexandria/` (Hypatia-thematic, not aftermath-prefix).

## What shipped (10 commits to master)

| Commit | Summary |
|---|---|
| `cbaa0f4` | Orchestrator: skip curator/janitor/distiller when no config block |
| `781bd1d` | Distiller: skip-distill gate uses body_hash, not full-file md5 |
| `(scaffold)` | `alfred instance new pat` + `~/library-alexandria/` vault scaffold + 9-section business-plan template + git init |
| `68a41bc` | Hypatia: register tool_set explicitly in VAULT_TOOLS_BY_SET |
| `754bdf6` | Hypatia: vault scope entry + create-types allowlist |
| `31b1b36` | Talker: template reply-context attribution by instance name |
| `427c489` | Talker: align reply-context fallback literal with InstanceConfig default |
| `b0217c2` | Vault: scope-aware `_validate_type` unblocks Hypatia/KAL-LE creates |
| `b8c843d` | Talker: per-instance scope routing in `_execute_tool` dispatcher |
| `78b954d` | Config: `InstanceConfig.name` required (no "Alfred" instance default) |
| `39a852a` | Hypatia SKILL.md: scholar/scribe/interlocutor instance prompt — Phase 1 MVP |
| `66b2e85` | Hypatia SKILL: address QA findings — Phase 2 leak, propose-person CLI, worked-example redirect, near-miss bad example, scholar-tone, mode-name consistency |
| `4def10d` | Talker: per-instance session-save shape, Hypatia mode-prefixed filenames |
| `b501535` | Hypatia SKILL: peer protocol honest about chat-time limitation, Andrew-as-bridge fallback |
| `4f0a243` | Hypatia SKILL: address P1 review findings — collapse Mode 1 dup, RRTS type gloss, Phase 2 note tightening |
| `644f21d` | Hypatia capture: centralize Opus temperature-strip across all SDK call sites |

Plus daemon-generated vault commits + the library-alexandria first commit (in `~/library-alexandria/` repo).

## Decisions made

- **Vault location**: `~/library-alexandria/` (Hypatia-thematic; departs from aftermath- prefix convention by intent)
- **Naming convention**: "Hypatia" formal everywhere (config, docs, signatures, brief headers, Daily Sync identity); "Pat" only as casual chat nickname. Env vars + filenames + data dir all renamed mid-session from `pat` to `hypatia` shorthand to enforce this rule
- **MVP scope**: Option C (full-shape — voice + business + Daily Sync + brief contribution + distiller + creative mode in Phase 2). Voice modes are not deferred; they're core to Hypatia's identity per Andrew's role spec.
- **Substack publication**: Andrew publishes manually; returns published URL to Hypatia for state update
- **Template flow**: Andrew sketches → Hypatia refines via voice session → both maintain
- **Daily Sync cadence**: daily, conditional on new material; quiet days emit "intentionally left blank" signal
- **Brief contribution**: project status + deadlines + reminders, via existing peer-digest pattern
- **InstanceConfig.name default**: required (option a), not empty-string fallback. Required-field semantics fail loud at config load; production blast radius confirmed zero (all 3 live configs explicitly set name)
- **Peer protocol architecture**: option (b) — patch SKILL to be honest about chat-time limitation and document Andrew-as-bridge workflow. Option (a) (add peer-query as a vault-level tool) deferred to Phase 2

## Standing procedures ratified (`feedback_qa_review_standard.md`)

This was a meta-thread that produced reusable team procedures:

1. **Every prompt-tuner ship gets a review-only second-pass** before fast-forward. Pattern proven on Hypatia SKILL — caught 2 P0 issues (Phase 2 vault_edit leak; propose-person flow described without CLI surface) and 4 P1 issues that would have shipped as wrong behavior. Asymmetric cost: deployed bad prompts produce silent wrong behavior; reviewer pass is cheap.

2. **Significant builder ships get code-reviewer pass** (was already in the agent list as "use proactively" but not actually invoked — recurring gotcha across projects). Trivial builder work (mechanical config tweaks, test-only) skippable. "Significant" = scope rules, schema, multi-instance plumbing, prompts-via-config, public API contracts, production code paths.

3. **Iteration loop** (extended): when reviewer finds fixable issues blocking the goal → send back to builder/prompt-tuner with findings as the brief → builder addresses → reviewer reviews the fix → confirm CLOSED → only THEN fast-forward. Don't trust intent-of-fix; require proof-of-fix.

4. **Decision authority during iteration**:
   - Minor decisions (within previously-discussed scope, low catastrophic risk, accurate fix path, reviewer's recommendation unambiguous) → parent agent calls it
   - Serious decisions (architectural shape, multi-path tradeoff, beyond previously-ratified scope) → bring to Andrew before acting

5. **Live-boot smoke required when daemon startup is in scope** (added 2026-04-26 after instructor regression slipped past static review). Code-reviewer must actually start the affected daemon, wait ~30s for spawn-and-stabilize, verify no `TypeError` / `Process alfred-X:` patterns, confirm idle-tick events. Static review alone is insufficient when daemon startup paths are touched.

## Bugs found and fixed

1. **Instructor regression on required `instance.name`** — `78b954d` made the field required in both telegram + instructor InstanceConfig dataclasses, but no config had an `instructor.instance` block. All 3 instances (Salem/KAL-LE/Hypatia) crashed instructor on next restart. **Fix**: added `instance:` block to instructor section in all 3 configs. Code-reviewer iteration-2 missed this in static review; this is what triggered the live-boot smoke amendment.

2. **`alfred instance new` scaffold's launch suggestion is wrong** — scaffold says `--only talker,transport,instructor` but `transport` isn't a daemon name (transport server runs INSIDE talker daemon). Worked around by running without `--only`. Worth fixing in the scaffold separately.

3. **`alfred janitor scan --deep` is not a CLI flag** (carryover learning) — deep sweep is internal daemon path, not exposed via CLI. To force a deep sweep, edit `config.yaml` `deep_sweep_schedule.time` to a near-future time and restart.

4. **Worktree filesystem confusion** — builder agent's `pip install -e` from worktree pinned the venv at the worktree path. When parent cleaned up the worktree post-merge, venv broke. **Pattern**: re-pin venv to `/home/andrew/alfred` after fast-forwarding any worktree-spawned editable install.

5. **`Capture` (no slash) instead of `/capture`** — Andrew typed "Capture" not `/capture`; Hypatia accepted it as the trigger anyway. Convention established in earlier Salem session, carried over.

6. **Anthropic Opus rejects `temperature` parameter** — `conversation.py` had inline strip; `capture_batch.py` and `capture_extract.py` didn't. Hypatia (first instance on Opus) hit it on first capture-extract attempt. **Fix** (`644f21d`): centralized `messages_create_kwargs()` helper at `src/alfred/telegram/_anthropic_compat.py`, applied to all 6 telegram-package call sites.

## Memos filed

- `feedback_qa_review_standard.md` — review pass + iteration loop + decision authority + live-boot smoke
- `feedback_hardcoding_and_alfred_naming.md` — single-instance hardcoded literals + "Alfred" as instance name default antipatterns
- `feedback_session_start_sweeps.md` — manual overnight sweep trigger when daemons missed the window
- `feedback_distiller_v2_calibration.md` — single-data-point watch on AIR MILES over-confidence
- `feedback_sdk_quirk_centralization.md` — SDK quirks belong in shared helper from day one
- `project_path_c_architecture.md` — hybrid local/API architecture for sensitive instances
- `project_distiller_drift_mitigation.md` — body-content-hash gate (Option 2 ratified, Option 3 deferred)
- `project_distiller_synthesis_layer.md` — Model B hybrid plan (post-merge arc)
- `project_hardcoding_followups.md` — 5 deferred sweep items
- `project_pat_mvp.md` → renamed `project_hypatia_mvp.md` — Hypatia MVP plan
- `project_hypatia_phase2_followups.md` — 4 deferred items from Phase 1 validation
- `reference_hypatia_bot.md` — bot identity + token

## Validation evidence (2026-04-27 Telegram session)

- ✅ Identity sanity (Hypatia formal + Pat alias acknowledged correctly)
- ✅ Business mode initiation (asked clarifying questions about audience, factual context)
- ✅ Hard guardrail held (refused to fabricate RRTS revenue, said "Ask Salem; she'd be the one holding that")
- ✅ Peer-protocol honesty (named the chat-time limitation, offered Andrew-as-bridge with verbatim relay prompt)
- ✅ Session save lands at `session/conversation-<date>-<slug>-<id>.md` and `session/capture-<date>-<slug>-<id>.md` per spec
- ✅ Capture mode silent during recording, accepted both `Capture` and `/capture` triggers
- ❌→✅ Capture extraction Opus 400 — fixed via temperature-strip centralization
- 📋 Mode 2 substantive overshoot — Andrew valued it ("Hypatia was genuinely more helpful than expected"); calibration data filed for Phase 2 SKILL iteration

## Alfred Learnings

### New gotchas

- **Required-field config schema changes break daemons that don't have the new block.** Adding `name: str` (required) to `InstanceConfig` broke instructor on all 3 instances because no config had an `instructor.instance` block. Static tests passed (unit tests on `InstanceConfig` in isolation); the integration gap (instructor expects an `instance:` block) only surfaces at daemon spawn. Lesson: when promoting a default-value to required, sweep all existing configs for the block's presence FIRST, fail loud at config-load if missing, AND live-boot smoke before issuing PASS verdict.

- **`alfred instance new` scaffold has stale launch instructions.** Scaffold suggests `--only talker,transport,instructor` but `transport` isn't a `TOOL_RUNNERS` daemon name. Worth a separate fix.

- **Editable install in venv pins to the path it was installed from.** If a builder runs `pip install -e .` from a worktree, the live venv now points at the worktree's `src/`. Removing the worktree post-merge breaks the venv. **Pattern**: re-pin to `/home/andrew/alfred` after fast-forwarding any worktree-installed branch.

- **SDK parameter quirks must live in a shared helper from the FIRST call site.** Anthropic Opus rejects `temperature`. `conversation.py` had the rule inline; `capture_batch.py` and `capture_extract.py` didn't. Salem on Sonnet never hit it; Hypatia on Opus bit on day one. The second call site is when the bug bites, not when it's discovered. Centralize from the first.

### Patterns validated

- **Review-fix-confirm iteration loop** caught real P0/P1 issues that would have shipped as wrong behavior. The 2 P0s on Hypatia SKILL alone (Phase 2 vault_edit leak, propose-person CLI form missing) make the standard worth its overhead.

- **Live-boot smoke test as part of code review** caught the instructor regression that static review missed. ~30s of daemon startup verification beats hours of post-merge debugging.

- **"Hypatia overshoots scribe-mode into helpful-analytical mode" is GOOD calibration data, not a defect.** The SKILL described pure scribe-mode; in practice she dispatched substantively when content was business-context, and Andrew valued it. Mode boundaries should be loose enough to allow this.

- **Centralized helper from day one** for SDK quirks. Builder's instinct here was right.

- **Configuration-by-presence pattern** for orchestrator daemon-skip works cleanly. No `enabled: false` flags needed; absence of the config block IS the disable signal.

### Anti-patterns confirmed

- **Hardcoded instance-specific literals** in code reachable by multiple instances. Found in `conversation.py:697` (`scope.check_scope("talker", ...)`), `bot.py` reply-prefix, `_validate_type` canonical-types-only, `audit/sweep.py` `agent="salem"` (filed for Phase 2 fix). Each becomes a release blocker for the next instance.

- **"Alfred" as default for instance NAME fields**. Alfred is the system; instances are NAMES. `InstanceConfig.name = "Alfred"` was producing silent misconfiguration (`"Alfred's earlier message"` in attribution prose). Required-field semantics is the right fix; "Alfred" as a default is forbidden.

- **Documented-as-proactive ≠ actually invoked.** Code-reviewer agent existed with "use proactively" instructions for weeks but wasn't actually invoked on every significant ship. Vague triggers don't enforce; explicit "use on every X ship" rules do.

### Corrections

- The `alfred instance new` scaffold's `--only talker,transport,instructor` launch hint should be `--only talker,instructor,brief_digest_push` or similar (depending on instance shape). `transport` isn't a daemon name.

- The instructor's `InstanceConfig` has only `name` + `canonical` fields, NOT the talker's full set (aliases, skill_bundle, tool_set). Don't assume symmetry.

- KAL-LE's distiller scope row in the rollout ledger was marked ✅ but distiller actually crashed there. Updated to ❌ + filed in `project_kalle_distiller_radar.md` as P0 plan.

### Missing knowledge

- **Code vs config vs prompt layer distinction wasn't explicit anywhere.** Andrew asked "How does Salem handle these things? Don't reinvent." The answer (most code is shared; per-instance config is just values; SKILL.md is where behavior diverges) was clear retroactively but not pre-documented. Worth adding to CLAUDE.md "Architecture" section.

- **Worktree-spawned editable installs** can break the live venv pinning. Should be in CLAUDE.md infra notes.

- **The full daemon list per instance** isn't documented anywhere central. The "skip if no config block" pattern means the daemon set is implicit in the config, not declared. Ledger has rough info but a per-instance daemon-set diagram would help future instance launches.
