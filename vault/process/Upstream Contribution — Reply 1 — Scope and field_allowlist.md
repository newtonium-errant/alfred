---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 1 — Scope system and the `field_allowlist` mechanism

**Problem shape.** The janitor tool has a legitimately broad mandate — it touches structural frontmatter across the vault during deep sweeps. But during a drift investigation we caught it rewriting `alfred_tags`, which surveyor owns. The agent hadn't been told it couldn't; the scope just said `"edit": True`, and the agent interpreted that latitude broadly. Scope creep that looked like a real diff but was an LLM deciding your fields were improvable.

**Solution shape.** We generalized the scope system to support per-field allowlists on any operation. `vault/scope.py` already had per-operation bools, plus a couple of special rules (`inbox_only`, `learn_types_only`). We added a `field_allowlist` permission value: when `check_scope` sees it, it looks up `{operation}_fields_allowlist` on the scope rules and requires every field the caller intends to write to be in the set.

```python
"janitor": {
    ...
    "edit": "field_allowlist",
    "edit_fields_allowlist": {
        "janitor_note",
        "type", "status",              # FM002/FM003 autofix
        "name", "subject",             # FM001 title
        "created",                     # FM001 mtime
        "related",                     # LINK002 autofix, DUP001 retargeting
        "tags",                        # FM004 scalar→list coercion
        "alfred_triage", "alfred_triage_kind", "alfred_triage_id",
        "candidates", "priority",
    },
    ...
}
```

The `alfred vault edit` CLI computes `fields = list(set_fields.keys()) + list(append_fields.keys())` before calling `check_scope`; `check_scope` fails closed when `fields is None`, so callers can't bypass by omission.

Janitor's legitimate Stage 3 enrichment (writing `description` / `role` / `email` etc. onto stub person and org records) needs a wider write surface than Stage 1/2 autofix. Rather than weaken the janitor allowlist, we split out a second scope, `janitor_enrich`, with its own allowlist for enrichment fields and with `create`/`move`/`delete` all denied. The Stage 3 enrichment pass runs under that scope; Stage 1/2 stays tight.

**Tradeoffs / what we rejected.**

- **SKILL-side "thou shalt not" guardrail** — cheapest to ship. Rejected as the primary mechanism because it relies on LLM compliance. Works as a belt alongside the scope braces, not a replacement.
- **Restructuring the agent invocation so the janitor sees issue metadata only, never the full record.** Bigger refactor; would have overlapped too much with the deterministic-writers work. Filed as a future option if field_allowlist proves insufficient.
- **Allowing the existing `edit: True` and just adding reviewer-side auditing.** The scope mechanism is the right layer to catch this — runtime enforcement, not post-hoc audit.

We also took the opportunity to close a sibling loophole: the `--body-append` / `--body-stdin` paths on `vault edit`. The frontmatter allowlist didn't cover body writes, which meant a Stage 1/2 janitor agent could theoretically sidestep by rewriting the entire body. Added an `allow_body_writes: False` flag on the janitor scope (commit `2b8ddbd`); `check_scope` rejects body writes early when the flag is set. Same-commit SKILL audit removed the "flesh out body" step from the janitor SKILL, per our scope-and-SKILL-bundled-audits rule.

**Commit range.** `433bf33..2d5e8cf` for the core Option E sequence (6 commits), plus follow-ups `657957a` (operator-directed merge scope, Q2), `2b8ddbd` (body-write loophole, Q3), `4701e56` (STUB001 fallback flag, Q6). Smoke test at `scripts/smoke_janitor_scope.sh` enforces the contract as a one-shot assertion.

Would love to hear how this echoes (or doesn't) in your thinking.
