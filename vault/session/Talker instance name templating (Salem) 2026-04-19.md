---
alfred_tags:
- software/alfred
- multi-instance
- talker
created: '2026-04-19'
description: Template the talker's persona name/canonical per-instance via
  two {{instance_name}} / {{instance_canonical}} placeholders in SKILL.md
  substituted at load time. First instance adopts Salem / S.A.L.E.M.
intent: Decouple the talker's persona (what it calls itself) from the
  product/codebase name (Alfred) so the multi-instance roster can run
  without SKILL forks
name: Talker instance name templating (Salem)
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[project/Alfred]]'
status: completed
tags:
- multi-instance
- talker
- persona
- skill-audit
type: session
---

# Talker instance name templating (Salem) — 2026-04-19

## Intent

The talker's SKILL introduces itself as "Alfred" in a handful of places. For the multi-instance roster (S.A.L.E.M., STAY-C, KAL-LE) to run off one codebase, the persona — the talker's self-identity — has to be configurable per instance. The product name ("Alfred") stays literal everywhere it refers to the codebase, other instances, or vault wikilinks.

This commit introduces the minimum viable surface: an `InstanceConfig` dataclass, two placeholders in SKILL.md, a two-line substitution at daemon load, and a Salem override in `config.yaml` (with `config.yaml.example` keeping the neutral Alfred default for fresh installs).

## Work Completed

### Config dataclass

Added `InstanceConfig` to `src/alfred/telegram/config.py`:

```python
@dataclass
class InstanceConfig:
    name: str = "Alfred"                  # casual, greeting-friendly
    canonical: str = "Alfred"             # formal, used in SKILL identity paragraph
    aliases: list[str] = field(default_factory=list)  # multi-instance future; unused today
```

Wired into `TalkerConfig.instance`, registered in `_DATACLASS_MAP`, threaded through `load_from_unified`. Defaults to "Alfred"/"Alfred"/[] so any existing install keeps working with zero config changes.

### Config YAML

- `config.yaml.example` — added `telegram.instance` block with `"Alfred"` / `"Alfred"` / `[]` (fresh installs stay neutral).
- `config.yaml` — added `telegram.instance` block with `"Salem"` / `"S.A.L.E.M."` / `["Salem"]` (Andrew's ops instance).

### SKILL templating

Added a comment block at the top of `src/alfred/_bundled/skills/vault-talker/SKILL.md`:

```markdown
<!--
`{{instance_name}}` and `{{instance_canonical}}` are replaced at load
time by the talker's conversation module. Do NOT swap to Jinja syntax
or similar — we use plain `str.replace` for speed and zero deps.
-->
```

Two placeholders inserted:

- `# {{instance_name}} — Talker` — section header (casual)
- `You are **{{instance_canonical}}**, an AI assistant for Andrew Newton's operational vault.` — identity paragraph (formal)

### SKILL audit: PERSONA vs PRODUCT classification

Grep turned up 8 Alfred occurrences. Each classified and acted on:

| Line | Text | Classification | Action |
|---|---|---|---|
| 3 | `description: ... Alfred's operational vault` | PRODUCT (frontmatter description of the tool) | literal |
| 18 | `# Alfred — Talker` (section header) | PERSONA (heading introducing identity section) | → `{{instance_name}}` |
| 20 | `You are **Alfred**` | PERSONA (direct "I am X" line) | → `{{instance_canonical}}` |
| 22 | `his work on Alfred itself` | PRODUCT (the codebase) | literal |
| 40 | `Knowledge Alfred's job` | PRODUCT (naming another instance by product line; the persona is TBD but the disambiguator is "Knowledge") | literal |
| 91 | `[[project/Alfred]]` (example wikilink) | PRODUCT (vault path) | literal |
| 165 | `project/Alfred` (privacy example) | PRODUCT (vault path) | literal |
| 186 | `Knowledge Alfred task` | PRODUCT (other-instance reference) | literal |

The audit mindset: PERSONA only where it's the assistant introducing or referring to itself. Everything else — framework references, vault paths, other-instance names — stays literal so `project/Alfred.md` doesn't get rewritten to `project/Salem.md` by accident.

### Conversation loader

Added `_apply_instance_templating(prompt, config)` to `src/alfred/telegram/daemon.py` — two `.replace()` calls, nothing more. Wired into `run()` so the loaded SKILL is templated before it hits the cache-control system block.

### Bot `/start` greeting

`src/alfred/telegram/bot.py::on_start` now f-strings `config.instance.name` instead of hardcoding "Alfred":

```python
f"Hi — this is {config.instance.name}. Send a voice note or type a "
"message and I'll reply. Use /end to close the current session, "
"/status for stats."
```

### Andrew Newton calibration block (inner-repo commit)

The calibration block in `vault/person/Andrew Newton.md` referenced "Alfred" as a persona in 4 bullets. Updated those to "Salem"; left the 4 product-sense references ("Alfred itself — multi-instance architecture", "Medical Alfred is the planned instance", "RRTS/Alfred") as literal "Alfred".

Diff summary:

| Bullet | Before | After | Reason |
|---|---|---|---|
| Communication Style #2 | "Assumes Alfred will proactively persist..." | "Assumes Salem will..." | describes behavior of the talker (persona) |
| Workflow Preferences #4 | "uses Alfred to capture essay templates" | "uses Salem..." | the thing Andrew talks to |
| Workflow Preferences #5 | "appends suggestions under a dated Alfred section" | "appends suggestions under a dated Salem section" | the assistant's section label in vault records |
| Section header | "## What Alfred Is Still Unsure About" | "## What Salem Is Still Unsure About" | epistemic state of the talker |
| Sub-bullet | "How Andrew wants Alfred to behave when..." | "How Andrew wants Salem to behave when..." | talker behavior |
| (left literal) | "Medical Alfred is the planned instance for that work" | (unchanged) | product-line label; the medical instance's persona is STAY-C |
| (left literal) | "Alfred itself — multi-instance architecture" | (unchanged) | codebase reference |
| (left literal) | "RxFax project — JWT + pgcrypto patterns, secondary to RRTS/Alfred" | (unchanged) | the project/codebase |

Committed to the inner vault repo as a separate commit since `vault/` is a nested git repo.

### Considered-but-deferred decisions

- **Router alias normalization** (memory says case-insensitive dict lookup on aliases maps back to canonical). Not in scope for this commit — no router yet, `aliases` is a passive list. When Stage 3.5 multi-instance routing lands, the `aliases` field is already on the dataclass waiting for it.
- **Other SKILL files** (vault-curator, vault-janitor, vault-distiller, vault-surveyor). None reference "Alfred" as a persona — they're all prompts for backend tools, not chatbots. Grepped and left untouched.
- **Logging tags** (`talker.*` structlog events). Product-facing, not user-facing. Left literal.
- **Structlog module name `alfred.telegram.*`**. Code path; PRODUCT. Left literal.

## Tests

`tests/telegram/test_instance_templating.py` — 4 tests, all passing:

1. `test_default_config_is_alfred` — loads the real bundled SKILL with default config, asserts no placeholder leaks, asserts persona references resolve to "Alfred", asserts product references (wikilinks, "Knowledge Alfred") stay literal.
2. `test_salem_instance_substitution` — swaps `instance` to Salem/S.A.L.E.M., asserts `You are **S.A.L.E.M.**` and `# Salem — Talker` both appear, asserts `You are **Alfred**` does NOT appear, asserts `[[project/Alfred]]` / `Knowledge Alfred` / `Alfred itself` all survive unchanged.
3. `test_aliases_config_roundtrip` — round-trips `aliases: ["Salem", "salem"]` through `load_from_unified`; verifies the no-instance-block path falls back to Alfred defaults.
4. `test_bot_start_greeting_uses_instance_name` — calls `on_start` with a Salem config and asserts the reply contains "Salem" and not "Alfred".

Test count before: 297. After: 301. +4, all passing.

## Outcome

- Outer-repo code + tests + this session note land in one commit.
- Inner-vault-repo Andrew Newton.md calibration edit lands in a second commit (vault is a nested git repo).
- Daemon restart required on Andrew's side to pick up the new `config.instance` block and the templated SKILL; tests prove correctness at unit level so the restart is just for propagation.
- Fresh installs (via `config.yaml.example`) continue to default to "Alfred" everywhere; the Salem override is purely an Andrew-vault concern.

## Alfred Learnings

**Two-sided contract again.** Persona templating is another instance of the scope+SKILL and config+prompt pattern from earlier rules: when one side of the contract moves, the other side has to move in the same cycle. Here it's `InstanceConfig.name` ↔ `{{instance_name}}` in the SKILL ↔ bot greeting string. All three touched in the same commit.

**Plain `str.replace` beats template engines here.** The spec called it out explicitly; noting it landed. No Jinja, no format-spec gymnastics — two `.replace()` calls, comment at the top of SKILL.md documenting the contract. Every future prompt edit costs exactly zero template-dependency reasoning.

**Audit classification was mostly unambiguous.** Of 8 Alfred occurrences in the SKILL, 6 classified cleanly as PRODUCT (wikilinks, codebase, other instances) and 2 as PERSONA (the identity intro pair). The one borderline case was line 18's `# Alfred — Talker` header — it doubles as the tool name (PRODUCT) and the identity-section intro (PERSONA). Classified PERSONA because the very next line reads "You are Alfred" and the two function together as an identity preamble. Would have escalated if there were more genuinely 50/50 cases.

**Calibration block has persona/product drift too.** The Andrew Newton record was authored pre-multi-instance and used "Alfred" loosely — sometimes meaning the talker, sometimes meaning the codebase. First multi-instance rename surfaces this tension. Pattern for future instance-named edits: scan the calibration block whenever an instance rename happens; fix the ambiguous phrasing as you touch it.

**`aliases` field is forward-placement but cheap.** Adding an unused field to a dataclass costs nothing at runtime and saves a migration when the router finally needs it. The memory (`project_multi_instance_design.md`) already specified the shape (case-insensitive alias-to-canonical map), so the field on `InstanceConfig` is already correctly typed for that future wiring.
