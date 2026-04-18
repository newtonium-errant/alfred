---
type: session
date: 2026-04-17
status: complete
tags: [upstream-port, janitor, link-repair, leftover]
---

# Upstream Port Batch C - Item 5: Janitor Stage 2 Link-Repair Mtime Guard

## Scope

Leftover piece from upstream `44cf675`. Batch B ported three of the four
pieces of that commit (wikilink regex negative lookbehind, .base skip,
distiller filesystem-diff fallback). The missing fourth piece was the
mtime guard on the janitor `_stage2_link_repair` counter so a no-op LLM
call doesn't inflate the `repaired` metric.

## What Changed

`src/alfred/janitor/pipeline.py::_stage2_link_repair`:

- Snapshot `target_path.stat().st_mtime` before the LLM call.
- After the call, re-read mtime and only increment `repaired` if the file
  was actually modified (`after_mtime > before_mtime`).
- Log discriminates `pipeline.s2_llm_repair` (actual repair) from
  `pipeline.s2_llm_no_change` (LLM couldn't resolve / file untouched).

The Python-path unambiguous fix above the LLM branch still increments
immediately after its `_fix_link_in_python(...)` returns true (that
function has its own file-write check), so the counter remains accurate
on both branches.

## Why This Matters

Upstream commit message explains: "The mutation log doesn't work
cross-container via openclaw-wrapper HTTP API" — the openclaw-wrapper
sends prompts to a container that doesn't inherit
`ALFRED_VAULT_SESSION`, so the mutation log is never written. The
filesystem mtime is the only authoritative signal of "something actually
changed". Even for in-process backends, the mtime guard protects against
the LLM returning text without invoking any `alfred vault edit`.

## Smoke Test

`/tmp/alfred_smoke/smoke_item5_mtime_guard.py`:

- Two note files with broken wikilinks routed through the LLM branch.
- `_call_llm` monkey-patched: one call modifies the target file (real
  repair), one call does nothing (no-op).

```
pipeline.s2_llm_repair         file=note/A.md target='person/Nonexistent Target Alpha'
pipeline.s2_llm_no_change      file=note/B.md target='person/Nonexistent Target Bravo'
pipeline.s2_complete           repaired=1
OK: 2 LLM calls, 1 counted repair (mtime guard held)
```

- Counter increments exactly once (real repair), not twice.
- Structured log output discriminates repair vs no-change for future
  debugging.

## Alfred Learnings

- **Filesystem signals survive container boundaries; in-process
  mutation logs don't.** Any counter that feeds metrics should key off
  the filesystem or a persisted log, never off a variable tied to the
  backend's success exit code. This is the second time this pattern
  has come up (Batch B had the distiller fallback), so it's worth
  noting as a cross-tool principle: "don't trust the session file
  exists — check the filesystem".

## Commit

- Code: e33af56
- Session note: (this file)
