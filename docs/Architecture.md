# Architecture

## Source Layout

```
src/alfred/
  cli.py               # Top-level argparse CLI dispatcher
  daemon.py             # Background process spawn/stop (re-exec pattern)
  orchestrator.py       # Multiprocess daemon manager with auto-restart
  dashboard.py          # Rich TUI live dashboard (2x2 worker feed grid)
  quickstart.py         # Interactive setup wizard
  _data.py              # Bundled resource locator (importlib.resources)

  curator/              # Inbox processor
    config.py           # Typed dataclass config
    daemon.py           # Async watcher/daemon entry point
    pipeline.py         # 4-stage processing pipeline
    state.py            # JSON-based state persistence
    context.py          # Vault context builder
    watcher.py          # Filesystem watcher for inbox
    backends/           # Agent interface implementations
      __init__.py       # BaseBackend ABC, build_prompt()
      cli.py            # Claude Code backend
      http.py           # Zo Computer backend
      openclaw.py       # OpenClaw backend

  janitor/              # Vault quality scanner + fixer
    config.py
    daemon.py
    pipeline.py         # 3-stage fix pipeline
    scanner.py          # Issue detection engine
    autofix.py          # Deterministic fix logic
    state.py
    context.py
    backends/

  distiller/            # Knowledge extractor
    config.py
    daemon.py
    pipeline.py         # 2-pass extraction pipeline
    candidates.py       # Candidate scoring engine
    state.py
    context.py
    backends/

  surveyor/             # Semantic embedder + clusterer
    config.py
    daemon.py           # Async daemon with 4-stage pipeline
    embedder.py         # Vector embedding (Ollama/OpenAI)
    clusterer.py        # HDBSCAN + Leiden clustering
    labeler.py          # LLM cluster labeling (OpenRouter)
    writer.py           # Tag/relationship writer
    parser.py           # Vault record parser for surveyor
    watcher.py          # Vault file watcher
    state.py

  vault/                # Vault operations layer
    ops.py              # CRUD operations (create, read, edit, search, move, delete)
    schema.py           # KNOWN_TYPES, STATUS_BY_TYPE, TYPE_DIRECTORY, etc.
    scope.py            # Per-tool operation restrictions
    mutation_log.py     # Session-scoped JSONL mutation tracking
    obsidian.py         # Optional Obsidian CLI integration
    cli.py              # alfred vault subcommands

  tui/                  # Textual TUI (Python, for alfred up --live)
    app.py              # Main Textual application
    data.py             # Shared data types and constants
    screens/            # Dashboard, logs, mutations, status screens
    widgets/            # Worker cards, stat cards, health badges

  _bundled/             # Data files shipped in the wheel
    skills/
      vault-curator/    # Curator skill prompts
        SKILL.md        # Main skill prompt (legacy single-call)
        prompts/        # Stage-specific prompts
        references/     # Per-type reference schemas
      vault-janitor/    # Janitor skill prompts
      vault-distiller/  # Distiller skill prompts
    scaffold/           # Vault scaffolding
      _templates/       # Per-type Markdown templates
      _bases/           # Dataview base view definitions
      user-profile.md   # User profile template
    tui_js/             # Bundled Ink TUI (built from tui-ink/)
      index.js          # Single-file esbuild bundle

tui-ink/                # Ink TUI source (React + TypeScript)
  src/
    index.tsx           # Entry point
    app.tsx             # Root component, screen state machine
    store.ts            # React context store
    data/               # Types, parsers, interpreters, constants
    hooks/              # File tailers, state pollers, time series
    components/         # Header, Footer, WorkerLine, FeedLine, etc.
  build.mjs             # esbuild config (outputs to _bundled/tui_js/)
```

## Design Patterns

### Agent-Writes-Directly

Curator, janitor, and distiller delegate work to an AI agent backend. The agent receives a skill prompt plus vault context, then reads/writes vault files via the `alfred vault` CLI. The tool's job is orchestration: detecting changes, invoking the agent, reading the mutation log, and updating state.

Each agent invocation gets environment variables injected:
- `ALFRED_VAULT_PATH` — path to the vault
- `ALFRED_VAULT_SCOPE` — tool scope (restricts operations)
- `ALFRED_VAULT_SESSION` — mutation log session file

### Scope Enforcement

Each tool has a scope that restricts which vault operations the agent can perform:

| Tool | Create | Edit | Delete |
|------|--------|------|--------|
| Curator | Yes | Yes | No |
| Janitor | No | Yes | Yes |
| Distiller | Yes (learning types only) | No | No |

Defined in `vault/scope.py` with `SCOPE_RULES` dict.

### Pluggable Backends

Three backend implementations in each tool's `backends/`:

| Backend | Implementation | When to use |
|---------|---------------|-------------|
| Claude Code | Subprocess via `claude -p` | Default, requires Claude Code CLI |
| Zo Computer | HTTP API | Cloud-based, requires API key |
| OpenClaw | Subprocess via `openclaw agent` | Required for multi-stage pipelines |

### Pipeline vs Legacy Mode

The multi-stage pipelines (curator 4-stage, janitor 3-stage, distiller 2-pass) currently only work with the OpenClaw backend. Claude Code and Zo backends fall back to a legacy single-call mode where one agent call handles everything.

### Config Loading

Each tool has its own `config.py` with typed dataclasses. All follow the same pattern:
1. `load_from_unified(raw: dict)` takes the pre-loaded unified config
2. `_substitute_env()` replaces `${VAR}` placeholders
3. `_build()` recursively constructs dataclasses
4. Config loaded lazily in CLI handlers (not at import time)

## Execution Model

### Daemon Management

- `alfred up` uses `multiprocessing` to spawn one process per tool
- `orchestrator.py` manages auto-restart (max 5 retries, exit code 78 = missing deps)
- `orchestrator.py` writes `data/workers.json` every 2s with per-tool PID, status, and restart counts (consumed by the Ink TUI)
- `alfred up` (no flag) daemonizes via re-exec pattern
- `alfred down` uses sentinel file + SIGTERM
- Graceful shutdown via signal handling

### Async Internals

- Curator, janitor, distiller use `asyncio` for watcher loops and agent I/O
- Surveyor uses `asyncio` for LLM calls (labeling) and embedding API calls
- Each tool runs in its own process (via multiprocessing)

### State Persistence

- Per-tool state files: `data/{tool}_state.json`
- Tracks processed file hashes, sweep history, run history
- Vault itself is source of truth — state files are just bookkeeping
- Deleting state files forces re-processing

### Mutation Tracking

Every vault write operation is logged to:
1. **Session file** — per-invocation JSONL (`/tmp/alfred-{tool}-{uuid}.jsonl`)
2. **Audit log** — append-only JSONL (`data/vault_audit.log`)

The session file lets the pipeline know what the agent did (which files were created/modified). The audit log provides a complete history of all vault mutations.

## Template System

Templates in `_templates/` define the default structure for each record type:
- YAML frontmatter with all fields and defaults
- Body with heading, description placeholder, and base-view embeds

Base-view embeds (`![[project.base#Tasks]]`) render live Dataview tables in Obsidian. When records are created with custom body content, the template's base-view embeds are automatically extracted and appended to ensure every record gets its Dataview sections.

## Manifest Files

The curator and distiller use JSON manifest files for structured data exchange with the LLM:

1. A unique temp file path is generated: `/tmp/alfred-{tool}-{uuid}-manifest.json`
2. The path is embedded in the LLM prompt with instructions to write JSON there
3. After the LLM call, the pipeline reads the file
4. If the file is missing, it falls back to parsing stdout
5. If both fail, it retries (up to 3 attempts)
6. The temp file is cleaned up after each attempt

This approach avoids the problem of parsing JSON from stdout that's polluted by agent conversation/reasoning output.
