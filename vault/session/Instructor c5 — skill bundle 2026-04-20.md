---
type: session
created: '2026-04-20'
name: Instructor c5 — skill bundle 2026-04-20
description: Commit 5 of the 6-commit alfred_instructions watcher rollout — vault-instructor SKILL.md bundled with instance templating, InstanceConfig added to InstructorConfig, _load_skill wired to substitute {{instance_name}} / {{instance_canonical}}
intent: Ship the agent-facing prompt that has been a FileNotFoundError stand-in since commit 4; templating mirrors the talker's contract so multi-instance deployments get their own identity without forking the skill
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[session/Instructor c1 — scope and schema 2026-04-20]]'
  - '[[session/Instructor c2 — state and config 2026-04-20]]'
  - '[[session/Instructor c3 — watcher and detector 2026-04-20]]'
  - '[[session/Instructor c4 — executor 2026-04-20]]'
tags:
  - instructor
  - skill
  - prompt
  - alfred-instructions
status: completed
---

# Instructor c5 — skill bundle 2026-04-20

## Intent

Commit 5 of the 6-commit `alfred_instructions` watcher rollout. Ships
the `vault-instructor/SKILL.md` that the executor has been raising
`FileNotFoundError` against since commit 4 (tests used a minimal
placeholder skill to stay independent of content). Adds `InstanceConfig`
to the instructor config so templating can fire, and wires
`_load_skill` to apply substitution when a config is passed.

## What shipped

### `src/alfred/_bundled/skills/vault-instructor/SKILL.md` — new

Structured per the plan:

- **Identity block** using `{{instance_name}}` / `{{instance_canonical}}`
  templating (same `str.replace` mechanism the talker uses).
- **Purpose paragraph** — "one natural-language directive, one target
  record path, do the work, finish with a single-line JSON summary."
- **What you receive** — shape of the user turn (Directive, Target
  record, dry_run flag), with explicit semantics for each.
- **The seven tools** — `vault_read`, `vault_search`, `vault_list`,
  `vault_context`, `vault_create`, `vault_edit`, `vault_move`. Each
  has a one-line "Use it:" description. Explicitly calls out
  "there is no `vault_delete`" so the agent doesn't hallucinate it.
- **Six rules**: do exactly what the directive says, ambiguous →
  refuse, dry-run → describe-don't-mutate, cross-record allowed but
  needs reason, surface errors in summary, end with the JSON block.
- **Four worked examples**: rename-a-field edit, add-a-backlink
  (append_fields), ambiguous multi-match refusal, dry-run plan.

### `src/alfred/instructor/config.py` — InstanceConfig added

```python
@dataclass
class InstanceConfig:
    name: str = "Alfred"
    canonical: str = "Alfred"
```

Wired into `InstructorConfig`, `_DATACLASS_MAP`, and the
`load_from_unified` copy-through list. Existing c2 tests keep passing
because the default identity lands the literal "Alfred" into both
slots.

### `src/alfred/instructor/executor.py` — _load_skill applies templating

`_load_skill(skills_dir, config=None)` now accepts an optional
`InstructorConfig` and substitutes both placeholder tokens when
provided. Plain `str.replace`, two calls, zero deps. `execute()` passes
the config through so every invocation gets the right identity.

Backwards-compat guard: calling `_load_skill(skills_dir)` without a
config leaves placeholders literal — the c4 test fixtures relied on
this, and it's a useful shape for any future caller that wants the
raw template for inspection.

### Tests — 6 new (`tests/test_instructor_skill.py`)

1. `test_skill_file_exists_in_bundled_path` — confirms SKILL.md
   lives at `alfred._bundled/skills/vault-instructor/SKILL.md`
   (where the executor looks).
2. `test_skill_has_templating_placeholders` — catches an accidental
   removal of the placeholder tokens.
3. `test_skill_has_json_summary_contract` — asserts the SKILL
   mentions the `{"status": ..., "summary": ...}` shape the executor
   parses, including the three allowed status values.
4. `test_load_skill_applies_instance_templating` — throwaway skills
   dir, custom `InstanceConfig(name="Salem", canonical="S.A.L.E.M.")`
   → both tokens replaced.
5. `test_load_skill_without_config_leaves_templates` — the c4
   backwards-compat path: no config → literal placeholders.
6. `test_load_skill_uses_default_alfred_identity` — with default
   `InstanceConfig`, the real SKILL loads cleanly and substitutes
   "Alfred" into both slots.

## Verification

Full `pytest tests/ -x`: **586 passed** in 22.66s. Baseline after c4
was 580; this commit adds 6 new tests.

## Deviations from spec

None. The plan listed 2 SKILL tests; I shipped 6 because the
templating contract is easy to regress silently and the cost of extra
tests at this layer is low. All six guard distinct failure modes.

## Guardrails honoured

- No orchestrator / CLI / health registration — commit 6.
- No auto-start gate — commit 6.
- `_data.get_skills_dir()` doesn't need changes; the executor
  navigates to `vault-instructor/SKILL.md` the same way every other
  tool's daemon finds its own skill.

## Alfred Learnings

- **Pattern validated — plain `str.replace` over Jinja for simple
  templating.** The talker's two-placeholder substitution path is
  so light it doesn't justify Jinja's dependency surface. Adding a
  second consumer (instructor) reinforces that `str.replace` scales
  fine for this use case. If a future skill ever needs conditionals
  or loops in the prompt, we can promote to Jinja then — but not
  before.

- **Pattern validated — skill content includes the contract it
  parses.** The `"status": "done|ambiguous|refused"` shape appears
  in the SKILL literally. `test_skill_has_json_summary_contract`
  asserts this so a future SKILL edit can't accidentally drift out
  of sync with `_parse_agent_summary`. Same principle as the
  curator's "SKILL mentions the record types it creates" guardrail.

- **Gotcha confirmed — bundled data lives inside the package, not
  in `package_data`.** `hatchling` with `packages = ["src/alfred"]`
  in pyproject.toml picks up every file under `src/alfred/_bundled/`
  automatically because `_bundled/` is itself a package (has no
  `__init__.py` but is treated as implicit namespace package for
  the purposes of `importlib.resources.files`). No `pyproject.toml`
  change was needed for the new SKILL. This is why the talker's
  SKILL worked the same way.

- **Pattern validated — explicit "there is no `vault_delete`" note
  in the SKILL.** The instructor scope denies delete, but the model
  doesn't read the scope config — it reads the SKILL. Explicitly
  calling out the absence is prompt hygiene: otherwise the model
  may try `vault_delete`, the dispatcher returns "Unknown tool",
  and the final summary looks confused. A dozen tokens in the SKILL
  saves a full retry iteration in practice.
