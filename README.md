<div align="center">

# 🎩 Alfred

**Your Obsidian vault runs itself.**

Drop files into your inbox. Alfred structures, links, and organizes everything — automatically.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/alfred-vault.svg)](https://pypi.org/project/alfred-vault/)

</div>

---

You paste a meeting transcript into `inbox/`. A few seconds later, Alfred has:

- Created a **conversation** record with participants, status, and activity log
- Created or updated **person** records for everyone mentioned
- Filed **tasks** with assignees and linked them to the right project
- Connected everything with wikilinks so it shows up in the right Obsidian views automatically

You didn't trigger any of this. It just happened.

---

## The Problem

Obsidian is powerful, but keeping a vault organized is a full-time job. You end up with orphaned notes, broken links, knowledge trapped inside meeting transcripts, and no clear picture of how your projects actually connect. The more you use it, the more maintenance it demands.

## The Fix

Alfred is a set of AI-powered background services — four tools that continuously watch, clean, extract, and connect your vault while you do real work.

| Tool | What it does |
|------|-------------|
| **Curator** | Watches `inbox/` and turns raw files (emails, transcripts, notes) into structured records |
| **Janitor** | Scans for broken links, missing frontmatter, orphaned files — and fixes them |
| **Distiller** | Reads your notes and extracts decisions, assumptions, and constraints into an evidence graph |
| **Surveyor** | Embeds your vault into vectors, clusters by semantic similarity, and writes relationship tags back |

## Quickstart

```bash
pip install alfred-vault
alfred quickstart          # interactive setup wizard
alfred up                  # start background daemons
```

That's it. The wizard handles vault path, agent backend, and directory scaffolding.

**Prerequisites:** Python 3.11+ and an AI agent on PATH. Default is [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Also supports Zo Computer (HTTP) and OpenClaw.

## How It Works

### Curator — Inbox → Structure

A new file appears in `inbox/`. Curator reads it, passes it to your AI agent with full vault context, and the agent creates whatever records the content calls for — conversations, people, tasks — all linked together.

### Janitor — Entropy → Order

Periodically sweeps every file for structural problems: broken wikilinks, invalid frontmatter, orphaned files, stub records. In fix mode, hands the issues to the AI agent to repair automatically.

### Distiller — Notes → Knowledge

Reads operational records (conversations, session logs, project notes) and surfaces latent knowledge worth extracting. Creates epistemic records: assumptions with confidence levels, decisions with rationale, constraints, contradictions, and syntheses. These form an evidence graph that evolves with your vault.

### Surveyor — Isolation → Connection

Embeds vault content into vectors (Ollama locally or OpenAI-compatible API), clusters with HDBSCAN + Leiden community detection, asks an LLM to label the clusters, and writes relationship tags and wikilinks back into files. Three notes about the same theme that you never connected? Surveyor finds them.

## Install

```bash
# Base (curator + janitor + distiller)
pip install alfred-vault

# Full (adds surveyor — requires Ollama + OpenRouter)
pip install "alfred-vault[all]"

# From source
git clone https://github.com/ssdavidai/alfred.git
cd alfred && pip install -e ".[all]"
```

## Agent Backends

| Backend | Type | Setup |
|---------|------|-------|
| **Claude Code** (default) | Subprocess | Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code), `claude` on PATH |
| **Zo Computer** | HTTP API | Set `ZO_API_KEY` in `.env` |
| **OpenClaw** | Subprocess | Install OpenClaw, `openclaw` on PATH |

Set `agent.backend` in `config.yaml` to `claude`, `zo`, or `openclaw`.

## Vault Structure

Structured Markdown with YAML frontmatter. 20 record types across three categories:

| Category | Types |
|----------|-------|
| **Operational** | project, task, session, conversation, input, note, process, run, event, thread |
| **Entity** | person, org, location, account, asset |
| **Epistemic** | assumption, decision, constraint, contradiction, synthesis |

Records link via `[[wikilinks]]` — open any project page and you'll see live tables of tasks, conversations, and people, populated automatically.

## CLI Reference

```bash
# Daemons
alfred up                              # start all (background)
alfred up --foreground                 # attached mode (dev/debug)
alfred up --only curator,janitor       # start specific tools
alfred down                            # stop
alfred status                          # overview
alfred tui                             # live Ink TUI dashboard (requires Node.js)

# Individual tools
alfred curator                         # curator daemon (foreground)
alfred janitor scan                    # scan + report
alfred janitor fix                     # scan + AI fix
alfred janitor watch                   # periodic sweep daemon
alfred distiller scan                  # find candidates
alfred distiller run                   # scan + extract
alfred distiller watch                 # periodic daemon
alfred surveyor                        # full pipeline

# Vault operations
alfred vault create <type> <name>      # create record
alfred vault read <path>               # read record
alfred vault edit <path>               # edit record
alfred vault list [type]               # list records

# External commands with vault context
alfred exec -- <command>               # injects ALFRED_VAULT_PATH
alfred exec --scope curator -- <cmd>   # also sets ALFRED_VAULT_SCOPE
```

## Configuration

```bash
alfred quickstart                      # recommended: interactive setup
# — or —
cp config.yaml.example config.yaml
cp .env.example .env
```

`config.yaml` has sections for `vault`, `agent`, `logging`, and each tool. Supports `${VAR}` environment variable substitution. See [`config.yaml.example`](config.yaml.example) for all options.

## Documentation

Full documentation is available in [`docs/`](docs/) and on the [GitHub Wiki](https://github.com/ssdavidai/alfred/wiki):

- [Installation](docs/Installation.md)
- [Configuration](docs/Configuration.md)
- [CLI Commands](docs/CLI-Commands.md)
- [Vault Schema](docs/Vault-Schema.md)
- [Curator](docs/Curator.md) | [Janitor](docs/Janitor.md) | [Distiller](docs/Distiller.md) | [Surveyor](docs/Surveyor.md)
- [Live Dashboard](docs/Live-Dashboard.md)
- [Architecture](docs/Architecture.md)
- [Agent Backends](docs/Agent-Backends.md)
- [User Profile](docs/User-Profile.md)

## Contributing

Alfred is early-stage and actively developed. Issues, PRs, and ideas are welcome.

## License

[MIT](LICENSE)

Built with ❤️ by David Szabo-Stuban -> ScreenlessDad.com
