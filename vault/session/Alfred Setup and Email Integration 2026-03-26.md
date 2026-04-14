---
type: session
status: completed
name: Alfred Setup and Email Integration
description: First session — setup, email pipeline, multi-instance design, Aftermath-Lab creation
intent: Get Alfred running, build email pipeline, design multi-instance ecosystem
project:
- "[[project/Alfred]]"
participants:
- "[[person/Andrew Newton]]"
outputs:
- "[[org/Anthropic]]"
- "[[asset/Claude Code]]"
- "[[note/Claude Code VS Code Setup]]"
- "[[project/Alfred]]"
related: []
relationships: []
created: "2026-03-26"
tags:
- setup
- email
- design
- aftermath-lab
---

# Alfred Setup and Email Integration — 2026-03-26 to 2026-03-31

## Intent

First session with Alfred after forking the open-source repo. Focused on setup, email integration, and extensive design work on the multi-instance Alfred ecosystem and shared development knowledge base.

## Work Completed

### Alfred Setup
- Installed Alfred (`pip install -e .`)
- Ran quickstart, verified config.yaml and .env
- Started all daemons — curator, janitor, distiller running successfully
- Curator processed test files (screenshot, test email)
- Janitor completed 20 sweeps, distiller created 4 learning records

### Email Integration
- Built `src/alfred/mail/` module (fetcher, webhook, config, state)
- Added `alfred mail webhook` command — receives POSTed email data, writes .md to vault inbox
- Added `alfred mail fetch` command — IMAP fetcher (blocked by Microsoft basic auth deprecation)
- Pivoted to n8n + webhook approach: n8n handles OAuth, POSTs to Alfred webhook
- Cloudflare tunnel set up: `webhook.ruralroutetransportation.ca` → localhost:5005
- n8n workflow built by Aftermath-Lab team, imported and connected
- Microsoft Outlook OAuth2 credential configured via Azure App Registration
- Full pipeline tested and working: email → Outlook → n8n → webhook → tunnel → Alfred inbox → curator

### Infrastructure
- Installed cloudflared (tunnel to expose local webhook)
- Installed GitHub CLI (gh)
- Cloudflare tunnel: `alfred-webhook` (ID: 5e44e541-b24c-4caa-8246-105559dd8744)
- Domain: ruralroutetransportation.ca (tunnel), strugglebus.ca (future use)

### Aftermath-Lab (Coding Alfred)
- Created shared development knowledge base at `/home/andrew/aftermath-lab/`
- GitHub: github.com/newtonium-errant/aftermath-lab (private)
- Extracted knowledge from RRTS (30KB CLAUDE.md + all docs) and RxFax
- 25+ knowledge files covering: n8n patterns/anti-patterns/gotchas, frontend patterns, supabase patterns, JWT auth with pgcrypto
- RxFax patterns supersede RRTS where applicable (JWT node > require('crypto'), pgcrypto RPC > simpleHash)
- Agent instruction files: frontend, n8n-backend, supabase-db, qa-ux
- Connected to RRTS and RxFax — both CLAUDE.md files updated

## Outcome

### Design Decisions
- **Multi-Instance Architecture** — 5 instances (Ops/Business/Knowledge/Medical/Coding), hub-and-spoke topology
- **Morning Brief** — RCAF squadron-style, weather first, other sections after RRTS integration
- **Knowledge Alfred** — zettelkasten instance, MOC phased placement, teaching model
- **Medical Alfred** — voice-to-prescription, PIPEDA/PHIA compliance
- **Cross-Instance Patterns** — living principles, intent chain, agent command vocabulary, voice interface

### Gotchas
- Microsoft live.ca blocked basic IMAP auth — personal accounts require OAuth2
- Azure Portal app registration doesn't work with personal Microsoft accounts directly
- OAuth2 app set to "Personal accounts only" conflicts with n8n's `/common/` endpoint
- GPG key for GitHub CLI apt repo needed `--dearmor` flag

### Azure Credential
- Microsoft Outlook OAuth2 app registered for n8n email trigger
- Client secret expires ~2028-03-30 (24 months)
