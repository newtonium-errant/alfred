---
type: session
status: completed
name: Distiller rebuild Week 0 transition
created: 2026-04-24
description: Ship the four Week 0 transition-plumbing patches ahead of the distiller rebuild — data-loss prevention (Stage 3 retry), log-noise cleanup (no-output short-circuit), defense-in-depth (scope kwarg on vault/ops.py), and SKILL contract smoke testing (curator-only).
intent: Land cheap wins that are valuable even if the Week 1+ rebuild thesis fails. S6 (Stage 3 retry) prevents losing vault records during the transition. S1 (no-output short-circuit) removes 327/day log noise that would contaminate Week 2 shadow-vs-legacy comparison. S7 (scope kwarg) provides tripwire layer during parallel-run. S8 (curator smoke) covers the SKILL surface we're keeping agentic.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Distiller rebuild research proposals preserved 2026-04-24]]'
- '[[session/Distiller rebuild Week 1 MVP 2026-04-24]]'
tags:
- distiller
- rebuild
- week-0
- transition
- scope
- smoke-test
---

# Distiller rebuild Week 0 transition

## Intent

Both research teams (stabilize vs rebuild) independently agreed these four surgical patches should ship ahead of any rebuild arc. The rebuild replaces most of the code these patches touch, but the patches have independent value: S1 + S6 reduce log noise + data loss during the transition weeks, S7 provides defense-in-depth during parallel-run when both legacy and v2 writer paths are active, S8 covers the curator SKILL (which stays agentic permanently per the rebuild plan's out-of-scope call).

## Work Completed

Four commits on master:

- `518f90c` — Distiller Week 0 c1: short-circuit `(no output)` in `_call_llm` fallback (+13/-6). When Claude subprocess returns exit 1, the pipeline was logging `manifest_parse_failed stdout_len=13` from trying to parse the literal string `"(no output)"`. Returns empty stdout instead so the existing guard short-circuits. Removes 327 spurious parse-fail warnings/day.
- `67f7d61` — Distiller Week 0 c2: retry Stage 3 once on transient Exit code 1 (+18/-1). Stage 3 had zero retry (Stage 1 has 3). 512 lost vault records since 2026-04-15 from transient Claude-CLI failures (rate limit, auth retry, connection reset). One retry rescues most of them.
- `3bd0678` — Vault defense-in-depth: optional `scope=` kwarg on `vault_ops` write functions (+55/-3). `vault_create`, `vault_edit`, `vault_move`, `vault_delete` accept optional `scope: str | None = None`; when provided, `check_scope` runs at the ops layer (currently only checked at CLI entry). Additive; default `None` means no caller changes. Tripwire for when the Week 1 `writer.py` opts in with `scope="distiller"`.
- `5ada988` — Curator: SKILL contract smoke test (+186 new, `scripts/smoke_curator_scope.py`). Parses `SKILL.md`, extracts every `alfred vault <subcommand>` invocation, asserts each against `SCOPE_RULES["curator"]` and scope-check logic. Exits non-zero on drift. Distiller + janitor get their deterministic rebuild; curator stays agentic so needs a contract-drift guard.

## Validation

Builder ran per-commit validation (no pytest):
- c1: `python -c "from alfred.distiller import pipeline"` imports clean
- c2: AST grep confirms retry-once wired inside `_stage3_create`
- c3: In-process scope matrix — 5 pass/fail cases covering distiller/janitor/curator scopes on create/edit/delete
- c4: Ran against real SKILL.md, extracted 33 `alfred vault` invocations, **caught 1 genuine drift** (curator SKILL L1118 move example — separate fix in `Curator SKILL L1118 drift fix 2026-04-24`)

## Outcome

Week 0 transition plumbing on master. Daemons running legacy code still (no restart), but changes load on next natural restart. The scope kwarg on ops.py is the bridge the Week 1 `writer.py` will opt into; the smoke script caught real drift on first run, justifying its existence.

## Alfred Learnings

- **Pattern validated**: architect plan → builder execution workflow for a c-series. Plan identified exactly which parser patches were local bugs (S1, S3, S6) vs structural issues (S2, S4 — skip in favor of rebuild). Saved builder from hardening code we're about to delete.
- **Pattern validated**: smoke scripts beat pytest tests for contract drift. 60-100 LOC of shell/Python, no pytest dependency (per `feedback_pytest_wsl_hang.md`), runnable via `python scripts/smoke_*.py`. Drift caught at commit time instead of 24h-dead-step discovery.
- **Gotcha**: S8's smoke script found the L1118 drift on its *first run*. Not a coincidence — this is exactly the class of mismatch the Q3 2026-04-19 incident warned about (root CLAUDE.md). Justifies the smoke-as-pre-commit pattern for any SKILL that stays agentic.
- **Anti-pattern avoided**: Team 1 originally proposed S7 as "skip in favor of rebuild." Team 2 argued (correctly) that S7 provides *migration* defense-in-depth because both legacy and new writers will coexist. Landed per Team 2's argument.
