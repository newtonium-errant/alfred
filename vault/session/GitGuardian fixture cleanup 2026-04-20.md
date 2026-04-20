---
type: session
created: '2026-04-20'
name: GitGuardian fixture cleanup 2026-04-20
description: Scrub pytest fixture strings that look like real API keys so GitGuardian's "Generic High Entropy Secret" scanner stops raising false positives
intent: Stop GitGuardian alert noise from commits that contain fake-but-realistic-looking test fixtures
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
related: []
tags:
  - tests
  - security
  - gitguardian
  - hygiene
status: completed
---

# GitGuardian fixture cleanup 2026-04-20

## Intent

GitGuardian flagged commit `2bab8e7` on 2026-04-20 11:00 UTC with a
"Generic High Entropy Secret" alert. The trigger was a pytest fixture
string `"sk-xi-legit-key-1234"` in `tests/telegram/test_health_tts_probes.py`.
It's a fake value used to verify a health-probe code path fires — not a
real key. Nothing real is exposed in git history; the real keys live
in `.env` (gitignored). But GitGuardian's pattern-matcher fires on
strings that look like secrets (common prefixes `sk-`, `sk_`, `gsk-`,
`xi-`, high entropy, proper length), so every commit touching these
fixtures generates noise.

Fix: strip the secret-looking prefixes from pytest fixture values and
replace with obviously-fake placeholders that still convey intent
(`DUMMY_ELEVENLABS_TEST_KEY`, `test-anthropic-key`, etc.). No behaviour
change — the tests never asserted on prefix matching; they just
compared the key to whatever value was passed into the config.

## Work Completed

### Scrubbed fixtures across 7 test files

All replacements are literal string swaps. No test logic changed, no
production code touched, no config touched. The only assertion that
referenced a fixture value (`assert "123" not in bot.detail`) was
updated to match the new token so it still proves what it intends —
"the bot token should not leak into the probe's public detail string".

Files modified and the mapping applied:

- `tests/telegram/test_health_tts_probes.py`
  * `sk-xi-legit-key-1234` → `DUMMY_ELEVENLABS_TEST_KEY` (the original GitGuardian trigger)
  * `sk-xi-test` → `DUMMY_ELEVENLABS_TEST_KEY`
  * `sk-xi-bad`  → `DUMMY_ELEVENLABS_BAD_KEY`
  * `gsk-test`   → `DUMMY_GROQ_TEST_KEY`
  * `sk-ant-test` → `DUMMY_ANTHROPIC_TEST_KEY`

- `tests/telegram/test_tts_brief.py`
  * `sk-xi-test` (6x) → `DUMMY_ELEVENLABS_TEST_KEY`
  * `sk-x`              → `test-key`

- `tests/telegram/test_tts_failure.py`
  * `sk-xi-test` (3x) → `DUMMY_ELEVENLABS_TEST_KEY`

- `tests/test_subprocess_env.py`
  * `sk-ant-should-be-removed` → `DUMMY_ANTHROPIC_SHOULD_BE_REMOVED`

- `tests/health/test_per_tool_telemetry.py`
  * `sk-x`     (multiple) → `test-anthropic-key`
  * `gsk-x`    (multiple) → `test-stt-key`
  * `sk-xi-x`  (multiple) → `test-tts-key`
  * `123:abc`  (4x)       → `DUMMY_TELEGRAM_TEST_TOKEN`
  * `123:abcdef`           → `DUMMY_TELEGRAM_TEST_TOKEN`
  * `gsk-real`             → `DUMMY_GROQ_TEST_KEY`
  * `sk-ant-real`          → `DUMMY_ANTHROPIC_TEST_KEY`
  * `sk-xi-real`           → `DUMMY_ELEVENLABS_TEST_KEY`
  * Assertion: `assert "123" not in bot.detail` → `assert "DUMMY_TELEGRAM_TEST_TOKEN" not in bot.detail`

- `tests/health/test_anthropic_auth.py`
  * `sk-test`    → `test-anthropic-key`
  * `sk-bad`     → `bad-anthropic-key`
  * `sk-garbage` → `garbage-anthropic-key`
  * `sk-env`     → `key-from-env`
  * `sk-config`  → `key-from-config`

- `tests/health/test_per_tool_core.py`
  * `sk-x` → `test-anthropic-key`

### Verified no production code depends on prefixes

Grepped `src/alfred/` for `.startswith("sk-")`, `.startswith("gsk-")`,
`.startswith("xi-")` — zero hits. No production code inspects key
prefixes, so removing them from fixtures cannot break any runtime
path. The only places that referenced a prefix were test fixtures
comparing themselves to a fixture value passed in — trivially updated
in the same edit.

### Tests

Pytest baseline 523 before. Pytest after: 523 passed (same count, no
tests added, skipped, or removed). Full run takes 22.85s.

## Outcome

- 7 test files modified, ~35 lines of fixture string changes.
- 523 → 523 pytest (unchanged, all passing).
- No `src/alfred/` changes, no `config.yaml` changes, no `.env` changes.
- No daemon restart required.
- No real keys were found in git history — nothing needs rotation.
- Next commit touching these test files should NOT trigger
  GitGuardian's "Generic High Entropy Secret" rule because the
  replacement strings are either short and obviously-placeholder
  (`test-key`, `test-stt-key`) or use the uppercase `DUMMY_*`
  convention which won't match the common-prefix detectors.

## Alfred Learnings

- **Pattern validated — prefix-less, obviously-fake fixture
  strings.** When a test needs a stand-in for an API key, don't
  mimic the real key's prefix (`sk-`, `sk_`, `gsk-`, `xi-`, etc.) —
  secret scanners (GitGuardian, detect-secrets, trufflehog) all key
  off those prefixes plus entropy. Use `DUMMY_<PROVIDER>_TEST_KEY`
  or `test-<purpose>-key` so the intent is clear to a human reader
  while the scanner sees nothing alarming. The only exception is a
  test that *asserts* on the prefix (e.g. "our client strips `sk-`
  from logged output") — none exist in this codebase today.

- **Gotcha — GitGuardian triggers are a signal about the fixture,
  not the commit.** The alert on `2bab8e7` wasn't about the commit
  leaking a real key (it didn't); it was about the fixture having a
  shape that *resembles* a leaked key. Treat these alerts as "your
  fixtures look too realistic" prompts, not "you leaked a secret"
  incidents. But still investigate every one before dismissing —
  the cost of a real leak is huge compared to the 20 minutes of
  fixture scrubbing.

- **Anti-pattern confirmed — "high-fidelity" test fixtures.** Making
  a pytest fixture look like a real key ("so the test feels more
  realistic") is a bad trade: the test runs the same regardless of
  whether the fixture is `sk-xi-legit-key-1234` or
  `DUMMY_ELEVENLABS_TEST_KEY`, but the first one generates scanner
  noise forever.

- **Missing knowledge — should be documented in builder
  instructions.** There's no guidance in `.claude/agents/builder.md`
  or `CLAUDE.md` about fixture naming for secret-shaped values.
  Candidate for a short section: "When writing tests that need an
  API-key-shaped fixture, use obviously-fake strings —
  `DUMMY_<PROVIDER>_TEST_KEY` for verbose fixtures,
  `test-<purpose>-key` for short ones. Never use real-looking
  prefixes (`sk-`, `sk_`, `gsk-`, `xi-`) even when they make the
  test 'feel' more realistic."
