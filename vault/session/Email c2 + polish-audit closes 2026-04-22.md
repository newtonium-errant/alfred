---
type: session
title: Email c2 — Daily Sync conversation channel + email calibration loop, plus polish-audit closures
date: 2026-04-22
tags: [email, daily-sync, calibration, ooda, axis-2, deterministic-writers, talker, session-pattern]
---

## Summary

Single big commit `2537de4` ships the Daily Sync conversation channel + email calibration loop (c2 of the email-surfacing arc). 22 files, +3252 lines, 67 new tests.

Also closes a polish queue: talker person scope (`c341b98`), surveyor regression test (`80b3344`), three talker polish bugs (already fixed — memo retired), deterministic writers audit (zero candidates — codebase clean).

Memory updates: `project_talker_polish_bugs.md` retired as RESOLVED, new `feedback_verify_stale_memos.md` filed.

## Why

c2 closes the second chunk of the chunked email-surfacing arc per `project_email_surfacing.md` and `project_daily_sync_ooda.md`. The Daily Sync at 09:00 ADT is the OODA-loop conversation channel between Salem and Andrew — calibration, friction, open questions. NOT a status report (that stays in the brief). Andrew's framing: "scheduling time to discuss calibration and friction in the system itself … part of the OODA Loop process."

The polish-audit closures complete the small-fix queue before bigger arcs (STAY-C Phase 1, deterministic writers, etc.) can start cleanly.

## What changed (c2 commit `2537de4`)

**New module `src/alfred/daily_sync/`:**
- `assembler.py` — section-provider registry + `assemble_message` + `parse_reply` + `apply_modifier` (tier ladder)
- `corpus.py` — append-only JSONL corpus: `append_correction`, `iter_corrections`, `recent_corrections` with diversification across tiers
- `confidence.py` — atomic state-file flag persistence (per-tier `high|medium|low|spam` toggles read by future c3/c4/c5 to gate surfacing)
- `email_section.py` — first concrete provider; samples N (default 5) recent classified emails preferring uncalibrated, falls back to stratified-across-tiers
- `daemon.py` — `fire_once` (reused by `/calibrate`) + `run_daemon` mirroring brief's `compute_next_fire` + `sleep_until` shape (DST-aware)
- `reply_dispatch.py` — Telegram-reply → corpus pipeline

**Wire-in changes:**
- `src/alfred/orchestrator.py` — `_run_daily_sync` runner; auto-starts when `daily_sync.enabled`
- `src/alfred/telegram/bot.py` — `/calibrate` and `/calibration_ok` handlers; `_extract_reply_message_id` + `_maybe_handle_daily_sync_reply` pre-check (runs BEFORE inline-command detector)
- `src/alfred/email_classifier/classifier.py` — `_build_system_prompt` reads `calibration_corpus_path` and injects `_build_few_shot_block` (most-recent N corrections, diversified by tier, oldest-first for chronological reading)
- `src/alfred/email_classifier/config.py` — `calibration_corpus_path` + `calibration_few_shot_count` fields
- `config.yaml.example` — new `daily_sync:` block + matching `calibration_corpus_path` in `email_classifier:`
- `tests/orchestrator/test_tool_dispatch.py` — added `daily_sync` to `EXPECTED_TOOLS`

**Reply identification (key design):** Telegram's `reply_to_message` ID. When non-None, `reply_targets_daily_sync(config, msg_id)` matches against the persisted `last_batch.message_ids` list in `data/daily_sync_state.json`. Match → corpus write; no match → fall through to normal pipeline.

**Daily Sync wiring:** Separate orchestrator process (mirrors brief). Assembles + pushes via existing outbound transport `/outbound/send_batch`. Only `/calibrate` and reply-handler logic runs inside the talker process.

## Polish-audit closures (no new commits — verifications only)

Three audits ran in parallel via builder/Explore agents. All came back with NOTHING TO FIX:

- **Talker polish bugs (3 of 3):** ALREADY FIXED in master. Bug 1 (per-turn `_ts`), Bug 2 (architectural switch from state-dict counter to per-turn `_kind` metadata), Bug 3 (`mutation_log.log_mutation` call dropped from `_execute_tool`). All have regression tests.
- **Deterministic writers audit:** ZERO candidates found across distiller, curator, janitor ORPHAN001. The codebase is well-aligned with the deterministic-writers pattern — `distiller_signals` / `distiller_learnings` / `related` / janitor ORPHAN001 are all already-deterministic; LLM-composed fields (`name`, `description`, `subtype`, classifier `priority` / `action_hint`) are correctly judgment-required.

## Design decisions (c2)

- **One commit, not three.** Splitting felt artificial — the assembler is meaningless without a provider, and `/calibrate` directly invokes the assembler so it can't ship before the email section. The PR-shaped slice is one coherent feature.
- **Per-instance via config-file scoping** (Salem's `config.yaml` gets the `daily_sync:` block; KAL-LE's `config.kalle.yaml` does not). Same architectural pattern as the c1 email classifier.
- **Section-provider registry** with priority slots ready for friction queue (priority 20) and open questions (priority 30) — neither shipped in c2, but adding them later is a single-file change.
- **Empty Daily Sync emits "No items today"** rather than silent skip — operator visibility, mirrors brief Phase 1's stance.
- **Few-shot rotation triggers on every `_build_system_prompt` call.** Acceptable cost (small JSONL read); memoize per-batch in c3 if measurements show it matters.
- **Action-hint corrections via reply parser:** corpus carries the `andrew_action_hint` field but the parser doesn't yet emit them. Add when c3 needs them.

## Alfred Learnings

- **Pattern validated — pre-spec calibration design with the user before specing the builder.** Andrew explicitly steered three load-bearing decisions (per-instance arch, action hints from day one, OODA framing for Daily Sync) before c1 or c2 was specified to a builder. Each decision was a 1-2 sentence Q+A that took ~5 min in chat. The builders shipped without scope drift on any of those axes. Compare to spawning with a vague "implement email triage" — would have produced something, then needed redos on at least 4 of the 6 design axes.
- **Pattern validated — conversation-channel framing changes infrastructure.** When Andrew named the 09:00 message as "OODA loop touch points" rather than "daily report," the c2 design shifted from a multi-section status push to a multi-section conversation push (with reply parser, corpus, confidence flags, slash commands). The framing was load-bearing; it determined the whole reply-path infrastructure.
- **Anti-pattern confirmed — don't trust 3-day-old bug memos cold.** Four memos this session described bugs that intervening work had fixed (surveyor silent-writer, talker inline-commands symptom-mismatch, voice 2b status, talker polish bugs). New `feedback_verify_stale_memos.md` captures the lesson: build a 30-second repro before specing a fix on a memo's claim. Saved >>1 hr per case if applied earlier.
- **Pattern validated — registry-style extension points let future chunks ship cheap.** c2's section-provider registry has slots for friction-queue + open-questions providers. Wiring those in c3+ is a single-file change because the framework is already in place. Same shape as the brief's section list.

## Next

- **First Daily Sync fire: 2026-04-23 09:00 ADT.** Live validation moment.
- **First brief with Upcoming Events on real data: 2026-04-23 06:00 ADT.** Same.
- **First emails through the c1 classifier:** active starting from this session's restart. Tomorrow's brief + 09:00 calibration batch will tell whether classifier accuracy is good enough to flip `/calibration_ok medium` immediately or whether the cold prompt needs tuning first.
- **c3-c6 of email surfacing** are gated on Andrew's per-tier `/calibration_ok` toggles. He validates each tier in the calibration loop before the corresponding surfacing layer activates.
- **Polish queue is empty.** Next bigger arcs by memory: STAY-C Phase 1 (Stage 3.5, ratified next instance, ~1-2 weeks); OpenClaw setup (~1-2 hr, infra); deterministic writers (audit found nothing — closed).
