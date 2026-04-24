# TEAM 2 PROPOSAL — DETERMINISTIC-FIRST REBUILD of Distiller & Janitor

## 1. Original intent

### Distiller

Canonical intent from CLAUDE.md:7-14 ("extracts latent knowledge — assumptions, decisions, constraints, contradictions, syntheses — from operational records") and the initial pipeline commit (`73eea83`, "Replace monolithic distiller with multi-stage pipeline + meta-analysis").

Two concerns stacked:
1. **Pass A — per-record extraction.** Read one operational record, identify latent learnings, deduplicate, create new learn-type records. SKILL at `src/alfred/_bundled/skills/vault-distiller/SKILL.md:9-13` is emphatic: distiller "read[s]" and "create[s]"; does NOT modify source records.
2. **Pass B — cross-learning meta-analysis.** Cluster existing learnings, look for contradictions or syntheses, create higher-order records.

Pipeline is already three stages (`distiller/pipeline.py:1-11`): Stage 1 EXTRACT (LLM per-source, JSON manifest), Stage 2 DEDUP+MERGE (**pure Python**, fuzzy title overlap, `pipeline.py:548-663`), Stage 3 CREATE (LLM per-learning).

Scope narrow: `scope.py:87-99`. Distiller may create only `LEARN_TYPES`, may edit only `distiller_signals`/`distiller_learnings`.

### Janitor

CLAUDE.md:12: "scans vault for structural issues (broken links, invalid frontmatter, orphans) and fixes them." Three-stage commit (`52cd52a`, 2026-02-23):
- **Stage 1 AUTOFIX** (`janitor/autofix.py`) — pure-Python for FM001–004
- **Stage 2 LINK REPAIR** — unambiguous rewrites Python; ambiguous to LLM
- **Stage 3 ENRICH** — LLM fills `description`/`role`/etc. on stubs

Option E arc (`3a21e21` + `8d3d33e` + `9ee94b5`) moved SEM001-004 and learn-type DUP001 into `_flag_issue`. Scope hardened in `2d5e8cf` to field allowlist + separate `janitor_enrich` scope.

### What this tells us about intent

The designer's trajectory is unmistakable: **start with LLM-everywhere, walk it back issue code by issue code whenever a failure forces the lesson.** Every commit since 52cd52a has moved responsibility *out of* the LLM and *into* deterministic Python. SKILL files are now majority "Handled by the structural scanner via deterministic flagging in `autofix.py`. You should not see this code in your issue report; if you do, log a warning and proceed" (`SKILL.md:453-481`).

Intent was: *LLM as cleanup crew for everything vault-structural.* Actual current state: *LLM does two narrow jobs (broken-wikilink disambiguation when ambiguous, stub body enrichment) plus distillation, and we are in the process of hollowing out the rest.* The architecture has been migrating toward deterministic-first without anyone writing down that this is the destination.

## 2. What went wrong with the LLM-everywhere approach

### 2.1 The Stage 1 extraction manifest is the loudest failure

From `data/distiller.log`:
- 2178 Stage 1 LLM calls
- 1274 `pipeline.manifest_parse_failed` — **58% failure rate on primary output format**
- 891 `pipeline.s1_manifest_retry`
- 400 `pipeline.llm_failed` (non-zero exit)
- Only 268 `pipeline.s1_manifest_from_file` — designed happy path
- 414 `pipeline.s3_created` — roughly 20% of LLM calls result in a created record

Design asks LLM to `cat > /tmp/alfred-distiller-{uuid}-manifest.json << 'MANIFEST_EOF'` (`stage1_extract.md:82-87`). Falls back to scraping stdout via regex (`pipeline.py:214-244`), retries 3× (`pipeline.py:445-497`).

**The structural problem:** The distiller is using an LLM as a structured-output generator. Structured output from an LLM over subprocess stdout is fundamentally unreliable — OpenClaw's JSON envelope mixes agent reasoning, tool call logs, and the intended payload. The retry loop is evidence the LLM and the parser cannot agree on a contract. A deterministic tool would simply return the data.

### 2.2 Janitor scope creep (confirmed by the vault itself)

Memory `project_janitor_scope_creep.md` documented it; the narrow-scope fix shipped in `2d5e8cf`. But the vault carries the evidence. `grep -c "janitor_note" vault/**/*.md → 1167 files`. Sampling: a huge fraction is `"LINK001 — scanner false positive, _bases/account.base exists"` (see `Borrowell Credit Monitoring.md:8`, `AppleCare Plus.md:11`, `RBC Royal Bank Account.md:8`). But the scanner *already skips `.base` embeds*: `scanner.py:261`. These janitor_notes are fossils from an LLM-era bug — the LLM authored them, the scanner got fixed, and now the vault is polluted with thousands of obsolete notes that no code can confidently clean up because Python didn't write them.

This is a cost the LLM-everywhere era imposed that won't be paid back by any narrow-scope fix. Accumulated technical debt that rusts the vault surface.

### 2.3 Opaque decisions, no replay

Every janitor agent call is a new subprocess with a multi-thousand-token prompt. `janitor.log`: 1197 `sweep.agent_invoke`, 117 failures. When one fails, you get stdout `summary[:500]`. You cannot replay it. You cannot run the same scanner output through a different implementation and compare. You cannot test it offline. A pure-Python autofix for FM001 is 8 lines at `autofix.py:291-338`. A pytest for it takes 10 lines. The LLM equivalent took a subprocess, a prompt, a config, a scope check, three fallback parsers, and produces prose we then parse.

### 2.4 Cost and latency are structural

`pipeline.py:276` (`MAX_ISSUES_PER_SWEEP = 15`) exists specifically because "observed 374 link issues in a single runaway sweep burning hundreds of dollars." The defense — caps — is a symptom: LLM-everywhere doesn't scale with vault size. A `rglob` pass in Python handles 374 files in milliseconds.

### 2.5 Scope is enforced, but enforcement is the wrong layer

`scope.py::check_scope` runs inside `vault ops`. The agent composes intent, calls `alfred vault edit`, THEN scope decides to reject. The agent still spent tokens thinking about a forbidden action. The janitor SKILL's lengthy rule-list (`SKILL.md:15-30` "What You MUST NOT Do") exists precisely because scope-enforced-at-edge is not the same as scope-enforced-by-not-giving-the-job. The correct posture for structural work: don't ask the LLM at all.

### 2.6 Dedup-by-title-overlap already won the bet

Stage 2 (`pipeline.py:548-663`) is already pure Python. ~100 lines of fuzzy matching. Quietest, fastest, most reproducible stage. The loud, flaky stages are the LLM bookends.

## 3. Responsibility split — deterministic vs LLM

The bar: **if a task has a clear algorithm, code owns it. LLM only when the task genuinely requires reading prose and forming a judgment that cannot be expressed as rules.**

### Janitor — current duties → proposed home

| Current duty | Proposed home | Why |
|---|---|---|
| FM001 MISSING_REQUIRED_FIELD (`autofix.py:291-338`) | Deterministic — already is | Infer from directory, mtime, filename |
| FM002 INVALID_TYPE_VALUE | Deterministic — already is | Type-alias table |
| FM003 INVALID_STATUS_VALUE | Deterministic — already is | Status-alias table |
| FM004 INVALID_FIELD_TYPE | Deterministic — already is | Scalar→list coercion |
| DIR001 WRONG_DIRECTORY | **Deterministic write, not just flag** | Correct dir is known. Do the move. |
| LINK001 unambiguous | Deterministic — already is | Exact-stem match → rewrite |
| LINK001 ambiguous | **Deterministic flag + triage queue** | Picking between "Acme Corp" vs "Acme Corporation" is *identity judgment*; don't guess — defer to operator-merge |
| LINK002 UNLINKED_BODY_ENTITY | Deterministic — already is | Promote body wikilinks to `related:` |
| ORPHAN001 | Deterministic flag — already is | Graph math |
| STUB001 enrichment | **LLM, tightly contracted** | Writing prose description is genuine synthesis |
| DUP001 entity types | Deterministic flag + triage — already is | Mechanical; emit triage |
| DUP001 learn types | Deterministic flag — already is | Never merge |
| SEM001-004 stale-status | Deterministic flag — already is | Pure date math |
| SEM005-006 (vague/semantic-dup) | **Deterministic or drop** | Scanner signals cover 90%. LLM "vagueness" is low-value and sweep-unstable. Drop. |
| Entity merge execution | Deterministic — already is | Mechanical wikilink retarget |

**Result: of 15 live issue codes, exactly one (STUB001 enrichment) needs an LLM.** Everything else is pure algorithm.

### Distiller — current duties → proposed home

| Current duty | Proposed home | Why |
|---|---|---|
| Candidate selection (keyword scoring) | Deterministic — already is | Regex counting |
| Stage 1 EXTRACT — identify learnings | **LLM — required** | Genuine semantic reading |
| Stage 1 output JSON manifest | **Deterministic (tool-call or schema)** | Where the 58% failure lives |
| Stage 2 DEDUP+MERGE | Deterministic — already is | Fuzzy title overlap |
| Stage 3 CREATE record file | **Deterministic — not currently** | Frontmatter schema, directory, filename, wikilinks are all mechanical given Stage 1 output |
| Stage 3 BODY prose | **LLM — required** | Free-form prose synthesis |
| `distiller_signals` writeback | Deterministic — already is | Just formats signal tuple |
| `distiller_learnings` writeback | Deterministic — already is | Just a list of wikilinks |
| Attribution audit wrap | Deterministic — already is | Wraps body with marker |
| Pass B clustering | Deterministic — already is | Group-by-project, count threshold |
| Pass B per-cluster LLM analysis | **LLM — with deterministic record write** | Semantic reasoning is LLM work; the *record* should be Python-written |
| Consolidation sweep | **Split**: deterministic duplicate detection + LLM for supersede judgment | Hybrid |

Distiller has a cleaner split. Keep LLM where it reads prose and judges. Remove LLM from every path where it merely formats/writes.

## 4. Proposed architecture

Treat LLMs as **inspectors** and Python as the **writer**. LLM is never given a file-writing tool. Returns structured data; Python writes files.

### 4.1 Module layout

```
src/alfred/distiller/
  scanner.py          (new) — candidate scoring, signal extraction
  extractor.py        (new) — LLM-as-service: record → LearningCandidate[]
  merger.py           (new) — deterministic dedup
  writer.py           (new) — deterministic record writer
  drafter.py          (new) — LLM-as-service: LearningSpec → BodyDraft (prose only)
  meta_analyzer.py    (new) — LLM-as-service for Pass B
  daemon.py           (slim) — orchestrator; no LLM inline
  contracts.py        (new) — Pydantic models for every LLM input+output

src/alfred/janitor/
  scanner.py          (existing)
  autofix.py          (existing) — stays
  link_resolver.py    (new) — broken wikilink → ResolverDecision (no LLM)
  enricher.py         (new) — LLM-as-service for stub descriptions
  writer.py           (new) — applies StubEnrichment; narrow field set
  merge.py            (existing)
  daemon.py           (slim)
```

### 4.2 Data flow — distiller

```python
candidates = scan(vault)                        # pure Python
for candidate in candidates:
    # LLM boundary #1 — narrow contract
    extraction = extractor.extract(             # returns ExtractionResult
        source_body=candidate.record.body[:4000],
        source_frontmatter=candidate.record.frontmatter,
        existing_learn_titles=...,
        signals=candidate.signals,
    )
    # LLM is blocked from ANY file I/O — it has no tools
    manifests[candidate.record.rel_path] = extraction.learnings

specs = merger.dedup_and_merge(manifests, existing_learns)  # pure Python

for spec in specs:
    # LLM boundary #2 — narrow contract, prose only
    body_draft = drafter.draft(                  # returns BodyDraft
        learn_type=spec.learn_type,
        title=spec.title,
        claim=spec.claim,
        evidence=spec.evidence_excerpts,
        links=spec.source_links + spec.entity_links,
    )
    # body_draft is just section-by-section prose — no frontmatter, no file path
    writer.write_learn_record(spec, body_draft)  # pure Python
```

### 4.3 Data flow — janitor

```python
issues = scanner.scan(vault)                     # pure Python
fixed, flagged = autofix.apply(issues)           # pure Python — grows to cover LINK001
                                                  # unambiguous case + DIR001 auto-move

for stub_issue in stub_issues_eligible_and_under_cap:
    linked_context = collect_linked_records(...)  # pure Python
    # LLM boundary — narrow contract
    enrichment = enricher.enrich(                 # returns StubEnrichment
        record_type=t,
        record_name=n,
        current_frontmatter=fm,                   # read-only to LLM
        linked_context=linked_context,
    )
    # LLM sees no files, touches no files
    writer.apply_enrichment(rel_path, enrichment)  # pure Python writes via vault_edit,
                                                   # under janitor_enrich scope
```

### 4.4 The LLM contract (the crux)

Today: LLM is a shell agent. Scope enforces rejections at the boundary.

**Redesign:** LLM is a pure function. Non-agentic Messages API call. **No tools** — no Bash, no filesystem, no `alfred vault` CLI. Sole deliverable: text that parses as a specific JSON schema. Pydantic `model_validate` decides success/failure.

### 4.5 Scope enforcement — at the source of writes, not the edge

Today: scope is a runtime check in `vault_ops` that raises if LLM-composed op is forbidden. Reactive.

Redesign: scope is a compile-time property of the writer module. `distiller/writer.py` has `write_learn_record(spec, body_draft)` that calls `vault_create` directly, passing `scope="distiller"`. The writer enforces: `spec.learn_type in LEARN_TYPES`; directory hardcoded; only fields defined on the writer's dataclass are written.

The LLM has no opportunity to write anything else because it is not invoking `alfred vault`. It returned a string; Python decided what to do with it.

### 4.6 Output validation (structured-output failure mode, closed)

Current Stage 1 re-reads stdout with regex and JSON parsers. Retry loop masks how often this fails.

Redesign: Pydantic v2's `model_validate_json` on the raw text. On `ValidationError`, retry *once* with a repair prompt that includes the validation error message. On second failure, return empty extraction, log `extractor.unrecoverable`, continue — do NOT block.

### 4.7 Pseudocode for the distiller extractor

```python
class LearningCandidate(BaseModel):
    type: Literal["assumption", "decision", "constraint", "contradiction", "synthesis"]
    title: str = Field(min_length=5, max_length=150)
    confidence: Literal["low", "medium", "high"]
    status: str  # validated against STATUS_BY_TYPE[type]
    claim: str = Field(min_length=20)
    evidence_excerpt: str = ""
    source_links: list[str] = []
    entity_links: list[str] = []
    project: str | None = None

class ExtractionResult(BaseModel):
    learnings: list[LearningCandidate]

async def extract(source_body, source_frontmatter, existing_learn_titles, signals, config):
    prompt = _render_prompt(...)
    for attempt in range(2):
        raw = await _call_llm_no_tools(prompt, config)
        try:
            return ExtractionResult.model_validate_json(raw)
        except ValidationError as e:
            if attempt == 0:
                prompt = _repair_prompt(raw, str(e))
                continue
            log.warning("extractor.validation_failed", error=str(e))
            return ExtractionResult(learnings=[])
    return ExtractionResult(learnings=[])
```

## 5. Migration path

### 5.1 Parallel-run (preferred)

Config flag `extraction.use_deterministic_v2: bool`. Daemon dispatches. Run both side by side for one week on a snapshot vault.

1. **Week 1** — ship `extractor.py`, `drafter.py`, `writer.py` for distiller behind flag. Old pipeline stays on. New pipeline read-only (writes to `vault/.distiller_shadow/`).
2. **Week 2** — compare shadow to live on sample of 20. Prompt-tune to parity. Flip flag for distiller.
3. **Week 3** — same for janitor enricher.
4. **Week 4** — delete legacy `_stage1_extract`, sidecar code, subprocess-agent wrappers on write path.

### 5.2 Record-by-record migration NOT needed

Records already in vault are valid; the issue is the *process* that wrote them. The 1167 existing `janitor_note`s can be cleaned up in a one-off sweep (§5.4) that runs after the new pipeline is live.

### 5.3 Cost & risk estimate

- LoC change: +1500 new / -800 deleted. Net ~+700.
- Config surface: +1 flag per tool.
- Schema risk: low — Pydantic models derived from `vault/schema.py`.
- LLM cost: drops substantially. 891 retries/day from manifest parse failures disappear.
- Quality risk: LLM without agentic tool access produces cleaner structured output. Cannot poke around mid-task — mitigated by providing full context up front.
- Downside: strict schema too rigid → edge cases become `learnings=[]` silently. Mitigation: log every validation failure with raw text.

### 5.4 Fossil cleanup

`scripts/purge_llm_janitor_fossils.py` — scan all `janitor_note`, match known-obsolete patterns, delete. Independent of main rebuild.

## 6. What this addresses and what it doesn't

### Addressed
- **manifest_parse_failed (80/day)**: gone. LLM returns validated JSON directly, or fails loudly once.
- **Janitor scope creep**: structurally gone. LLM holds no write tool.
- **SKILL drift**: the Q3 24h-dead-step problem can't happen because there is no instructional SKILL for the LLM to drift from — the contract is code.
- **Opaque audit**: every decision is a function call with typed inputs/outputs.
- **Runaway cost**: caps become soft because hot path doesn't enumerate all issues to an LLM.

### Not addressed
- STUB enrichment quality (still LLM-bound)
- Cross-record semantic judgment (Pass B)
- Curator (out of scope)
- Obsidian integration quirks

### New risks
- Prompt-tuning feedback loop more technical: prompt-tuner edits prompts that feed Pydantic returns, not SKILL.md
- Schema evolution: adding new learn type = Pydantic + writer + scope (one more surface)
- Loss of emergent behavior: LLM sometimes does clever things not spec'd. Accept the loss.

## 7. Tradeoffs vs stabilization

### Affirmative case for rebuild
1. **The architecture is already migrating there.** Every janitor commit in last two months moves responsibility out of LLM. Paying transition cost in increments. One-shot rebuild completes journey, frees from dual-maintenance.
2. **58% manifest parse failure is not a bug; it is a design smell.** No prompt tuning will make "structured output over subprocess stdout" reliable. Response should be to eliminate the failure surface.
3. **The vault has fossilized bad decisions.** Every obsolete janitor_note is a cost the LLM-everywhere era imposed.
4. **Debuggability.** When Python misbehaves, you read the code. When LLM misbehaves, you add a rule, hope it generalizes, deploy. Asymmetric cost per round.
5. **SKILL/scope split is a recurring contract-drift problem.** Rebuild eliminates dual contract.

### Steelman for stabilization
We have already done most of the work. Autofix owns 6+ issue codes. Stage 2 is half-deterministic. Stage 3 is narrowly scoped. Extraction-manifest is the one big remaining LLM-as-format-generator failure; fix *that* and the scorecard flips. Net: 2-3 commits, not a rebuild. Surgical fix for Stage 1: drop sidecar, non-agentic LLM, Pydantic-validated, retry once. ~150 LoC, one week.

### Why we still prefer rebuild

**The code paths that are already deterministic are not architected as deterministic-first.** `autofix.py` is a *patch* on top of an architecture that expected LLM to handle everything. `pipeline.py` is 769 lines for janitor, 1193 for distiller — mostly subprocess orchestration, mutation-log snapshot diffing, mtime guards, session-file plumbing, fallback stdout parsers. All that complexity exists *because* LLM was invited to write files. Strip the assumption and 60% of pipeline code evaporates.

Stabilization keeps scaffolding designed for LLM-everywhere even after we've hollowed out its purpose. Rebuild reshapes scaffolding to match current reality. Pay once instead of forever.

Andrew's statement — *"can't keep building on a shaky foundation"* — is about foundational posture. Stabilization treats the symptom; rebuild treats the posture.

## 8. Open questions for Andrew

1. **OpenClaw agent-mode future.** Redesign wants non-agentic LLM calls. Does OpenClaw support tool-less mode, or would this force Anthropic SDK / OpenAI-compatible endpoints?
2. **Pass B (meta-analysis) appetite.** Delivering value? If no, remove.
3. **Consolidation sweep.** Trust it? If yes, keep with structured-proposals-and-human-approves. If no, reduce to triage tasks.
4. **Curator tagalong.** Blueprint curator too, or keep in agentic regime?
5. **Fossil cleanup.** Delete 1167 obsolete janitor_notes, or leave as residue?
6. **Prompt-tuner role** under new regime. Edit prompts feeding Pydantic returns, not SKILL anti-pattern lists. Fit?
7. **Budget / timing.** 4-week rebuild vs 1-week window? 1-week version would ship distiller Stage 1 contract fix only.

### Critical Files
- /home/andrew/alfred/src/alfred/distiller/pipeline.py
- /home/andrew/alfred/src/alfred/janitor/pipeline.py
- /home/andrew/alfred/src/alfred/janitor/autofix.py
- /home/andrew/alfred/src/alfred/vault/scope.py
- /home/andrew/alfred/src/alfred/_bundled/skills/vault-distiller/prompts/stage1_extract.md
