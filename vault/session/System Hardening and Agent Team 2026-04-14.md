---
alfred_tags:
- software/alfred
- email/integration
- system/buildout
created: '2026-04-14'
description: Consolidation unblock, surveyor activation, Morning Brief, agent team
  setup, vault snapshots, curator duplication fix
distiller_signals: decision:1, constraint:3, contradiction:3, has_outcome
intent: Unblock deferred features, build Morning Brief, establish agent team, harden
  system reliability
janitor_note: LINK001 — broken link [[person/Andrew Newton]] in participants field,
  no person record exists. Create person/Andrew Newton.md or update participants link.
name: System Hardening and Agent Team
outputs:
- '[[run/Morning Brief 2026-04-14]]'
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Ollama Local LLM and System Buildout 2026-04-08]]'
relationships: []
status: completed
tags:
- consolidation
- surveyor
- morning-brief
- agent-team
- vault-snapshots
- curator-fix
- infrastructure
type: session
---

# System Hardening and Agent Team — 2026-04-14

## Intent

Unblock deferred features, build Morning Brief weather module, establish specialist agent team, harden system reliability with vault snapshots and curator duplication fix.

## Work Completed

### Consolidation Sweep Unblocked
- Refactored `pipeline.py` `_call_llm` from OpenClaw-only to backend-agnostic (Claude + OpenClaw)
- Changed `daemon.py` `_use_pipeline()` to route Claude through multi-stage pipeline
- First run: 31 records modified across 5 learn types in ~13 minutes

### Surveyor Fully Operational
- Ollama nomic-embed-text for embeddings + qwen2.5:14b for labeling (zero API cost)
- Fixed: import bug, setuptools/pkg_resources, pymilvus version mismatch, Milvus URI, Ollama model swap crash
- Initial sync: 1419 files embedded (~6 min), 165 semantic clusters, 538 clusters labeled (~1 hour)

### Mail Webhook Wired into Orchestrator
- `alfred up` now auto-starts mail webhook alongside all other tools

### Morning Brief — Weather + Operations
- Weather module: METAR + TAF from aviationweather.gov for 4 NS stations
- Operations module: daily snapshot of tool activity from state files + audit log
- `alfred brief weather` — refresh weather in-place for storm tracking
- Daemon auto-starts with `alfred up`, generates at 0600 ADT daily

### Session Notes Moved to Vault
- From `docs/session-notes/` into `vault/session/` as proper session records
- Now processed by distiller, surveyor, janitor automatically

### Specialist Agent Team Established
5 agents at `.claude/agents/`:
- **builder** — Python implementation across all tools
- **vault-reviewer** — QA on vault output quality
- **prompt-tuner** — owns SKILL.md and extraction prompts
- **infra** — Ollama, n8n, tunnel, WSL2
- **code-reviewer** — regression checking, pattern compliance

Applied Aftermath-Lab operational patterns: concurrent reviews, pattern discovery triggers, team lead discipline, cross-agent contracts, structured reporting, session learnings section.

### Vault Snapshot System
- Separate git repo inside `vault/` — independently versioned
- `alfred vault snapshot --init / --status / --restore`
- Auto-snapshot after daily Morning Brief generation
- Full rollback capability for any vault record

### Curator Duplication Fix
- **Root cause:** Zombie curator process from previous `alfred up` survived restart. Two processes watched same inbox, every email produced duplicate records.
- **Fix 1:** File-level locking in `curator/daemon.py` — atomic `.lock` file with PID prevents concurrent processing
- **Fix 2:** Per-tool PID tracking in `orchestrator.py` — kills stale processes on startup, writes per-tool PIDs, cleans up on shutdown
- Zombie process killed manually during session

### First Agent Team Deployment
Vault-reviewer ran initial scan and found:
- 3 BLOCKs: curator duplicates (systemic), duplicate org records (PocketPills, Alliance Dental)
- 4 WARNs: duplicate base embeds in learning records, surveyor noise relationships, `project: ''` schema violations
- 4 NOTEs: distiller self-awareness strong, good janitor annotations, curator marketing record accumulation

## Outcome

### System State After This Session
- `alfred up` manages 6 tools: curator, janitor, distiller, surveyor, mail, brief
- Vault: ~1450 records, independently git-versioned with daily snapshots
- Curator duplication fixed — file locking + per-tool PID tracking
- Morning Brief: weather + operations sections, daily at 0600 ADT
- Agent team: 5 specialists with structured reporting and Aftermath-Lab patterns

### Open Items from Vault Review
- SKILL.md STEP 7 removal (prompt-tuner — agent should not move inbox files)
- Duplicate base embed cleanup (WARN-1, ~50% of learning records affected)
- Surveyor noise relationships (WARN-2, e.g., DigitalOcean "contradicts" Marriott)
- `project: ''` empty string schema violations (WARN-3, ~20 records)

## Alfred Learnings

### New Gotchas
- `pymilvus 2.6.10` incompatible with `milvus-lite 2.5.1` — gRPC hangs on MilvusClient init. Pin to `pymilvus[milvus_lite]==2.5.7`
- `setuptools>=81` removed `pkg_resources` but milvus-lite depends on it — pin `setuptools<81`
- Milvus URI must be absolute path, not relative `./data/milvus_lite.db`
- Ollama model swap crashes if context length set to 32k — reset to 4k before swapping between nomic-embed-text and qwen2.5:14b
- aviationweather.gov returns wind direction as string in some responses — format code must handle both int and str
- `multiprocessing.Process(daemon=True)` children survive parent SIGKILL — per-tool PID tracking needed for cleanup

### Anti-Patterns Confirmed
- OpenClaw is a cloud model router, not local inference — don't use it for local LLM
- Single PID file for orchestrator is insufficient — zombie tool processes survive across restarts undetected

### Patterns Validated
- Separate git repo inside vault for versioning — clean separation from code repo, trivial rollback
- Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) works as drop-in for OpenRouter config — zero-cost labeling
- Agent team split by concern (build/review/tune/infra) rather than by technology — fits monorepo architecture
- Concurrent review pattern from Aftermath-Lab — vault-reviewer found curator duplication in initial scan, immediately actionable
- Brief renderer as section assembler — weather and operations plug in independently, future sections just add a module

### Corrections
- CLAUDE.md Key Config section was missing `brief` and `mail` — updated
- Orchestrator `start_process` conditional for no-skills_dir tools was incomplete — now includes `brief`

### Missing Knowledge
- No documentation on how to safely restart Alfred daemons without losing state
- No runbook for common infrastructure issues (Ollama crash, tunnel down, Milvus lock)
- Agent team instructions should include a "morning review" routine checklist
