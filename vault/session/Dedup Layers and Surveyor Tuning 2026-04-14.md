---
alfred_tags:
- alfred/dedup
- alfred/surveyor
- alfred/agent-team
- alfred/hooks
- alfred/observability
created: '2026-04-14'
description: Completed 3-layer dedup prevention work, surveyor labeler prompt + threshold
  tuning, Pocketpills note merge, janitor follow-link rule, foreground-default enforcement
  hook, and subprocess failure logging hardening across CLI/pipeline/OpenClaw backends
distiller_signals: decision:6, assumption:3, constraint:9, contradiction:9, has_outcome
intent: Resume dedup-prevention work deferred from the earlier 2026-04-14 session
  and close out followups from vault review
janitor_note: LINK001 — [[person/Andrew Newton]] does not exist; vault owner has no
  person record yet. LINK002 — added [[account/PocketPills Pharmacy Account]] to related.
name: Dedup Layers and Surveyor Tuning
outputs: []
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/System Hardening and Agent Team 2026-04-14]]'
- '[[account/PocketPills Pharmacy Account]]'
relationships: []
status: completed
tags:
- dedup
- surveyor
- prompt-tuning
- janitor
- agent-team
- hook
- curator
- observability
- subprocess-logging
type: session
---

# Dedup Layers and Surveyor Tuning — 2026-04-14

## Intent

Resume the dedup-prevention work deferred from the earlier 2026-04-14 session. Three planned layers plus followups: duplicate Pocketpills note pair, surveyor noise relationships (WARN-2), and the org-merge sweep's follow-link gap.

## Work Completed

### Daemon Restart
`alfred up` clean start (pid 725530). All six tools came up: curator, janitor, distiller, surveyor, mail, brief. First run against yesterday's curator file-locking + per-tool PID tracking fixes.

### Dedup Prevention — Layer 1 (Curator SKILL.md)
Prompt-tuner added a mandatory `STEP 2a: DEDUP CHECK` sub-procedure to `src/alfred/_bundled/skills/vault-curator/SKILL.md` before any standing-entity creation: list target TYPE_DIRECTORY, case-insensitive comparison, reuse on near-match, canonical naming rules, `aliases: []` for known variants. PocketPills and Alliance Dental worked examples added to anti-patterns. The "STEP 7 removal" item from yesterday's vault review turned out to be a phantom — the curator 6-step procedure has no STEP 7; the inbox-move prohibition was already in place at lines 927, 989, 1130-1131.

### Dedup Prevention — Layer 2 (`vault_create` near-match warning)
Builder added `_check_near_match` helper in `src/alfred/vault/ops.py` and called it in `vault_create` after the `dir_warn` block. Emits a `vault_create.near_match` structlog warning and appends to the existing `warnings` list when a new filename collides case-insensitively with an existing file in the same TYPE_DIRECTORY. Non-blocking safety net. Verified against the live `Pocketpills` record.

### Dedup Prevention — Layer 3 (Janitor triage queue plan)
Builder delivered a full plan, not implementation. Concept: janitor creates `task` records with `alfred_triage: true` frontmatter for DUP001 candidates needing human judgment. Scope change in `src/alfred/vault/scope.py` — new `triage_tasks_only` permission. Idempotency via deterministic `alfred_triage_id` from sorted candidate paths plus state tracking. Two decisions tentatively agreed: agent creates triage tasks via janitor SKILL.md (not Python-only), advisory-only for Layer 3 (no auto-merge loop — that's a future Layer 4).

### Pocketpills Note Merge Executed
Vault-reviewer identified the live duplicate as a note pair (not org — already cleaned up): `note/PocketPills Ozempic Order Preparation 2026-04-13.md` and `note/Pocketpills Ozempic Order Preparation 2026-04-13.md`. Canonical winner = lowercase (richer body, tags, matches already-canonical org record). Builder deleted the loser, added `[[account/PocketPills Pharmacy Account]]` to the winner's `related:`, retargeted references in `vault/org/Pocketpills.md` and `vault/account/PocketPills Pharmacy Account.md`. Account filename left as-is — brand capitalization is intentional. Final grep confirmed zero remaining `PocketPills Ozempic Order Preparation 2026-04-13` wikilinks.

### Janitor Follow-Link Rule (DUP001 Section)
Prompt-tuner expanded `src/alfred/_bundled/skills/vault-janitor/SKILL.md` DUP001 section from 5 to 22 lines with a 6-step operator-directed merge procedure. After merging the two entity records, agent must grep the vault case-insensitively for both names, inspect inbound links, recursively merge case-variant siblings in adjacent directories (max one hop), retarget wikilinks to the winner's exact casing. Worked PocketPills example included. Catches the class of ghost duplicate the Pocketpills note pair represented.

### Surveyor Noise Relationships (WARN-2)
Investigation diagnosed the signature: low-confidence generic org-to-org hallucinations from shallow semantic similarity ("both offer online services"). High-confidence same-type relationships (event-to-event, asset-to-note) were mostly sensible. Chose prompt-first fix, threshold-second.

**Prompt-tuner** rewrote `RELATIONSHIP_PROMPT` in `src/alfred/surveyor/labeler.py` (lines 32-70, grew from 19 to 39 lines):
- Groundedness rule: require explicit factual anchor (named person/org/project/product/date/location/event present in both records)
- Cite-or-drop: new required JSON fields `source_anchor` and `target_anchor` with verbatim quoted phrases from each side
- `contradicts` removed from allowed types — contradiction analysis is the distiller's job
- One-line definitions for each remaining type (`related-to`, `supports`, `depends-on`, `part-of`, `supersedes`)
- Negative example (DigitalOcean/Marriott) baked into the prompt

**Builder** then:
- Lifted the 0.5 hardcoded confidence gate into `LabelerConfig.min_relationship_confidence` (default 0.65) — now configurable via `config.yaml` and `config.yaml.example`
- Expanded the validation `all(...)` check in `labeler.py:146` to require `source_anchor` and `target_anchor` — enforces the prompt contract in code
- Cleaned up 7 historical machine-generated `contradicts` relationships across `vault/org/` and `vault/note/` (DigitalOcean/Marriott plus 6 marketing-email pairs). Left 2 legitimate assumption→contradiction pointers in `vault/assumption/Email Pipeline Production-Ready From Single Test.md` untouched.

Prompt-tuner also flipped the literal `confidence >= 0.5` → `0.65` in `RELATIONSHIP_PROMPT` text to match the code gate after builder flagged the drift.

### Foreground-Default Enforcement Hook
After twice spawning editing agents in background and having them silently denied write permissions (prompt-tuner for Layer 1, builder for Layer 2), added `.claude/hooks/block-bg-edit-agent.py` — a PreToolUse hook on the Agent tool that blocks `run_in_background: true` spawns whose prompt matches edit-implying verbs (apply/edit/implement/add/change/modify/write/remove/delete/refactor/fix/rename/create). Wired via `.claude/settings.local.json` → `hooks.PreToolUse[matcher=Agent]`. First draft used `jq` but `jq` isn't installed on this WSL2 environment; rewrote in Python. Hook fired correctly on the next background spawn attempt (a research task that had "fix" in a "do not fix anything" instruction). Correct default: the hook errs toward blocking, I rewrite the prompt to be unambiguously research-only.

### Distiller Consolidation Rate-Limit Incident
At 00:53 UTC, distiller's hourly consolidation sweep failed silently on every learn-type stage: `claude.nonzero_exit code=1 stderr=''` followed by `pipeline.llm_failed summary='Exit code 1: '` for all 5 stages (contradiction/constraint/assumption/synthesis/decision). `changes=0`, no records consolidated. A manual `claude -p "reply with just OK"` at 01:40 returned exit 0, and a manual re-run of `alfred distiller consolidate` at 01:42 succeeded cleanly: 52 records modified (4+18+21+45+7 across the 5 types, 43 changes applied). The ~18 min runtime confirms the re-run was a full pass, not a no-op. Root cause was a transient Claude usage/rate limit — the error message landed on stdout, the subprocess exited 1 without writing to stderr, and our logging discarded stdout entirely. Had to do a manual probe to diagnose what should have been visible in the log.

### Subprocess Failure Logging Hardening
Routed three sequential fix waves to builder to eliminate this class of silent failure across the codebase:

**Wave 1 — Claude CLI backends.** Updated `src/alfred/{curator,janitor,distiller}/backends/cli.py` — `claude.nonzero_exit` log event now carries `stdout_tail=raw[-2000:] if raw else ""` alongside existing `stderr[:500]`. Empty-string sentinel is load-bearing: makes "no diagnostic output at all" grep-able as `stdout_tail=''`. Reproduction confirmed via a bogus-flag dispatcher call.

**Wave 2 — Pipeline layer.** The distiller Claude path runs consolidation through `ClaudeBackend.process()` and emits its own `pipeline.llm_failed` with an opaque summary string. Builder expanded `BackendResult` in `src/alfred/distiller/backends/__init__.py` with `stdout` and `stderr` fields, plumbed them through `distiller/backends/cli.py`, and rewrote `pipeline.llm_failed` in `distiller/pipeline.py` to build an enriched summary: `f"Exit code 1: {detail}"` where detail is first 200 chars of stdout, falling back to stderr, falling back to `"(no output)"`. Curator and janitor pipeline.py got the lighter one-line addition (they dispatch OpenClaw inline, not via BackendResult). Structural asymmetry between the 3 pipeline.py files discovered in this pass — curator/janitor are openclaw-only, distiller has a proper backend switch with `pipeline.llm_failed` wrapping `BackendResult`.

**Wave 3 — OpenClaw backends + convention codified.** Applied the same `stdout_tail` pattern to `{curator,janitor,distiller}/backends/openclaw.py`. Codified the rule in `.claude/agents/builder.md` under a new "Subprocess Failure Logging" subsection — spec includes the dual-capture pattern, the load-bearing empty-string sentinel, the enriched-summary fallback chain, and the call sites that already comply. Pattern-discovery trigger from `builder.md` fired correctly: the same bug class was fixed twice in one task, triggered documentation, now captured as a codebase convention.

### Memory Updates
- New: `feedback_surveyor_rel_origin.md` — target record type, not `confidence` field presence, is the discriminator for machine-vs-human relationships
- Rewritten: `feedback_agent_foreground.md` — foreground is the default broadly, not just for editing agents; hook path documented
- Rewritten: `project_next_session.md` — stale dedup-layers pickup replaced with Layer 3 plan status, surveyor re-sync, and dedup-layer effectiveness watch points
- Updated: `MEMORY.md` index

## Outcome

### System State After This Session
- All 6 daemons running, restarted fresh under yesterday's fixes
- Curator: prompt-level dedup defenses (STEP 2a + aliases + anti-patterns) plus code-level safety net in `vault_create`
- Janitor: operator-directed merge procedure with mandatory follow-link sweep
- Surveyor labeler: demands factual anchors, cites phrases from both sides, `contradicts` removed, threshold raised and now configurable. Historical noise cleared.
- PreToolUse hook makes foreground the default for edit-implying Agent spawns; verified firing
- Vault: 7 `contradicts` hallucinations removed, Pocketpills note duplicate resolved

### Deferred / Open
- **Layer 3 triage queue** — planned, not implemented. Scope change + janitor SKILL.md update + state tracking + CLI command needed.
- **Surveyor re-sync** on the new prompt not yet run. Next sync will re-emit relationships under the new contract — expect significant volume drop (expected, not a regression).
- **Dedup Layers 1/2 in practice** — next inbox file referencing a case-variant org should exercise them end-to-end.
- **Janitor follow-link rule in practice** — next org/person/project merge should exercise it.

## Alfred Learnings

### New Gotchas
- `jq` is not installed on this WSL2 environment. Assumed availability is wrong for hook scripts. Use Python instead (already in heavy use by Alfred).
- PreToolUse hooks that use `set -euo pipefail` with a missing tool (e.g., jq) silently exit 0, because the pipeline's failing command gets consumed by the `if` condition. Always pipe-test hooks with a synthetic payload before trusting them.
- The `confidence`-field heuristic is NOT a reliable machine-vs-human discriminator for vault relationships. Distiller/curator writes legitimate `contradicts` pointers with confidence scores (e.g., `assumption/Email Pipeline Production-Ready From Single Test.md` has `confidence: 0.8` and `0.7` on real semantic pointers). Use **target record type** instead — entity types for machine rels, learn types for human semantic pointers.
- Historical vault-review findings can reference prior versions of a file. Always verify against current state before acting — the "STEP 7 removal" item turned out to be a phantom.
- Prompt text and code gates can drift independently. When lifting a hardcoded constant into config, grep the prompt strings too — the LLM doesn't care what the prompt says if code enforces differently, but it wastes generation tokens.
- **`claude -p` writes rate-limit and quota errors to stdout, not stderr, and exits 1.** Stderr-only logging produced a fully silent distiller consolidation failure storm at 00:53 UTC — zero diagnostic output in the logs despite all 5 learn-type stages failing. Only a manual `claude -p "OK"` probe confirmed the cause. Every subprocess dispatcher in the codebase needs dual stderr + `stdout_tail` capture.
- Alfred's three `pipeline.py` files (curator/janitor/distiller) have divergent structure. Curator and janitor are openclaw-inline; distiller has a proper backend switch with `BackendResult` plumbing. Symmetric-looking problems can require asymmetric fixes — grep before assuming.

### Anti-Patterns Confirmed
- **Spawning editing agents in background** — they get silently denied permission and fail partway through designing their edits. Happened twice today before the hook was added.
- **Raising a confidence threshold without tightening the generation prompt** — filters good relationships alongside noise, because the noise is often high-confidence too (LLM is confident in generic patterns). Apple Services ↔ Patreon hallucination at 0.75 proves the point.
- **Over-relying on text-based heuristics** (like `confidence` field presence) when a structural signal (like target type) is cleaner and more robust.
- **Logging only stderr on subprocess nonzero exit** — silent failure when the upstream tool writes its errors to stdout. Must capture both, always, with the empty-string sentinel for the "no output at all" case.
- **Leaving an enriched summary string with a bare trailing colon** (`'Exit code 1: '`) — opaque by design. Always include a fallback to `"(no output)"` so the summary self-documents.

### Patterns Validated
- **Prompt-first, threshold-second** for LLM-generated noise: tighten the generation instructions before filtering the output.
- **Hook-based enforcement for recurring slip-ups**: when memory alone doesn't stop a mistake (the foreground/background rule existed in memory but was violated twice in one session), move the defense into the harness where the model can't route around it. The hook trips occasional false positives on research tasks — that's the right bias; rewrite the prompt instead of weakening the hook.
- **Cite-or-drop prompt pattern**: requiring the LLM to quote a verbatim phrase from each side before emitting a relationship makes hallucination structurally harder. Cheaper than raising temperature filtering.
- **Target-type discriminator for cleanup rules**: target record type is a more stable signal than field presence when distinguishing machine from human data.
- **Operator-directed merges with mandatory follow-link sweep**: catches case-variant ghost duplicates in adjacent directories that rename-in-place would miss.
- **Team-lead discipline under pressure**: every implementation step routed to the right specialist (builder / prompt-tuner / vault-reviewer). No code written in the main thread except the hook and session notes.
- **Dual-capture subprocess logging with load-bearing empty-string sentinel**: `stderr[:500]` + `stdout_tail=raw[-2000:] if raw else ""`. The sentinel makes "no diagnostic output" grep-able as `stdout_tail=''`, distinguishing "we discarded it" from "the tool really produced nothing."
- **Pattern-discovery trigger → documentation, not just point fix**: the builder.md "fix same bug twice → documentation trigger" rule fired correctly for the subprocess logging work. Two waves of identical fixes generated a codebase convention in builder.md; the third wave (OpenClaw) applied the convention without restating it. Rule works as designed.
- **Reconcile prompt text with code gates**: when lifting a hardcoded LLM-facing constant into config, grep the prompt text for the old literal and sync in the same pass.

### Corrections
- `feedback_agent_foreground.md` memory was too narrow ("editing agents foreground, QA agents background"). Andrew's actual preference is broader: foreground is the default across the board, for visibility as much as permission resolution. Memory file rewritten.
- `project_next_session.md` memory was stale — described completed dedup-layers pickup. Replaced with current deferred items.
- `RELATIONSHIP_PROMPT` text still said `confidence >= 0.5` after the code gate moved to 0.65. Synced after builder flagged the drift.

### Missing Knowledge
- No documentation on which system tools are assumed to be installed for hook scripts in this WSL2 environment. A `jq` / `python3` / `bash` availability check before writing a new hook would have saved a rewrite.
- No documented procedure for "exercise new prompt changes end-to-end" — after prompt-tuner lands a SKILL.md or LLM-prompt change, the validation loop (drop a known-tricky input, observe behavior, confirm the rule fires) is ad-hoc. Could formalize into an agent-instruction checklist.
- The Layer 3 triage queue plan assumes agent-creates-via-SKILL + Python-tracks-ids, but the contract between those two sides (how state reads back into the agent's prompt context) isn't specified anywhere yet. Needs design as part of implementation.
