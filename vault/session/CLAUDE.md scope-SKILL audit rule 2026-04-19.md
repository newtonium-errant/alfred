---
alfred_tags:
- software/alfred
- team-rules
created: '2026-04-19'
description: Add a team-lead rule to CLAUDE.md requiring a SKILL audit when
  a scope or schema narrowing commit lands. Surfaced by Q3 (body-write
  denial) leaving a dead STUB001 instruction in the janitor SKILL for
  ~24h until Q2's SKILL update caught it.
intent: Prevent scope/prompt contract drift between tightenings of the
  enforcement layer and the instructions fed to LLM agents
name: CLAUDE.md scope-SKILL audit rule
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Option E Q3 — body-write loophole 2026-04-19]]'
- '[[session/Option E Q2 — merge scope 2026-04-19]]'
- '[[session/Janitor SKILL — DUP001 merge deterministic path 2026-04-19]]'
status: completed
tags:
- team-rules
- scope
- prompt-tuner
- learning
type: session
---

# CLAUDE.md scope-SKILL audit rule — 2026-04-19

## Intent

Q3 shipped yesterday and denied body writes on the janitor scope. The SKILL's STUB001 fix instruction — "flesh out the body with a heading and brief description" — became dead code from that commit onward. The LLM would read that instruction, attempt `--body-append`, hit ScopeError, and the user would see a silent failure on the fix path. The problem was caught today only because Q2's SKILL pass required the prompt-tuner to open the same file and grep for body writes.

The team-lead rule needed: when the builder narrows a scope or schema, the prompt-tuner pass lands in the same cycle. Scope + prompt are two halves of one contract; shipping one without the other creates a silent divergence.

## What shipped

One line added to CLAUDE.md's Team Lead Rules section, alongside the existing Cross-agent contracts rule:

> **Scope/schema-narrowing commits trigger a SKILL audit in the same cycle.** When the builder tightens a vault scope (field allowlist, new denied op, stricter type filter) or narrows a record schema, the agent-facing instructions in the affected SKILL(s) may contain dead or now-forbidden steps. Bundle a prompt-tuner pass with the scope change OR schedule it immediately after, before the SKILL silently drifts out of sync. Ship-same-day is the goal.

## Verification

Rule lives at CLAUDE.md line 159 (immediately after Cross-agent contracts). Will apply from next session onward — any team-lead reading the rules before spawning work will see it.

## Alfred Learnings

**The two-sided contract pattern is worth recognizing.** Scope lock + SKILL instruction = one contract. Config lock + prompt example = one contract. Tool schema + SKILL usage = one contract. Tightening one side without reviewing the other is how silent drift starts. Generalizes beyond scope/SKILL.

**Audit cadence matters more than audit depth.** A fast shallow audit (grep the SKILL for tokens matching the denied op) at the moment of change beats a deep audit a week later. Caught-same-day is repairable; caught-six-commits-later is archaeological.
