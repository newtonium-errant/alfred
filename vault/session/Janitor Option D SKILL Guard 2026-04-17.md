---
type: session
status: completed
name: "Janitor Option D SKILL Guard"
created: 2026-04-17
description: "Bug 3 of 3 drift-bug batch. Ship Option D interim — SKILL.md idempotency rule telling janitor agent to read target before rewriting a janitor_note and to leave unchanged when the issue-code prefix matches. Larger Option E code refactor tracked for later."
tags: [janitor, drift-bug, prompt, skill, session-note]
---

# Janitor Option D SKILL Guard — 2026-04-17

## Intent

Bug 3 of 3 from the daemon-drift triage. The janitor's LLM agent was re-composing `janitor_note` prose every sweep even when the underlying issue was identical, producing sweep-to-sweep diffs that were just LLM wording variance. Temperature cannot be pinned per-call on `claude -p`, so the only fix available without a larger refactor is an in-prompt rule telling the agent to detect and preserve its own prior work.

Builder analyzed four options for this class of bug. Option D (a SKILL.md idempotency rule) is the low-risk interim. Option E (move the `janitor_note` write into a deterministic code path keyed on issue code, bypassing the agent for the flagging step) is the correct long-term fix but is larger in scope and tracked for later.

## What shipped

- `src/alfred/_bundled/skills/vault-janitor/SKILL.md` — added a new subsection `Writing janitor_note — Idempotency Rule` at the top of Section 3 (Fix Procedures by Issue Code), before the first per-code procedure. Placement was chosen deliberately: every fix procedure below writes a `janitor_note` with an issue-code prefix, so the rule applies uniformly and the reader encounters it before any code-specific guidance.

The rule tells the agent to:

1. Always `alfred vault read` the target before writing `janitor_note`.
2. If the existing `janitor_note` starts with the same issue code that would be written, leave it untouched and log the sweep action as `SKIPPED`.
3. If the existing code differs, replace the note.
4. If no note exists, write normally.

The issue-code prefix convention (`FM002 —`, `LINK001 —`, `DUP001 —`, `ORPHAN001 —`, `STUB001 —`) is already canonical across every example in the SKILL — the rule leans on that existing pattern rather than introducing a new equality contract.

No other files were touched. No daemon was restarted (per instructions).

## Verification

- Re-read the SKILL.md section in context. Rule reads naturally in the SKILL's voice, flows into FM001 without a seam, and preserves the imperative-second-person style used elsewhere.
- Confirmed the issue-code prefix convention is already the SKILL's canonical `janitor_note` format — every `janitor_note` example in the file uses it (FM002, DIR001, LINK001, ORPHAN001, STUB001, DUP001 learn-type variant).
- Behavioral verification requires a live janitor sweep against a vault where a record already carries a `janitor_note` with a matching issue code — deferred to a future sweep, not run now because daemons are intentionally stopped.

## Alfred Learnings

- **LLM prose variance is a first-class source of vault churn.** Even with a deterministic model path, free-form text fields re-written by an agent every sweep will diff on wording alone. Any agent-written frontmatter field that is meant to be stable needs either a machine-verifiable equality gate (issue code prefix, content hash, structured format) or a code-path refactor that moves the write out of the agent. "Temperature=0" is not a cure — it only helps if the input is also identical, and agent prompts rarely guarantee that.
- **When a code fix is too large for the current session, a prompt fix is a legitimate interim.** Option D is explicitly not the correct end state — Option E (deterministic code-path writer keyed on issue code) is. But shipping D stops the bleeding in the vault today at the cost of one SKILL subsection, and the rule text itself doubles as a spec for what E eventually has to implement. Prompt-level guards are a valid rung on the remediation ladder between "noticed the bug" and "refactored the code path."
- **Rule placement matters as much as rule text.** This rule could have lived in Section 1 (Authority & Scope), at the end of Section 6 (Anti-patterns), or bolted into each per-code procedure. Placing it at the top of Section 3 means every procedure below inherits it by reading order — the rule is encountered exactly once, in the section that defines what writing a `janitor_note` actually entails. Prompt engineering lesson: put cross-cutting rules where the reader's attention is already pointed, not where they are logically categorized.
- **The issue-code prefix convention is now a real contract, not just a stylistic convention.** Before this change, `FM002 —` at the start of a `janitor_note` was a human-readable convenience. After this change, it is the equality key the agent uses to decide whether to rewrite. If a future prompt-tuner change drops or reformats the prefix in any procedure, the idempotency guard silently fails. Worth flagging in the prompt-tuner agent's instructions as a trip-wire — the prefix style is now load-bearing across every per-code procedure in Section 3.
