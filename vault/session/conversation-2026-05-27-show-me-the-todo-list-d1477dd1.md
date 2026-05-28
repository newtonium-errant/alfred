---
continues_from: null
created: '2026-05-27'
description: Telegram talker session (14 turns, 0 vault ops, closed via timeout).
intent: Capture a voice/text conversation with Alfred and any vault actions it produced.
name: Conversation — 2026-05-27 show-me-the-todo-list
outputs: []
participants:
- '[[person/Andrew Newton]]'
process: null
project: []
related: []
relationships: []
session_type: note
status: completed
tags:
- voice
- telegram
telegram:
  chat_id: 8661018406
  close_reason: timeout
  ended_at: '2026-05-27T17:36:57.612477+00:00'
  message_count: 14
  model: claude-sonnet-4-6
  pushback_level: 1
  session_id: d1477dd1-c3e6-4a83-87f1-9a7ca4a5e699
  started_at: '2026-05-27T16:59:48.110815+00:00'
  stt_model: whisper-large-v3
  text_messages: 7
  vault_operations: []
  voice_messages: 0
type: session
---

# Transcript

**Andrew** (16:59): Show me the todo list

**Alfred** (16:59): [tool_use: vault_search glob=task/*.md, grep=status: todo]

**Andrew** (16:59): [tool_result: toolu_01…]

**Alfred** (17:00): 43 open tasks. Grouped by domain:

**OFW / Legal**
- Check OFW Message from Jennifer Newton (×2 — May 18 and May 24)

**Health / Medical**
- Complete Tilray Medical Registration Renewal
- Prep Blue Cross Call List for Medical Admin Handoff
- Prepare Training Materials and Finish Loose Ends for Handoff

**Finance / Accounting**
- Complete Personal Taxes — Andrew Newton
- RRTS Corporate Taxes — Awaiting Accountant
- RRTS Finance Page — Validate Coded Work
- RRTS Invoicing
- RRTS Payroll
- Follow Up on QBO Payroll Paperwork
- Follow Up with MBF Portal — Awaiting Callback
- Resend QuickBooks Invite to Kayleigh Stirling
- Transfer Payroll Data Wave to QBO — 4567150 NS Ltd
- Update Payment Method for Floatation Centre Membership

**RRTS / Dev**
- Apply for RRTS Vehicle Financing — Annapolis Ventures then Scotiabank
- Find Annapolis Ventures Email — December 2024
- Connect QBO API — RRTS
- Set Up QuickBooks Online Developer Access for RRTS Website
- RRTS Bug List — Burn Through
- RRTS Schedule Page — Build

**Dedup Triage** (13 tasks — curator cleanup backlog)
- 3335556 NS Ltd invoice (×1), Dental Appointment (×2), Hinge (×3), Invoice 2 (×2), Invoice 3 (×3), Tim Denning (×2), TurboTax (×3)

**Routines / Misc**
- Exercise, Listening to Music, Playing Music, Reading, Writing

Want me to pull details on any of these, or filter by priority?

**Andrew** (17:03): I want to discuss the tiers again. There are items that come up routinely that are t2 when they first appear but become t1 closer to deadlines. 

For example, RRTS invoicing happens weekly on tuesdays as a t2 item. By Friday it’s a t1 item. 

Another example, RRTS payroll is biweekly, and is due this week. Tuesday May 26 it appears as a t2, but today a May 27 it’s t1 because the deadline is eod may 28

**Alfred** (17:03): This is a good structural problem. The tier isn't a property of the task — it's a function of the task plus time-to-deadline.

What you're describing is **deadline-relative tier escalation**: a recurring task has a base tier (t2 when it first appears) and escalates to t1 as the deadline approaches. The escalation threshold is what varies — invoicing flips at ~3 days out, payroll at ~1 day out.

A few questions to nail the design:

**1. Where does this live?**
Options:
- On the task record itself — e.g., `base_priority: medium`, `escalate_to: high`, `escalate_at_hours: 24` (or similar). The brief renderer reads these and applies the escalation at display time.
- In the brief config — a separate escalation rule table keyed by task name or tag.
- As a preference record — a forward-policy rule the brief filter honors.

The task-record approach is most portable (the data travels with the record), but it requires the brief to understand the schema. The preference approach is more flexible but adds indirection.

**2. What's the escalation trigger?**
For invoicing you said "by Friday" — is that calendar-day-of-week anchored (Friday = flip), or is it relative to a deadline field on the task (e.g., `due: tuesday`, flip 3 days before)?

Payroll sounds deadline-relative: due EOD May 28, flip ~24h before.

Are these the same mechanism or two different ones?

**3. Do recurring tasks get a new record each cycle, or does the same record get its due date updated?**
That affects whether the escalation logic reads `due` off the current record or needs to know the recurrence pattern.

What's your instinct on where this should live?

**Andrew** (17:06): 1. I’m not sure where the clause code session finished with this ability. Usage resets tonight so it might have to wait until tomorrow. 
2. Escalation trigger varies per item, and that’s why I want to discuss and see tier, importance, and deadline for each item. Some I can tell you explicitly, some I want you to discern as we go. Payroll and invoicing are ones I can explain. Responding to a specific persons email could vary and I need help finding the nuance there

**Alfred** (17:06): Got it — let's just work through the list. Usage resets tonight, we can pick up the implementation side tomorrow with KAL-LE once you have the full picture mapped out.

Let me pull the current state of the recurring/deadline-sensitive tasks so we're working from real data. [tool_use: vault_read task/RRTS Invoicing.md=
] [tool_use: vault_read path=task/RRTS Payroll.md]

**Andrew** (17:06): [tool_result: toolu_01…] [tool_result: toolu_01…]

**Alfred** (17:06): [tool_use: vault_search glob=task/RRTS Invoicing.md]

**Andrew** (17:06): [tool_result: toolu_01…]

**Alfred** (17:06): [tool_use: vault_read path=task/RRTS Invoicing.md]

**Andrew** (17:06): [tool_result: toolu_01…]

**Alfred** (17:06): OK, so current state:

- **RRTS Invoicing** — `due: 2026-05-27` (today), `priority: urgent` — already at the top, due today
- **RRTS Payroll** — `due: 2026-05-28` (tomorrow EOD), `priority: urgent` — also already urgent

Both are already marked urgent, so the escalation already happened manually at creation. What we're really designing is the *automatic* version of that — so you don't have to set it by hand.

Let's map these two out first as the template cases:

**RRTS Invoicing (weekly, Tuesdays)**
- Appears: Tuesday → t2
- Escalates to t1: when? You said "by Friday" — so Thursday EOD, or Friday morning?
- Deadline: end of day Tuesday the following week? Or same-week Friday?

**RRTS Payroll (biweekly)**
- Appears: ~Tuesday of pay week → t2
- Escalates to t1: ~24h before deadline
- Deadline: EOD Thursday? (you said May 28 = Thursday)

Confirm those and we'll have two clean anchor cases. Then we can move to the email-response nuance — that one's more interesting.

![[related.base#All]]
