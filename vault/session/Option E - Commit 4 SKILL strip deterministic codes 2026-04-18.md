---
type: session
date: 2026-04-18
status: complete
tags: [option-e, janitor, skill, prompt]
---

# Option E - Commit 4: SKILL Strip Deterministic Codes

## Scope

Fourth of the six-commit Option E sequence. Plan Part 4: remove fix
procedures from the janitor SKILL for codes the structural scanner
now handles deterministically in Python. Keep the headings for
discoverability — an agent that sees the code unexpectedly can
search the SKILL and find the "handled by autofix.py" breadcrumb.

## What Changed

`src/alfred/_bundled/skills/vault-janitor/SKILL.md`:

- **FM001 MISSING_REQUIRED_FIELD** — body replaced with the one-line
  "handled by autofix.py" notice.
- **FM002 INVALID_TYPE_VALUE** — same.
- **FM003 INVALID_STATUS_VALUE** — same.
- **FM004 INVALID_FIELD_TYPE** — same.
- **DIR001 WRONG_DIRECTORY** — same.
- **ORPHAN001 ORPHANED_RECORD** — same.
- **LINK001 BROKEN_WIKILINK** — retained the "fix unambiguously if
  possible" instruction (that's where the LLM still adds value), but
  stripped the "if ambiguous, flag with janitor_note" clause. Added
  a note that unresolved LINK001s are deterministically flagged by
  the scanner, so the agent only acts on clear wins.
- **DUP001** learn-type branch — stripped the in-SKILL "add
  `janitor_note: 'DUP001 — learn-type duplicate, not a triage
  candidate, ignored'`" instruction. Replaced with a breadcrumb
  pointing to autofix.py. Entity-type triage-task creation procedure
  preserved intact (that's the LLM's job).
- **SEM001-SEM004** — body replaced with the one-line notice.

Preserved intact:

- Idempotency rule (issue-code-prefix match) — still governs all
  remaining LLM-authored janitor_notes.
- STUB001 body-enrichment procedure — genuine judgment call.
- DUP001 entity-type triage flow — genuine judgment call (CLI to
  compute triage ID, scan existing-tasks block, create scoped task).
- DUP001 operator-directed merge escalation — human-driven only.
- SEM005-SEM006 — agent-detected, not currently wired but kept as
  scaffolding.

## Why This Matters

Cuts the prompt by roughly 40 lines of now-dead instructions. An LLM
reading the SKILL no longer spends attention on "what to write in a
janitor_note for FM003" — Python has already written it by the time
the agent gets invoked. Keeping the headings means the agent can
still look up what a code means if it appears unexpectedly, which is
useful during the transition period and for future debugging.

## Smoke Test

- Visual read-through of §3 to confirm the remaining procedures
  (LINK001 partial, STUB001, DUP001 entity triage, SEM005-006) still
  read as complete instructions without the stripped bodies.
- The idempotency rule still applies to STUB001, entity DUP001
  triage-task content, and SEM005-006 notes — all cases where the
  LLM writes prose. Rule wording unchanged.
- No code changes. This commit is prompt-only; no pipeline behavior
  change until the daemons pick up the next sweep.

## Alfred Learnings

- **Strip bodies, keep headings.** The pattern from plan Part 6 Q5
  recommendation — headings are cheap discoverability, bodies are
  expensive prompt surface. Apply to any future code that moves
  from LLM to deterministic.
- **Prompt-tuner cross-agent contract:** the structural scanner and
  the SKILL were already in contract-partnership (scanner emits
  codes, SKILL tells LLM how to fix them). This commit explicitly
  encodes "code → handled by autofix.py" as a contract term. If a
  future prompt change wants to re-add logic for any of these codes,
  it needs to walk backwards into autofix.py first.

## Commit

- Code: (this commit)
- Session note: (this file)
