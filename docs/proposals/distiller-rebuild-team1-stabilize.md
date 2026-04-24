# TEAM 1 PROPOSAL — STABILIZE IN PLACE

## 1. Original intent

**Distiller.** The designer's intent, reconstructed from `src/alfred/distiller/pipeline.py` and `src/alfred/_bundled/skills/vault-distiller/SKILL.md`, is a three-stage mining operation over existing vault records:

- **Stage 1 (per-source, LLM):** Read one operational record (`session`, `note`, `conversation`, `task`, `project`) and emit a JSON manifest of candidate learnings — no writes to vault.
- **Stage 2 (pure Python):** Fuzzy-dedup candidates across sources and against the existing learn corpus using the Simpson overlap coefficient on normalized titles (`pipeline.py:164-176`).
- **Stage 3 (per-learning, LLM):** For each surviving candidate, create exactly one `assumption/`, `decision/`, `constraint/`, `contradiction/`, or `synthesis/` record via `alfred vault create --body-stdin`.

There's also a **Pass B** meta-analysis (`pipeline.py:906`) that clusters existing learnings by project or type and looks for contradictions/syntheses, and a **consolidation sweep** (`pipeline.py:1135`) that runs weekly.

The commit history confirms the design intent: `73eea83 Replace monolithic distiller with multi-stage pipeline + meta-analysis` was the move *away* from "one LLM call to rule them all" and *toward* separation of extraction (LLM judgment) from dedup (deterministic Python) from record authoring (LLM again, but one record per call). The `50b141e Per-worker dashboard feeds, manifest retry loops, entity relevance filtering, template preservation` commit added the retry loop that's producing the warnings today.

**Janitor.** From `src/alfred/janitor/pipeline.py` and `src/alfred/_bundled/skills/vault-janitor/SKILL.md` v2.0, the intent is a structural-issue fixer also organised in three stages:

- **Stage 1 — AUTOFIX (pure Python):** Deterministic fixes for `FM001-FM004`, `DIR001`, `ORPHAN001`, `DUP001`, `SEM001-004` (type corrections, status corrections, list-coercion, directory moves; otherwise `janitor_note` with a stable issue-code prefix). Implemented in `janitor/autofix.py`.
- **Stage 2 — LINK REPAIR:** For `LINK001` (broken wikilink), try an unambiguous Python fix first (`pipeline.py:323`), else fall through to an LLM call with candidate shortlist. Capped at 15 LLM calls per sweep (`MAX_ISSUES_PER_SWEEP`, `pipeline.py:276`) after a runaway sweep burned several hundred dollars on 374 link issues.
- **Stage 3 — ENRICH (LLM):** For `STUB001` (thin records missing `description`, `role`, `email`, etc.), fill fields under the `janitor_enrich` scope with its own allowlist and body-write permission. Stubs that go stale on content-hash collision get deferred (`pipeline.py:526-544`).

The root `CLAUDE.md` documents the scope contract (`CLAUDE.md:55-58`): curator can create/edit but not delete; janitor can edit/delete but not create (except triage tasks); distiller can only create learn types. The key design shift visible in `git log` is commits `2d5e8cf Janitor scope: narrow edit allowlist and split Stage 3 into janitor_enrich` and `2b8ddbd Option E Q3: close body-write loophole in janitor scope` — the field allowlist + body-write gate now lives in `src/alfred/vault/scope.py:15-173`. The `project_janitor_scope_creep.md` memory's "Fix options — Option 1" was **shipped**.

The intent for both tools is coherent: **the LLM does judgment work; Python does structural work; scope.py is the wall between them.**

## 2. Current failure catalogue

### F1 — `pipeline.manifest_parse_failed` + `s1_manifest_retry` storm

**Evidence.** Cleaned log counts (ANSI stripped):

- `pipeline.manifest_parse_failed`: **1194**
- `pipeline.s1_manifest_retry`: **835** (attempt=1: 448, attempt=2: 387)
- `pipeline.s1_manifest_file_missing`: **320**
- `pipeline.s1_manifest_from_file`: **252**
- `pipeline.s1_manifest_from_stdout`: **0**
- `pipeline.s1_complete`: **654** total — **362 with `learnings=0`**, 254 with ≥1 learning
- `claude.nonzero_exit` / `pipeline.llm_failed`: **512 Stage-3 failures** (mostly after s3_no_record_created)
- Distribution of `stdout_len` on parse failures: min=13, median=247, max=1522, with **327 occurrences at exactly 13 chars**

**Path in code.** `distiller/pipeline.py:420` generates a per-source temp manifest path (`/tmp/alfred-distiller-<uuid>-manifest.json`), interpolates it into the Stage 1 prompt (`pipeline.py:437`), invokes the Claude subprocess (`pipeline.py:453`), then tries to read the file (`pipeline.py:456-476`). If that fails, it attempts to parse the subprocess stdout with `_parse_extraction_manifest` (`pipeline.py:214-244`), which looks for `{"learnings": [...]}` with depth-matched braces.

**What's actually going wrong.** Three distinct sub-failures coexist under this one symptom:

- **F1a — agent skips the manifest file entirely.** When the source record is short and contains no real learnings (the top failing file is `Netfirms Domain Expiry Warning annapolisvalleydreamhome.ca 2026-04-12.md` — a 127-line notification email), the agent returns a terse prose reply ("No significant learnings found in this record.") and never executes the `cat > /tmp/...` heredoc. stdout_len=13 is consistent with a short prose summary. The primary file-read path fails (`s1_manifest_file_missing` +320), the stdout parse fails (never logs `s1_manifest_from_stdout` because stdout has no JSON), retry fires, retry also fails, 3 attempts burned, `s1_complete learnings=0`.
- **F1b — "(no output)" short-circuits.** When `_call_llm` returns "(no output)" on exit code 1 (`pipeline.py:283`), the stdout fallback parser runs anyway and logs `manifest_parse_failed stdout_len=13`. 327/1194 failures are this exact case.
- **F1c — no repair pass.** `_parse_extraction_manifest` (`pipeline.py:214-244`) is strict JSON: balanced braces + `json.loads`. If the agent emits trailing prose, unclosed braces, or JSON wrapped in an unterminated markdown fence, there is **no repair pass** — the strict parser returns empty and retries. No code fence stripping, no brace completion, no `json5` fallback.

**Cost.** Every failure triggers a full retry (each `_call_llm` is a subprocess dispatch to `claude -p` with a 600s timeout). Three attempts × 362 zero-result sources = 1086 needless LLM calls in the retry path. Plus the `Stage 3 Exit code 1: ` failures (512) — same timeout envelope.

### F2 — Janitor scope creep — **already fixed, but verify**

The memory `project_janitor_scope_creep.md` (5 days old) reports the janitor rewriting `alfred_tags` beyond its mandate. The code today says this is resolved:

- `src/alfred/vault/scope.py:42-52` — `janitor` edit permission is `"field_allowlist"` with a concrete set (`janitor_note`, `type`, `status`, `name`, `subject`, `created`, `related`, `tags`, `alfred_triage*`, `candidates`, `priority`). Note `alfred_tags` is NOT on the allowlist — write would be rejected by `check_scope`.
- `src/alfred/vault/scope.py:62` — `allow_body_writes: False` closes the Q3 body-write loophole.
- `src/alfred/vault/scope.py:69-86` — separate `janitor_enrich` scope with its own allowlist (`description`, `role`, `org`, `email`, `aliases`, `website`, `phone`, `org_type`, `related`, `tags`) for Stage 3 only.
- `src/alfred/vault/cli.py:202-221` — `cmd_edit` computes `fields_list` from `--set`/`--append` and passes to `check_scope` with `body_write` gate.
- `src/alfred/vault/cli.py:140-161` — `cmd_create` mirrors for `triage_tasks_only`.

**Residual risk (F2a).** Scope is checked *only at the `alfred vault` CLI entry points* (`vault/cli.py`). The direct `vault_edit()` / `vault_create()` / `vault_delete()` functions in `vault/ops.py` do NOT invoke `check_scope`. Daemon code imports `vault_edit` directly in:

- `distiller/pipeline.py:529-533` (saves `distiller_signals`)
- `distiller/pipeline.py:1076-1080` (saves `distiller_learnings`)
- `janitor/pipeline.py:266` (direct `full_path.write_text` for link repair)
- `janitor/autofix.py` (deterministic autofixes)

These callers are in-process and can write any field. If a future refactor or new feature adds a path that routes LLM-proposed field names into one of these callers, scope is not a safety net. The safeguard today is code review.

### F3 — Stage 3 "(no output)" storms in the distiller

**Evidence.** `pipeline.llm_failed stage=s3-...` events: 512 instances with `summary='Exit code 1: '`. These are Stage 3 `vault create` calls that the `claude` subprocess failed to complete. The enriched error log at `pipeline.py:276-292` was added exactly because "the backend's summary alone reads `Exit code 1:`" — the designer already knew this was a recurring pain point. The symptom is real; the diagnostic is thin.

### F4 — SKILL/code drift hazard (structural, not live)

The root `CLAUDE.md:121-124` explicitly documents the Q3 incident: after scope narrowing on 2026-04-19 (commit `2b8ddbd`), the SKILL's `STUB001 "flesh out body"` step stayed dead for ~24h until the Q2 SKILL update caught it. The janitor SKILL v2.0 today (`vault-janitor/SKILL.md:483-485`) does reroute STUB001 to the pipeline, and all the FMxxx/DIR001/ORPHAN001 codes are marked "handled by structural scanner via deterministic flagging in `autofix.py`." So the specific Q3 drift is fixed.

**Residual risk:** every scope/schema tightening creates this hazard. Today's process is a human lifecycle rule (team-lead rule in CLAUDE.md). There is no mechanical contract check that a SKILL.md line referencing `--body-stdin` on the janitor scope would fail with `ScopeError`. A CI-style smoke run would have caught the Q3 dead step in under a minute.

### F5 — Retry-attempt cost amplification

`pipeline.py:445` hardcodes `max_attempts = 3` for Stage 1 manifests. Three identical prompts, identical temperature, identical context → three nearly-identical outputs. The only thing that changes between attempts is model sampling variance, which on a short "no learnings here" record is zero signal. 835 retries × ~30-40s per Claude call = **~8 hours of wasted subprocess time** in the observed window.

### F6 — Minor: distiller `_parse_extraction_manifest` regex is brittle

`pipeline.py:217` uses `r'\{[^{}]*"learnings"\s*:\s*\[`. The `[^{}]*` is there to avoid matching a top-level `{` before the key — but if the agent prefixes the JSON with a nested object (e.g. `{"meta": {...}, "learnings": [...]}`), the regex bails because there are braces between `{` and `"learnings"`. Any well-meaning wrapper by the agent breaks the regex.

## 3. Root cause analysis

| # | Failure | Root cause | Category |
|---|---------|------------|----------|
| F1a | Agent skips manifest file | **Contract issue.** The SKILL says "CRITICAL: You MUST write the JSON manifest file" but Claude-the-agent, running under an agentic backend, has latitude. When the record is short and vacuous, the agent's internal policy to "not output empty content" overrides the instruction. The Python pipeline treats a no-learnings outcome and a "agent refused to comply" outcome identically — both just become `s1_complete learnings=0`. | Contract + prompt |
| F1b | `"(no output)"` → `manifest_parse_failed` | **Bug.** `_call_llm` returns the literal string `"(no output)"` on a subprocess failure (13 chars), and the calling code does not short-circuit — it continues to the stdout parser, which (correctly) reports `manifest_parse_failed`. This inflates the failure count without adding signal. | Bug |
| F1c | Strict JSON parser | **Structural.** The parser assumes the agent emits clean JSON. In practice even GPT-class models commonly wrap JSON in ```json fences, append explanatory prose, or (after reflection) correct the JSON to a different valid JSON that doesn't match the regex anchor. | Structural (parser) |
| F2 | Janitor scope creep | **Already fixed.** Field allowlist + body-write gate closed the original complaint. | n/a (resolved) |
| F2a | `ops.py` not scope-enforced | **Structural.** Defense-in-depth is missing on the second layer. Today, only the CLI is gated. | Structural (architecture) |
| F3 | Stage 3 "Exit code 1" | **Bug cluster.** The claude CLI sometimes errors (rate limit, auth retry, connection reset). No retry on Stage 3 (`pipeline.py:736`) — unlike Stage 1 which has 3 attempts. One transient failure = one lost learning record. | Bug |
| F4 | SKILL/code drift | **Process issue with mechanical remedy available.** A scope-contract smoke test would eliminate the 24h window. | Process + missing test |
| F5 | Retry identical prompt | **Structural.** The retry loop doesn't vary anything between attempts. Tokens and wall time are spent to re-sample the same distribution. | Structural |
| F6 | Brittle regex | **Bug.** `[^{}]*` is too restrictive. | Bug |

None of these require an architecture rebuild. Every one is a short patch against the existing flow.

## 4. Proposed changes, grouped by size

### Surgical (<50 LOC each)

**S1 — Short-circuit "(no output)" in distiller `_call_llm`.** Files: `src/alfred/distiller/pipeline.py`. Change: When `_call_llm` hits the `detail = "(no output)"` branch at `pipeline.py:283`, return a sentinel (`""` or `None`) and have the caller treat an empty stdout as "no data, don't bother parsing" instead of running `_parse_extraction_manifest(stdout="")`. Prevents: ~327 spurious `manifest_parse_failed stdout_len=13` warnings. ~5 lines. Risk: trivial.

**S2 — Lenient JSON repair in `_parse_extraction_manifest`.** Files: `src/alfred/distiller/pipeline.py:214-244`. Change: Before the strict JSON loop, strip a fenced block if present. Then try: (a) the existing brace-match loop; (b) the whole-text parse; (c) a "find the first `[` after `"learnings"`" — extract a JSON array by balanced brackets and coerce to `{"learnings": [...]}`. Optional: a naive trailing-comma stripper. Prevents F1c. ~30-40 lines. Risk: low.

**S3 — Fix the `_parse_extraction_manifest` regex.** Files: `src/alfred/distiller/pipeline.py:217`. Change: Replace `r'\{[^{}]*"learnings"\s*:\s*\[` with a scan that finds `"learnings"\s*:\s*\[` and walks *backwards* to the enclosing `{`. ~20 lines. Risk: low.

**S4 — Skip Stage 1 for records with zero extraction signals.** Files: `src/alfred/distiller/pipeline.py:402-502`. Change: Before invoking `_call_llm`, check `source.signals`: if decision_keywords == assumption_keywords == constraint_keywords == contradiction_keywords == 0 AND has_outcome == has_context == False AND body_length < 500, skip the LLM call entirely, log `pipeline.s1_skipped reason=no_signals`, return `[]`. Prevents ~50% of the F1 retry storm. ~15 lines. Risk: medium — pair with `min_signals: int = 1` config knob.

**S5 — Treat "(no output)" as a non-retryable outcome.** Files: `src/alfred/distiller/pipeline.py:448-497`. Change: If three attempts all produce stdout of length < 20 AND no manifest file was created, stop retrying. Prevents F5. ~10 lines. Risk: low.

**S6 — Retry Stage 3 once on `Exit code 1: (empty)`.** Files: `src/alfred/distiller/pipeline.py:671-763`. Change: Wrap the single `_call_llm` in `_stage3_create` in a retry-once loop gated on `""` stdout / subprocess non-zero. Prevents F3 transient. ~15 lines. Risk: low.

**S7 — Push `check_scope` into `vault/ops.py` write functions as an optional gate.** Files: `src/alfred/vault/ops.py`. Change: Add optional `scope: str | None = None` kwargs to `vault_create`, `vault_edit`, `vault_move`, `vault_delete`. When provided, call `check_scope` before doing the work. Strictly additive. Prevents F2a. ~25 lines. Risk: low-medium.

**S8 — Add a SKILL contract smoke test.** Files: `scripts/smoke_janitor_scope.sh` (exists), add `scripts/smoke_distiller_scope.sh`. Change: For each SKILL, grep for `alfred vault` subcommands and flags, assert via a shell call that the scope allows it. No pytest dependency. Prevents F4 (24h drift windows). ~30-60 lines. Risk: low.

### Medium (50-500 LOC)

**M1 — Give the distiller agent a schema-validated-tool interface instead of "write JSON to a file."** Files: `src/alfred/distiller/pipeline.py`, new `src/alfred/distiller/cli.py` subcommand `alfred distiller emit-candidate`, updated `stage1_extract.md`. Change: Instead of instructing the agent to `cat > /tmp/...-manifest.json`, instruct it to call `alfred distiller emit-candidate --type decision --title '...' --confidence high --claim '...' --evidence '...' --source-link '[[...]]'` once per learning. The CLI validates each call and writes to the same per-session JSONL file. Pipeline reads the JSONL at the end of the Stage 1 call. Prevents F1a, F1c, F6. Matches the architectural pattern already used for `alfred vault`. ~200-300 LOC total. Risk: medium.

**M2 — Strict output validator after every LLM-authored vault write.** Files: `src/alfred/distiller/pipeline.py`, `src/alfred/janitor/pipeline.py`, new `src/alfred/vault/post_write_audit.py`. Change: After Stage 3 `_call_llm` returns, resolve the newly-created record, validate its frontmatter against `schema.py`'s `KNOWN_TYPES`/`REQUIRED_FIELDS`/`LIST_FIELDS`. If invalid: log, flag a triage task, do NOT add the broken link to `distiller_learnings`. Same for janitor Stage 3 enrichment. ~200-400 LOC. Risk: medium.

### Large (>500 LOC)

*We don't propose anything in this band.* The stabilization thesis is that the failures above are either cheap-to-fix local bugs or contract tightenings that stay inside the existing pattern.

## 5. Migration path

Order the surgical patches safety-dominated-first, then fidelity-improving, then contract hardening:

1. **S1 + S3 + S6** together as one commit. Smoke: tail `data/alfred.log` for 24h, confirm `manifest_parse_failed stdout_len=13` drops to zero.
2. **S2** separately so a regression can be bisected. Smoke: grep for `s1_manifest_from_stdout`.
3. **S4 + S5** behind a config knob `distiller.extraction.skip_no_signal_sources: true`. Start conservative.
4. **S7** additive kwarg, `None` default. Then opt in on specific callers.
5. **S8** (smoke scripts) before M1. The smoke test catches regressions from M1.
6. **M1** (structured emit-candidate CLI) gate behind feature flag. Dual-path observation for a week. Then remove legacy.
7. **M2** (post-write validator) — landable independently.

## 6. What this addresses and what it doesn't

**Addresses:** F1a (partially via S4/S5, fully via M1), F1b (fully via S1), F1c (fully via S2+S3+M1), F3 (partially via S6), F2a (fully via S7), F4 (fully via S8 for structural), F5 (fully via S5), F6 (fully via S3).

**Does NOT address:** Agent writes prose instead of data structurally; cost of subprocess-per-stage; consolidation sweep quality; janitor Stage 2 LLM-path quality; Pass B meta-analysis quality.

## 7. Tradeoffs vs a rewrite

**Affirmative case for stabilization.**
1. The architecture is not the source of the instability. Scope.py is good. The three-stage pipeline is good. The failures are concentrated in a handful of parser/contract/retry sites, all under 50 lines each.
2. The invariants are already encoded in code that works. `SCOPE_RULES` took iterations to get right.
3. Vault data at risk. Thousands of records with distiller_learnings, distiller_signals, janitor_note frontmatter.
4. Evidence of recent successful stabilization: `3a21e21`, `2d5e8cf`, `4701e56` — shipped-and-forgotten.
5. Small patches are evaluable. Each S-patch can be shipped, observed for 24h, reverted cleanly.

**Steelman for the rewrite.**
1. "Agent writes to stdout, Python parses" is inherently brittle.
2. Retry/cost dynamics have been papered over repeatedly. Each cap is a symptom.
3. 2165 failures since Apr 15 — not a bug, a design that doesn't fit the agent.
4. Subprocess-per-stage is the wrong unit of work.

**Why we still prefer stabilize.** The rewrite case has merit for distiller Stage 1 specifically, but janitor's structural fixes are already deterministic Python and the LLM has narrow caps. Janitor is not the shaky part. For distiller, M1 IS a partial rewrite — of one method's contract, not the architecture. 80% of the rewrite's benefit at 10% of the risk.

## 8. Open questions

1. Is the distiller Stage 1 yield (38.5%) acceptable once noise classes are excluded?
2. Do current distiller-produced learn records contain malformed frontmatter or orphan links?
3. The "skip zero-signal sources" threshold (S4) — any cases where a near-empty record had a real learning?
4. Stage 3 `Exit code 1` root cause — local rate limit? OAuth expiry? Needs targeted log capture.
5. Should `vault_edit` call `check_scope` by default once S7 lands?
6. Any callers currently rely on the absence of scope enforcement?
7. Does OpenClaw obey the manifest-writing contract at the same rate as Claude?

### Critical Files
- /home/andrew/alfred/src/alfred/distiller/pipeline.py
- /home/andrew/alfred/src/alfred/vault/scope.py
- /home/andrew/alfred/src/alfred/vault/ops.py
- /home/andrew/alfred/src/alfred/vault/cli.py
- /home/andrew/alfred/src/alfred/_bundled/skills/vault-distiller/prompts/stage1_extract.md
