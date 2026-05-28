---
alfred_tags:
- vault-contract
- multi-instance
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
distiller_signals: constraint:2, contradiction:3
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
janitor_note: LINK001 — [[preference/use-keyboard-friendly-labels]] referenced in
  the body as a worked example for cites_canonical override; the slug is illustrative
  within the upstream contribution draft and no such preference record exists in the
  vault. FM001/DIR001 — record is type=note stored under process/ as a writing-arc
  grouping convention; deterministic scanner flags expected, no janitor action.
project:
- '[[project/Alfred]]'
related_orgs: []
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

# Reply 9 — Operator Preferences Vault Contract V1

**Problem shape.** Andrew said to Salem on 2026-05-21 at 18:19 UTC: *"Remove the open house from the schedule. No more going forward unless I specifically ask for it."* Salem set `status: cancelled` on the event record, replied *"Done — Open House May 24 cancelled and removed from Andrew's Calendar (S.A.L.E.M.). Won't add open houses going forward unless you ask."* The next morning's brief still listed the open house. The structural failure had three layers:

1. **Brief filter bug** — `brief/upcoming_events.py:151` cancelled-event filter was inside the `elif rec_type == "task"` branch and never applied to event records. (See Reply 11 for that arc.)
2. **Talker hallucination** — Salem claimed "removed from Andrew's Calendar (S.A.L.E.M.)" when the event had no `gcal_event_id` and was never on gcal. The gcal-side step couldn't happen.
3. **Underlying structural problem** — Salem's "won't add open houses going forward unless you ask" was an unenforceable commitment. The curator extracts events from emails based on its SKILL prompt; no persistence mechanism existed for the forward policy. Next ViewPoint email would produce another open-house event.

Today's prompt-layer fix (Reply 10 arc) gave Salem the discipline to honestly disclaim what she can't enforce. The structural fix is this — a vault-record contract for operator preferences that consumers actually honor.

**Architectural debate.** Three positions seriously considered:

- **Algernon's path (chosen):** `preference/` as a first-class vault type. Operator preferences are records in the vault graph, browseable in Obsidian, with structured matchers + JSON index for cheap consumer pre-filter and a system-prompt voice block at session start.
- **Alfred Black's path (deferred):** four `.md` files in a Hermes runtime — `SOUL.md`, `AGENTS.md`, `MEMORY.md`, `USER.md` — loaded into every system prompt by the runtime. Clean separation of concerns at the storage layer; sole-writer with HTTP 422 promotion contract; four-store model (vault + state.db + cold.db + ingest.db).
- **Event-sourced negotiation log (parked as live option):** append-only `preference-log/*.jsonl` of operator-agent negotiations (`propose`/`confirm`/`narrow`/`supersede`/`revoke`); the records consumers read are derived materializations via a projector daemon. Thread parentage makes the canonical/override pattern a first-class primitive instead of an emergent property.

The convergent design (Plan C) took Algernon's vault-record-as-first-class shape with two corrections from the third-way critique: (1) a separate `preference/` type rather than reusing `decision/`, and (2) read-only cross-instance load at session-init via filesystem so Hypatia can see Salem's canonical preferences without a peer-protocol round-trip in V1.

**Two preference shapes in V1.**

**Shape A — action gates.** Affects extraction / inclusion / proposal decisions. Consumed by curator (Stage 1.5 gate after entity manifest emission) and brief (`upcoming_events` filter). Structured matchers ride alongside the prose policy:

```yaml
matcher:
  domain: curator
  rule: skip_event_if
  args:
    title_regex: "(?i)\\bopen[\\s-]house\\b"
```

A small enum of named rules (`skip_event_if`, `skip_brief_event_if`, `skip_brief_task_if` in V1) covers the immediate friction class. The matcher index materializes to `data/operator_preferences.json` on every preference write; consumers read in-memory and pre-filter candidates before LLM-bearing work. No per-extraction LLM call for the structured cases.

**Shape B — voice / behavior directives.** Affects response generation. Loaded into the talker system prompt at session start as a dedicated block — not per-turn LLM applicability evaluation. Two sub-shapes:

- **B1 universal** — applies to all instances. Lives on Salem's canonical vault.
- **B2 instance-specific** — applies to one named instance. E.g. *"Hypatia: don't start replies with 'stop'"* lives in Hypatia's vault.

The "shape" frontmatter field discriminates; consumers route accordingly.

**Cross-instance read via filesystem, not peer-protocol.** Salem holds canonical preferences at `vault/preference/`. Hypatia reads BOTH her own local `preference/` directory AND Salem's canonical directory at session start, merging with **local-wins** conflict resolution (matched via `cites_canonical:` wikilink OR slug collision). Peer protocol is reserved for V2 if cross-instance write-propagation becomes load-bearing; V1 is read-only crossing via filesystem mount.

**`cites_canonical:` wikilink for instance-application records.** When Hypatia wants an instance-specific override of a Salem-canonical universal — say, Salem's *"use plain English letters in option lists"* is right except Hypatia uses roman numerals for essay version markers — Hypatia writes a local `preference/` record with `cites_canonical: "[[preference/use-keyboard-friendly-labels]]"`. The link resolves in Obsidian's backlinks pane so Andrew can stand on the canonical record and SEE which instances override it. The two records coexist in the vault graph; the conflict-resolution rule (local-wins per instance) is in the loader.

**Status flip revocation.** `status: active` → `status: revoked` via `vault_edit set_fields`. Status enum is exactly `{active, revoked}` — no supersedes-chain, no draft/superseded/reversed complexity. The revoked record stays in `preference/` for audit; consumers filter on `status: active`. `git log preference/<slug>.md` shows exactly when a commitment died.

**Talker proposes-and-confirms write workflow.** When the operator sets a forward-policy in conversation, the talker:

1. Recognizes the forward-policy phrasing (existing SKILL pattern from "Unenforceable forward-policy commitments" section, shipped before this arc)
2. Drafts a preference record proposal (frontmatter + body + matcher if Shape A)
3. Sends the draft to the operator inline ("I'll add this as a preference — sound right?")
4. On confirm, calls `vault_create type=preference`
5. Triggers index rebuild (`alfred prefs rebuild-index`)

No silent persistence. Operator sees the draft and can edit the matcher, the scope, or the wording before it lands.

**What we deliberately rejected from Alfred Black's path.**

- **Sole-writer + HTTP 422 promotion contract.** Heavy for single-operator deployment. We get per-instance × per-type × per-op authority enforcement via existing scope dicts in `vault/scope.py` without a sole-writer service or HTTP envelope. The promotion-contract pattern is right for commercial multi-tenant; ours is right for single-operator multi-instance.
- **Four-store model.** Most operator-context fits cleanly in the vault. We don't have the "this isn't really a vault record" pressure that justifies a state.db / cold.db split.
- **Monorepo restructuring.** 10-package monorepo (`packages/{web,ctrl,learn,hermes,mcp-server,...}`) is premature for a fork. Stays a single-package shape.

**What we deliberately rejected from the event-sourced third-way path.**

- **Append-only log + projector daemon as primary substrate.** ~2-3x V1's code weight (~1500-2000 LOC vs ~700). Less than 24h after V1 ship, the appetite isn't there. But the option stays live — the third-way migration plan keeps V1 as the materialized-view layer, so the V1 work isn't sunk cost if we pivot later.

**Friction triggers to reconsider the third-way.** Three concrete signals to watch for in the next ~1 week of lived use:

1. `cites_canonical` chains accumulating sibling records (third-way's thread parentage handles this more cleanly)
2. Provenance loss on status-flip costing debug time ("when was this preference active?" requiring git archaeology)
3. Multiple narrow / override events on the same canonical that get hard to track

If any of these surfaces, the third-way's 4-phase migration plan (Phase 0 dual-write events alongside V1; Phase 1 projector lands; Phase 2 event-first writes; Phase 3 cutover) becomes the next-arc candidate.

**Tradeoffs / what else we rejected.**

- **Pure prose preferences without structured matchers.** Tempting because operator expression is natural language. But every consumer call becomes an LLM applicability evaluation — cost balloons. Hybrid (prose source-of-truth + auto-distilled matchers for cheap pre-filter + prose-only fallback for harder cases via `prose_eval.py`) is the V1 shape; the fallback is stubbed (`raise NotImplementedError`) and lands when the first concrete miss surfaces.
- **Auto-detect forward-policy phrasings + silent persistence.** Higher operator-friction-saving but loses agency. Proposes-and-confirms keeps the operator in the loop — at the cost of a one-turn confirmation per preference.
- **`/preferences` slash command in V1.** Deferred. Operator browses the vault directory in Obsidian for V1; the slash command is a Phase 2 candidate when preference count grows.

**Multi-user forward-compatibility hook.** Every V1 record carries `applies_to_user: null` as a forward-compat field. In V1 always null (single-user); V.E.R.A. multi-user arc populates it when that ships. Loader signatures already accept the `user` parameter (defaults to None today); the V2 plumbing is the loading-context-pass-through, not a contract change.

**Test surface.** ~58 new tests across 7 test files: loader round-trip, matcher predicate evaluation, JSON index rebuild atomic-write, curator preference filter integration, brief preference filter integration, system-prompt block injection (including cross-instance conflict resolution), cross-instance filesystem read.

**SKILL audit shipped same-cycle.** Per the standing rule that feature-enabling commits trigger a SKILL capability audit AND scope-narrowing commits trigger a SKILL audit, all six affected vault-* SKILLs updated in commit `1874161`:

- vault-talker: forward-policy section UPGRADE (drafts preference record proposal, propose-and-confirm flow, two worked examples)
- vault-hypatia: new "Operator preferences" section, cites_canonical override worked example, universal-defer-to-Salem routing
- vault-kalle: read-only capability, route persistence requests to Salem
- vault-curator: brief mention of Stage 1.5 action-gate (filter runs out-of-prompt; don't second-guess)
- vault-janitor: scope-narrowing — preference cannot be deleted (revoke via status flip); body cannot be edited
- vault-distiller: V1 has no distiller-side preference gate; V2 future-mention

**Commits.** `4c088ac` (V1 code: vault contract + curator/brief/talker wiring) + `1874161` (same-cycle SKILL audit across 6 bundles).

**Open questions.**

- **When does prose-LLM fallback land?** The `prose_eval.py` stub raises `NotImplementedError` by design — surfacing the gap when the first concrete miss arrives. We don't want to build it speculatively. Watching for: operator writes a preference whose intent doesn't reduce to a structured matcher (e.g., *"Don't be enthusiastic in replies"*).
- **V.E.R.A. multi-user evolution.** Single-user-first V1 with `applies_to_user: null` forward-compat. When V.E.R.A. lands, per-user scoping plumbing is the V2 work. The interesting design question: does the canonical/instance split scale to canonical/instance × per-user, or do we collapse one dimension?
- **Conditional preferences (Shape C).** Deferred entirely from V1 — *"don't surface anything stressful before 10am"* needs to evaluate state at consumer time. Hard to fit cleanly into structured matchers; would need its own predicate-evaluation layer.

The interesting design choice — at least from where we stand a week in — was the canonical-application split with rejection-retention. Most multi-instance systems either deduplicate everything to a single canonical (losing per-instance variation) or fork everything per-instance (losing the canonical authority). The third path (canonical-with-per-instance-overrides-via-citation) is what made this contract feel "Algernon-shaped" rather than a port of someone else's pattern.

Would love to hear how operator-context-as-vault-records lands in your thinking, especially relative to the `SOUL.md` / `AGENTS.md` / `MEMORY.md` / `USER.md` shape — and whether the canonical/application-with-rejection-retention pattern resonates as a primitive for multi-instance personal-AI systems.
