---
alfred_tags:
- divergence-research
- horizontal-audit
- decision-making
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
distiller_signals: decision:5, contradiction:11
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's
  review
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

# Reply 13 — Horizontal Divergence Research Pattern (3-position architectural debate)

**Problem shape.** How do you make a load-bearing architectural decision when multiple legitimate options exist? Not "pick the obvious best" (no obvious best exists) and not "decide quickly so we can ship" (the decision will compound across the next year of work). The pattern that landed for us: a **3-position architectural debate** with cross-critique and synthesis.

The trigger was the operator preferences arc (Reply 9). Algernon had just shipped V1 of a vault-record-based operator preferences contract. The same week, Alfred Prime released `platform-2026.05.24` — the "operational-context release" — which addresses the same operator-context-persistence problem with a fundamentally different architecture (Hermes runtime loading `SOUL.md` / `AGENTS.md` / `MEMORY.md` / `USER.md` into every system prompt). Two production systems with parallel solutions to the same problem.

The natural question: do we double down on V1, adopt Alfred Black's pattern, or build something else? Both architectures are defensible. Neither is "obviously" right. The decision will shape Algernon's next 12+ months of operator-context work.

**The 3-position pattern.** Three research agents dispatched in parallel, each with a different framing:

1. **Pro-Position-A advocate** — argue genuinely for one of the existing approaches (in this case, Alfred Black's SOUL/AGENTS/MEMORY/USER pattern). Identify what we'd gain by adopting it. Identify what we got wrong in our existing approach.
2. **Pro-Position-B advocate** — argue genuinely for the other existing approach (Algernon V1). Identify what we got right. Identify what the alternative is missing.
3. **Third-way imaginer** — NOT bound to either existing approach. Free to invent or propose entirely different architectures. Engage with both Position A and Position B framings, then propose alternatives.

Each agent works independently — no cross-coordination during research. They produce ~700-2000 word advocacies. Then a **cross-critique round** where each agent reads the others' positions and produces a critique identifying failure modes / scope creep / convergence points.

**Why three positions, not two.** A two-agent debate (pro-A vs pro-B) tends to anchor the decision on the existing options. The third-way imaginer prevents premature anchoring — they're explicitly tasked with "what would you build if neither A nor B existed?"

The third-way agent for the preferences arc produced an **event-sourced negotiation log** approach that neither Position A nor Position B had on offer. The log + materialized-view shape unified four things our V1 treats as separate (forward-policy create, voice block load, cites_canonical override, status-flip revoke). It would cost 2-3x V1's code weight. Andrew didn't pick it for V1, but it's now a live option with a 4-phase migration plan if V1's limitations bite.

The third position's value isn't winning — it's expanding the option space so the choice between A and B is made against a richer landscape.

**Cross-critique round.** Each agent reads the others and produces a 500-word critique. The cross-critic for Position A (pro-Alfred-Black) reads the Algernon V1 advocacy and identifies where V1 is genuinely ahead (vault-graph integration, multi-instance distinct-voices) — concessions strengthen the debate. The cross-critic for Position B (pro-Algernon) reads the Alfred Black advocacy and identifies where Algernon V1 is genuinely missing something (separation of concerns at the storage layer, sole-writer pattern's authority enforcement).

The cross-critique is not adversarial — it's calibration. Each agent's brief includes "concede where the other position is right; argue where you genuinely disagree." The result is positions that engage with each other's strongest framings, not strawmen.

**Synthesis convergence.** Team-lead reads all three positions + cross-critiques + identifies convergent points. For the preferences arc, convergence emerged surprisingly cleanly:

- BOTH critics agreed on new `preference/` type rather than reusing `decision/` (Position A's correctness anchor)
- BOTH critics agreed on JSON index for cheap pre-filter rather than per-call LLM eval (Position B's pragmatic constraint)
- BOTH critics agreed on status-flip revocation rather than supersedes-chain (Position B's lean simplicity)
- BOTH critics agreed on rejection-retention pattern (Position A's correctness anchor for the canonical/instance split)
- BOTH critics agreed the third-way's 2-3x code investment isn't justified TODAY but the migration path is genuinely available IF V1 friction surfaces

The synthesis (Plan C) took Position B's skeleton + three corrections from Position A + the third-way's option-preservation. Andrew ratified Plan C with three detail decisions (canonical vs application semantics; multi-user forward-compat; per-context vs single canonical) and the implementation shipped.

**Convergence emerged because constraints were anchored before debate.** The three agents shared a hard-constraints document (two shapes V1, B1/B2 subdivision, prose source-of-truth + optional matchers, talker proposes-and-confirms, canonical-on-Salem with instance-application records, single-user-first with forward-compat, reuse peer-protocol machinery where helpful). The debate was about the *implementation* of those constraints, not the constraints themselves. Sharing the constraints up-front meant the agents diverged on optional/tactical choices, not foundational ones — which made synthesis tractable.

**The architectural-decision preservation pattern.** A side-output worth naming. The 3-position research is ~5000+ words of substantive position-papers. Without preservation, evaporates after the decision lands. We save full agent outputs as session-note artifacts (`aftermath-lab/session/YYYY-MM-DD-<decision-name>-research.md`) so future revisitations have the depth, not just the synthesis. When friction signals shift the decision (the third-way becomes viable; Position A's commercial wisdom becomes load-bearing), the original arguments are recoverable.

Each saved research artifact includes: the question, the hard constraints anchored before debate, the three positions in full, the cross-critique round, the synthesis, the operator's directional response, and post-decision practice notes.

**Worked example deltas (preferences arc specifically).**

Position A (pro-Alfred-Black) flagged that Algernon's structured matcher enum (3 rules in V1) was over-engineering — for 3 rules, prose-only "Hard Rules" injected into the system prompt would work and generalize better. Position B (pro-Algernon) responded that structured matchers enable cheap pre-filter at consumer time (no per-extraction LLM call), which is load-bearing for curator processing ~50 inbox items/day. The third-way (event-sourced) flagged that both approaches store STATE; the canonical shape might be a LOG, with the records consumers read being derived materializations.

The Position A critique landed: V1 ships the structured-matcher enum AS a structured-matcher enum, but the prose-LLM fallback (`prose_eval.py` stub raising NotImplementedError) preserves the option to evolve toward Position A's read if structural matchers don't generalize. Position B's pre-filter cost argument stuck. The third-way's log substrate didn't ship V1 but stays live.

This is the pattern's value: the synthesis isn't pure compromise; it's principled integration of strongest arguments from each position, with the rejected paths preserved as live options if friction surfaces.

**When to use this pattern.** Load-bearing architectural decisions with multiple legitimate options. NOT for:

- Decisions where one option is obviously right (just pick it)
- Decisions where the cost of debate exceeds the cost of being wrong (ship, learn, iterate)
- Decisions inside a well-explored design space (memos + a single Plan agent usually suffice)

The 3-position pattern is for the rare-but-load-bearing decision where multiple defensible options exist + the choice compounds across many subsequent decisions. The preferences arc fit this shape. Future candidates: cross-instance authentication model, vault-substrate migration (if it ever happens), operator-canonical-identity-resolution.

**Cadence.** Paired with the vertical divergence audit (Reply 12) at monthly cadence — "4-quadrant grid" framing. Horizontal (Algernon vs Alfred Black/Prime) and vertical (Algernon-as-it-is vs Algernon-built-fresh-today) run as the same arc, surfacing complementary findings. Less frequent than monthly risks missing fast-moving deltas (David's commercializing Alfred Black is accelerating divergence); more frequent than monthly is too much architectural-overhead for the cadence of actual ship work.

**Tradeoffs / what we rejected.**

- **Two-agent debate (skip third-way imaginer).** Faster (~30% less time), simpler synthesis. Rejected because the third-way's value is structural — without it, the decision anchors prematurely. The preferences arc's third-way produced a genuinely-different approach that didn't win V1 but became a live migration option.
- **Single Plan agent with multi-position analysis.** Cheaper, but the single-agent framing converges on a recommendation early. Independent agents with different briefs preserve the spread of arguments.
- **Cross-critique as adversarial debate.** Tried in early iterations; produced strawmen. Reframing the cross-critique as "concede where the other is right; argue where you genuinely disagree" produces honest engagement with strongest framings.
- **Synthesizing without the operator's directional input.** Team-lead synthesizes but the operator's preferences shape which convergences are load-bearing. For the preferences arc, Andrew's "Position B + open to C if friction surfaces" framing was the synthesis-anchor; without that, team-lead synthesis would have over-weighted certain convergences.

**Generalization.** The pattern extends beyond architectural debates:

- **Tool-surface decisions.** When choosing between (e.g.) MCP server design alternatives, the same 3-position pattern applies — pro-Alfred-Black-approach + pro-Algernon-approach + third-way.
- **Pattern-language decisions.** When naming a new principle (e.g., "three-layer fail-loud defense"), three positions on the framing produces sharper memo content than single-agent synthesis.
- **Roadmap prioritization.** When picking the next major arc, three positions (Position A: high-friction operational item; Position B: strategic capability arc; Position C: research/learning arc) sharpen the priority decision.

**Preservation as the practice's load-bearing artifact.** Without the saved research documents, the 3-position pattern would be a one-shot decision tool. With preservation, it becomes institutional memory: future Andrew (or future agents joining the team) can read the original deliberation when the decision needs revisitation. The preservation discipline IS the practice as a recurring discipline.

**Open questions.**

- **When does the 3-position pattern not scale?** For decisions with 4+ legitimate positions, the pattern would need extension (4-position with multi-axis cross-critique). Untested. Current cadence keeps decisions tractable at 3 positions.
- **Should the third-way imaginer be a different agent type (not Plan)?** The Plan agent works well, but a more explicitly speculative agent (no codebase grounding, only first-principles design) might produce stronger third-ways. Worth experimenting with on the next iteration.
- **How does the pattern interact with vertical-divergence audit?** Paired execution starts next iteration (June 2026). Untested but the 4-quadrant framing predicts convergent synthesis across both arcs.

The interesting framing — at least the framing that landed for us — is that **the 3-position pattern's value isn't producing the right decision; it's producing a decision made against the richest possible option space**. The synthesis can still be wrong; future friction can shift the choice. But the original deliberation has the depth to support principled revisitation rather than reactive pivoting.

Would love to hear how this echoes against your own architectural-debate patterns — particularly whether you've found a more durable shape for the "multiple legitimate options" decision problem, and whether the 3-position framing maps onto Alfred Black's design-decision process.
