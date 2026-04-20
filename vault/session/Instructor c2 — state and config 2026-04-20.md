---
type: session
created: '2026-04-20'
name: Instructor c2 — state and config 2026-04-20
description: Commit 2 of the 6-commit alfred_instructions watcher rollout — InstructorConfig dataclass, InstructorState JSON persistence, utils (setup_logging + file_hash), config.yaml.example instructor section
intent: Land the instructor module skeleton (config + state + utils) so commits 3–6 can plug in the daemon, executor, SKILL, CLI, and orchestrator registration without the module dir existing only in the file tree
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Instructor c1 — scope and schema 2026-04-20]]'
tags:
  - instructor
  - config
  - state
  - alfred-instructions
status: completed
---

# Instructor c2 — state and config 2026-04-20

## Intent

Commit 2 of the 6-commit `alfred_instructions` watcher rollout.
Groundwork only: the instructor module directory plus the three files
every tool needs before a daemon can stand up.

## What shipped

### Module skeleton — `src/alfred/instructor/`

New package with four files:

- `__init__.py` — package marker + docstring.
- `config.py` — `InstructorConfig` dataclass + `load_from_unified`
  that extracts the `instructor:` section from the unified config.
- `state.py` — `InstructorState` with atomic `.tmp → os.replace`
  persistence. Tracks `file_hashes`, `retry_counts`, `last_run_ts`.
- `utils.py` — `setup_logging()` + `get_logger()` + `file_hash()`.

Mirrors the shape of `src/alfred/janitor/` so downstream commits can
follow the same wiring pattern.

### `InstructorConfig` shape

```python
@dataclass
class InstructorConfig:
    vault: VaultConfig
    anthropic: AnthropicConfig        # api_key, model, max_tokens
    state: StateConfig                # path
    logging: LoggingConfig            # level, file
    poll_interval_seconds: int = 60
    max_retries: int = 3
    audit_window_size: int = 5
    destructive_keywords: tuple[str, ...] = (
        "delete", "remove", "drop", "purge", "wipe", "clear all",
    )
```

Defaults chosen from the plan. `destructive_keywords` stays a `tuple`
on the dataclass (immutability) but the YAML loader accepts a list
and coerces to tuple — YAML has no tuple type.

### `config.yaml.example` — new `instructor:` section

```yaml
instructor:
  poll_interval_seconds: 60
  max_retries: 3
  audit_window_size: 5
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-4-6"
    max_tokens: 4096
  state:
    path: ./data/instructor_state.json
```

### Tests — 9 new

- `tests/test_instructor_config.py` (6 tests):
  - Empty config returns defaults.
  - `instructor:` section overrides apply.
  - `${VAR}` env substitution fires.
  - Missing env var leaves placeholder literal intact.
  - Shared `logging.dir` maps to a per-tool `logging.file`.
  - YAML list for `destructive_keywords` coerces to tuple.
- `tests/state/test_state_roundtrip.py` (3 new tests):
  - `test_instructor_state_roundtrip` — populate → save → reload,
    assert equality. Same shape as every other tool's round-trip.
  - `test_instructor_state_clear_retry_on_load` — `clear_retry`
    drops the entry; `hash_unchanged` gate works on reload.
  - `test_instructor_state_load_tolerates_corrupt_file` — corrupt
    JSON doesn't crash the daemon on startup (matches the
    mail/curator tolerance tests).

## Verification

Full `pytest tests/ -x`: **560 passed** in 22.35s. Baseline after C1
was 551; this commit adds 9 new tests.

## Deviations from spec

None. The dataclass + state patterns matched curator/janitor's shape
cleanly, so there were no judgment calls.

## Guardrails honoured

- No daemon wiring — commit 3.
- No executor or SDK calls — commit 4.
- No SKILL file — commit 5.
- No orchestrator / CLI / health registration — commit 6.
- Instructor module is importable but not yet invoked from anywhere.

## Alfred Learnings

- **Pattern validated — state-manager shape as a shared contract.**
  Every tool's `state.py` follows the same skeleton: in-memory
  dataclass attrs, `load()` that tolerates a missing or corrupt file,
  `save()` with `.tmp → os.replace`. The state round-trip test in
  `tests/state/test_state_roundtrip.py` exercises the contract once
  per tool, so adding a new tool is "add a round-trip test" not
  "reinvent the safety net." Getting this right at commit 2 means
  commit 3's daemon can call `state.bump_retry()` without the risk
  of a silent state-corruption bug sneaking in later.

- **Gotcha confirmed — YAML has no tuple type.** The
  `destructive_keywords: tuple[str, ...]` field on the dataclass
  needed a coercion in `_build` because YAML can only emit lists.
  Without the explicit `tuple(...)` conversion, a user who configures
  custom keywords in YAML would end up with a list on the dataclass
  and downstream code expecting tuple semantics would subtly break.
  Same pattern will show up for any future field whose dataclass
  type is an immutable collection.

- **Pattern validated — test-scope discipline from the step-c
  round-trip playbook.** The state-roundtrip test for instructor is
  one `populate → save → reload` assertion, not a deep read of every
  state field. That's on purpose: the cross-cutting test's job is
  "does the atomic-write contract hold for this tool?", not "does
  every helper method work." Dedicated tests live alongside the
  specific helpers (see `test_instructor_state_clear_retry_on_load`).
