# Alfred

Alfred is a set of AI-powered background services that keep your [Obsidian](https://obsidian.md) vault organized, connected, and intelligent — without you doing the busywork.

You drop a raw file into your inbox. Alfred turns it into a structured record, links it to the right projects and people, scans for broken references, extracts decisions and assumptions you made along the way, and maps how everything in your vault relates to everything else. It runs quietly in the background.

## What does that look like?

You paste a meeting transcript into `inbox/`. A few seconds later, Alfred has:

- Created a **conversation** record with participants, status, and activity log
- Created or updated **person** records for everyone mentioned
- Filed **tasks** with assignees and linked them to the right project
- Linked everything together with wikilinks so it shows up in the right views automatically

Later, the janitor notices a project page has a broken link and fixes it. The distiller reads your recent session notes and extracts a **decision** record ("We chose Postgres over DynamoDB") with rationale and evidence links. The surveyor notices that three unrelated notes are all about the same theme and tags them as a cluster.

You don't trigger any of this. It just happens.

## Quick Start

**Prerequisites:** Python 3.11+ and an AI agent backend. The default is [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude` on PATH). Alternatives: Zo Computer (HTTP API) or OpenClaw.

```bash
pip install alfred-vault
alfred quickstart
```

The quickstart wizard walks you through choosing a vault path, agent backend, and optional surveyor setup. It scaffolds the vault directory structure, writes `config.yaml`, and offers to start daemons immediately.

```bash
alfred up       # start background daemons
alfred status   # check what's running
alfred down     # stop everything
```

## The Four Tools

**Curator** watches your `inbox/` folder. When a new file appears (email, voice memo transcript, raw notes), curator processes it through a 4-stage pipeline: (1) an LLM analyzes the content and creates a rich note, (2) pure-Python entity resolution deduplicates and creates people, orgs, projects, and other entities, (3) interlinking wires up wikilinks between the note and all entities, and (4) a per-entity LLM pass enriches each record with substantive body content and filled frontmatter. The result is a dense, well-connected graph — not stubs.

**Janitor** periodically scans every file in your vault for structural problems: broken wikilinks, missing or invalid frontmatter fields, orphaned files with no connections, stub records with no real content. It reports what it finds, and in fix mode, hands the issues to the AI agent to repair.

**Distiller** reads your operational records — conversations, session logs, project notes — and identifies latent knowledge worth extracting. It creates epistemic records: assumptions (tracked beliefs with confidence levels), decisions (with context, options, and rationale), constraints, contradictions, and syntheses. These form an evidence graph that evolves as your vault grows.

**Surveyor** works differently from the other three. It embeds your vault content into vectors (via Ollama locally or an OpenAI-compatible API), clusters records by semantic similarity using HDBSCAN + Leiden community detection, asks an LLM to label the clusters, and writes relationship tags and wikilinks back into your files.

## Install

```bash
# Base install (curator + janitor + distiller)
pip install alfred-vault

# Full install (adds surveyor — requires Ollama for embeddings + OpenRouter for labeling)
pip install "alfred-vault[all]"

# From source
git clone https://github.com/ssdavidai/alfred.git
cd alfred
pip install -e .          # base
pip install -e ".[all]"   # full
```

## CLI

```bash
# Daemon management
alfred up                              # start all daemons (background)
alfred up --foreground                 # stay attached (dev/debug)
alfred up --only curator,janitor       # start specific tools
alfred down                            # stop daemons
alfred status                          # per-tool status overview

# Batch processing
alfred process                         # batch-process all inbox files (Rich TUI)
alfred process -j 8                    # 8 parallel workers (default: 4)
alfred process -n 10                   # process only 10 files
alfred process --dry-run               # show what would be processed

# Bulk import
alfred ingest conversations.json       # split ChatGPT/Anthropic export into inbox files
alfred ingest export.json --dry-run    # preview without writing

# Run tools individually
alfred curator                         # curator daemon (foreground)
alfred janitor scan                    # structural scan, print report
alfred janitor fix                     # scan + AI agent fix
alfred janitor watch                   # periodic sweep daemon
alfred distiller scan                  # find extraction candidates
alfred distiller run                   # scan + extract knowledge records
alfred distiller watch                 # periodic extraction daemon
alfred surveyor                        # full embed/cluster/label/write pipeline

# Direct vault operations
alfred vault create <type> <name>      # create a record
alfred vault read <path>               # read a record
alfred vault edit <path>               # edit a record
alfred vault list [type]               # list records

# Run external commands with vault context
alfred exec -- <command>               # injects ALFRED_VAULT_PATH etc.
alfred exec --scope curator -- <cmd>   # also sets ALFRED_VAULT_SCOPE
```

All commands accept `--config path/to/config.yaml` (default: `config.yaml` in cwd).

## Configuration

`alfred quickstart` generates both files interactively. To configure manually instead:

```bash
cp config.yaml.example config.yaml
cp .env.example .env
# Edit both files
```

`config.yaml` has sections for `vault`, `agent`, `logging`, and each tool. Environment variables are substituted via `${VAR}` syntax. See `config.yaml.example` for all options.

## Agent Backends

Curator, janitor, and distiller delegate the actual reading and writing to an AI agent. You choose which one:

| Backend | How it runs | Setup |
|---------|------------|-------|
| **Claude Code** (default) | `claude -p` subprocess | Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code), ensure `claude` is on PATH |
| **Zo Computer** | HTTP API | Set `ZO_API_KEY` in `.env` |
| **OpenClaw** | `openclaw` subprocess | Install OpenClaw, ensure `openclaw` is on PATH |

Set `agent.backend` in `config.yaml` to `claude`, `zo`, or `openclaw`.

## Vault Structure

The vault uses structured Markdown files with YAML frontmatter. Records link to each other with `[[wikilinks]]` — open any project page and you'll see live tables of its tasks, conversations, sessions, and people, all populated automatically.

**20 record types:**

| Category | Types |
|----------|-------|
| Operational | project, task, session, conversation, input, note, process, run, event, thread |
| Entity | person, org, location, account, asset |
| Epistemic | assumption, decision, constraint, contradiction, synthesis |

`alfred quickstart` scaffolds the full directory structure with templates, base view definitions, and starter views (Home, CRM, Task Manager).

## Data & State

Runtime state lives in `data/`. The vault itself is the source of truth — state files are bookkeeping and can be deleted to force a full re-process.

| File | Purpose |
|------|---------|
| `data/*_state.json` | Per-tool processing state (what's been seen, sweep history, etc.) |
| `data/vault_audit.log` | Append-only JSONL log of every vault mutation |
| `data/alfred.pid` | PID file for the background daemon |
| `data/*.log` | Per-tool log files |

## Architecture

```
src/alfred/
  cli.py               # CLI dispatcher
  daemon.py             # background process management
  orchestrator.py       # multiprocess daemon manager with auto-restart
  quickstart.py         # interactive setup wizard
  _data.py              # bundled resource locator (importlib.resources)

  curator/              # inbox processor
  janitor/              # vault quality scanner + fixer
  distiller/            # knowledge extractor
  surveyor/             # semantic embedder + clusterer

  vault/                # vault operations layer (CRUD, mutation log, scoping)
  agent/                # pluggable AI backends (claude, zo, openclaw)

  _bundled/             # data files shipped in the wheel
    skills/             # agent skill prompts (one per tool)
    scaffold/           # vault directory structure, templates, base views
```

Each tool follows the same module pattern: `config.py` (typed dataclass), `daemon.py` (async entry point), `state.py` (JSON persistence), `backends/` (agent interface), `cli.py` (subcommands).

## License

MIT
