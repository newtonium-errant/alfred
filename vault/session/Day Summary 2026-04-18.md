---
alfred_tags:
- software/alfred
- voice
- summary
created: '2026-04-18'
description: End-of-day index for 2026-04-18's work spanning drift fixes,
  upstream surgical ports, Option E janitor refactor, pytest bootstrap,
  surveyor silent-writer fix, voice Stages 2a-wk2 and wk3, plus three
  hotfixes surfaced by live E2E. All individual commits bundle their own
  session notes per feedback_session_notes_per_commit.md; this summary
  is a navigation index.
intent: Mark end-of-session and give future-me (or next-session Alfred)
  a one-page view of everything that shipped today
name: Day Summary 2026-04-18
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Talker Wiring Complete 2026-04-17]]'
- '[[session/Talker SKILL Full Draft 2026-04-17]]'
status: completed
tags:
- voice
- summary
- milestone
type: session
---

# Day Summary — 2026-04-18

## Scope

Longest productive session of the project. Started with a drift-investigation stand-up, ended with wk3 calibration writes firing live from Opus 4.7. ~50 commits across 8 distinct units of work. All pushed.

## Units of work (chronological)

### 1. Drift fixes (3 commits)
- Distiller `distiller_learnings` wipe → merge semantics (`a9f6ec0`)
- Surveyor `alfred_tags` skip-if-equal guard (`7c1a452`)
- Janitor Option D SKILL idempotency rule (`574dd02`)

### 2. Scope creep revert + inner vault cleanup
- 194-file surgical reconciliation preserving legit distiller learnings, reverting drift

### 3. Upstream surgical ports — Batch A, B, C (18 commits)
- Batch A: distiller MD5 refresh + config tuning + surveyor dim-mismatch (`a3a44a4` lineage)
- Batch B: curator mark_processed on failure, slim vault context, skip_entity_enrichment, wikilink regex, fs-diff fallback
- Batch C: parallel curator, full-record Stage 1, model-agnostic prompts, HermesBackend, Stage 2 mtime guard

### 4. Option E + janitor scope narrowing (12 commits, coordinated pair)
- SEM001-004 + learn-type DUP001 routed through deterministic `_flag_issue`
- LINK001 unresolved flag written in Python, not LLM
- SKILL slimmed (deterministic codes reduced to "handled in code" one-liners)
- `field_allowlist` scope mechanism + narrow janitor allowlist
- Separate `janitor_enrich` scope for Stage 3
- Smoke script `scripts/smoke_janitor_scope.sh`

### 5. Pytest bootstrap (1 commit)
- `tests/` directory, fixtures, 12 initial smoke tests
- `pyproject.toml` `[dev]` extra + `[tool.pytest.ini_options]`

### 6. Surveyor silent-writer fix (2 commits)
- Root cause: `PrintLoggerFactory` → stdout → `/dev/null` in daemon mode
- Fix: switch to `structlog.stdlib.LoggerFactory()` routed through file handler
- Plus audit log wiring (surveyor writes now land in `data/vault_audit.log`)
- 7 new regression tests

### 7. ANTHROPIC_API_KEY subprocess isolation (1 commit)
- New `alfred.subprocess_env.claude_subprocess_env()` strips credential env vars
- Prevents `claude -p` from silently switching to API-credit billing when user is on Max plan
- 3 regression tests

### 8. Voice Stage 2a-wk2 (5 commits)
- Session types (note/task/journal/article/brainstorm) as first-class
- Opening-cue router (Sonnet classification)
- Continuation mechanism (transcript pre-seeding from `closed_sessions`)
- Session-record schema additions (`session_type`, `continues_from`)
- wk1 polish bug bundle (transcript timestamps, voice counter, stray mutation log)
- 19 new tests

### 9. Voice Stage 2a-wk3 (8 commits + migration)
- Pushback dial (0-5, per-type defaults) injected into system prompt
- Calibration block I/O (read at session open, inject as third cache-control block)
- Migration of `vault/user-profile.md` → `person/Andrew Newton.md` `<!-- ALFRED:CALIBRATION -->` section
- Distiller exclusion of calibration + ALFRED:DYNAMIC blocks
- `/opus` and `/sonnet` commands + pre-existing `run_turn` bug fix (was reading `config.anthropic.model`, should be `session.model`)
- Implicit escalation detection (3 signals, rate-limited) + `/no_auto_escalate` opt-out
- Session-end calibration writes (propose → dial 4 → apply with `_source` attribution)
- Model-selection calibration scaffold (3-of-5 threshold for default flip)
- 93 new tests

### 10. Hotfixes surfaced by live E2E (3 commits)
- `AttributeError: 'coroutine' object has no attribute 'content'` — wk1 `run_turn` missing `await` on async Anthropic SDK call
- `'messages.0._ts: Extra inputs are not permitted'` — wk2 polish's `_ts`/`_kind` metadata leaking into API payload; `_messages_for_api()` strips them
- `'temperature' is deprecated for this model.'` — Opus 4.x doesn't accept `temperature`; per-model kwarg gate

## Live E2E validation

Multiple session types exercised:
- `note` session with text + voice + task create + `/end` → session record + one calibration bullet added
- `article` session on Opus 4.7 with continuation from prior session → 2× vault_read + 1× vault_edit + substantive 2803-char Opus reply + self-reflective calibration bullet about append-not-overwrite behavior

All wk2 and wk3 features fired end-to-end. Surveyor observability fix validated by 1354 `writer.tags_*` events in log (previously 0). Distiller consolidation succeeded via Max plan OAuth for all 5 learning types.

## Memory additions

- `project_deterministic_writers.md` (marked shipped for janitor, open for other tools)
- `project_janitor_scope_creep.md` (marked shipped, carve-outs tracked)
- `project_surveyor_silent_writer.md` (marked shipped)
- `project_talker_polish_bugs.md` (3 bugs, all shipped in wk2 commit 5 + polish fixes in wk3 and hotfixes)
- `project_talker_inline_commands.md` (new — fix deferred, workaround documented)
- `feedback_upstream_check.md` (tiered cadence rule)
- `project_multi_instance_design.md` (naming convention: STAY-C, KALLE, SALEM)

## Outstanding for next session

- Surveyor scope-lock live validation (deep sweep won't fire until ~00:39 UTC nightly)
- Opus model id fallback ready if 4.7 alias deprecates (`claude-opus-4-5` tracked in 3 spots)
- Operator-directed merge scope (plan Q2), body-write loophole (Q3), STUB001 fallback (Q6) — all Option E follow-ups
- Talker inline commands (`Good. /end` → trigger close) — deferred per `project_talker_inline_commands.md`
- Stage 2b (brainstorm-capture mode), Stage 3.5 (multi-instance), Knowledge Alfred

## Alfred Learnings

**Async SDK tests need async mocks.** wk1 smoke tests used a mock with sync return values; three wk3-era hotfixes (AttributeError, `_ts` leak, Opus temperature) all traced back to real-SDK-behavior that mocks didn't replicate. Future builder work touching any Anthropic SDK path should include at least one contract test for the outbound payload shape.

**Bundle session notes into code commits.** Re-discovered `feedback_session_notes_per_commit.md` mid-session after drift into a two-commit pattern. Bundling is the correct convention. Going forward: one `git add`, one `git commit` with both code and note.

**Env-var leaks across subprocess boundaries are silent and expensive.** Max plan users paying for Claude Code shouldn't have `ANTHROPIC_API_KEY` leaking into `claude -p` invocations — the switch to API-credit billing is invisible until the credit balance depletes. Any project with multiple auth-path tools needs env scoping at the subprocess call site.

**Calibration mechanism works at depth.** The wk3 calibration block is learning self-reflective observations ("when asked to save mid-session writing, appends under dated Alfred section rather than overwriting") — the talker is modeling its own interaction style with the user, not just facts. Architecturally the most interesting thing Alfred does now.

**Observability bugs can hide real-work-is-happening.** Surveyor had been successfully writing tags all along but emitted zero log events. The fix exposed 1343 skip-if-equal events per sweep — a guard we'd added 3 days earlier working perfectly, completely invisible until the logging routed correctly.
