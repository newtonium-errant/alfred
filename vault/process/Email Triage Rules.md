---
type: process
status: active
name: Email Triage Rules
description: Living rules for how Alfred prioritizes, categorizes, and files incoming email
owner: "[[person/Andrew Newton]]"
frequency: as-needed
area: email
depends_on: []
governed_by: []
related:
- "[[project/Alfred]]"
relationships: []
created: "2026-04-02"
tags: [email, triage, living-document]
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
