<div align="center">

# Alfred

**Your AI runs 24/7. It maintains your knowledge, executes your workflows, and learns while you sleep.**

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/alfred-vault.svg)](https://pypi.org/project/alfred-vault/)

</div>

---

You go to bed. While you sleep, Alfred processes today's meeting transcripts into structured records — people, tasks, decisions, projects — all wikilinked. It notices two records contradict each other and flags it. It sweeps the vault, fixes three broken links, and fills in two stub records. It discovers a cluster of notes about the same theme you never connected and writes the relationship. By morning, your knowledge graph is richer than when you left it.

You didn't ask for any of this. It just happened.

---

## What Alfred Does

**It turns your Obsidian vault into a self-maintaining knowledge graph.**

Drop anything into `inbox/` — meeting transcripts, emails, voice memos, notes. Alfred structures it, links it, and files it. Four background workers keep your vault clean, connected, and growing:

| | |
|---|---|
| **Curator** | Turns raw files into structured, interlinked records |
| **Janitor** | Finds and fixes broken links, orphaned files, invalid metadata |
| **Distiller** | Extracts hidden knowledge — assumptions, decisions, contradictions |
| **Surveyor** | Discovers semantic relationships across hundreds of records |

**It executes durable workflows on your behalf.**

Schedule your agent to process your inbox every morning. Run a weekly vault health sweep. Chain together multi-step operations that survive crashes and pick up where they left off. Write workflows in Python — the agent handles reasoning, Python handles control flow.

**It plugs into the tools you already use.**

Telegram, WhatsApp, Slack, iMessage, email, CLI — Alfred meets you where you are. Three pluggable AI backends (Claude Code, Zo Computer, OpenClaw) mean you're never locked in. Self-hosted on your own hardware, so your data never leaves your control.

---

## Get Started

```bash
pip install alfred-vault
alfred quickstart
alfred up
```

Three commands. The wizard sets up your vault, picks your AI backend, and starts the workers. Drop a file into `inbox/` and watch it happen.

---

## Use Cases

### Personal Task Manager

Paste a meeting transcript. Alfred extracts every task mentioned, assigns them to people, links them to the right project, and tracks status. Open a project page in Obsidian — live tables of tasks, conversations, and people, populated automatically.

### Relationship Manager

Every conversation Alfred processes creates or updates person and org records. Who said what, when, in what context. Your vault becomes a relationship graph — open anyone's page and see every interaction, every project, every commitment.

### Ambient Knowledge Base

The Distiller reads your notes and surfaces what's implicit: assumptions your team operates on, decisions that were made in passing, constraints mentioned once and forgotten. The Surveyor finds patterns across hundreds of records you'd never connect manually. Together they build an evidence graph that evolves with your work.

### Scheduled Automation

Daily inbox processing at 7am. Weekly vault health sweeps. Monthly knowledge distillation. Temporal workflows that survive crashes, sleep for days, and resume with full state. Nothing gets dropped.

---

## How It Works

Alfred is six layers of infrastructure working together:

```
 Interface     Telegram, WhatsApp, Slack, iMessage, email, CLI, TUI
     |
   Agent        Claude Code · Zo Computer · OpenClaw
     |
  Kinetic       Temporal — durable, scheduled workflow execution
     |
 Semantic       Obsidian vault — knowledge graph for humans and agents
     |
   Data          Omi · Zoom · email · RSS — ambient capture pipelines
     |
   Infra         Mac Mini · VPS · personal cloud — your hardware
```

**Semantic Layer** — Your Obsidian vault is the single source of truth. 20 structured record types with YAML frontmatter, connected by wikilinks. Both you and your agents read and write to it. The four workers maintain it continuously. Not a database — a browseable, versioned knowledge base.

**Kinetic Layer** — A [Temporal](https://temporal.io)-based execution engine. Write workflows in Python. Built-in activities: `spawn_agent`, `run_script`, `notify_slack`, and more. If the worker crashes mid-workflow, it picks up exactly where it left off.

**Agent Layer** — Pluggable backends. Claude Code (subprocess, default), Zo Computer (HTTP API), OpenClaw (subprocess, multi-stage pipelines). Switch without rewiring. Configure per-workflow agent profiles with different backends, skills, and scopes.

**Data Layer** — Anything that produces text can feed Alfred's inbox. Omi wearable transcripts, Zoom recordings, email digests, RSS feeds, API webhooks, bulk conversation exports.

**Interface Layer** — Governed by your agent runtime. OpenClaw gives you Telegram, WhatsApp, Slack, iMessage, Discord, Signal. Zo gives you Telegram, SMS, email. Locally: CLI and TUI dashboard.

**Infra Layer** — Self-hosted. A Mac Mini under your desk. A Hetzner VPS. A Zo Computer instance. Your data, your infrastructure, your control.

---

## Install

```bash
pip install alfred-vault                    # core (curator + janitor + distiller)
pip install "alfred-vault[temporal]"        # + workflow engine
pip install "alfred-vault[all]"             # + surveyor + temporal
```

**Prerequisites:** Python 3.11+ and an AI backend on PATH. Default is [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

<details>
<summary>From source</summary>

```bash
git clone https://github.com/ssdavidai/alfred.git
cd alfred && pip install -e ".[all]"
```
</details>

## CLI

```bash
alfred up                              # start workers (background)
alfred up --foreground                 # attached mode
alfred down                            # stop
alfred status                          # overview
alfred tui                             # live dashboard

alfred temporal worker                 # start workflow worker
alfred temporal run <workflow>         # trigger a workflow
alfred temporal schedule register <f>  # register cron schedules

alfred vault create <type> <name>      # create record
alfred vault list [type]               # list records
alfred process                         # batch-process inbox
alfred ingest <file>                   # import conversation export
```

## Configuration

```bash
alfred quickstart                      # recommended: interactive wizard
```

Or manually: `cp config.yaml.example config.yaml && cp .env.example .env`. Supports `${VAR}` environment variable substitution. See [`config.yaml.example`](config.yaml.example).

## Documentation

[GitHub Wiki](https://github.com/ssdavidai/alfred/wiki) · [Architecture](docs/Architecture.md) · [Semantic Layer](docs/Semantic-Layer.md) · [Kinetic Layer](docs/Kinetic-Layer.md) · [Agent Backends](docs/Agent-Backends.md)

[Curator](docs/Curator.md) · [Janitor](docs/Janitor.md) · [Distiller](docs/Distiller.md) · [Surveyor](docs/Surveyor.md) · [Vault Schema](docs/Vault-Schema.md) · [CLI Commands](docs/CLI-Commands.md)

## Contributing

Early-stage, actively developed. Issues, PRs, and ideas welcome.

## License

[MIT](LICENSE) · Built by [David Szabo-Stuban](https://screenlessdad.com)
