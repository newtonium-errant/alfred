---
alfred_tags:
- schema-evolution
- consumer-audit
created: '2026-05-25'
distiller_learnings:
- '[[synthesis/Schema Evolution in One Consumer Silently Invalidates Assumptions in
  Sibling Consumers]]'
- '[[decision/Brief Closed-Status Filter Applied Uniformly Via Shared Helper After
  Type Dispatch]]'
- '[[decision/Vault Status String Comparison Uses casefold Not lower]]'
- '[[decision/Brief Upcoming Events Renders Filter-Visibility Footer When Records
  Were Excluded]]'
- '[[decision/Operator Preferences Get a Dedicated preference Vault Type Not Reused
  decision Type]]'
- '[[decision/Cross-Instance Preferences Load Read-Only via Filesystem at Session
  Init in V1]]'
- '[[decision/Operator Preferences Split Into Two Shapes — Action Gates and Voice
  Directives]]'
- '[[decision/Preference Matcher Index Materializes to data/operator_preferences.json
  on Every Write]]'
- '[[decision/V1 Preference Matcher Rule Enum Is Three Named Rules]]'
- '[[decision/Voice Preferences Load Into Talker System Prompt at Session Start Not
  Per-Turn]]'
- '[[decision/Universal Operator Preferences Live on Salem''s Canonical Vault]]'
- '[[decision/Curator Consumes Preferences at Stage 1.5 After Entity Manifest Emission]]'
- '[[decision/Alfred Black Hermes-Runtime Four-File Approach Deferred in Favor of
  Vault Records]]'
- '[[decision/Event-Sourced Preference-Log Architecture Parked as Live Option Not
  Killed]]'
- '[[synthesis/Talker Forward-Policy Commitments Without Persistence Mechanism Recur
  as Drift]]'
- '[[synthesis/Prompt-Layer Fixes Restore Discipline Structural Fixes Restore Persistence]]'
- '[[synthesis/Talker Hallucinates Downstream Side-Effects on Records Lacking Sync
  Keys]]'
- '[[synthesis/Preference Contract Design Converged From Three Competing Architectures
  Via Targeted Corrections]]'
- '[[decision/Preference Records Carry Both Prose Policy and Structured Matcher]]'
- '[[assumption/Small Named-Rule Enum Covers the Immediate Friction Class for Preferences]]'
- '[[decision/Three-Layer Fail-Loud Defense for vault_edit Tool Surface]]'
- '[[decision/vault_edit Raises VaultError When No Mutation Parameter Supplied]]'
- '[[decision/Talker Dispatcher Pre-Detects max_tokens-Truncated Tool Input Before
  Ops Dispatch]]'
- '[[decision/Talker Emits Structured talker.tool.input_truncated Log Event With Operator-Grep
  Fields]]'
- '[[synthesis/Anthropic SDK Best-Effort JSON Repair Silently Returns Partial Tool
  Use Blocks on max_tokens Truncation]]'
- '[[synthesis/Silent-Success on Identifier-Only Tool Input Is a Cross-Surface Failure
  Mode Requiring Layered Diagnostics]]'
- '[[constraint/LLM max_tokens Budget Can Truncate Tool Use JSON Mid-Emission Producing
  Structurally-Valid Partial Input]]'
- '[[synthesis/Talker Is 2026-Native While Daemons Remain 2024-Bound in Same Codebase]]'
- '[[synthesis/Vertical and Horizontal Divergence Research Are Parallel Disciplines
  With Different Problem Shapes]]'
- '[[synthesis/Modernization Audit Triggered by Convergence of External Pressure,
  Capability Shift, and Operator Question]]'
- '[[decision/Audit Verdict Taxonomy Is KEEP EVOLVE REPLACE DEFER]]'
- '[[synthesis/Dead-Backend Inheritance Tax Compounds Per New Feature]]'
- '[[assumption/Cross-Agent Convergence in Synthesis Is Strong Signal When Agents
  Researched Independently]]'
- '[[assumption/Two-Year-Old Agentic-AI Design Baselines Warrant Explicit Modernization
  Audit]]'
- '[[synthesis/Vertical Audit Catalog Sized at 35 Load-Bearing Architectural Choices]]'
- '[[decision/Three-Position Architectural Debate for Load-Bearing Decisions With
  Multiple Defensible Options]]'
- '[[decision/Cross-Critique Round Briefed for Calibration Not Adversarial Argument]]'
- '[[synthesis/Two-Position Debates Anchor on Existing Options; Third Unconstrained
  Position Prevents Premature Anchoring]]'
- '[[synthesis/Third-Position Value Is Option-Space Expansion Not Winning the Debate]]'
- '[[decision/Horizontal Divergence Research Trigger Is Parallel Solutions to Same
  Problem in Forks]]'
- '[[assumption/Independent Parallel Research Without Cross-Coordination Produces
  Stronger Position Advocacy]]'
distiller_signals: decision:2, assumption:3, contradiction:9
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
project:
- '[[project/Alfred]]'
related_orgs:
- org/Kit.co.md
related_projects:
- project/Alfred.md
status: draft
subtype: draft
tags:
- upstream
- contribution
- writing
type: note
---

*Ghostwritten by Salem (Andrew's personal AI instance) on Andrew's behalf.*

# Reply 11 — Brief Filter Parity (schema-evolution consumer audit)

**Problem shape.** A small bug with a surprisingly general lesson. Andrew told Salem on 2026-05-21 at 18:19 UTC to remove an open house from upcoming events. Salem set `status: cancelled` on the event record. The next morning's brief still listed the open house under "## Upcoming Events → ### This Week."

The Tier B prompt-layer fix in Reply 9's arc addressed the talker's hallucination ("removed from Andrew's Calendar (S.A.L.E.M.)") and the unenforceable forward-policy commitment. But that wasn't the operator-visible symptom. The operator-visible symptom was that **the cancelled event was STILL in the brief.** That's what this fix addresses.

**Root cause.** `src/alfred/brief/upcoming_events.py` had a closed-state filter for tasks but not for events:

```python
# Pre-fix (simplified shape):
if rec_type == "event":
    d = _event_date(fm)
    # ... no status check, event is bucketed regardless of status
elif rec_type == "task":
    if fm.get("status") in {"cancelled", "done", "superseded"}:
        continue  # filter
    d = _coerce_date(fm.get("due"))
    # ...
```

The cancelled-event filter was syntactically inside the `elif rec_type == "task":` branch. Cancelled tasks were filtered (the design intent). Cancelled events flowed straight through to bucketing.

The phase-1 design assumed events don't carry `status` — true at the time. The assumption stopped being true once Phase A+ gcal sync started using `status: cancelled` on events (the cancel-on-gcal hook reads vault `status` when patching the gcal mirror, and the talker SKILL teaches operators to cancel events by setting status). The filter was never updated to match the schema's new shape.

**The lesson is the pattern, not the bug.** A schema change in one consumer (gcal sync started writing `status: cancelled` on events) invalidates a prior assumption baked into a different consumer (brief upcoming-events). The brief consumer was correct under the old assumption; nothing about the brief's code changed; but the world around it changed.

This is a **schema-evolution → consumer-audit pattern**. When a schema gains a new semantic for an existing type — a field that previously didn't apply now does, a status enum that previously was task-only now applies to events too — every consumer that depended on the prior assumption needs review. We didn't have that audit discipline when Phase A+ gcal sync shipped; the assumption rotted silently for ~3 weeks before the operator-visible symptom surfaced.

**Fix shape.** Single shared helper after the type dispatch:

```python
def _is_closed_status(value):
    if not isinstance(value, str):
        return False
    return value.strip().casefold() in _CLOSED_STATUSES

# In the bucketing loop:
if _is_closed_status(fm.get("status")):
    log.info("upcoming_events.closed_status_excluded",
             path=path, rec_type=rec_type, status=fm.get("status"))
    continue
```

Three notable choices:

1. **`.casefold()` not `.lower()`.** Unicode-correct for case-insensitive comparison; canonical CPython recommendation since 3.3. Catches `Cancelled` / `CANCELLED` / `cancelled` uniformly without ASCII-only assumptions.
2. **`.strip()` to handle whitespace-around-token.** Defensive against operator-typed `status: ' cancelled '` (YAML accepts this; we now normalize).
3. **Log emission per filtered record.** Per the `feedback_intentionally_left_blank.md` principle (silence is ambiguous), every filter event emits `upcoming_events.closed_status_excluded` with `rec_type` + `status` + `path`. Operators grep-debug-able. Counterpart to the existing `upcoming_events.event_missing_date` log emitted for the adjacent skip case.

**Footer message when filtered records existed.** When the filter fires on any candidates during a brief render, the Upcoming Events section gets a footer line:

> *N items filtered by operator preferences (see brief.log).*

Singular/plural handled. Empty-state still emits the explicit "No upcoming events." marker if the bucket is fully empty post-filter. The footer makes filter behavior visible without forcing the operator to read logs to know things were dropped.

**The deleted test was load-bearing in the wrong direction.** Pre-fix, `tests/test_brief_upcoming_events.py` had a test named `test_event_status_does_not_trigger_task_filter` that asserted *"workshop with status cancelled SURFACES in upcoming events because event-status doesn't gate."* That test was pinning the Phase 1 contract — and it was correct for Phase 1, where events didn't have status semantics. Post-Phase-A+, the contract should be inverted. The fix replaced the test with seven new tests in the opposite direction, with names + docstrings that reflect the post-fix contract (`test_event_with_cancelled_status_excluded`, `test_event_with_done_status_excluded`, `test_event_with_superseded_status_excluded`, defensive case-handling tests, etc.).

Deleting a test that pinned the wrong contract is the right move — keeping it negated-in-place would have been actively misleading. The narrative comment in the deleted test was load-bearing for the Phase 1 design; the new tests carry the post-fix narrative.

**Sibling-generator audit confirmed minimal scope.** The fix touched only `upcoming_events.py`. A grep for `fm.get("status")` + `rec_type == "event"` across `src/alfred/brief/` confirmed no parallel filter sites needed updating. Other modules (`peer_digests.py`, `operations.py`, `weather.py`, `health.py`, `renderer.py`) don't read event records with status discrimination. The bug was localized; the lesson generalizes.

**Incidental win — 6 task records that had been bleeding through.** With the filter centralized into the shared `_is_closed_status` helper applied to BOTH event and task branches, the regenerated brief caught six tasks with `status: done` that had been showing up in upcoming-items: "Reset Cineplex Account Password," "Request Kit.co Data Export Before Shutdown," "Verify Halifax Marriott Reservation Details," "Refill Prescription at Pocketpills," and two others. They were in the brief because the prior task-side filter only ran on certain code paths. Consolidation caught them.

This is the satisfying part of the fix: a structural cleanup (shared helper after type dispatch) caught both the named bug AND a class of silent misses that had been there longer.

**What we deliberately rejected.**

- **Patching the filter inside the event branch only.** The minimal fix would have been adding `if fm.get("status") in {...}: continue` to the event branch. Rejected — leaves the duplicate-logic-in-task-branch in place, creates future drift opportunity if a third record type ever needs the same gate. Shared helper + post-dispatch placement is the right shape.
- **Treating this as a one-off without naming the pattern.** Tempting because the fix is small. But the pattern (schema-evolution invalidates downstream assumptions) is general enough that we should be looking for the next instance, not just patching this one. Hence the framing.
- **Building schema-version-tracking infrastructure.** Tempting because the root cause is "schema changed and consumers didn't notice." Rejected as over-engineering — a memo-class capturing the pattern + a discipline of consumer-audit on schema-evolution commits is lighter and probably sufficient. If we hit the pattern a third time, the infrastructure becomes worth building.

**Generalization candidates.** When in this codebase has schema-evolution invalidated a downstream assumption?

- **Phase A+ gcal sync introduced `status: cancelled` on events** — the bug this commit fixes. Identified after operator-visible symptom.
- **The preferences arc added `cites_canonical:` as a vault-graph link** — any consumer that walks the vault graph for relationship analysis (surveyor cluster suggestion, distiller) should know to treat preference records' `cites_canonical` as a meaningful edge. Audit not done yet; flagged.
- **Hypatia Zettelkasten added 5 new record types** (zettel, source, author, MOC, question, research-pointer). Consumers that iterate over `KNOWN_TYPES` got these for free, but consumers that hand-coded type lists (some janitor autofix branches, some brief sections) needed manual review.

The pattern is real, recurring, and worth a memo class. Filed for the next time it surfaces — if it's a third time, promote to a discipline.

**Test surface.** 7 new event-side regression-pin tests in `tests/test_brief_upcoming_events.py` (replaced one Phase-1-contract test). Per `feedback_regression_pin_unconditional.md`, no module-level `importorskip` — these tests run unconditionally. Pre-existing brief-suite 243 tests still pass.

**Commits.** `16adee0` (brief.upcoming_events: extend closed-state filter to event records).

**Open questions.**

- **Should "schema-evolution → consumer-audit" become its own memo class?** Today we have the `feedback_intentionally_left_blank.md` memo (silence is ambiguous), `feedback_partial_failure_tool_result_surface.md` (partial-failure side-channel fields need consumer audit). The schema-evolution flavor is adjacent but distinct. If a third instance lands, promote.
- **Tooling for the audit discipline?** A pre-commit check that flags "this commit changes a schema-relevant file; have you audited consumers?" might be over-engineering for the frequency. A discipline + memo is probably sufficient.
- **What's the right cadence for proactive consumer audit?** Today reactive (operator-visible symptom triggers the audit). Could be quarterly — pick one consumer category (brief sections, distiller stages, surveyor rules) and audit against current schema for assumption-drift.

The interesting framing — at least the framing that landed for us — is that **assumption-drift is a kind of slow-motion bit-rot** that doesn't show up in tests because the tests pin the prior contract. The fix isn't more tests on the same shape; it's identifying which consumer assumptions span which schema dimensions, and revisiting them when those dimensions change.

Would love to hear how this echoes (or doesn't) in your thinking — particularly whether you've found a more durable shape for consumer-audit discipline when schema and consumer evolve at different cadences.
