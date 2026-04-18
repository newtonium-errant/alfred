---
type: session
status: completed
name: Pytest Bootstrap
created: '2026-04-18'
description: Stand up pytest as the project's test harness with a minimal
  set of passing smoke tests for scope, subprocess_env, and vault_ops. No
  prior test infra existed; this is foundation only, not coverage.
intent: Make `pytest` runnable against the repo so future dev work can add
  tests incrementally without designing harness each time.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Isolate Anthropic Key From Subprocess 2026-04-18]]'
- '[[session/Option E - Commit 1 Scope field_allowlist infrastructure 2026-04-18]]'
tags:
- testing
- infra
- bootstrap
---

# Pytest Bootstrap — 2026-04-18

## Intent

The repo had no test infrastructure of any kind — no `tests/` directory,
no `pytest` in deps, no fixtures, nothing. Every prior change was
validated by running `alfred` end-to-end and reading logs. That's slow,
expensive, and skips regressions. This session lays the foundation so
the next time the builder agent (or anyone) wants to add a regression
test for a fix, the harness already exists.

Scope was deliberately tight: one commit, one session note, 12 smoke
tests across the three modules where regressions would hurt most.

## What landed

1. **`[dev]` extra in `pyproject.toml`** — `pytest>=8.0` and
   `pytest-asyncio>=0.23`. Installable via `pip install -e ".[dev]"`.
   Picked up `pytest 9.0.3` and `pytest-asyncio 1.3.0` in the venv.
2. **`[tool.pytest.ini_options]` block** — `testpaths = ["tests"]`,
   `asyncio_mode = "auto"` (no decorator needed on async tests),
   `addopts = "-ra"` (short summary of skips/errors at the end).
3. **`tests/` directory**:
   - `tests/__init__.py` — empty marker.
   - `tests/conftest.py` — `tmp_vault` fixture (temp dir with the
     subset of entity directories vault ops needs, plus a sample
     `person/Sample Person.md` record); `ephemeral_config` fixture
     (loads `config.yaml.example` and repoints `vault.path` at the
     temp vault).
   - `tests/test_scope.py` — 7 tests covering `learn_types_only`,
     `talker_types_only`, and the new Option E `field_allowlist` rule
     (allow path, deny path, and the fail-closed-when-fields-omitted
     guard).
   - `tests/test_subprocess_env.py` — 3 tests pinning the
     `claude_subprocess_env` contract: Anthropic credential keys are
     stripped, unrelated env (PATH, HOME, OAuth tokens, ALFRED_*) is
     preserved, overrides win.
   - `tests/test_vault_ops.py` — 2 tests: `vault_create` →
     `vault_read` round-trip on a trivial task record, and
     `vault_search` glob-filter hit on the seeded fixture record.

`pytest -v` from the repo root: **12 passed in 0.15s**, 0 failures, 0
skips.

## What was NOT done

- **No CI config.** GitHub Actions, pre-commit, etc. are explicitly
  out of scope — that's a separate decision (and likely a separate
  conversation with the user about what's worth automating).
- **No production code touched.** Every test was written against the
  existing public API. If a test would have required refactoring to be
  testable, the rule was to skip it and note the friction here. None
  came up — scope, subprocess_env, and the vault ops in question were
  already pure-function-shaped enough to test directly.
- **No comprehensive coverage attempted.** Each test file is 2–7
  minimal tests proving the plumbing works. Per-rule and per-field
  coverage comes incrementally as we add features and fix bugs.
- **No tests for async code yet.** `asyncio_mode = "auto"` is wired
  so the next test that needs it (e.g., daemon loop or backend HTTP
  call) just declares `async def` and gets the loop for free, but
  there's no example async test in the bootstrap commit. Add one with
  the first async test that's actually needed.

## Files created

- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_scope.py`
- `tests/test_subprocess_env.py`
- `tests/test_vault_ops.py`
- `pyproject.toml` — `[dev]` extra + `[tool.pytest.ini_options]`
  block (existing file modified, not created).

## Pytest output

```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0
rootdir: /home/andrew/alfred
configfile: pyproject.toml
testpaths: tests
plugins: asyncio-1.3.0, anyio-4.13.0
asyncio: mode=Mode.AUTO
collected 12 items

tests/test_scope.py ........                                           [ 58%]
tests/test_subprocess_env.py ...                                       [ 83%]
tests/test_vault_ops.py ..                                             [100%]

============================== 12 passed in 0.15s ==============================
```

## Alfred Learnings

- **Pattern validated — pytest discovery from `pyproject.toml` works
  cleanly with the existing `src/alfred/` layout.** No `setup.py`,
  no `conftest.py` `sys.path` mangling, no `pytest.ini` needed. The
  hatchling-built `alfred-vault` package installs editable into the
  venv and `from alfred.vault.scope import ...` just works in tests.
  Use this pattern for every future test file — don't introduce
  per-test `sys.path` hacks.
- **Pattern validated — `asyncio_mode = "auto"`.** Removes the
  `@pytest.mark.asyncio` boilerplate from every async test. Future
  async tests just declare `async def test_foo(...)` and pytest-asyncio
  handles the loop. Standard for the project from this session forward.
- **New gotcha — pytest 9.x is the current major.** I asked for
  `pytest>=8.0` in `[dev]` and got 9.0.3 from PyPI. No surprises in this
  bootstrap — every smoke test ran clean — but if anyone pins pytest
  later for repeatability, set the floor to `>=9.0` to match what's
  actually in the venv now.
- **Missing knowledge — what's testable today vs. what needs
  refactoring.** The three modules I touched (`scope.py`,
  `subprocess_env.py`, vault `ops.py` minus the Obsidian CLI branches)
  were trivially testable because they're pure-ish functions over
  arguments. Daemon loops, the orchestrator, and anything that shells
  out to `claude -p` will need fixtures or fakes the next person tries
  to test them. Useful signal for the code-reviewer agent: when adding
  tests for those areas, plan to invest in test scaffolding (fake
  subprocess runner, async loop helpers) before writing assertions.
- **Anti-pattern avoided — comprehensive coverage in bootstrap.** The
  temptation was to write a test per scope, per operation, per rule.
  Resisted. Bootstrap is plumbing; coverage is per-feature. If we
  start every new harness with comprehensive coverage we'll never
  ship the harness.

## Commit

- `2b7c681` — Bootstrap pytest with smoke tests for scope,
  subprocess_env, vault_ops
