---
type: session
status: completed
name: Surveyor embedder context-length fix
created: 2026-04-24
description: Fix two stacked surveyor bugs — Ollama /api/embeddings 500s on files exceeding its hard 2048-token cap (code assumed nomic-embed-text's 8192), and `diff_processed` logged `upserted=1` even when embed_failed (bookkeeping lie). Silent vector drift closed.
intent: Stop Milvus vectors from silently going stale during surveyor ticks when Ollama rejects oversized embed text. Root cause wasn't wikilink expansion or batching (my initial hypotheses); was a stale code comment assuming the wrong context window.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related:
- '[[session/Surveyor cascade safety fix 2026-04-24]]'
tags:
- surveyor
- embedder
- ollama
- bookkeeping
- correctness
---

# Surveyor embedder context-length fix

## Intent

Surfaced while validating the c1 cascade safety fix 2026-04-24 04:35-04:36 ADT. A trailing-newline touch on a 2.2KB assumption file triggered a Diff with 31 changed records; Ollama returned HTTP 500 on one of them with `{"error":"the input length exceeds the context length"}`. Logs then showed `embedder.diff_processed upserted=1` as if the embed had succeeded — but Milvus received nothing fresh. Correctness drift: state claims record is processed, vector is stale or missing.

## Work Completed

Two commits on master:

- `c093f53` — Surveyor: fix embedder context-length failure on single-record embed (`src/alfred/surveyor/parser.py` + `embedder.py`, +37/-9). `MAX_EMBEDDING_CHARS` 8000 → 6000 because **Ollama's legacy `/api/embeddings` endpoint caps input at 2048 tokens regardless of the model's advertised 8192 context window**. The 8000-char cap was ~2.6× too large. Live Ollama probe confirmed: 8000-char input on worst-case file returns 500; 6000-char embeds cleanly. Also added `rel_path` + `embed_text_len` to `embedder.embed_retry`/`embed_failed` log lines for future diagnosis.
- `53914e3` — Surveyor: stop lying about upserted count when embed failed (`embedder.py`, +30/-1). `embedder.diff_processed` now reports real `upserted=len(records)` successes + `attempted/embed_failed/parse_errors/empty_skipped` breakdown. New `embedder.diff_failed_records records=[...]` warning names stale-vector paths so audits can identify them.

## Validation

- Bug A: live Ollama probe against the worst-case vault file (`session/Upstream Port Batch A - janitor perf 2026-04-17.md`) at new 6000-char cap returns HTTP 200. All 42 previously-at-risk files fit under the new limit.
- Bug B: in-process Python asyncio test (no pytest) with mocked `parse_file` + fake `_get_embedding`:
  - Fail scenario: `upserted=0, embed_failed=1, attempted=1` + `diff_failed_records` fires. Before: would log `upserted=1`.
  - Success scenario: `upserted=1, embed_failed=0`, no `diff_failed_records` event.
- Daemon import smoke: all modules import; `_get_embedding` signature backwards-compat (default `rel_path=''`).

## Outcome

Embedder truthful. The upserted counter now correlates with actual Milvus writes; operators can audit stale-vector paths via the new warning event. Silent drift closed.

Surveyor was confirmed architecturally sound during the same-day distiller-rebuild debate — it already uses the LLM-as-inspector + Python-as-writer pattern. See `project_surveyor_improvement_candidates.md` for three parked opportunistic options (scope kwarg opt-in, Pydantic labeler pattern, incremental clustering) that are NOT blockers — review when triggers fire.

## Alfred Learnings

- **New gotcha**: Ollama's `/api/embeddings` (legacy) caps at 2048 tokens regardless of model metadata. To unlock nomic-embed-text's full 8192 window, use `/api/embed` (newer) with `options={"num_ctx": 8192}`. Filed as a future migration, not urgent.
- **Pattern validated**: log the actual input size (`embed_text_len`) alongside any retry/fail event. Future diagnosis won't need another reproduction cycle.
- **Anti-pattern avoided**: my initial hypotheses (wikilink expansion, batch concat) were both wrong. Builder went to live Ollama to probe the real limit, not the model-metadata-advertised one. When an external service's actual behavior contradicts its docs/metadata, the service is truth.
- **Pattern validated**: state.json lies are worse than outright failures. An explicit `embed_failed` log + stale-vector audit trail is strictly better than `upserted=1` when nothing was upserted. Apply this posture anywhere Alfred's state could diverge from truth (distiller dedup, curator processed-hash, janitor sweep state).
