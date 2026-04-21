---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 3 — Instructor watcher

**Problem shape.** Users want to drop natural-language directives onto records without opening a chat session. "Set tags to [instructor, smoke-test] and mark this active." "Archive the last three run records." The vault already has `alfred_instructions` as a frontmatter field (your design, kept). What was missing was the daemon that actually executes pending directives, plus the destructive-ops safety gate, plus the audit trail.

**Solution shape.** A new `instructor` tool alongside curator/janitor/distiller/surveyor. It polls the vault every 60s, hash-gating on full file bytes so unchanged records don't get rescanned. When it finds a pending directive, it dispatches an in-process Anthropic SDK tool-use loop with the `instructor` scope.

The contract on the record frontmatter:

```yaml
alfred_instructions:
  - "Set tags to ['instructor', 'smoke-test'] and set status to 'active'"
alfred_instructions_last:
  - text: "Set tags to…"
    executed_at: "2026-04-20T17:42:10Z"
    result: "tags → […], status → active"
alfred_instructions_error: null   # set only after max_retries
```

Executed directives move from `alfred_instructions` into `alfred_instructions_last` (rolling window of 5). An audit comment gets appended to the record body: `<!-- ALFRED:INSTRUCTION 2026-04-20T17:42:10Z "Set tags…" → tags →[…], status → active -->`. Rolling-5 window pruning is done against a regex on that comment format; the regex lives next to the writer so it can't silently drift.

**Destructive-keyword gate.** Before the tool-use loop starts, the directive text is scanned for `delete|remove|drop|purge|wipe|clear all`. If matched, the executor runs in `dry_run=True` mode with read-only tool access. The archive entry documents the refusal reason. Live-validated on 2026-04-20: `"Delete this record entirely"` returned dry-run only, no mutation, archive entry wrote the refusal.

**Why in-process SDK, not `claude -p`.** Three reasons that all came up at the same time:

1. The tool-use loop needs streaming and careful turn-by-turn dispatch.
2. The API key path varies per instance, and subprocess env inheritance had been biting us (we'd already landed a separate fix — `103a2ca` — to stop `ANTHROPIC_API_KEY` leaking into `claude -p` subprocesses). In-process skips the class entirely.
3. Startup cost per turn. The instructor fires often enough during smoke testing that subprocess setup was a noticeable fraction of runtime.

Curator still uses the subprocess agent-backend pattern. It's a one-shot call with a big prompt and no tool-use loop; the pattern fits it well.

**Tradeoffs / what we rejected.**

- **Executing all `alfred_instructions` at once per record.** Rejected — processed one at a time, with the executed directive moving to archive before the next starts. Clean audit trail and failure isolation.
- **Letting the tool-use loop write any field.** Deliberately ran under a new `instructor` scope with `delete` denied globally (even without the dry-run gate). The destructive-keyword gate sits inside the scope; two layers.
- **Cross-record operations in a single directive.** v1 is one directive per record. "Archive the last three run records" has to be three directives or an operator script. Filed as a future extension once real usage patterns shape it.

**Commit range.** `6f66649..316f6b9` (6 commits): scope + schema → config + state → watcher + `detect_pending()` → executor + daemon wiring → SKILL bundle with `{{instance_name}}`/`{{instance_canonical}}` templating → orchestrator + CLI + BIT probe.

The templating piece matters for multi-instance: the instructor SKILL.md has placeholders so KAL-LE's instructor and Salem's instructor identify themselves correctly in their tool-use reasoning.

Would love to hear how this echoes (or doesn't) in your thinking — particularly the in-process SDK choice vs your subprocess agent backend.
