---
type: session
title: Upstream Port Batch B - Slim context and skip entity enrichment
date: 2026-04-17
tags: [session, upstream-port, curator, perf]
---

## Summary

Ported upstream commit `ba1f7d0` — two curator token-consumption reductions.

### Change A: Slim vault context

`VaultContext.to_prompt_text()` in `src/alfred/curator/context.py` previously emitted one line per record with a full wikilink and status marker — `- [[person/Alice|Alice]] — status: active`. At ~900 records this hit ~54KB per prompt.

The slim form emits type headers with counts plus comma-separated entity names wrapping at ~120 chars. No wikilinks, no status annotations. Ballpark 4-5x reduction in body size. The LLM only needs names for dedup awareness — the actual existence check happens in Stage 2 Python (`_entity_exists`), not in the LLM's head.

Our 10-record smoke test produced 184 bytes total for 3 types.

### Change B: skip_entity_enrichment flag

Added a `skip_entity_enrichment: bool = True` field to `CuratorConfig`. When true (the default, matching upstream), `run_pipeline` skips the Stage 4 `_stage4_enrich` LLM call and preserves entity stubs from Stage 2. Stage 2 stubs already carry the manifest description, so entities are usable without enrichment.

The flag is threaded through `load_from_unified` so users can opt back into full enrichment by setting `curator.skip_entity_enrichment: false` in their config.yaml — but the code default stays True.

Stage 4 code itself is preserved in place and still callable via the `False` path.

## Changes

- `src/alfred/curator/context.py` — new `to_prompt_text()` body: name-only index, 120-char line wrapping.
- `src/alfred/curator/config.py` — added `skip_entity_enrichment: bool = True` to `CuratorConfig`; `load_from_unified` passes the value through from the `curator` section when present.
- `src/alfred/curator/pipeline.py` — gated Stage 4 behind `config.skip_entity_enrichment`; added `pipeline.s4_skipped` log; `enriched = []` on the skip branch so downstream summary/log lines still work.

## Downstream effect on the talker

`src/alfred/telegram/daemon.py:_build_vault_context_str` re-uses curator's `build_vault_context`. After the slim change, the talker sees a compact name index instead of a wikilinked listing. That's actually better for the talker — it only needs names for lookup, not full wikilinks, and the talker's prompt budget was already tight.

## Config compatibility

`config.yaml` was not modified. The user's existing config keeps working; the default kicks in silently. If the user wants full entity enrichment back, they can add `skip_entity_enrichment: false` under `curator:` in their config.yaml.

## Smoke Tests

**Slim renderer** — fake vault with 10 records across 3 types produced:
```
### org (2)
Acme Corp, Foo Industries

### person (5)
Alice, Bob, Carol Diaz-Martinez, Dave, Edward Long Surname Person

### project (3)
Alpha Initiative, Beta Program, Gamma Research
```
No wikilinks, no status lines, counts in parens — matches upstream format.

**Config round-trip**:
- default (no key): True
- explicit True: True
- explicit False: False
- `CuratorConfig()` direct: True

All pass.

## Alfred Learnings

- **When porting a config default, also check the serialization path.** `CuratorConfig` had the field added, but `load_from_unified` doesn't roundtrip arbitrary top-level keys — it explicitly builds a restricted dict. Without threading `skip_entity_enrichment` through that function, the daemon would have ignored the user's config.yaml override and always used the dataclass default.
- **Talker context size** — we should keep this in mind when tuning the talker's own prompt. The slim change was motivated by curator but incidentally helps the voice path too.
- **`enriched` variable lifetime** — upstream's patch let `enriched` go out of scope on the skip branch, then referenced `len(enriched)` in the trailing log line. Our code has the same trailing log, so I set `enriched = []` on the else branch as well. Pay attention to where "result" fields are both written and read.
