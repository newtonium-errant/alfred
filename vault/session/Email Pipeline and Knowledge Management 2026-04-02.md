---
type: session
status: completed
name: Email Pipeline and Knowledge Management
description: Extended email pipeline to full triage/filing, added KAIROS-inspired features, established team conventions
intent: Build email triage and filing, strengthen knowledge management, document team conventions
project:
- "[[project/Alfred]]"
participants:
- "[[person/Andrew Newton]]"
outputs: []
related:
- "[[session/Alfred Setup and Email Integration 2026-03-26]]"
relationships: []
created: "2026-04-02"
tags:
- email
- triage
- kairos
- aftermath-lab
---

# Email Pipeline and Knowledge Management — 2026-04-02

## Intent

Extend Alfred's email pipeline from basic ingestion to full triage, filing, and knowledge management strengthening. Establish team conventions and document implicit standards in Aftermath-Lab.

## Work Completed

### Email Pipeline — HTML Fix
- Fixed empty email body problem: n8n was already sending `body.content` (HTML), but the webhook was writing raw HTML as-is
- Added `_strip_html()` to `src/alfred/mail/webhook.py` — converts HTML to readable plain text

### Email Pipeline — Webhook Auth
- Generated and set `MAIL_WEBHOOK_TOKEN` in `.env`
- Created "Alfred Auth" Header Auth credential in n8n

### Email Triage — Living Document
- Created `vault/process/Email Triage Rules.md` — living document defining priority levels, financial document tags, Outlook folder mappings, sender trust levels
- Added pre-step to curator SKILL.md: reads triage rules before processing any email

### Email Filing — n8n Outlook Integration
- Extended n8n workflow: Categorize (Code) → Route (Switch) → Resolve Folder (HTTP) → Move Email (HTTP) → Mark Read (Outlook native)
- Auto-creates Outlook folders on first use via Graph API
- Pattern-matching triage rules in Code node
- Skipped emails marked as read automatically

### Knowledge Management (KAIROS-inspired)
1. **Proactive Context Injection** (curator) — extracts sender email, finds person record, injects linked context into prompt
2. **Semantic Drift Detection** (janitor) — weekly scan for stale records. CLI: `alfred janitor drift`
3. **Consolidation Sweep** (distiller) — weekly LLM pass to merge duplicates, upgrade assumptions. CLI: `alfred distiller consolidate`

### Team Conventions & Documentation
- Added Coding Team section to Alfred CLAUDE.md
- Added Code node version header/footer convention to aftermath-lab
- Updated all 4 aftermath-lab agent templates with missing rules
- Standardized session notes location to `docs/session-notes/`

## Outcome

### Design Decisions
- **Email triage architecture** — categorization in n8n Code node (instant) not Alfred webhook (async). Two independent paths: n8n for filing, curator for deeper processing.
- **HTTP Request nodes** for Graph API calls, not Code node `httpRequestWithAuthentication` (not available in sandbox)
- **Native Outlook node** for mark-as-read (Graph API PATCH not supported via HTTP Request with predefined credentials)

### KAIROS Comparison
- Alfred's typed knowledge graph is fundamentally stronger than KAIROS's flat-file grep approach
- Three features added to close the gaps: proactive recall, drift detection, consolidation

### Aftermath-Lab Gotchas
- `this.helpers.httpRequestWithAuthentication` NOT available in n8n Code nodes
- n8n Outlook trigger returns `from` as plain string, not object — Code nodes must handle both
- Graph API message update (PATCH) returns 405 via n8n HTTP Request with predefined credentials
