---
type: session
status: completed
name: Distiller rebuild Week 1 MVP
created: 2026-04-24
branch: rebuild/distiller
shadow: true
description: Week 1 MVP rebuild of the distiller shipped on `rebuild/distiller` branch (NOT master) — six commits introducing Pydantic contracts, direct Anthropic SDK backend (no tools), non-agentic extractor with schema validation, deterministic writer, daemon dispatch behind feature flag, and contract-parity smoke script. Flag off by default; live distiller continues on legacy path. Week 2 measurement requires explicit operator flag-flip.
intent: Prove the "LLM as pure inspector, Python as writer" thesis in shadow mode without touching the live vault. Eliminate the 1194 `pipeline.manifest_parse_failed` events by replacing the subprocess-stdout-JSON contract with a Pydantic-validated Messages API call.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Distiller rebuild Week 0 transition 2026-04-24]]'
- '[[session/Distiller rebuild research proposals preserved 2026-04-24]]'
tags:
- distiller
- rebuild
- week-1
- shadow
- mvp
- branch
---

# Distiller rebuild Week 1 MVP

## Intent

**Branch: `rebuild/distiller` (NOT master). Shadow mode (feature flag OFF by default). Zero live-vault writes, zero Anthropic API calls during build.**

The rebuild replaces the legacy distiller's agent-writes-files pattern — where Claude is invoked as a shell agent that `cat > /tmp/manifest.json`s its output — with a non-agentic Messages API call that returns validated JSON, parsed by Pydantic, with Python code doing all file writes. This is Week 1's minimum viable slice: extractor + writer + dispatch + smoke, enough to test the thesis in Week 2 when the operator flips the flag for assumption-type records only.

## Work Completed

Six commits on `rebuild/distiller` branch, branched from master HEAD `b93ce1e` (post-Week 0, post-research-proposal preservation):

- `54010da` — Distiller rebuild c1: Pydantic contracts for learning candidates (101 LOC, `distiller/contracts.py` new). `LearningCandidate` + `ExtractionResult` with type/status validators locked to `vault/schema.py`.
- `3fe698b` — Distiller rebuild c2: direct Anthropic SDK backend (no tools) (147 LOC, `distiller/backends/anthropic_sdk.py` new). `call_anthropic_no_tools()` helper borrowing the `AsyncAnthropic` client init pattern from `src/alfred/instructor/executor.py` but OMITTING `tools=` — returns raw text. Bypasses OpenClaw entirely (OpenClaw has no tool-less mode, confirmed 2026-04-24 via Explore agent).
- `e879aa8` — Distiller rebuild c3: non-agentic extractor with Pydantic validation (229 LOC, `distiller/extractor.py` new). Prompt inlined as module constant (small-surface bias); one repair retry on `ValidationError`; fenced-block stripping; empty `ExtractionResult(learnings=[])` fallback on double failure. Pure function — no file I/O, no tool access.
- `c7d346b` — Distiller rebuild c4: deterministic writer for learn records (252 LOC, `distiller/writer.py` new). `write_learn_record(spec, body_draft, shadow_root=None)`. Shadow mode bypasses `vault_create` entirely and writes directly to `shadow_root/TYPE_DIRECTORY[spec.type]/slug.md` (reason: `vault_create` does template loading, near-match checks, base-embed injection, wikilink validation — all assume real vault layout and would fail under a bare shadow tree). BEGIN_INFERRED attribution wrapping preserved. Live mode opts into `vault_create(scope="distiller")` from Week 0's `3bd0678`.
- `e7b4ae2` — Distiller rebuild c5: daemon dispatch + feature flag + shadow path (122 LOC, `distiller/daemon.py` + `config.py`). New `ExtractionConfig.use_deterministic_v2: bool = False` gates the v2 dispatch; when ON and source matches `v2_types: list[str] = ["assumption"]`, routes through extractor + writer in parallel with legacy. New `extraction.shadow_root: str = "data/shadow/distiller"`. `AnthropicConfig` section added for API key / model / max_tokens.
- `45927d9` — Distiller rebuild c6: smoke_contract_parity.py (156 LOC new). Imports both `distiller.contracts` and `vault.schema`, asserts Pydantic type literals match `LEARN_TYPES`, status validators match `STATUS_BY_TYPE[type]`, writer's type→directory resolution matches `TYPE_DIRECTORY`. Exits 0 on parity.

**Total: ~1007 LOC across 6 commits. Branch HEAD: `45927d9`.** Current working tree still on `rebuild/distiller` at end of session.

## Validation

- Every commit preceded by `git branch --show-current` confirming `rebuild/distiller`
- Module-import smoke for all six new modules: `[OK]`
- `scripts/smoke_contract_parity.py` exit 0: 5 types, 18 type/status pairs, 5/5 directory entries all aligned
- Feature flag default OFF confirmed by reading config.py change + tracing the daemon dispatch
- Zero Anthropic API calls during build — `ANTHROPIC_API_KEY` untouched
- Zero live-vault writes — shadow mode only writes to `data/shadow/distiller/` which is already covered by `.gitignore` line 16 (`data/`)

## Outcome

**Shadow pipeline ready. Live distiller continues on legacy path.** The daemons running right now are on master code (no restart happened); they would pick up the rebuild code only on next natural restart when working tree is on `rebuild/distiller` AND the operator has flipped `use_deterministic_v2: true` in config.yaml.

## Week 2 operator steps (documented in `project_distiller_rebuild.md`)

1. Set `ANTHROPIC_API_KEY` (or `distiller.anthropic.api_key` in config.yaml)
2. Flip `extraction.use_deterministic_v2: true`
3. Restart distiller (`alfred down && alfred up`, or just the distiller module)
4. Legacy path keeps running; v2 runs in parallel on `assumption/`-type sources only
5. Next 03:30 Halifax deep-extraction window fires both
6. Compare `data/shadow/distiller/assumption/*.md` (v2) against new `vault/assumption/*.md` entries (legacy)
7. Hand-rate 3-5 disagreement samples (~30-45 min operator time)
8. Signals: parse-success rate ≥95%, learning-set similarity, subprocess-time reduction, cost reduction

**Rollback path if thesis fails**: `git checkout master && git branch -D rebuild/distiller`. Vault untouched because shadow mode never writes to live.

## Alfred Learnings

- **Pattern validated**: branch discipline for experimental foundation work. Week 0 went to master (cheap wins, independent value), Week 1+ on a branch so rollback is a one-command operation. Mental model: master = what production runs; branches = what we're testing. The feature flag is belt + suspenders on top of the branch.
- **Pattern validated**: shadow mode with parallel-run is the right evaluation harness for architectural changes that could affect vault contents. Can't break what we don't write to.
- **Architectural insight**: `write_learn_record`'s shadow mode couldn't simply redirect `vault_create` — it had to bypass it. `vault_create` does template loading, near-match detection, base-embed injection, wikilink validation that all assume real vault layout. The writer re-uses `frontmatter.Post` + `_assemble_frontmatter` so shape stays identical without inheriting those dependencies. Reusable pattern for any future shadow/dry-run mode.
- **Pattern validated**: "LLM as pure inspector, Python as writer" boundary. The extractor has NO tools — it's a pure function from (source_text, frontmatter, existing_titles, signals) → `ExtractionResult`. Pydantic validates the shape; Python owns all file I/O. If this proves out in Week 2, same pattern applies to drafter.py (Week 3), janitor enricher (Week 4+), and opportunistically to surveyor labeler.
- **Gotcha**: builder's estimate on c3 (extractor) and c4 (writer) both came in near the 300-LOC stop-line. Not a scope-creep violation — each has justified complexity (c3 owns both prompts + validation + retry; c4 owns shadow + live + slugify + frontmatter assembly). For future rebuild arcs, budget 200-300 LOC per LLM-boundary module, not 100.
- **Deviation worth noting**: `v2_types` was spec'd as hardcoded `{"assumption"}` but builder made it config-driven `list[str] = ["assumption"]` with no code change needed to widen blast radius in Week 2. One line of flexibility, zero cost, operator-friendly.

## Related memory

- `project_distiller_rebuild.md` — active rebuild plan, Week 0 + Week 1 status, Week 2 operator steps
- `feedback_architectural_debate_pattern.md` — two-team debate that produced this plan
- `docs/proposals/distiller-rebuild-team1-stabilize.md` — Team 1 proposal (committed master)
- `docs/proposals/distiller-rebuild-team2-rebuild.md` — Team 2 proposal (committed master)
