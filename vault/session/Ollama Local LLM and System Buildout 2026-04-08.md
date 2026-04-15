---
alfred_tags:
- software/alfred
- email/integration
- system/buildout
created: '2026-04-08'
description: Local LLM setup, consolidation unblock, surveyor activation, mail webhook
  wiring, Morning Brief weather module
distiller_signals: contradiction:1, has_outcome
intent: Test local LLM, unblock deferred features, build Morning Brief
name: Ollama Local LLM and System Buildout
outputs:
- '[[run/Morning Brief 2026-04-14]]'
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Email Pipeline and Knowledge Management 2026-04-02]]'
relationships:
- confidence: 0.7
  context: LLM system needs security.
  source: session/Ollama Local LLM and System Buildout 2026-04-08.md
  target: session/System Hardening and Agent Team 2026-04-14.md
  type: supports
status: completed
tags:
- ollama
- local-llm
- surveyor
- consolidation
- morning-brief
- infrastructure
type: session
---

# Ollama Local LLM and System Buildout — 2026-04-08 to 2026-04-14

## Intent

Test local LLM, unblock deferred features, build Morning Brief weather module. Spans multiple sub-sessions across a week.

## Operational Period (Apr 2–8, unrecorded)

The email pipeline ran autonomously for 6 days with no code changes:
- **779 vault records** total (365 notes, 28 orgs, 26 syntheses, 11 constraints, 11 assumptions, 10 accounts, 9 events, 7 persons, 7 decisions, 5 locations, 5 tasks)
- **283 processed emails** in `inbox/processed/`
- **2,765 audit log entries**
- All three daemons (curator, janitor, distiller) running continuously since Apr 2
- Janitor flagged missing `person/Andrew Newton` record (LINK001)

## Work Completed

### Local LLM Setup — Ollama (Apr 8–10)
- Pivoted from OpenClaw (cloud model router, not local inference) to direct Ollama
- Installed Ollama desktop on Windows (not WSL2 — sudo password forgotten, Windows-native gives better GPU access)
- Pulled `qwen2.5:14b` (Q4_K_M, 9GB) — fits RTX 5070 Ti 16GB VRAM
- Exposed to network (`OLLAMA_HOST=0.0.0.0`), Windows firewall rule for port 11434
- Reachable from WSL2 at `http://172.22.0.1:11434`
- **Decision:** Stick with Claude API for production. Local LLM deferred to Mac. Reasoning: infrastructure swap not new capability, 14B quality below Claude, vault is source of truth

### Ollama Smoke Test (Apr 10)
All 4 tests passed — connectivity (72s cold start), structured JSON (26s), single tool call (11s), multi-turn tool use on real email (73s). Script at `scripts/ollama_smoke_test.py`.

### Consolidation Sweep Unblocked (Apr 14)
- Refactored `pipeline.py` `_call_llm` from OpenClaw-only to backend-agnostic (Claude + OpenClaw)
- Changed `daemon.py` `_use_pipeline()` to route Claude through multi-stage pipeline
- First run: 31 records modified across 5 learn types in ~13 minutes

### Surveyor Fully Operational (Apr 14)
- Added config with Ollama for both embeddings (nomic-embed-text) and labeling (qwen2.5:14b via OpenAI-compatible endpoint)
- Fixed: relative import bug, setuptools/pkg_resources, pymilvus version mismatch, Milvus URI, Ollama model swap crash
- Initial sync: 1419 files embedded (~6 min), 165 semantic clusters, 538 clusters labeled (~1 hour)
- Tags written to vault: spam/unsolicited, music/releases, finance/invoicing, food/recipes, etc.

### Mail Webhook Wired into Orchestrator (Apr 14)
- `alfred up` now auto-starts mail webhook. No separate terminal needed.

### Morning Brief — Weather Module (Apr 14)
- New tool: `src/alfred/brief/` — 8 files, ~900 lines
- METAR (current conditions) + TAF (forecasts) from aviationweather.gov — free, no auth
- Stations: CYZX Greenwood (primary), CYHZ Halifax, CYAW Shearwater, CYQI Yarmouth
- `alfred brief` (generate), `alfred brief weather` (refresh weather in-place), `alfred brief generate --refresh` (full regen)
- Daemon at 0600 ADT daily, auto-starts with `alfred up`

### Session Notes Moved to Vault (Apr 14)
- Moved from `docs/session-notes/` (outside vault, invisible to Alfred) into `vault/session/` as proper session records
- Now picked up by distiller, surveyor, and janitor automatically

## Outcome

### Design Decisions
- **Ollama over OpenClaw** — OpenClaw is cloud router not local inference. Ollama gives true on-device execution.
- **Claude API for production** — local LLM deferred to Mac. 14B quality below Claude for nuanced extraction.
- **Backend-agnostic pipeline** — `_call_llm` dispatches to Claude or OpenClaw. Prepares for future OllamaBackend.
- **Surveyor on local Ollama** — zero API cost for embeddings + labeling. Uses nomic-embed-text + qwen2.5:14b.
- **Morning Brief as section assembler** — renderer accepts pluggable sections. Weather first, operations/personnel/etc later.
- **Weather refresh command** — `alfred brief weather` updates weather in-place for storm tracking without regenerating full brief.
- **Session notes belong in vault** — design decisions and trade-off reasoning are high-value distiller input.

### System State After This Session
- `alfred up` manages 6 tools: curator, janitor, distiller, surveyor, mail, brief
- Vault: ~1450 records, all being embedded/clustered/labeled by surveyor
- Email pipeline: autonomous, ~50 emails/day processed
- Consolidation sweep: working with Claude API, runs weekly
- Morning Brief: generates daily at 0600 ADT with NS weather
- Local Ollama: running on Windows for surveyor, available for future use