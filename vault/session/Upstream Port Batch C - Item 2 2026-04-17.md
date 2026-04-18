---
type: session
date: 2026-04-17
status: complete
tags: [upstream-port, curator, schema-shift, needs-validation]
---

# Upstream Port Batch C - Item 2: Full Records from Stage 1

## Scope

Port upstream `cbedd04` — the curator Stage 1 prompt now emits complete
markdown `body` for every manifest entity, and Stage 2 uses that body
directly when creating vault records instead of falling back to a
`# Name\n\ndescription\n` stub.

**This is a schema-shifting change.** The manifest contract is different on
both sides of the LLM boundary. The vault-reviewer should pass over the
first post-restart curator output before we trust the new shape in
production.

## What Changed

- `src/alfred/_bundled/skills/vault-curator/prompts/stage1_analyze.md`:
  - New content-type handling section (transcripts, emails, notifications,
    audio) — lifted verbatim from upstream.
  - `body` field added to each manifest entity schema and to the worked
    example for all five entity types.
  - "Entity body content must be substantive" rule added to Important Rules.
  - Task 2 header now says "Write the Entity Manifest to a File" and the
    preamble flags FULL records with body content.

- `src/alfred/curator/pipeline.py::_resolve_entities`:
  - Prefers `entity["body"]` when non-empty; ensures trailing newline.
  - Falls back to the legacy `# Name\n\ndescription\n` stub when body is
    missing or empty. This is deliberate — if a legacy manifest somehow
    shows up (retry with stale prompt, non-compliant model), we still
    produce a valid record rather than dropping the entity.

## Style Deviation from Upstream

Upstream's `cbedd04` landed on top of `f4dea8c` (Item 3 — model-agnostic
Write-tool pattern). We're porting these two items in sequence, so this
Item 2 commit still uses the `cat <<MANIFEST_EOF` heredoc for the manifest
step. Item 3 will rewrite both the note and the manifest sections to the
Write-tool + Bash pattern. The intermediate shape committed here matches
what upstream had between `cbedd04` and before `f4dea8c` would have
produced if applied in order.

Net effect on the Claude Code backend is zero — heredocs still work fine
for Claude. The only downstream impact is that the next commit (Item 3)
will then replace both the note and manifest sections at once.

## Smoke Test

`/tmp/alfred_smoke/smoke_item2_stage2_body.py`: constructs a two-entity
manifest — one with a full `body`, one without — runs `_resolve_entities`
against a temp vault, and diffs the resulting files.

```
OK: manifest body used for person, fallback stub used for org
  person body length: 199 chars
  org body length: 136 chars
```

- Person record contains the exact manifest body text (not a stub).
- Org record falls back to the description-based stub (length tells the
  story — 136 chars is the old stub size; 199 chars is the full body).
- Mutation log captures both creates.

## Risk Flags

1. **Body quality depends on prompt compliance.** If the LLM ignores the
   `body` field or emits a truncated stub, Stage 2 will happily write a
   bad record because the fallback only triggers on "missing or empty",
   not on "shorter than some threshold". First post-restart run should
   be reviewed — the rule "Entity body content must be substantive"
   carries real weight here.
2. **Old in-flight manifests are tolerated.** Anything pre-schema-shift
   (a stale manifest tempfile, a retry that uses old stdout cache) still
   produces a valid record via the fallback. No data loss path.
3. **Stage 4 enrichment interaction.** We have `skip_entity_enrichment=True`
   by default (Batch B). So Stage 4 doesn't run and the Stage-1 body is the
   only source of record content. This makes prompt compliance critical —
   there is no second-pass recovery from a weak body.

## Alfred Learnings

- **Schema shifts need fallback tolerance in both directions.** Pipeline
  fallback to the legacy stub means we can ship the schema change without
  coordinating a hard cutover — a transitional LLM that forgets `body`
  still produces a usable record.
- **When enrichment is off (Batch B default), Stage 1 quality is all we
  get.** Cross-contract note: if prompt-tuner tunes Stage 4, and builder
  tunes the Stage 1 body requirement, we need both to agree on which stage
  owns body quality when Stage 4 is skipped.

## Commit

- Code: b3fe4b6
- Session note: (this file)
