---
type: session
status: completed
name: Speed command 2026-04-21
created: 2026-04-21
description: Add /speed slash-command for per-(instance, user) ElevenLabs TTS
  speed preference. Stored on person-record frontmatter under preferences.voice
  with full history tracking. Applies to every ElevenLabs TTS path.
intent: Give Andrew a quick in-conversation knob to calibrate TTS speed per
  instance. Voices differ (Rachel vs future clinical narrator) so preferences
  need to be per-instance. History-tracking from day one enables cohort
  analysis when STAY-C commercializes.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Voice wk3 — Session close calibration writes 2026-04-18]]'
tags:
- voice
- talker
- tts
- calibration
---

# Speed command 2026-04-21

## Intent

Andrew wanted a simple inline control for ElevenLabs TTS speed during
Telegram conversations. Each Alfred instance has its own voice with
different speech patterns (Salem's Rachel, future STAY-C clinical
narrator, V.E.R.A. dispatch voice), so preferences are keyed by
`(instance, user)` — not a global setting. History is tracked so that
when STAY-C commercializes for clinical use, the collected calibration
data informs default-speed tuning per demographic.

## Work Completed

### New module: `src/alfred/telegram/speed_pref.py`

Owns read/write of the `preferences.voice` block on the user's person
record. Three public functions:

- `resolve_tts_speed(vault_path, user_rel, instance_name) -> float` —
  safe for every TTS call path. Returns `SPEED_DEFAULT` (1.0) when no
  preference is set. Never raises.
- `set_tts_speed(vault_path, user_rel, instance_name, speed, by=, note=)
  -> summary dict` — persists via `ops.vault_edit` so the talker scope's
  existing person-record edit permission covers the call. Preserves
  unrelated `preferences.*` keys (the full `preferences` dict is read,
  mutated in place, written back).
- `format_report(vault_path, user_rel, instance_name) -> str` — report-
  mode reply. Shows current value + last 3 history entries filtered to
  this instance (so STAY-C calibration history doesn't pollute a Salem
  report).

Helpers: `parse_speed_command` (handles `/speed`, `/speed 1.2`,
`/speed 1.2 free-text note`, `/speed default`, garbage input), and
`validate_speed` (rejects out of 0.7-1.2 per ElevenLabs v2.5 spec).

### Handler + dispatch: `src/alfred/telegram/bot.py`

- New `on_speed` handler registered via `CommandHandler("speed",
  on_speed)`. Fires BEFORE the router (same timing as `/end`, `/brief`,
  `/extract` — PTB CommandHandler always runs ahead of the
  MessageHandler for messages starting with `/`).
- Inline-command detection extended: `_INLINE_CMD_WITH_ARG_RE` now has a
  second alternation for `/speed` with rest-of-line body so "Good.
  /speed 1.2 too slow" routes to the speed handler rather than to
  Claude.
- Added to `_INLINE_COMMANDS` and `_INLINE_HANDLERS` dispatch map.

### TTS path integration: `tts.py` + `on_brief`

- `tts.synthesize` gained an optional `speed: float | None` kwarg.
  When provided, forwarded as `voice_settings.speed` in the ElevenLabs
  request. When `None`, the key is omitted (ElevenLabs applies its own
  default). Range validation stays at the caller — synthesize doesn't
  clamp, since the `/speed` handler has already rejected out-of-range
  values.
- `on_brief` resolves the preference via `speed_pref.resolve_tts_speed`
  right before synthesis, using the same `(instance, user)` key the
  `/speed` handler writes to. Future TTS call paths follow the same
  pattern — one resolve call, one kwarg.

### SKILL update: `vault-talker/SKILL.md`

Small addition under "Session boundaries" — a user-slash-command list
for the model's reference. The model never invokes these itself but
should understand what Andrew can do. Includes `/speed`, `/end`,
`/brief`, `/extract`, `/opus`, `/sonnet`, `/no_auto_escalate`,
`/status`.

Bundled in the same commit as the backend change per the scope/SKILL
audit rule in CLAUDE.md.

### Tests: `tests/telegram/test_speed_command.py`

36 new tests covering:
- Parse helpers (no arg, numeric, numeric + note, default, garbage, raw
  body with no slash).
- Range validation (0.7, 1.0, 1.2 accepted; 0.5 and 1.5 rejected).
- Resolve happy paths (missing record, no preferences block, stored
  value, per-instance scoping).
- Set path (create from scratch, preserve unrelated preferences,
  append history, per-instance scoping, per-user scoping, default
  reset with `by=reset` history entry).
- Report formatting (unset, set with history, filtered by instance).
- `on_speed` handler behaviors (report, set, set-with-note, range
  rejection, default reset, unauthorized user silent).
- `/brief` integration (stored speed forwarded to synthesize; default
  1.0 when unset).
- `synthesize` signature (forwards speed, omits when None).
- Inline dispatch (`/speed`, `/speed 1.2`, `/speed 1.2 too slow` all
  route to the speed handler).

Two test mocks in `test_tts_brief.py` / `test_tts_failure.py` updated
to accept the new `speed` kwarg on `_fake_synth`.

## Design Decisions

- **Instance-name normalisation duplicated in `speed_pref.py`.** The
  shape matches `bot._normalize_instance_name` byte-for-byte but lives
  in the preferences module to avoid a circular import (bot imports
  speed_pref). Produces transport-peer-key form (`salem`, `stay-c`,
  `kal-le`) so the preference key space is consistent with the peer
  routing table.
- **History filtered by instance in the report.** Andrew calibrating
  three instances in one afternoon shouldn't see 3 STAY-C entries when
  he asks "what's Salem set to?". Filter at render time, not write
  time — the raw history keeps all entries for cohort analysis.
- **Write goes through `ops.vault_edit`** with a full `preferences`
  dict in `set_fields`. The talker scope has `"edit": True` and
  `allow_body_writes: True` — no allowlist to navigate. Alternative
  would've been a dedicated `preferences_fields_only` rule, but that's
  over-engineering for the current use case and can be added later
  if another scope needs preferences without edit-everything.
- **Range validation at the handler, not at synthesize.** Keeps
  ElevenLabs knowledge localized to the TTS module; the handler
  rejects out-of-range with a helpful message before the API would
  return an opaque 400.

## Edge Cases Discovered

- **Missing person record.** `resolve_tts_speed` returns default 1.0
  (logged as `talker.speed.person_record_missing`). `set_tts_speed`
  returns `written=False` with reason `person_record_missing` — the
  handler surfaces this as a user-facing error. In practice Andrew's
  person record always exists; this guards against a misconfigured
  `primary_users` list.
- **Multi-user isn't a concern yet.** `primary_users[0]` is the only
  target — future work if a second person ever uses the same bot,
  we'd resolve via `chat_id → allowed_users → person record`
  lookup. Today's single-user shape is fine.
- **No per-chat_id routing.** If two users somehow share a chat_id
  (impossible via Telegram's own semantics), whichever user types
  `/speed` first wins because we key on `primary_users[0]` rather
  than the sender's user_id. Explicitly not solving for this — when
  multi-user lands it'll come with its own routing story.

## Test Results

Baseline: 986 passing (1 pre-existing flaky test excluded —
`test_failure_log_has_subprocess_contract_fields`, a test-isolation
flake unrelated to this work).

After: 1022 passing (986 + 36 new).

## Alfred Learnings

- **Per-instance TTS preference is the right shape.** Each voice has
  different speech characteristics — Rachel's clarity curve differs
  from a future clinical narrator's. Users calibrate per-voice, not
  globally. Global preferences would force a compromise that suits
  no voice perfectly.
- **History-tracking from day one enables future cohort analysis.**
  When STAY-C commercializes for clinical use, the per-instance
  calibration history across user cohorts informs default-speed
  tuning per demographic (age, language, clinical context). Logging
  the `set_at`, `by`, and optional `note` per change means this data
  is already captured — no retroactive instrumentation needed.
- **Slash-command preference-set pattern is reusable.** `/speed` is
  the first of what will probably be a family: `/verbosity`,
  `/voice` (instance selection), `/model` (default model tier per
  session type). Shape is: `parse → validate → resolve key →
  vault_edit person record → confirm`. Future commands can lift the
  template from `speed_pref.py`.
- **Scope/SKILL audit rule paid off.** Per CLAUDE.md, a backend
  change that affects user-facing commands should bundle a SKILL
  update in the same commit. Adding `/speed` to the user-slash-
  command list in the SKILL keeps the model's understanding of
  available commands in sync with reality — no drift window.
