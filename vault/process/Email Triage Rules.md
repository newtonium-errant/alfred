---
area: email
created: '2026-04-02'
depends_on: []
description: Living rules for how Alfred prioritizes, categorizes, and files incoming
  email
frequency: as-needed
governed_by: []
janitor_note: LINK001 — [[person/Andrew Newton]] does not exist. No person record
  found for the vault owner. Create person/Andrew Newton.md to resolve.
name: Email Triage Rules
owner: '[[person/Andrew Newton]]'
related:
- '[[project/Alfred]]'
related_orgs:
- org/Daily Stoic.md
- org/AIR MILES.md
- org/Intuit Canada.md
- org/Capital One.md
- org/Patreon Ireland Limited.md
related_persons:
- person/Magdalena Ponurska.md
- person/P Chudnovsky.md
- person/Tim Denning.md
- person/Jamie Sweetland.md
- person/Mark Johnston.md
related_projects:
- project/Alfred.md
relationships: []
status: active
tags:
- email
- triage
- living-document
type: process
---

# Email Triage Rules

This is a living document. Alfred reads these rules when processing every incoming email. Edit this file in Obsidian to refine how email is handled — changes take effect on the next email processed.

## Priority Levels

### Actionable
Requires a response, payment, decision, or follow-up. Creates a task record.

**Patterns:**
- Invoices and bills (DigitalOcean, hosting, SaaS subscriptions)
- Receipts for business purchases (Patreon, domain renewals, software licenses)
- Delivery confirmations and tracking updates for expected packages
- School communications (newsletters, permission forms, report cards)
- Replies from real people expecting a response
- Appointment confirmations or schedule changes
- Account security alerts (password resets, 2FA, login notifications)
- Government or regulatory correspondence

### Important
Worth reading and filing, but no immediate action needed. Creates a note record.

**Patterns:**
- Business communications from known contacts
- Infrastructure alerts (DigitalOcean maintenance windows, service status)
- Financial statements and account summaries
- Subscription renewal notices (upcoming, not yet due)
- Community or professional newsletters the user actually reads
- Shipping notifications (not yet delivered)

### Low
Skim or skip. Creates a minimal note record only.

**Patterns:**
- Marketing emails from subscribed services
- Product announcements and feature updates
- Promotional offers and sales
- Content recommendations ("you might like...")
- Survey requests
- App update notifications

### Ignore
Do not create vault records. Move inbox file to processed without creating notes.

**Patterns:**
- Obvious spam and phishing ("you've won", "claim your prize", "account blocked")
- Gambling and cannabis promotions
- VPN/security product spam
- Emails with empty bodies and promotional subjects
- Duplicate delivery notifications (same tracking number)

## Financial Document Rules

Any email matching these patterns gets tagged `finance` and the appropriate sub-tag. These must be searchable for accountant purposes.

### Business Expenses (RRTS)
- **Tags:** `finance`, `business-expense`, `rrts`
- **Outlook folder:** `Business/Invoices`
- DigitalOcean invoices
- Railway.app billing
- Domain renewals (Cloudflare, registrars)
- SaaS subscriptions used for business (n8n, Supabase, etc.)

### Business Receipts
- **Tags:** `finance`, `business-receipt`, `rrts`
- **Outlook folder:** `Business/Receipts`
- Payment confirmations for business services
- Software license purchases

### Personal Finance
- **Tags:** `finance`, `personal`
- **Outlook folder:** `Finance/Personal`
- Personal subscriptions (Patreon, streaming, gaming)
- Personal purchase receipts (Costco, Amazon, food delivery)
- Bank/credit card notifications

### Tax-Relevant
- **Tags:** `finance`, `tax`
- **Outlook folder:** `Finance/Tax`
- T4s, tax slips, CRA correspondence
- Charitable donation receipts
- RRSP/investment statements

## Sender Trust Levels

### Known Contacts
Real people Andrew communicates with. Always priority: actionable or important.
- Check against existing person records in the vault
- If sender matches a known person, link the email to their record

### Known Services
Automated emails from services Andrew uses. Priority depends on content type (invoice vs marketing).
- DigitalOcean, Railway, Cloudflare, Supabase
- Patreon, Apple, Microsoft
- Canada Post, FedEx, Purolator
- Pizza Hut, truLOCAL, Costco

### Unknown Senders
First-time senders not in the vault. Default to low priority unless content signals otherwise.

## How to Update These Rules

Edit this file directly in Obsidian. Examples of updates:
- "Add sender X to Known Services" — add them to the list above
- "Emails from school should be actionable" — already listed, adjust if needed
- "Stop filing marketing from X" — add to Ignore patterns
- "New Outlook folder for medical receipts" — add a new section under Financial Document Rules

**Important:** The Outlook folder rules are mirrored in the n8n workflow's "Restore Context & Categorize" Code node. When you change folder rules here, also update the n8n Code node to match. See `docs/n8n-email-filing-instructions.md` for details.


## Sender-Specific Overrides

### Substack
Generic Substack notification emails (article published, new post, digest) → **Ignore**. Andrew reads Substack on the app; email duplicates are inbox clutter. Do NOT create vault records for these.

**Exceptions (whitelist — treat as Low or higher based on content):**
- Tim Denning — keep, file as Low

When a new Substack sender appears: default Ignore unless Andrew explicitly adds them to the whitelist above.
