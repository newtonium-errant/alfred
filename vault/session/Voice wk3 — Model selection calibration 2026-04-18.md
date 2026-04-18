---
type: session
status: completed
name: Voice wk3 — Model-selection calibration scaffold
created: 2026-04-18
description: Commit 8 of 8 in Voice Stage 2a-wk3 — parse the Model Preferences (learned) subsection for per-type opening-model overrides, and add a threshold-based Proposal generator that feeds commit 7's apply pipeline.
intent: Let the calibration block learn which session types deserve Opus vs Sonnet openings without a code change. Threshold (3-of-5 escalations in a session type) guards against one-off spikes.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Calibration IO 2026-04-18]]'
- '[[session/Voice wk3 — Session close calibration writes 2026-04-18]]'
tags:
- voice
- talker
- wk3
- calibration
- model-selection
---

# Voice wk3 — Model-selection calibration scaffold

## Intent

Commits 5 and 6 gave Andrew explicit and implicit ways to escalate mid-session. Commit 8 observes those escalations over time and eventually suggests flipping the session-type default. If journal keeps ending on Opus even though it opens on Sonnet, the scaffold proposes adding `journal: claude-opus-4-7` to Model Preferences (learned), which then takes effect on the next session of that type.

The mechanism is minimal on purpose: one new module (`model_calibration.py`), two public functions (`parse_model_preferences`, `propose_default_flip`), one `Session` field (`opening_model`), and two new `closed_sessions` keys (`opening_model`, `closing_model`). The actual *writing* of the flip proposal piggybacks on commit 7's `apply_proposals` — so the same confirmation dial and marker logic govern model-default changes.

## Work Completed

- `src/alfred/telegram/session.py`:
  - `Session` dataclass gains `opening_model: str = ""`. Defaults and `from_dict` fall back to `model` when the field is missing (wk2 records) — conservative "the session was opened on whatever model it's currently on" answer.
  - `open_session` seeds `opening_model=model` at creation time.
  - `close_session` writes both `opening_model` and `closing_model` into the `closed_sessions` summary. The delta is what commit 8's threshold check reads.
- New `src/alfred/telegram/model_calibration.py`:
  - `MODEL_CAL_THRESHOLD = 3`, `MODEL_CAL_WINDOW = 5` constants.
  - `ModelPref` frozen dataclass with `session_type`, `model`, `raw`.
  - `parse_model_preferences(calibration_str) -> dict[str, ModelPref]` — finds the "## Model Preferences (learned)" subsection (tolerant of any trailing italic attribution or `[needs confirmation]` marker), matches `-<type>: <model>` bullets, skips malformed lines. Stops at the next `## ` heading so preferences can't accidentally absorb bullets from other subsections.
  - `propose_default_flip(session_type, state_mgr) -> Proposal | None` — looks at the most recent 5 closed sessions of the same type, counts how many were escalated mid-session (`opening_model != closing_model`), returns a Proposal when the count hits the threshold. Tie-breaking picks the most-recent escalated-to model.
- `src/alfred/telegram/bot.py`:
  - `_open_routed_session` now reads the calibration snapshot BEFORE opening the session (previously it was read after), consults `parse_model_preferences`, and overrides `decision.model` with any type-specific learned preference. Logs `talker.model_cal.override` when the override fires.
  - `on_end` runs `propose_default_flip` after `close_session` returns (so the just-closed session is visible to the threshold) and appends any returned Proposal to the list handed to `apply_proposals`. Same dial and marker handling as the other proposals.
- Tests (`tests/telegram/test_model_calibration.py`, 16 new):
  - `parse_model_preferences`: empty/missing/happy/malformed/duplicate/heading-boundary.
  - `propose_default_flip`: below-window, below-threshold, at-threshold (fires), wrong-session-type (skipped), missing-opening-model (conservative — skipped).
  - `Session.opening_model` round-trip + wk2-fallback.
  - `close_session` writes both model fields into the closed-session summary.
  - `_open_routed_session` honours the override (journal → Opus when calibration says so), falls back to router default when no preference is set.

131 tests pass (115 after commit 7 + 16 new).

## Outcome

Wk3 lands. Andrew can run real sessions; every explicit `/end` produces calibration proposals (commit 7) including the model-default flip suggestion (commit 8) when the threshold is met. Next session's opening model now respects learned preferences. The full loop — read at open, strip in distillation, update at close, surface in reply — is live.

## Alfred Learnings

- **Pattern validated**: making `propose_default_flip` return the same `Proposal` type that commit 7's `apply_proposals` consumes means model-preference updates ride the exact same machinery as communication-style updates. No special-case code path, no separate dial, no second apply function. The pattern generalises: any future calibration scaffold (workflow-preference learner, priority detector) just needs to emit `Proposal` objects.
- **Pattern validated**: recording `opening_model` + `closing_model` in `closed_sessions` (not just `model`) preserves the escalation signal past session-close. Without `opening_model`, wk2 records look like "this session was always on whatever model it ended on" — which is wrong, and would produce phantom escalations the moment a wk3 sessions closes on the *same* model it opened on.
- **Pattern validated**: `parse_model_preferences` matches against a strict-ish bullet shape (`- <type>: <model>`) rather than trying to understand arbitrary prose. If the user (or Alfred's own apply_proposals) produces a line matching the shape, it parses; anything else is silently skipped. That's exactly the robustness property we want — the block is human-readable but machine-parseable only where needed.
- **Anti-pattern avoided**: I almost made `propose_default_flip` mutate state directly (flipping the default in the session-types table). Reverted — the calibration block is the single source of truth for learned preferences, not a code-level table. Keeping the table as a fallback and the block as the override means Andrew can audit every preference in one place by opening his person record.
- **Gotcha**: the `_open_routed_session` code path re-order (read calibration before open, not after) was easy to miss. The previous order (open first, read second) meant the router's model choice was locked in before the calibration override could fire. Caught it by running the override test against Sonnet — and watching Sonnet win because the session was already open on Sonnet by the time the override check happened.
