# Alfred

Alfred is a set of AI-powered background services that maintain an [Obsidian](https://obsidian.md) vault. You drop files into an inbox, and Alfred processes them into structured records, scans for quality issues, extracts latent knowledge, and maps semantic relationships — all automatically.

The vault itself is an operational system: 20 record types (projects, tasks, people, conversations, decisions, etc.) connected by wikilinks, with live base views and AI-maintained dynamic sections. Alfred treats the vault as a living knowledge graph and keeps it healthy.

## The Four Tools

| Tool | What it does |
|------|-------------|
| **Curator** | Watches `inbox/` for raw inputs (emails, notes, voice memos). Processes each into structured vault records with proper frontmatter, wikilinks, and filing. |
| **Janitor** | Periodically scans the vault for structural issues — broken wikilinks, invalid frontmatter, orphaned files, stub records — then invokes an AI agent to fix them. |
| **Distiller** | Reads operational records (conversations, sessions, notes) and extracts latent knowledge into epistemic records: assumptions, decisions, constraints, contradictions, and syntheses. |
| **Surveyor** | Embeds vault content into vectors, clusters records by semantic similarity, labels clusters via LLM, and writes relationship wikilinks back into the vault. |

All four share one config file (`config.yaml`), one CLI (`alfred`), and a common AI agent backend.

## How It Works

Curator, Janitor, and Distiller follow an **agent-writes-directly** pattern: each tool detects work to do, assembles context, hands it to an AI agent with a detailed skill prompt, and the agent reads/writes vault files directly. The tool's job is orchestration — detecting changes, tracking state, and logging what happened.

Surveyor has its own pipeline (embed → cluster → label → write) using local embeddings (Ollama) and an LLM for labeling (OpenRouter).

All vault mutations are recorded in a unified audit log (`data/vault_audit.log`) as append-only JSONL.

## Quick Start

```bash
pip install -e .
alfred quickstart    # interactive setup — picks vault path, backend, scaffolds dirs
alfred up            # starts daemons in background, prints PID, exits
```

Quickstart will offer to launch daemons automatically when it finishes.

## Install

```bash
# Base (curator + janitor + distiller)
pip install -e .

# Full (adds surveyor — needs Ollama for embeddings + OpenRouter for labeling)
pip install -e ".[all]"
```

Requires Python 3.11+.

## CLI Reference

```bash
# Daemon management
alfred up                         # start daemons (background, detached)
alfred up --foreground            # stay attached to terminal (dev/debug)
alfred up --only curator,janitor  # start selected tools only
alfred down                       # stop background daemons
alfred status                     # show daemon state + per-tool status

# Curator
alfred curator                    # run curator daemon in foreground

# Janitor
alfred janitor scan               # run structural scan (no fixes)
alfred janitor fix                # scan + AI agent fix
alfred janitor watch              # daemon mode (periodic sweeps)
alfred janitor status             # show sweep status
alfred janitor history            # show sweep history
alfred janitor ignore <file>      # exclude a file from scans

# Distiller
alfred distiller scan             # scan for extraction candidates
alfred distiller run              # scan + extract knowledge records
alfred distiller watch            # daemon mode (periodic extraction)
alfred distiller status           # show extraction status
alfred distiller history          # show run history

# Surveyor
alfred surveyor                   # run full embed/cluster/label/write pipeline

# Vault operations
alfred vault create <type> <name> # create a vault record
alfred vault read <path>          # read a record
alfred vault edit <path>          # edit a record
alfred vault list [type]          # list records

# Exec (run any command with vault env vars injected)
alfred exec -- <command>          # sets ALFRED_VAULT_PATH, ALFRED_VAULT_SESSION
alfred exec --scope curator -- <cmd>  # also sets ALFRED_VAULT_SCOPE
```

All commands accept `--config path/to/config.yaml` (default: `config.yaml`).

## Agent Backends

Three pluggable backends for the AI agent:

| Backend | How it works | Setup |
|---------|-------------|-------|
| **Claude Code** (default) | Runs `claude -p` as a subprocess | Install [Claude Code](https://claude.ai/code), ensure `claude` is on PATH |
| **Zo Computer** | HTTP API calls | Set `ZO_API_KEY` in `.env` |
| **OpenClaw** | Runs `openclaw` as a subprocess | Install OpenClaw, ensure `openclaw` is on PATH |

The agent receives a skill prompt (`skills/vault-{tool}/SKILL.md`) with the full record schema, extraction rules, and worked examples, plus live vault context. It then reads and writes vault files directly using `alfred vault` CLI commands.

## Vault Structure

The vault uses 20 record types, all Markdown with YAML frontmatter:

- **Operational:** project, task, session, conversation, input, note, process, run, event, thread
- **Entity:** person, org, location, account, asset
- **Epistemic (Learn system):** assumption, decision, constraint, contradiction, synthesis

Records reference each other via `[[wikilinks]]` in frontmatter (e.g., `project: "[[project/My Project]]"`). Three view types pull everything together:

- **Base views** (`_bases/*.base`) — live tables filtered by `file.hasLink(this.file)`
- **Dynamic sections** — blocks Alfred rewrites with synthesized briefings
- **Alfred instructions** — `alfred_instructions` frontmatter field for natural language commands

The `scaffold/` directory contains the canonical vault structure (templates, base views, starter views) that `alfred quickstart` copies into your vault.

## Configuration

```bash
cp config.yaml.example config.yaml
cp .env.example .env
```

`config.yaml` has sections for `vault`, `agent`, `logging`, and each tool. Environment variables are substituted via `${VAR}` syntax. See `config.yaml.example` for all options.

## Data & State

All runtime data lives in `data/`:

| File | Purpose |
|------|---------|
| `data/curator_state.json` | Tracks processed inbox files |
| `data/janitor_state.json` | Tracks scanned files, open issues, sweep history |
| `data/distiller_state.json` | Tracks distilled files, extraction history |
| `data/surveyor_state.json` | Tracks embedded files, clusters |
| `data/vault_audit.log` | Unified append-only JSONL log of all vault mutations |
| `data/alfred.pid` | PID file for background daemon |
| `data/*.log` | Per-tool log files |

The vault itself is the source of truth. State files are bookkeeping that can be deleted to force a full re-process.

## Architecture

```
src/alfred/
  cli.py              # top-level CLI dispatcher
  daemon.py            # background process management (spawn, stop, PID)
  orchestrator.py      # multiprocess daemon manager with auto-restart
  quickstart.py        # interactive setup wizard

  curator/             # inbox processor
  janitor/             # vault quality scanner + fixer
  distiller/           # knowledge extractor
  surveyor/            # semantic embedder + clusterer

  vault/               # vault operations layer
    mutation_log.py    # session + audit log tracking
    scope.py           # per-tool file access rules
    cli.py             # vault CRUD subcommands

  agent/               # pluggable AI backends
    claude.py, zo.py, openclaw.py

skills/
  vault-curator/SKILL.md    # curator agent prompt
  vault-janitor/SKILL.md    # janitor agent prompt
  vault-distiller/SKILL.md  # distiller agent prompt

scaffold/                   # canonical vault structure (copied by quickstart)
```

Each tool module follows the same pattern: `config.py` (typed dataclass config), `daemon.py` (async entry point), `state.py` (JSON persistence), `backends/` (agent interface).
