---
type: session
date: 2026-04-19
status: complete
tags: [option-e, janitor, skill, dup001, prompt-tuner]
---

# Janitor SKILL — DUP001 Merge Points to merge_entities

## Intent

Q2 (commit `657957a`) moved the DUP001 Operator-Directed Merge from an
LLM-executed multi-step procedure into deterministic Python at
`src/alfred/janitor/merge.py::merge_entities`. The janitor SKILL still
described the old by-hand steps (copy fields, follow-link sweep,
retarget inbound wikilinks, delete loser) as if the agent runs them.
With Q3 (commit `2b8ddbd`) the janitor scope now also denies body
writes via the `allow_body_writes: False` gate, so most of those
by-hand steps would fail at the CLI scope gate even if an agent tried
to execute them.

This SKILL-only update aligns the prompt with the shipped code.

**Before:** §3.DUP001 Operator-Directed Merge was a 6-step procedure
(pick winner → merge records → follow-link sweep → inspect siblings
→ retarget inbound links → verify) plus a worked PocketPills example.

**After:** §3.DUP001 Operator-Directed Merge is a short directive —
"Do NOT run merges yourself. The mechanical retargeting runs in
deterministic Python via `alfred.janitor.merge.merge_entities(...)`.
Your only role in DUP001 is the Default Triage Flow." If the sweep
context contains an explicit operator merge instruction, log a
SKIPPED line and do nothing. Belt + braces retained (scope lock +
prompt directive).

**Why:** An LLM attempting the by-hand procedure under the narrow
Stage 1/2 scope would now fail — the field allowlist rejects most of
the frontmatter rewrites and the body-write gate (Q3) rejects body
edits. The SKILL telling it to try was dead prose at best and a
self-inflicted scope-error trap at worst. Option (c) — keep the
judgment call ("these are dupes, file triage") in the LLM and the
mechanical retargeting in Python — is the philosophy we picked in Q2,
and the prompt should reflect it.

## Work Completed

- **§3.DUP001 Operator-Directed Merge rewritten** (lines 554-560).
  Replaced ~18 lines of numbered procedure + worked example with
  ~5 lines of directive pointing at `merge_entities`. Preserved the
  "Do NOT run autonomously" guardrail (as belt-and-braces — the scope
  lock is the actual enforcement, but the directive prompts the
  LLM's reasoning and produces clearer SKIPPED action-log entries).
- **§3.DUP001 header paragraph tightened** (line 493). Removed the
  phrase "The operator-directed merge procedure below is reserved for
  the escalation path" since the procedure below no longer exists as
  an agent procedure.
- **§3.STUB001 body-fleshing instruction removed** (line 487). The
  old text told the agent to "flesh out the body with a heading and
  brief description" when frontmatter had enough context. After Q3
  body writes fail under the janitor scope; body content is Stage 3
  enrichment territory (under the `janitor_enrich` scope). New fix
  is flag-only with an explicit note that Stage 3 owns body writes.
- **§5 Output Format example updated** (line 599). The sample action
  log had a SKIPPED line for STUB001 saying "Not enough context to
  flesh out body" — obsolete under the new flag-only rule. Replaced
  with a FLAGGED line.
- **Default Triage Flow (§3.DUP001) preserved unchanged.** The
  triage-ID computation, idempotency check, `alfred vault create
  task` example, "MUST NOT while pending" list, and worked Acme
  example all stayed — that's still the LLM's judgment call and
  still how the autonomous sweep path emits DUP001 work.

LoC delta: roughly -20 lines net (operator-merge section shrank from
~18 lines to ~5; STUB001 fix grew by ~1 line; output format example
stayed the same length).

## Verification

Diff-review only — SKILL changes affect the next janitor LLM agent
invocation at prompt assembly time. No runtime verification possible
from this seat:

- No daemon restart needed (SKILL is loaded per-invocation by the
  backend, not cached at startup).
- No test suite covers SKILL.md prose directly.
- Confirmed by reading that the edited section reads cleanly —
  §3.DUP001 now has a coherent flow: diagnosis → default triage
  (full procedure) → operator-directed merge (short deterministic-
  path directive). No dangling references to deleted steps.
- Confirmed the anti-pattern "Never merge duplicate records"
  (§4.2) still applies — the agent never merges; the Python helper
  is not an agent operation.

Next janitor sweep will pick up the new SKILL. The behavioural change
is "agent no longer attempts merge retargeting"; the measurable
outcome is absence of ScopeError rows in the agent transcript when a
sweep happens to contain a dupe candidate the operator has flagged.

## Alfred Learnings

- **Scope-lock landings trigger SKILL audits.** When a scope is
  narrowed (Q3's body-write gate, or the field allowlist before it),
  every SKILL instruction that assumes the old broader scope becomes
  dead prose — the CLI will reject the action. Next time we narrow
  a scope, grep the affected tool's SKILL for the now-forbidden
  operations (`body_append`, body edits, fields outside the new
  allowlist) and clean them up in the same commit cycle. The Q3
  commit caught the merge procedure via Q2's follow-up note, but
  also orphaned the STUB001 body-fleshing instruction silently.
- **Belt + braces prompt directives have real value even when the
  scope lock is the actual enforcement.** "Do NOT run autonomously"
  is redundant from an enforcement standpoint (the scope denies it),
  but it cleans up LLM reasoning — the agent doesn't spend tokens
  trying and failing and retrying. Keep the prose directive when
  deleting it would save < ~5 lines; the reasoning-cleanup is worth
  more than the prompt real estate.
- **Prompt-tuner follow-up is a normal artifact of Option E
  landings.** Code changes that tighten scope almost always leave
  SKILL instructions stranded. Make the follow-up note explicit in
  the code commit (Q2 did this) so the next prompt-tuner pass has
  a clean handoff, rather than discovering the drift cold.
