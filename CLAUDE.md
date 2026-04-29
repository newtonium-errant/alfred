# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Alfred is a Python monorepo containing five AI-powered tools for managing an Obsidian vault. All tools share one config (`config.yaml`), one CLI entry point (`alfred`), and common infrastructure.

| Tool | Purpose |
|------|---------|
| **Curator** | Watches `inbox/` and processes raw inputs into structured vault records |
| **Janitor** | Scans vault for structural issues (broken links, invalid frontmatter, orphans) and fixes them |
| **Distiller** | Extracts latent knowledge (assumptions, decisions, constraints) from operational records |
| **Surveyor** | Embeds vault content, clusters semantically, labels clusters, discovers relationships |
| **Talker** | Telegram voice/text chat with Alfred, vault-grounded |

## Install & Run

```bash
# Base install (curator + janitor + distiller)
pip install -e .

# Full install (adds surveyor with ML/vector deps)
pip install -e ".[all]"

# Setup
cp config.yaml.example config.yaml
cp .env.example .env

# Run
alfred quickstart        # Interactive setup wizard
alfred up                # Start all daemons (background)
alfred up --foreground   # Stay attached (dev/debug)
alfred up --only curator,janitor  # Start selected daemons
alfred down              # Stop daemons
alfred status            # Per-tool status overview
```

There are no tests, linter, or CI configured.

### Worktree + editable-install gotcha

`pip install -e` writes a path-pin into the active venv pointing at whatever directory you ran it from. When a builder agent runs `pip install -e .` from a git worktree under `.claude/worktrees/agent-*/`, the venv re-pins to that worktree path. After the worktree is removed, the venv's pin is broken — `import alfred` resolves to a deleted directory, all daemons crash on next restart.

Fix: after any worktree-based install, re-pin to the main repo with `pip install -e /home/andrew/alfred`. Builders should avoid running `pip install -e` from a worktree unless they're testing a dependency change; for code-only edits, the existing pin works because Python resolves `alfred.*` modules via the installed egg-link, not via the worktree's source tree.

## Architecture

### Source Layout

All code lives under `src/alfred/`. Each tool follows the same module pattern:
- `config.py` — typed dataclass config loaded from the tool's section in `config.yaml`
- `daemon.py` — async watcher/daemon entry point
- `state.py` — JSON-based state persistence (processed hashes, sweep history)
- `cli.py` — subcommand handlers
- `backends/__init__.py` — `BaseBackend` ABC, `BackendResult` dataclass, and prompt builder
- `backends/cli.py`, `http.py`, `openclaw.py` — concrete backend implementations

Shared infrastructure:
- `src/alfred/cli.py` — top-level argparse CLI dispatcher, all subcommand handlers
- `src/alfred/daemon.py` — background process spawn/stop via re-exec pattern (`alfred up` re-launches itself with `--_internal-foreground`)
- `src/alfred/orchestrator.py` — multiprocess daemon manager with auto-restart (max 5 retries)
- `src/alfred/_data.py` — `importlib.resources` locator for bundled skills/scaffold/examples

### Agent-Writes-Directly Pattern

Curator, Janitor, and Distiller delegate work to an AI agent backend. The agent receives a skill prompt (from `src/alfred/_bundled/skills/vault-{tool}/SKILL.md`) plus vault context, then reads/writes vault files via the `alfred vault` CLI. The tool's job is orchestration: detecting changes, invoking the agent, reading the mutation log, and updating state.

**Important flow:** For CLI backends (Claude Code, OpenClaw), each agent invocation gets environment variables (`ALFRED_VAULT_PATH`, `ALFRED_VAULT_SCOPE`, `ALFRED_VAULT_SESSION`) injected. The agent uses `alfred vault` commands (never direct filesystem access). Changes are tracked via a JSONL session file (`vault/mutation_log.py`). For the HTTP backend (Zo), a snapshot/diff fallback is used instead.

**Scope enforcement:** Each tool has a scope (`curator`, `janitor`, `distiller`) that restricts which vault operations the agent can perform. Defined in `vault/scope.py` with `SCOPE_RULES` dict. Curator can create/edit but not delete; janitor can edit/delete but not create; distiller can only create learning types.

Three pluggable backends in each tool's `backends/`: Claude Code (subprocess via `claude -p`), Zo Computer (HTTP API), OpenClaw (subprocess via `openclaw agent --message`). Selected via `agent.backend` in config.

### Each Tool's Backend Has Its Own Prompt Builder

Each tool's `backends/__init__.py` contains a different `build_*_prompt()` function tailored to that tool's needs. Curator sends inbox content + vault context. Janitor sends issue reports + affected records. Distiller sends source records + existing learning records for dedup. They are NOT shared — each tool has independent prompt assembly.

### Three Layers — Code vs Config vs Prompt

Behavior in this repo lives in three distinct layers. When you reach for a "rebuild," first check whether the existing layer just needs different content.

- **Code** (`src/alfred/`) — how the system runs. Process orchestration, scope enforcement, vault ops, state persistence, daemon loops. Stable across instances. Changes here affect every instance.
- **Config** (`config.yaml`, `config.<instance>.yaml`) — per-instance customization. Ports, tokens, scheduling, feature flags, vault paths, instance name. Changes here affect ONE instance without touching code.
- **Prompt** (`src/alfred/_bundled/skills/vault-*/SKILL.md`, distiller stage prompts) — what the LLM is told to do. Extraction rules, type discrimination, voice calibration, worked examples. Changes here affect agent behavior without touching the code path that invokes the agent.

Common drift: behavior that belongs in **prompt** ends up hardcoded in **code** ("if instance == X, do Y differently"); per-instance values that belong in **config** end up hardcoded in **code** (`agent="salem"` literals in writer paths). When you see either, route the fix to the right layer — prompt-tuner for prompt-layer work, builder for config/code-layer work.

### Validation Gate Ordering — Vault Ops

Vault writes go through two gates in order:
1. `_validate_type` (in `vault/ops.py`) — checks the record type against `KNOWN_TYPES` / `LEARN_TYPES` registries
2. `check_scope` (in `vault/scope.py`) — checks the calling tool's scope against `SCOPE_RULES` for the (op, type) pair

When adding a new instance type, scope rule, or per-instance type registry: the scope check needs the calling instance's identity to look up the right rule. Hardcoded `agent="salem"` or `scope="talker"` literals at the call site silently route every instance through Salem's gate. Per-instance scope work should plumb the instance name through the call, not default it. See `project_hardcoding_followups.md` for the open sweep items.

### Surveyor Pipeline

Surveyor doesn't use the agent backend. It has its own 4-stage pipeline:
1. **Embed** — vectorize vault records via Ollama (local) or OpenAI-compatible API (OpenRouter)
2. **Cluster** — HDBSCAN + Leiden community detection
3. **Label** — LLM labels clusters and suggests relationships (OpenRouter)
4. **Write** — writes cluster tags and relationship wikilinks back to vault

Vector store: Milvus Lite (file-based, `data/milvus_lite.db`).

### Bundled Data (`src/alfred/_bundled/`)

Shipped in the wheel. Located via `_data.py` using `importlib.resources`:
- `skills/vault-{curator,janitor,distiller}/SKILL.md` — full prompts with record type schemas, extraction rules, worked examples. Reference files in the same directory are inlined into the prompt at runtime.
- `scaffold/` — vault directory structure, Obsidian config, `_templates/` (per-type Markdown templates with `{{title}}`/`{{date}}` placeholders), `_bases/` (Dataview base views), starter views.

### Config Loading Pattern

Each tool has its own `config.py` with typed dataclasses. All follow the same pattern:
- `load_from_unified(raw: dict)` takes the pre-loaded unified config dict and builds the tool's config
- `_substitute_env()` replaces `${VAR}` placeholders with environment variables
- `_build()` recursively constructs dataclasses from nested dicts
- Config is loaded lazily in CLI handlers (not at import time)

### State persistence — load() schema-tolerance contract

Every tool's `state.py` `load()` MUST filter incoming JSONL/JSON data against the dataclass's known fields before constructing instances. The pattern, validated across distiller, surveyor, janitor, and curator:

```python
@classmethod
def from_dict(cls, data: dict) -> "FileState":
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    return cls(**known)
```

This is the **load-time schema-tolerance contract**. It enforces forward-compatibility: a state file written by an older version of the tool that has an extra field doesn't crash the loader; a state file written by a newer version with extra fields silently ignores them on rollback. Without this filter, schema drift between tool versions becomes a deployment-blocking failure.

**Default state paths must be tool-scoped** to avoid collision when an instance config omits a tool's block. Each tool's `StateConfig.path` defaults to `./data/<tool>_state.json` (e.g., `./data/distiller_state.json`). Sharing the same default `./data/state.json` across tools would let one tool silently load another tool's state file and present misleading status info — the schema-tolerance filter prevents the crash but not the wrong-source-of-truth.

### Vault Operations Layer (`src/alfred/vault/`)

- `ops.py` — CRUD operations (`vault_create`, `vault_read`, `vault_edit`, `vault_move`, `vault_delete`, `vault_search`, `vault_list`, `vault_context`). Integrates with Obsidian CLI (1.12+) when available for search and moves.
- `schema.py` — `KNOWN_TYPES` (20 entity types), `LEARN_TYPES` (5), `STATUS_BY_TYPE`, `TYPE_DIRECTORY`, `LIST_FIELDS`, `REQUIRED_FIELDS`, `NAME_FIELD_BY_TYPE`
- `scope.py` — per-tool operation restrictions
- `mutation_log.py` — session-scoped JSONL mutation tracking, audit log
- `obsidian.py` — optional Obsidian CLI integration
- `cli.py` — `alfred vault` subcommands (JSON output)

### State & Data

- Per-tool state: `data/{tool}_state.json` — tracks processed file hashes, sweep/run history
- Per-tool logs: `data/{tool}.log`
- Audit log: `data/vault_audit.log` — append-only JSONL of every vault mutation
- PID file: `data/alfred.pid` — for daemon management
- The vault itself is the source of truth; state files are just bookkeeping and can be deleted to force re-processing

### Execution Model

- Curator, Janitor, Distiller use `asyncio` for watcher loops and agent I/O
- `alfred up` uses `multiprocessing` to spawn one process per tool with auto-restart (max 5 retries, exit code 78 = missing deps, skip restart)
- `alfred up` (no flag) daemonizes via re-exec; `alfred down` uses sentinel file + SIGTERM
- Graceful shutdown via signal handling in `orchestrator.py`

## Coding Team

This project uses two knowledge sources:
- **Aftermath-Lab** at `/home/andrew/aftermath-lab/` — shared development patterns (n8n, frontend, Supabase, auth, QA)
- **Agent instructions** at `.claude/agents/` — Alfred-specific specialist roles

### Agent Team

| Agent | Role | Mode | When to spawn |
|-------|------|------|---------------|
| **builder** | Python implementation across all tools | foreground | Code changes to any tool, new features, refactors |
| **vault-reviewer** | QA on vault output quality (not code) | background | After bulk processing, after prompt changes, periodic quality checks |
| **prompt-tuner** | Owns SKILL.md and extraction prompts | foreground | When vault-reviewer finds output quality issues, when adding new record types |
| **infra** | Ollama, n8n, tunnel, WSL2, dependencies | foreground | When infrastructure breaks or needs configuration |
| **code-reviewer** | Code QA, regression checking | background | Before committing significant changes |

Each agent reads `.claude/agents/{name}.md` for their specific instructions.

### Agent Lifecycle

- **Persistent agents** — spawn vault-reviewer and builder at session start. Keep alive via SendMessage. Don't respawn per task.
- **On-demand agents** — spawn prompt-tuner, infra, code-reviewer when needed.
- **Concurrent reviews** — vault-reviewer reviews each piece of work as it's completed, not batched at session end.
- **Feedback loop** — vault-reviewer findings inform prompt-tuner changes, which the builder may need to support with code changes. This cycle is how Alfred improves.

### Spawning Rules

- **Editing agents run in foreground** — they need permission prompts for file writes
- **QA/review agents run in background** — read-only, no permissions needed
- **Responsive spawning** — don't create all 5 agents for a simple task. Spawn specialists when the work requires them.
- **Task routing** — route work to the right specialist. Builder writes code, prompt-tuner changes prompts, vault-reviewer evaluates output.
- **Plan mode** is the default for agents. Use `acceptEdits` for simple, well-defined tasks where plan approval adds friction without value.

### Team Lead Rules

- **Don't do agent work as team lead.** Route implementation to the builder, prompt changes to the prompt-tuner. The team lead orchestrates, reviews reports, makes decisions, and writes session notes. If you're writing code directly in the main conversation, you're doing it wrong.
- **Pattern discovery = documentation trigger.** If the same bug appears twice, don't just fix it — flag it for documentation in agent instructions or CLAUDE.md.
- **Cross-agent contracts first.** When work crosses domains (builder changing template variables that prompt-tuner depends on), agree on the interface before implementing.
- **Scope/schema-narrowing commits trigger a SKILL audit in the same cycle.** When the builder tightens a vault scope (field allowlist, new denied op, stricter type filter) or narrows a record schema, the agent-facing instructions in the affected SKILL(s) may contain dead or now-forbidden steps. Bundle a prompt-tuner pass with the scope change OR schedule it immediately after, before the SKILL silently drifts out of sync. Ship-same-day is the goal. Reason: Q3 (2026-04-19 commit `2b8ddbd`) denied body writes on janitor scope; the SKILL's STUB001 "flesh out body" step stayed dead for ~24h until caught during Q2's SKILL update. Scope and prompt are two sides of one contract.
- **Session start requires a dirty-tree audit.** Before taking on new work, run `git status` and classify every dirty or untracked path in outer-repo scope as one of: (a) commit now, (b) discard, (c) explicitly deferred with a reason. No new work begins until every path has been accounted for. If something is intentionally deferred across sessions (e.g. a feature-in-progress like Layer 3), the reason must live in a memory entry or session note so the next session doesn't rediscover it cold. This is how uncommitted work stops accumulating.
- **Surgical staging when pre-existing dirty files are in scope.** If this session touches a file that already has unrelated uncommitted changes from a prior session, do not stage the whole file. Back it up, revert to HEAD, re-apply only this session's hunks, commit, then restore the backup so the pre-existing work stays in the working tree for its own eventual commit. Scope bleed is worse than verbose git gymnastics.
- **Aftermath-lab downstream sync at session start.** Pull canonical pattern updates into the Alfred project fork: `cd ~/aftermath-alfred && git pull upstream master && git push origin master`. Also check `teams/alfred/reviews/` for any feedback from origin (Coding Alfred). This ensures the team starts every session with the latest canonical patterns and any review dialogue from the origin agent. If the pull conflicts, resolve before starting new work — conflicts mean the fork accidentally modified canonical content, which shouldn't happen per convention.

### Workflow Deliverables

- **New n8n workflows:** generate importable JSON files (unless user says otherwise)
- **Existing n8n workflows:** create instruction docs for manual UI application (workflow JSON breaks on import after UI edits). If the user asks for a complete rewrite, generate importable JSON.

### Session Notes — Where they live + Learnings Section

**Convention (effective 2026-04-29)**: hand-authored **dev session notes** (the kind team-lead writes summarizing development arcs — "Phase 1 ship", "Upstream merge", "Observability arc", "BIT c1 skeleton", etc.) go to **`aftermath-lab/session/`**, not Salem's `vault/session/`.

Reasoning: dev session notes are about Alfred's development. KAL-LE owns aftermath-lab and is the canonical-pattern curator. KAL-LE distiller-radar is the natural consumer. Salem's vault is for operational/personal records (RRTS, persons, projects). Dev notes are out-of-scope for Salem's distiller; they're load-bearing for KAL-LE's.

| Note type | Where it lives | Authored by |
|---|---|---|
| Hand-authored dev session notes | `aftermath-lab/session/` | team-lead (this convention) |
| Auto-generated talker conversation records | `<instance>/session/` (each instance's own vault) | talker daemon |
| Auto-generated capture session records | `<instance>/session/` | capture daemon |
| Operational session notes about Salem-domain work (RRTS, etc.) | `vault/session/` (Salem) | situational |

**Historical dev session notes already in Salem's `vault/session/`** stay there as historical baseline (~50+ notes pre-2026-04-29). KAL-LE's Phase 1 backfill will do a one-time extraction pass over them but will not migrate the files.

**Every dev session note must include an `## Alfred Learnings` section**. This flags reusable knowledge for the KAL-LE distiller-radar:

- **New gotchas** — bugs that cost debugging time (e.g., "pymilvus 2.6.10 incompatible with milvus-lite 2.5.1")
- **Anti-patterns confirmed** — tried something, it failed (e.g., "OpenClaw can't run local models")
- **Patterns validated** — approach worked well, should be standard (e.g., "Ollama's OpenAI-compatible endpoint works for labeling")
- **Corrections** — something in agent instructions or CLAUDE.md is wrong or outdated
- **Missing knowledge** — had to figure out something that should have been documented

The KAL-LE distiller processes these on two levels: explicit flagged learnings first, then full session scan for implicit patterns (repeated rework, knowledge compliance gaps, undocumented standards). Output lands as `learn/` records in `aftermath-lab/`, feeding the canonical curation flow.

### Agent Knowledge Requirements

- **builder** reads aftermath-lab docs + CLAUDE.md before writing code
- **vault-reviewer** reads vault CLAUDE.md + schema.py + relevant SKILL.md
- **prompt-tuner** reads current SKILL.md + vault-reviewer findings + sample output
- **infra** reads infra agent instructions (contains current system state and known issues)
- **code-reviewer** reads CLAUDE.md + the specific code being reviewed

## Key Config

`config.yaml` has sections: `vault`, `agent`, `logging`, `curator`, `janitor`, `distiller`, `surveyor`, `brief`, `mail`. Environment variables are substituted via `${VAR}` syntax. See `config.yaml.example` for all options.

## Vault Record Format

Records are Markdown files with YAML frontmatter. 20 entity types (person, org, project, task, etc.) plus 5 learning types (assumption, decision, constraint, contradiction, synthesis). Relationships use Obsidian wikilinks: `[[type/Record Name]]`. The full schema is in `scaffold/CLAUDE.md` and the skill files.
