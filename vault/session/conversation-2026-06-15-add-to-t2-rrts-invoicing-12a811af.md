---
continues_from: null
created: '2026-06-15'
description: Telegram talker session (20 turns, 1 vault ops, closed via explicit).
intent: Capture a voice/text conversation with Alfred and any vault actions it produced.
name: Conversation — 2026-06-15 add-to-t2-rrts-invoicing
outputs:
- '[[daily/2026-06-15.md]]'
participants:
- '[[person/Andrew Newton]]'
process: null
project: []
related: []
relationships: []
session_type: task
status: completed
tags:
- voice
- telegram
telegram:
  chat_id: 8661018406
  close_reason: explicit
  ended_at: '2026-06-15T17:19:02.920954+00:00'
  message_count: 20
  model: claude-sonnet-4-6
  pushback_level: 0
  session_id: 12a811af-0887-4b84-874c-448dc68fae57
  started_at: '2026-06-15T17:04:08.726569+00:00'
  stt_model: whisper-large-v3
  text_messages: 10
  vault_operations:
  - op: edit
    path: daily/2026-06-15.md
    ts: '2026-06-15T17:18:27.207776+00:00'
  voice_messages: 0
type: session
---

# Transcript

**Andrew** (17:04): Add to t2: RRTS invoicing, call Aurora cannabis, call roomba, call Tilray about vape coverage,

**Alfred** (17:04): [tool_use: vault_read path=daily/2026-06-15.md]

**Tool** (17:04): [tool_result: toolu_01…]

**Alfred** (17:04): Now let me resolve the task wikilinks: [tool_use: vault_search glob=task/RRTS Invoicing.md] [tool_use: vault_search glob=task/Call Aurora Cannabis.md] [tool_use: vault_search glob=task/Call Roomba.md] [tool_use: vault_search glob=task/Call Tilray About Vape Coverage.md]

**Tool** (17:04): [tool_result: toolu_01…] [tool_result: toolu_01…] [tool_result: toolu_01…] [tool_result: toolu_01…]

**Alfred** (17:04): RRTS Invoicing is cancelled, and Call Aurora Cannabis doesn't exist. Let me search more broadly for both: [tool_use: vault_search grep=RRTS Invoicing] [tool_use: vault_search grep=Aurora Cannabis]

**Tool** (17:04): [tool_result: toolu_01…] [tool_result: toolu_01…]

**Alfred** (17:04): Two issues to flag:

1. **RRTS Invoicing** — the task record is `cancelled`. Should I add it to T2 anyway, or is there a current invoicing task I should look for instead?
2. **Call Aurora Cannabis** — the task is `Call Aurora Cannabis About Coverage` (`task/Call Aurora Cannabis About Coverage.md`). I'll use that.

The other two are confirmed: `Call Roomba` and `Call Tilray About Vape Coverage`.

Can you clarify on RRTS Invoicing — add the cancelled record, or skip it?

**Andrew** (17:09): RRTS invoicing has been a recurring problem. It’s supposed to show up every week as a recurring task. Every Tuesday it surfaces as a t2 item, and by Wednesday (or Thursday, I can’t remember exactly what we set) it becomes t1. I do it every week and mark it complete. There have been issues recently with it showing up even after I mark it complete

**Alfred** (17:09): Let me check the routine record to see what's actually configured. [tool_use: vault_read path=routine/Recurring Bills + Admin.md]

**Tool** (17:09): [tool_result: toolu_01…]

**Alfred** (17:10): Found the issue. Here's what's configured:

**RRTS Invoicing** in the routine:
- `due_pattern: weekly, day: tue` — due every Tuesday
- `surface_at_days: 1` — T2 surfaces Monday
- `escalate_at_days: 0` — T1 fires ON Tuesday (the due date itself)

So it's surfacing as T2 on Monday, escalating to T1 on Tuesday — **not Wednesday/Thursday** as you expected. And critically: **there's no `completion_log` entry for RRTS Invoicing** — the routine has never had a completion logged for it. That's why it keeps re-surfacing even after you mark it done. You've been completing the old cancelled task record, not logging it through the routine system.

Two things to fix:
1. **Escalation timing** — do you want T2 on Tuesday, T1 on Wednesday? That would be `escalate_at_days: 1`. Or Tuesday T2, Thursday T1 (`escalate_at_days: -1` doesn't exist, so that'd need a different approach). What's the actual cadence you want?
2. **Going forward** — when you complete invoicing each week, say "RRTS Invoicing done" and I'll log it through `routine_done`, which will stop the re-surfacing problem.

What escalation window do you want?

**Andrew** (17:17): 1. T2 Tuesday, continue showing as t2 Wednesday, but upgrade to t1 on Thursday if it has not been marked complete.

**Alfred** (17:17): So: `surface_at_days: 2` (T2 from Tuesday, 2 days before Thursday due), `escalate_at_days: 0` (T1 on Thursday), and change `due_pattern` to `day: thu`.

Let me update that: [tool_use: routine_item action=edit, record=Recurring Bills + Admin, item=RRTS Invoicing, fields={'due_pattern': {'type': 'weekly', 'day': 'thu'}, 'surfac...]

**Tool** (17:17): [tool_result: toolu_01…]

**Alfred** (17:17): Done. New cadence:
- **Tuesday** — T2 surfaces (2 days before Thursday due)
- **Wednesday** — stays T2
- **Thursday** — escalates to T1 if not marked complete

And when you do invoicing each week, just say "RRTS Invoicing done" so it logs to the routine and stops re-surfacing. Now — back to the original T2 adds. Want me to add the other three (Call Aurora Cannabis About Coverage, Call Roomba, Call Tilray About Vape Coverage) plus RRTS Invoicing manually for today?

**Andrew** (17:18): Yes, except the RRTS invoice. I want to see if that shows up tomorrow properly

**Alfred** (17:18): [tool_use: vault_edit path=daily/2026-06-15.md, set_fields={'tier_curation': {'curated_at': '2026-06-15T14:30:00-03:...]

**Tool** (17:18): [tool_result: toolu_01…]

**Alfred** (17:18): Added to today's T2:
- Call Aurora Cannabis About Coverage
- Call Roomba
- Call Tilray About Vape Coverage

RRTS Invoicing left out — should surface Tuesday as T2 if the routine fix took.

![[related.base#All]]
