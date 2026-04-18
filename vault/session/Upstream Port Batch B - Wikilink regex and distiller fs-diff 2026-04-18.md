---
type: session
title: Upstream Port Batch B - Wikilink regex and distiller fs-diff
date: 2026-04-18
tags: [session, upstream-port, janitor, distiller, bugfix]
---

## Summary

Ported upstream commit `44cf675` — three defensive fixes covering janitor phantom issues and distiller cross-container record detection.

### Piece 1: Wikilink regex negative lookbehind

`WIKILINK_RE` in `src/alfred/janitor/parser.py` previously matched `[[target]]` without distinguishing between a plain wikilink and an Obsidian embed `![[target]]`. The scanner ran LINK001 against every match, so embeds were raising broken-link phantoms whenever the embed target was unresolvable by stem (e.g. `![[person.base#Decisions]]`).

Added `(?<!!)` negative lookbehind so the regex only matches `[[...]]` that is NOT preceded by `!`. Embeds still get picked up by the separate `EMBED_RE` in the same module.

### Piece 2: `.base` skip in LINK001

Even with the regex fix, `.base` targets can still appear in frontmatter fields that are rendered without the `!` prefix — and they're Dataview views, not vault records, so they should never be LINK001 candidates anyway. Added a `if ".base" in target: continue` guard in `scanner._check_record` right before the stem lookup. Covers both frontmatter and body occurrences defensively.

### Piece 3: Distiller openclaw fs-diff fallback

Upstream documented: when distiller's `_stage3_create` invokes an agent via the openclaw backend, the HTTP-bridged container doesn't have `ALFRED_VAULT_SESSION` set, so the mutation log is never populated. The existing before/after mutation-log diff returns an empty set even when a record was written.

Added a filesystem-snapshot fallback: take a `set(learn_dir.glob("*.md"))` before the LLM call; after the call, if the mutation log path-returning logic failed, diff the directory and return the first new file's path. The primary mutation-log path is still preferred (it has better resolution and catches other learn types) — the fs-diff only runs when that path yielded nothing.

## Changes

- `src/alfred/janitor/parser.py` — `WIKILINK_RE` gains `(?<!!)` negative lookbehind.
- `src/alfred/janitor/scanner.py` — LINK001 loop skips targets containing `.base`.
- `src/alfred/distiller/pipeline.py` — `_stage3_create` snapshots the learn-type directory before the LLM call and uses a filesystem diff as a fallback when the mutation log returns no new paths.

## Scope note (intentional omission)

Upstream's commit ALSO added a `before_mtime`/`after_mtime` guard to `_stage2_link_repair` in `janitor/pipeline.py` — the repaired counter was incrementing even when the cross-container LLM made no actual edit. The task spec for Item 3 enumerated three pieces and did not include this fourth change. I left it out so Item 3 stays on-scope; the builder can port it later as part of a janitor-counter hygiene pass if wanted.

## Smoke Tests

**Wikilink regex** — unit checks via `re.findall`:
- `[[target]]` → `['target']`
- `![[person.base#Decisions]]` → `[]`
- `embed ![[image.png]] and link [[target]]` → `['target']`
- Mixed sequence: `[[link1]] ![[embed1]] [[link2|alias]]` → `['link1', 'link2']`

**`.base` skip** — built a `VaultRecord` with two wikilinks (`person.base#Decisions` and `person/NonExistent`), ran `_check_record` with an empty stem index. Result: exactly one LINK001 issue, for `person/NonExistent`. `.base` target was skipped.

**Distiller fs-diff fallback** — integration test with a temp vault + `decision/` dir + empty session log. Patched `_call_llm` to write a new file directly (simulating openclaw cross-container behaviour where no mutation log entry appears). `_stage3_create` returned `decision/Test Decision.md` via the `pipeline.s3_created_via_fs_diff` log path. Primary mutation-log path still takes precedence when populated.

All pass.

## Alfred Learnings

- **Negative lookbehind on wikilinks is a one-character fix that prevents a noisy false-positive class.** Every janitor scan pre-patch was flagging every embed as broken because the scanner had no way to tell `[[x]]` from `![[x]]`. Remember: `WIKILINK_RE` and `EMBED_RE` coexist, and only one should match any given position.
- **Mutation log is only reliable for same-process agents.** The openclaw-wrapper pattern (HTTP into a separate container) loses the `ALFRED_VAULT_SESSION` env var and therefore can't write mutation entries. Any code path that uses the mutation log as ground truth for "did the agent create/edit a file" needs a filesystem-snapshot fallback. This is now documented in both distiller `_stage3_create` and should inform future patches.
- **Upstream commits can bundle 3-4 related fixes** — stay disciplined about scope. Upstream `44cf675` had four changes; the task asked for three; I ported three and noted the fourth as a future item rather than silently pulling it in.
