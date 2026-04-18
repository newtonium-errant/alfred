---
type: session
status: completed
name: Voice wk3 — Session-close calibration writes
created: 2026-04-18
description: Commit 7 of 8 in Voice Stage 2a-wk3 — add the write half of calibration (propose via Sonnet, apply via vault_edit body_rewriter), wire into /end close path.
intent: Close the calibration read-write loop. Every explicit /end now produces calibration proposals from the transcript, applies them per confirmation dial, and surfaces the applied items inline in the close reply.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Calibration IO 2026-04-18]]'
- '[[session/Voice wk3 — Calibration migration 2026-04-18]]'
tags:
- voice
- talker
- wk3
- calibration
---

# Voice wk3 — Session-close calibration writes

## Intent

Commit 2 read the calibration block. Commit 3 populated it. Commit 4 shielded the distiller from it. Commit 7 makes it actually *grow*: after every explicit `/end`, Alfred asks Sonnet "what should we update in the calibration block based on this session?", applies the returned proposals via a marker-block-aware rewriter, and surfaces the applied items inline in the close reply so Andrew can immediately object.

## Work Completed

- `src/alfred/vault/ops.py`: added `body_rewriter: Callable[[str], str] | None` kwarg to `vault_edit`. Runs after `body_append` so a single edit can do both; if the rewriter returns the body unchanged, `body` is NOT added to `fields_changed` (caller's "did anything actually change?" check stays honest). Generic surface rather than calibration-specific — the same mechanism works for any marker-fenced rewrite.
- `src/alfred/telegram/calibration.py` (expanded):
  - `DEFAULT_CONFIRMATION_DIAL = 4` per team-lead decision on open question #1.
  - `KNOWN_SUBSECTIONS` tuple locks the five subsection names introduced in commit 3.
  - `Proposal` frozen dataclass: `subsection`, `bullet`, `confidence`, `source_session_rel`.
  - `propose_updates(client, transcript_text, current_calibration, session_type, source_session_rel, model, transcript_tail_turns) -> list[Proposal]` — Sonnet call with a tightly-specified JSON-only response schema. Graceful fallback: network error, parse failure, empty bullets, non-list root → empty list. Confidence clamped to `[0, 1]`.
  - `apply_proposals(vault_path, user_rel_path, proposals, session_record_path, confirmation_dial) -> dict` — builds rendered bullets per dial (0 = skip, 1 = silent, 2 = mark low-confidence, 3 = mark everything, 4/5 = mark low-confidence; dial 5's "inline during session" is a wk4+ bot-layer concern), groups them by subsection, invokes `vault_edit(body_rewriter=...)`. Returns a summary dict: `written`, `applied`, `skipped`, `reason`.
  - `_insert_into_block` + `_append_to_subsection` handle the marker-block surgery — finds existing subsection headings and inserts bullets before the next heading, or appends a fresh heading at the end if missing. Unknown subsections fall through to a `## Notes` catch-all so proposals are never silently dropped.
- `src/alfred/telegram/bot.py`:
  - `on_end` snapshots the transcript + user path + calibration snapshot BEFORE calling `close_session` (which pops the active dict). After the session record is persisted, runs the propose → apply pipeline. Errors logged (`talker.bot.calibration_write_failed`) but never block the close reply — a vault record without a calibration update is better than no record at all.
  - `_render_transcript_for_calibration` compacts the last 20 turns into a `USER: …\nASSISTANT: …` format. Tool-use / tool-result blocks elide to `[tool_use]` / `[tool_result]`.
  - Applied proposals are appended to the close reply as `• [Subsection] bullet` lines when any landed. Dial 4 default means Andrew sees the update inline with the session-closed confirmation.
- Tests (`tests/telegram/test_calibration_writes.py`, 20 new):
  - `body_rewriter` runs / no-op / composes-with-body_append.
  - `Proposal` defaults.
  - `propose_updates` happy path, parse-failure fallback, api-error fallback, empty-bullet filtering, confidence clamping.
  - `apply_proposals` per dial (0/1/2/3/4), source attribution, missing-heading-creates-fresh, unknown-subsection → Notes, no-block is a no-op, empty-list skip, preserves surrounding body, inserts under existing heading in correct position.

115 tests pass (95 after commit 6 + 20 new).

## Outcome

The full loop is live: the calibration block is read at open, stripped from distillation, updated at close, surfaced to the user at close. Dial 4 (default for wk3 validation) keeps Andrew visibly in the loop — every session close lists what Alfred inferred so a false positive is immediately correctable.

## Alfred Learnings

- **Pattern validated**: generic `body_rewriter` kwarg on `vault_edit` generalises beyond calibration. Marker-fenced dynamic blocks (the other thing `ALFRED:DYNAMIC` is used for) can use the same mechanism without a second pass on vault_ops. Saved the equivalent of a `body_replace_between_markers` special case that would have needed its own scope check.
- **Pattern validated**: snapshot-before-close is load-bearing. `close_session` pops the active dict, so anything we need from the session (transcript, user_rel, calibration snapshot) must be copied out first. The lesson: when a state-mutating operation sits in the middle of a handler, the handler should do its reads up-front and pass values, not re-read from the state after the mutation.
- **Pattern validated**: `propose_updates` returning `list[Proposal]` (even empty) rather than `list | None` keeps the caller branchless — `apply_proposals([])` is a legitimate no-op, not a special case. Same fallback-is-a-real-value pattern wk2's session-types module used.
- **Gotcha**: the `_insert_into_block` regex walks by `## ` heading level 2 specifically. If a future calibration block uses `### ` (H3) subsections, the append logic would misbehave (it would insert before the next H2, skipping over H3s). Locked this implicitly by pinning known subsections to H2 in commit 3; documenting here so a future schema change remembers to update both sides.
- **Anti-pattern noted**: almost made `apply_proposals` return `None` on no-proposals to signal "nothing happened". Kept it as a structured dict with `written=False, reason="no_proposals"` because the caller (bot.py) already has to branch on dial behaviour, and a single return type simplifies the error path. The structured return also means the eventual wk4 `/pushback` scaffold can log the same summary schema.
