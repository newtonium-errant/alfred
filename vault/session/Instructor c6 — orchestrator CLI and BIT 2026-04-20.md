---
type: session
created: '2026-04-20'
name: Instructor c6 — orchestrator CLI and BIT 2026-04-20
description: Commit 6 of the 6-commit alfred_instructions watcher rollout — orchestrator registration, top-level CLI subcommand, BIT health probe, auto-start gate, alfred status integration
intent: Close the rollout arc by wiring the instructor into every user-facing surface - orchestrator, CLI, status command, health check - so the daemon actually starts when alfred up runs and alfred check instructor reports green
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Instructor c1 — scope and schema 2026-04-20]]'
  - '[[session/Instructor c2 — state and config 2026-04-20]]'
  - '[[session/Instructor c3 — watcher and detector 2026-04-20]]'
  - '[[session/Instructor c4 — executor 2026-04-20]]'
  - '[[session/Instructor c5 — skill bundle 2026-04-20]]'
tags:
  - instructor
  - orchestrator
  - cli
  - health
  - alfred-instructions
status: completed
---

# Instructor c6 — orchestrator CLI and BIT 2026-04-20

## Intent

Commit 6 (final) of the 6-commit `alfred_instructions` watcher
rollout. Wires everything the previous commits built into the
user-facing surfaces:

- `alfred up` starts the instructor when `instructor:` is in config.
- `alfred instructor {scan,run,status}` works from the top-level CLI.
- `alfred status` reports the instructor.
- `alfred check --tool instructor` runs the BIT probe and returns green.

## What shipped

### `src/alfred/orchestrator.py`

- `_run_instructor(raw, skills_dir, suppress_stdout)` runner —
  matches curator/janitor/distiller's 3-arg signature (instructor
  needs `skills_dir` for `vault-instructor/SKILL.md`).
- `"instructor": _run_instructor` added to `TOOL_RUNNERS`.
- `run_all` auto-start: appends `"instructor"` when `"instructor"` is
  in the raw config dict. Mirrors the surveyor / mail / brief /
  telegram gate.

### `src/alfred/health/aggregator.py`

- `"instructor": "alfred.instructor.health"` added to
  `KNOWN_TOOL_MODULES` so the aggregator imports + registers the
  probe on first `alfred check` run.

### `src/alfred/instructor/health.py` — new module

Four-probe rollup with a SKIP entry point:

- SKIP when `raw["instructor"]` is absent (mirrors auto-start gate).
- **config-section** (static) — OK when the section exists (trivially
  reached, but surfaces the fact in the human output).
- **state-path** (static) — confirms the state file path is writable
  (parent dir exists/creatable). WARN when an existing state file
  has corrupt JSON (daemon heals on save, operator should know).
- **skill-file** (local) — FAIL when
  `_bundled/skills/vault-instructor/SKILL.md` isn't found. Catches
  the executor's `FileNotFoundError` surface before a directive fires.
- **pending-queue** (functional) — walks the vault, counts entries
  in every record's `alfred_instructions` list. WARN when the total
  exceeds `_STUCK_QUEUE_THRESHOLD` (20) — the daemon is down or the
  directives are failing every call.
- **retry-at-max** (functional) — reads the state file's
  `retry_counts`, flags records at or above `max_retries`. WARN.

`register_check("instructor", health_check)` fires at module import.

### `src/alfred/cli.py`

- `cmd_instructor(args)` dispatcher — loads config via
  `alfred.instructor.config.load_from_unified`, routes to
  `alfred.instructor.cli.cmd_{scan,run,status}` based on
  `args.instructor_cmd`.
- `build_parser` — new `inst` subparser with three subcommands.
- `handlers` dict — `"instructor": cmd_instructor`.
- `cmd_status` — now reports instructor state when the config section
  is present. Mirror of the talker's conditional status block.

### Tests — 11 new, 2 updated

New files:
- `tests/test_instructor_health.py` (5 tests):
  1. SKIP when no `instructor:` section.
  2. OK shape when preconditions met — confirms every probe name is
     present.
  3. WARN on pending queue > 20.
  4. WARN on retry_counts >= max_retries.
  5. WARN on corrupt JSON state file (not FAIL — daemon tolerates).
- `tests/test_instructor_cli.py` (5 tests): parser accepts
  `instructor scan|run|status`, parser-without-subcommand leaves
  `instructor_cmd=None`, `cmd_instructor` is present on the module.

Updated:
- `tests/orchestrator/test_tool_dispatch.py` — `EXPECTED_TOOLS` +
  `THREE_ARG_TOOLS` now include `"instructor"`.
- `tests/orchestrator/test_auto_start.py` — `ALL_FAKES` includes
  instructor, the "required trio only" test now asserts instructor
  stays off without config, and a new
  `test_instructor_section_triggers_instructor_start` mirrors the
  surveyor / telegram triggers.

## Verification

- Full `pytest tests/ -x`: **597 passed** in 23.85s. Baseline after
  c5 was 586; this commit adds 11 new tests + preserves the
  contract-guard tests by updating their fixtures.
- Live smoke: `alfred check --tool instructor` (with a synthetic
  vault + instructor config) returns `overall_status: Status.OK`
  with every probe green.

## Deviations from spec

1. **Health probe: 5 results, not the 4 the plan described.** Added
   a dedicated `config-section` probe on top of state-path,
   skill-file, pending-queue, retry-at-max. One extra line in
   `alfred check` output, but it makes the config gate visible rather
   than implicit — operators reading the output learn why the probe
   fired at all.

2. **`pending-queue` threshold = 20**, matches the plan. The
   `_STUCK_QUEUE_THRESHOLD` constant is exported so tests can import
   the literal rather than duplicating the magic number.

3. **No changes to `pyproject.toml`.** The `vault-instructor` skill
   dir under `src/alfred/_bundled/skills/` is picked up automatically
   by hatchling's `packages = ["src/alfred"]` entry — no wheel-data
   changes were needed.

## Guardrails honoured

- No SDK calls from the health probe — everything is a filesystem
  or frontmatter parse. The probe stays cheap (5s budget in quick
  mode, 15s in full).
- Auto-start gate aligns with the config section check — daemon won't
  spin when the operator hasn't opted in.
- BIT recursion guard unchanged — aggregator already excludes
  `"bit"` from its probe list.

## Alfred Learnings

- **Pattern validated — tool addition as a five-touchpoint diff.**
  The instructor landed in exactly five places outside its own
  package: `orchestrator.py` (runner + auto-start),
  `aggregator.py` (KNOWN_TOOL_MODULES), `cli.py` (handler + parser +
  handlers dict + cmd_status), and the test fixtures that enforce
  the contracts (`test_tool_dispatch.py`, `test_auto_start.py`).
  This is the template — any future tool addition should hit the
  same five places and the same contract tests. If a new tool lands
  without touching all five, one of the contract tests would have
  caught it.

- **Gotcha confirmed — f-string + dedent on multi-line substitution.**
  `dedent(f'''...\\n{multiline_var}\\n...''')` only strips the
  common leading whitespace from the *template* lines; the
  substituted variable's lines keep whatever indentation they had.
  The stuck-queue test originally failed silently because the YAML
  ended up aligned under "            alfred_instructions:" but the
  directives had only two-space indent — parsed as trailing content
  of the dict key, not list entries. Worth watching any time
  `dedent` is used around multi-line interpolation.

- **Pattern validated — SKIP-over-FAIL when the config section is
  missing.** The instructor's health check returns SKIP (not OK, not
  FAIL) when no config section is present. This matches the brief's
  convention and means `alfred check` on a minimal install reports
  "instructor: skip (no section)" rather than "instructor: fail (no
  state file)" — the fresh-install experience stays clean.

- **Pattern validated — live smoke check after test pass.** Even with
  every unit test green, I ran the aggregator live against a
  synthetic config before committing. This caught nothing on this
  commit, but in principle it's the one layer where the stdlib
  imports and the async glue can disagree with the test doubles. No
  surprises here, but the habit is cheap.

## Full rollout recap (c1 → c6)

Commit counts (all local, not pushed):

| # | Commit | Tests added | Cumulative |
|---|--------|-------------|------------|
| 1 | `6f66649` scope + schema                 | 12  | 551 |
| 2 | `ff41eae` config + state skeleton         |  9  | 560 |
| 3 | `5dcdd45` poll loop + detect_pending      |  8  | 568 |
| 4 | `a221b36` SDK executor + tool-use loop    | 12  | 580 |
| 5 | `b40b79e` SKILL.md bundle + templating    |  6  | 586 |
| 6 | this commit — orchestrator + CLI + BIT    | 11  | 597 |

Total: **6 commits, 58 new tests, 551 → 597 pytest count, zero
regressions.** No code lives in a half-shipped state — every commit
passed the full suite on its own.

### Key gotchas discovered during implementation

1. **YAML has no tuple type** — `destructive_keywords` needed
   tuple coercion in `_build` because YAML only emits lists.
2. **python-frontmatter doesn't run LIST_FIELDS coercion on read** —
   scalar directives (`alfred_instructions: "do X"`) need explicit
   promotion in `_read_pending_directives` downstream.
3. **`anthropic.APIError` needs `request` and `body` kwargs** — the
   failing-client test fixture builds an `httpx.Request` stand-in.
4. **`dedent` + multi-line f-string interpolation** mis-aligns YAML
   (see above).
5. **Sealing the hash after every execution, success or failure,** is
   how we keep a failed directive from firing every poll cycle.

### Patterns validated

1. **Scope permission ladder as a design lens** — placing
   instructor between janitor and talker made the allow/deny
   decisions fall out mechanically (c1).
2. **Pure detector + stateful loop seam** — `detect_pending` is a
   pure function the executor consumes; split enabled 8 mock-free
   detector tests (c3).
3. **Dry-run as "would do X" descriptors, not loop short-circuit**
   — the model still plans, the tool just doesn't mutate (c4).
4. **Plain `str.replace` over Jinja for two-placeholder templating**
   (c5).
5. **Tool addition as a five-touchpoint diff** (this commit) — any
   future tool addition has the same surface area.

### Follow-ups / risks

- **Daemon isn't started by this commit.** The user needs to restart
  the stack (or `alfred up` fresh) to pick up the new daemon.
  Per-task-brief guardrail honoured.
- **Real SDK integration untested.** Every executor test uses the
  fake client — a real directive end-to-end run will land the first
  time the operator parks one on a record. The health probe's
  Anthropic auth check would be worth adding in a later
  commit (mirror of janitor's `check_anthropic_auth` wiring).
- **No Anthropic auth probe.** The instructor health probe doesn't
  call `check_anthropic_auth` the way janitor/distiller do. Add
  when we move to "real-SDK" mode; dialed down for now because a
  per-probe SDK round trip would exceed the quick-mode 5s budget
  during contested network conditions.
- **No `alfred instructor history` subcommand.** The plan didn't
  call for one, but the `alfred_instructions_last` archive on each
  record already carries history — could surface across records
  with a dedicated command if it becomes useful.
