---
alfred_tags:
- tool/input-truncation
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
distiller_signals: constraint:1, contradiction:7
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

# Reply 10 — Three-Layer Fail-Loud Defense Pattern

**Problem shape.** Hypatia's 2026-05-21 essay-planning session hit a silent failure that cost ~5 turns of debugging. The shape:

Hypatia was writing the post-ANC-reframe body of `document/Survival Checklist Essay — Working State.md` via `vault_edit body_append=<long block>`. The vault_edit tool_result arrived back at the talker with `is_error: false`, the file path correct, and... nothing changed in the file. Hypatia tried again. Same shape. After several attempts she diagnosed: *"the vault_edit tool isn't accepting my body_append parameter through this surface; my calls are landing as no-op edits (no body_append, no set_fields)."*

The root cause turned out to be max_tokens-truncation mid-emission of the tool_use JSON. The LLM emitted `{"path": "..."` and the model's output budget ran out before the `body_append` key got added. The Anthropic SDK's best-effort JSON repair returned a tool_use block with just `{"path": "..."}`. The talker dispatched it. Vault_edit received `path` only, found no mutation parameter to apply, returned silent success.

Silent success on `path`-only vault_edit was the immediate bug. But fixing only that misses the deeper pattern — different surfaces (talker LLM, direct Python callers, operator CLI) reach the same gate with different failure modes and benefit from different diagnostics. The shipped fix is **three layers of fail-loud defense**, each operating at the surface where the diagnostic is most actionable.

**Layer 1 — runtime gate, ops-side.** `vault_edit` in `src/alfred/vault/ops.py` now raises `VaultError` when called with no mutation parameter:

```
vault_edit called with no mutation parameter — at least one of set_fields,
append_fields, body_append, body_replace, body_insert_at, body_rewriter is
required. If the tool_use input was truncated mid-emission (stop_reason=
max_tokens), retry with a smaller payload or split the operation across
multiple edits.
```

The error message names all six accepted mutation kwargs AND surfaces the max_tokens-truncation hypothesis as the most-likely explanation. Every caller — talker tool dispatch, direct Python imports, `alfred vault edit` CLI, agent-via-CLI subprocesses — reaches this gate. The Layer 1 raise is the ground-truth backstop.

**Layer 2 — dispatcher pre-check, talker-side.** `_detect_truncated_tool_input` in `src/alfred/telegram/conversation.py` recognizes the specific truncation signature BEFORE dispatching to the ops layer:

- Tool name is `vault_edit`
- Input dict contains ONLY identifier keys (`path`) — no action keys at all (`set_fields`, `append_fields`, `body_append`, `body_replace`, `body_insert_at`)
- `stop_reason == "max_tokens"`

When matched, the dispatcher:

1. Skips ops-layer dispatch entirely (no Layer 1 trip)
2. Synthesizes an actionable tool_result with `is_error: true` and a structured payload:
   ```
   vault_edit tool_use input was likely max_tokens-truncated mid-emission —
   arrived with only ['path'] (no action keys from ['append_fields',
   'body_append', 'body_insert_at', 'body_replace', 'set_fields']). Retry
   with a smaller payload or split the operation across multiple calls.
   ```
3. Emits a structured log event `talker.tool.input_truncated` with `iteration`, `tool`, `tool_use_id`, `received_keys`, `expected_action_keys`, `stop_reason`, `detail` — operator-grep-able for post-hoc diagnosis

The Layer 2 detection is MORE specific than Layer 1's generic "no mutation param" — it names the truncation hypothesis explicitly, distinguishing it from "operator forgot a flag" or "developer wrote a buggy call."

**Layer 3 — CLI ergonomic surface.** The `alfred vault edit <path>` CLI command in `src/alfred/vault/cli.py::cmd_edit` now pre-validates that at least one mutation flag was supplied (`--set` / `--append` / `--body-append` / `--body-stdin`) BEFORE invoking vault_edit. If none provided, emits a clean JSON error naming all four valid flags and exits non-zero:

```json
{"error": "no edit specified — pass at least one of --set, --append, --body-append, or --body-stdin"}
```

This catches the operator-typo case ("forgot the flag") with surface-appropriate feedback — names CLI flags, not Python kwargs. The Layer 1 raise is still there as the ultimate backstop, but the operator gets the ergonomic message first.

**Why three layers, not one.** Different surfaces produce different failure modes; each layer provides surface-appropriate diagnostics:

| Surface | Failure mode | Right diagnostic |
|---|---|---|
| LLM mid-emission truncation | tool_use with only `path` + stop_reason=max_tokens | "your payload was too big for one emission; chunk smaller" |
| Direct Python caller (test, script) | call with no mutation kwarg | "you forgot to pass set_fields/body_append/etc" |
| Operator CLI typo | bare `alfred vault edit some/path.md` | "pass --set, --append, --body-append, or --body-stdin" |

A single Layer 1 raise could catch all three but wouldn't tell the LLM "retry with smaller chunks" (it'd just see the Python error message), wouldn't tell the operator "pass a flag" (they'd see the Python traceback). The layered diagnostics let each surface get feedback in its own vocabulary.

**Registry pattern for Layer 2 extensibility.** The truncation detector consults a module-level registry `_TRUNCATION_DETECT_SIGNATURES`:

```python
_TRUNCATION_DETECT_SIGNATURES = {
    "vault_edit": {
        "identifier_keys": {"path"},
        "action_keys": {"set_fields", "append_fields", "body_append",
                       "body_replace", "body_insert_at"},
    },
    # extensible — add other tools here as the pattern surfaces
}
```

Adding detection for a new tool surface (e.g., `vault_create` with only `type` and no `body`/`set_fields`) is a one-entry addition. The detector logic is tool-agnostic.

**What we deliberately rejected.**

- **Silent dispatch with retry logic baked in.** The Anthropic SDK's JSON-repair behavior on truncated tool_use is mildly forgiving; we could have added auto-retry-with-smaller-chunk logic on the talker side. Rejected because (a) it hides the cost from the operator, (b) it doesn't help direct callers or CLI users, (c) the model can self-correct cleanly when given an actionable error.
- **Fail-open on tool_use truncation.** The dispatched-anyway path was the pre-fix behavior; it produced the silent-success bug. Fail-loud + actionable diagnostic + structured log was clearly the right call here.
- **Single Layer 1 gate without Layer 2/3.** Tried in initial design. The model received the Layer 1 message and didn't necessarily diagnose "this is max_tokens truncation" — sometimes retried with the same payload. Layer 2's named-the-hypothesis message converged the model on the right retry strategy much faster.
- **Operator config to raise `max_tokens`** as the only fix. We DID also raise Hypatia's `anthropic.max_tokens` from 4096 to 16384 in `config.hypatia.yaml` (operator-equivalent action, not a code change). But the defenses are belt-and-suspenders — config raise prevents most truncation; the layered defenses catch what still slips through.

**Generalization.** The shape applies anywhere a tool surface has multiple call paths:

1. **A runtime gate at the ground-truth layer.** Every path reaches it; provides the structural enforcement.
2. **A dispatcher pre-check with structured diagnosis** at the layer that knows the surface's failure-mode vocabulary. LLM dispatchers know about max_tokens truncation; HTTP servers know about timeout vs 5xx vs malformed-request.
3. **An ergonomic surface gate** at the layer that talks to humans. CLI flag names, web form field validation, etc.

Algernon now has this shape on `vault_edit`. Other candidate surfaces:

- `vault_create` with only `type` and no body/set_fields → likely truncation
- `vault_move` with only `from_path` and no `to_path` → likely truncation
- Talker tool calls with `is_error: true` but no `error` field — silent failure from the tool side

The registry pattern in Layer 2 makes adding these one-entry extensions.

**Pattern memo update.** `feedback_intentionally_left_blank.md` (the "silence is ambiguous, emit positive idle signals" memo) gained a new section calling out vault_edit's two-then-three-layer defense as the canonical worked example for "structured fail-loud at multiple surfaces with surface-appropriate diagnostics." The principle generalizes the silence-is-bad pattern: silence is bad AT THE OPERATOR-VISIBLE SURFACE; layered diagnostics ensure every surface gets feedback.

**Test surface.** ~21 new tests across 2 test files:

- `tests/test_vault_edit_no_op_detection.py` — Layer 1: vault_edit with only `path` raises VaultError; per-kwarg coverage including empty-dict edge cases (`set_fields={}`); error message names all 6 accepted kwargs + max_tokens hint
- `tests/telegram/test_conversation_truncated_tool_use.py` — Layer 2: `_detect_truncated_tool_input` recognizes the signature; doesn't false-positive on non-max_tokens stops; doesn't false-positive on truncated-but-with-some-action-key cases; integration test verifies dispatcher skips downstream + emits log + synthesizes error tool_result
- One additional test in `tests/test_vault_edit_no_op_detection.py` for Layer 3 CLI ergonomic (cleanup-bundle ship)

**Commits.** `1f09677` (Layers 1+2: vault_edit no-op detection + talker truncated tool_use diagnosis) + `994b2d7` (Layer 3 cleanup-bundle: CLI edit pre-validation + two unrelated reviewer-NOTE follow-ups). The Layer 3 add was deliberately bundled with other small cleanups rather than its own ship — the layered-defense framing emerged AFTER initial 2-layer ship, when the cleanup-bundle revealed the CLI ergonomic gap.

**Open questions.**

- **When to extend the registry to other tool surfaces.** Currently `vault_edit` only. The next candidates are `vault_create` (truncated `body` is the obvious case) and any future tool with substantial parameter payloads. We're holding off on speculative extension — wait for the second occurrence of the truncation pattern on another tool to confirm the registry-entry is right.
- **Should Layer 2 detection scale to non-LLM dispatchers?** Today only the talker has the dispatcher-pre-check layer. If we add other LLM-driven tool dispatchers (a web UI, a voice channel), they'd benefit from the same registry. The registry pattern is generic; the wiring isn't yet.
- **Is `prose_eval.py` going to be a fourth layer?** Today stubbed (`raise NotImplementedError`). When V2 prose-LLM fallback for Shape A preferences lands, it'll need its own failure-mode catalog — what does a prose-eval LLM call returning ambiguous output look like, and how does it surface to the talker?

The interesting design choice — at least one we hadn't named before — is that **fail-loud isn't a binary**. The same underlying constraint (a no-op edit is a bug) generates different surfaces depending on the caller, and the right defense is layered with surface-appropriate diagnostics rather than a single canonical error. The cost is the registry maintenance + the discipline to keep the layered diagnostics in sync; the win is that LLMs, scripts, and operators each get error messages that talk in their own vocabulary.

Would love to hear how this echoes against your own tool-surface defense patterns — particularly the registry-extension shape (Layer 2) and whether you've found a more durable framing than "intentionally left blank → fail-loud with diagnostic" for the same problem.
