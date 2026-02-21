# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Alfred is a Python monorepo containing four AI-powered tools for managing an Obsidian vault. All tools share one config (`config.yaml`), one CLI entry point (`alfred`), and common infrastructure.

| Tool | Purpose |
|------|---------|
| **Curator** | Watches `inbox/` and processes raw inputs into structured vault records |
| **Janitor** | Scans vault for structural issues (broken links, invalid frontmatter, orphans) and fixes them |
| **Distiller** | Extracts latent knowledge (assumptions, decisions, constraints) from operational records |
| **Surveyor** | Embeds vault content, clusters semantically, labels clusters, discovers relationships |

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
alfred up                # Start all daemons (multiprocessing)
alfred up --only curator,janitor  # Start selected daemons
```

There are no tests, linter, or CI configured.

## Architecture

### Source Layout

All code lives under `src/alfred/`. Each tool follows the same module pattern:
- `config.py` — typed dataclass config loaded from the tool's section in `config.yaml`
- `daemon.py` — async watcher/daemon entry point
- `state.py` — JSON-based state persistence (processed hashes, sweep history)
- `cli.py` — subcommand handlers
- Tool-specific modules (scanner, backends, pipeline stages, etc.)

Shared infrastructure: `src/alfred/config.py` (unified config loader with `${ENV_VAR}` substitution), `src/alfred/log.py` (structlog setup), `src/alfred/cli.py` (top-level CLI dispatcher), `src/alfred/orchestrator.py` (multiprocess daemon manager).

### Agent-Writes-Directly Pattern

Curator, Janitor, and Distiller delegate work to an AI agent backend. The agent receives a skill prompt (from `skills/vault-{tool}/SKILL.md`) plus vault context, then reads/writes vault files directly. The tool's job is orchestration: detecting changes, diffing the vault before/after agent runs, and updating state.

Three pluggable backends in `src/alfred/agent/`: Claude Code (subprocess), Zo Computer (HTTP API), OpenClaw (subprocess). Selected via `agent.backend` in config.

### Surveyor Pipeline

Surveyor doesn't use the agent backend. It has its own 4-stage pipeline:
1. **Embed** — vectorize vault records via Ollama (local) or OpenAI-compatible API (OpenRouter)
2. **Cluster** — HDBSCAN + Leiden community detection
3. **Label** — LLM labels clusters and suggests relationships (OpenRouter)
4. **Write** — writes cluster tags and relationship wikilinks back to vault

Vector store: Milvus Lite (file-based, `data/milvus_lite.db`).

### Skill Files

`skills/vault-{curator,janitor,distiller}/SKILL.md` contain full prompts with record type schemas, extraction rules, and worked examples. These are loaded and sent to the agent backend at invocation time. Reference files in the same directory are inlined into the prompt.

### State & Data

- Per-tool state: `data/{tool}_state.json` — tracks processed file hashes, sweep/run history
- Per-tool logs: `data/{tool}.log`
- The vault itself is the source of truth; state files are just bookkeeping

### Execution Model

- Curator, Janitor, Distiller use `asyncio` for watcher loops and agent I/O
- `alfred up` uses `multiprocessing` to spawn one process per tool with auto-restart (max 5 retries)
- Graceful shutdown via signal handling in `orchestrator.py`

## Key Config

`config.yaml` has sections: `vault`, `agent`, `logging`, `curator`, `janitor`, `distiller`, `surveyor`. Environment variables are substituted via `${VAR}` syntax. See `config.yaml.example` for all options.

## Vault Record Format

Records are Markdown files with YAML frontmatter. 20 entity types (person, org, project, task, etc.) plus 5 learning types (assumption, decision, constraint, contradiction, synthesis). Relationships use Obsidian wikilinks: `[[type/Record Name]]`. The full schema is in `scaffold/CLAUDE.md` and the skill files.
