---
type: session
date: 2026-04-17
status: complete
tags: [upstream-port, curator, prompt, backend-agnostic]
---

# Upstream Port Batch C - Item 3: Model-Agnostic Curator Prompts

## Scope

Port upstream `f4dea8c`. Rewrite the Stage 1 prompt so non-Claude models can
follow it reliably: replace bash heredocs (`cat <<'BODY'` and
`cat > manifest.json <<'MANIFEST_EOF'`) with Write-tool + simple Bash pipe
patterns, and teach `_parse_entity_manifest` to extract manifest JSON from
markdown code blocks as a fallback.

## What Changed

- `src/alfred/_bundled/skills/vault-curator/prompts/stage1_analyze.md`:
  - Note creation is now a two-step instruction: "Write to /tmp/note-body.md"
    followed by `alfred vault create note ... --body-stdin < /tmp/note-body.md`.
  - Manifest creation is now: "Use the Write tool to write to
    `{manifest_path}`", with a fallback instruction to include the JSON
    inline inside a ```json code block if the Write tool fails.
  - Important Rules updated to reference the Write tool for both the note
    body and the manifest, and to mention the ```json fallback path.

- `src/alfred/curator/pipeline.py`:
  - Split `_parse_entity_manifest` into `_extract_entities_from_text`
    (reusable brace-depth scanner) and the tiered `_parse_entity_manifest`.
  - New Tier 1: find `{{"entities": [...]}}` inside ```json fenced blocks
    first. This is the primary fallback when a model emits the manifest
    inline instead of writing it to the tmp file.
  - Tier 2 (raw JSON anywhere) and Tier 3 (whole-stdout-as-JSON) are kept
    as backstops.

## Claude Backend Impact

None in practice. The Claude backend already handles Write + Bash well and
continues to work. The cost is a small amount of prompt churn — the Stage 1
prompt is now a few lines longer for the extra "Fallback" path. Benefit:
swapping in OpenClaw-local or Hermes (Item 4) or a future Ollama backend
is cheaper because the prompt doesn't lean on heredoc fidelity.

## Smoke Tests

### Parser tiers — `/tmp/alfred_smoke/smoke_item3_manifest_parse.py`

```
OK: all 6 parser tiers work
```

- ```json fenced block: parsed correctly.
- Plain ``` fenced block (no language tag): parsed correctly.
- Raw inline JSON: still works.
- Whole-stdout-as-JSON: still works.
- Empty/garbage input: returns [] with warning.
- Nested objects (fields contain nested braces): parsed correctly via
  brace-depth tracker.

### Template round-trip

`_load_stage_prompt('stage1_analyze.md')` loads cleanly; `.format(...)`
with the six expected placeholders (`vault_cli_reference`, `vault_context`,
`inbox_filename`, `inbox_content`, `manifest_path`, `user_profile`)
produces a 13,686-char prompt with 3 `"entities": [` example references
and the Write/Bash tool instructions intact. No stray heredoc markers
(`BODY`, `MANIFEST_EOF`) remain. The `{{...}}` escapes survive round-trip.

## Diff-Review for Claude

Visual inspection of the rendered prompt shows:

- Two-step Write-then-Bash pattern is clear and numbered.
- Example manifest JSON is a valid JSON object (no truncation, no stray
  `"` at the end like the upstream version had).
- Fallback instruction is explicit: "If the Write tool is unavailable or
  fails, include the JSON in your response inside a fenced code block
  marked `json`".
- No contradictions with the Task 1 note-creation instructions.

## Alfred Learnings

- **The `"` upstream artifact was caught.** Upstream `cbedd04` had a
  stray trailing `"` after the closing `]}` in the manifest example
  (`]}}"`). Dropped it in our port — leaving it in could teach the model
  to emit invalid JSON.
- **Brace-depth scanners are load-bearing for nested `fields: {}`.**
  A naive `re.search` for `{...}` would match the inner fields dict and
  miss the outer manifest. `_extract_entities_from_text` tracks depth,
  which is why we factored it out rather than inlining.

## Commit

- Code: 129072f
- Session note: (this file)
