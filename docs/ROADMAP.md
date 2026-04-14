# Alfred Roadmap

Last updated: 2026-04-14

## Done

### Core Platform
- [x] Curator — watches inbox, processes raw inputs into structured vault records
- [x] Janitor — structural scan (12 issue codes), agent-driven fixes, autofix pipeline
- [x] Distiller — extracts learning records (assumption, decision, constraint, contradiction, synthesis)
- [x] Surveyor — embed, cluster, label, write (Milvus Lite + HDBSCAN + Leiden). Fully operational (2026-04-14): Ollama nomic-embed-text for embeddings, qwen2.5:14b for labeling, both running on local Windows GPU. Initial sync: 1419 files embedded, 165 semantic clusters, 538 clusters labeled
- [x] Unified CLI (`alfred`), config (`config.yaml`), daemon management (`alfred up/down/status`)
- [x] Vault operations layer with scope enforcement and mutation logging
- [x] TUI dashboard (`alfred tui`)

### Email Pipeline
- [x] Mail webhook receiver (`alfred mail webhook`) with HTML-to-text stripping
- [x] Cloudflare tunnel (`webhook.ruralroutetransportation.ca` → localhost:5005)
- [x] n8n workflow: Outlook trigger → build body → POST to Alfred webhook
- [x] Webhook auth (Bearer token)
- [x] Email triage living document (`vault/process/Email Triage Rules.md`)
- [x] Curator pre-step: reads triage rules before processing emails
- [x] n8n Outlook filing: categorize → auto-create folders → move email (Business/Invoices, Business/Receipts, Finance/Tax, Finance/Personal)
- [x] Skipped emails marked as read automatically

### Knowledge Management (KAIROS-inspired)
- [x] Proactive context injection — curator extracts sender email, injects linked person/org/project/task context
- [x] Semantic drift detection — weekly scan for stale projects (30d), tasks (90d), conversations (30d), persons (60d). CLI: `alfred janitor drift`
- [x] Consolidation sweep — weekly LLM pass to merge duplicates, upgrade assumptions, resolve contradictions. CLI: `alfred distiller consolidate`. Refactored pipeline from OpenClaw-only to backend-agnostic (2026-04-14); first successful run with Claude API: 31 records modified across 5 learn types

### Morning Brief
- [x] Weather module — METAR + TAF from aviationweather.gov (CYZX/CYHZ/CYAW/CYQI). No API key needed. CLI: `alfred brief`, `alfred brief weather` (refresh), `alfred brief generate --refresh`. Daemon auto-starts with `alfred up` at 0600 ADT daily. Output: `vault/run/Morning Brief {date}.md`
- [ ] Strategic Overview — depends on RRTS integration
- [ ] Personnel, Equipment, Operations, Finance — depends on RRTS integration

### Team & Knowledge Base
- [x] Aftermath-Lab shared knowledge base (25+ docs: n8n, frontend, Supabase, auth, QA)
- [x] Agent instruction files (frontend, n8n-backend, supabase-db, qa-ux)
- [x] Code node version comment convention
- [x] Coding Team section in project CLAUDE.md (spawn rules, deliverable conventions)
- [x] Global Claude Code permissions for aftermath-lab access

## In Progress

### Email Pipeline Refinement
- [ ] Expand triage rules as real emails arrive (new senders, new patterns)
- [ ] Add Outlook categories/tags to filed emails for searchability
- [ ] Gmail account integration (address TBD — webhook + n8n trigger)

### Session Notes Pipeline
- [ ] Currently manual (point aftermath team at session notes file path)
- [ ] Neutral channel not built — design calls for vault-based Phase 1, Supabase Phase 2

## Next Up

### Multi-Instance Architecture
5 Alfred instances in hub-and-spoke topology:

| Instance | Purpose | Status |
|----------|---------|--------|
| **Ops** | Personal/coordinator (hub) | Current instance — running |
| **Business** | RRTS operations | Designed, not built |
| **Knowledge** | Zettelkasten/intellectual work | Designed, not built |
| **Medical** | NP practice | Designed, not built |
| **Coding** | Aftermath-Lab | Designed, not built |

Key design decisions:
- Hub and spoke by default (Ops routes), mesh available for direct dept-to-dept
- Shared central task list, direct messaging with Ops visibility
- Each instance has its own vault, config, and scope

### Knowledge Alfred
Separate instance for zettelkasten/intellectual work:
- Core jobs: MOC support → surface connections → process raw imports
- Librarian/curator/archivist approach — acts then reports, additive and reversible
- MOC phased placement: Phase 1 (unsorted) → Phase 2 (drafted) → Phase 3 (confident)
- Teaching model: Andrew explains reasoning, Alfred accumulates curatorial principles

### Medical Alfred
NP practice AI assistant:
- Voice-to-prescription: voice → AI drafts structured Rx → NP signs on screen → fax
- Dev on WSL with synthetic data, production on separate box
- PIPEDA + NS PHIA compliance required

## Future

### Voice Interface
Stitched pipeline: Whisper/Deepgram → Claude → ElevenLabs. Applies across all instances.

### Neutral Discussion Space
Cross-instance communication channel:
- Phase 1: vault-based (shared folder or wikilinks across vaults)
- Phase 2: Supabase + n8n (real-time, structured)

### Cross-Instance Patterns
- Living principles documents across all instances
- Intent chain & brief-back pattern (SMESC-inspired)
- Agent command vocabulary: "open to suggestions", "check my work", "create a briefing note", "town hall", "red team this"

### Local LLM
- Eliminates API token costs for curator/distiller/consolidation
- **Smoke test passed (2026-04-10):** Ollama on Windows (RTX 5070 Ti, 16GB VRAM) running `qwen2.5:14b` — all 4 tests passed (connectivity, structured JSON, single tool call, multi-turn tool use on real email). Details: `docs/ollama-smoke-test-2026-04-10.md`, test script: `scripts/ollama_smoke_test.py`
- OpenClaw is a cloud model router, not local inference — pivoted to direct Ollama integration
- Full `OllamaBackend` (~200-300 lines) deferred until Mac arrives for larger models
- Current decision: stick with Claude API for production — 14B quality below Claude for nuanced extraction, vault is source of truth
- Tradeoff: cost → zero, quality → depends on model and hardware
