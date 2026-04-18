---
type: session
status: completed
name: "Surveyor Silent Writer — Audit Log Wiring"
created: 2026-04-18
description: "Fix 2 of 2 for the surveyor silent-writer observability bug. Wire VaultWriter through to data/vault_audit.log so surveyor mutations show up in the unified audit log alongside curator/janitor/distiller, with a distinct detail field per write type."
tags: [surveyor, observability, audit-log, drift-bug, session-note]
---

# Surveyor Silent Writer — Audit Log Wiring — 2026-04-18

## Intent

Companion fix to the structlog routing change in commit `e6b5ad6`. After that change, `data/surveyor.log` will contain `writer.tags_updated` / `writer.tags_unchanged` / `daemon.*` events as designed — but `data/vault_audit.log` would still have zero surveyor entries, because `VaultWriter` never invoked the unified audit-log helper.

The audit log is what tools downstream (drift investigations, `alfred vault audit` queries, attribution analyses) use to answer "which daemon touched this file?" Without surveyor entries, those tools systematically under-attribute mutations to surveyor and over-attribute them to "unknown" or to whichever daemon happened to also touch the file last.

## Diagnosis

Hypothesis #4 from the project memory: `mutation_log.log_mutation()` / `append_to_audit_log()` aren't invoked from the surveyor path. Confirmed by `grep -rn 'mutation_log\|append_to_audit_log' src/alfred/surveyor/` returning zero hits. By contrast, curator/janitor/distiller all call `append_to_audit_log(audit_path, "<tool>", mutations, detail=...)` after each batch of mutations, with the audit path derived from `Path(config.state.path).parent / "vault_audit.log"`.

Surveyor's writes are fundamentally different from the other tools — there's no agent → mutation-log JSONL → `read_mutations` flow. The writer is direct Python code. So the cleanest hook is inside `VaultWriter._write_atomic`, which is the single chokepoint every persisted write passes through. Adding the audit-emission there means both `write_alfred_tags` and `write_relationships` get covered with one call site, and the skip-if-equal short-circuit (which returns before reaching `_write_atomic`) correctly does NOT produce phantom audit entries.

## Files changed

- `src/alfred/surveyor/writer.py` — added optional `audit_log_path` constructor parameter; on successful `_write_atomic` completion, append a `{"tool": "surveyor", "op": "modify", "path": rel_path, "detail": <kind>}` JSONL line to the audit log via `alfred.vault.mutation_log.append_to_audit_log`. The two public methods now pass distinct `audit_detail` strings (`"alfred_tags"` vs `"relationships"`) so the audit log distinguishes the two write types. Backwards-compat: omitting the constructor arg keeps the old behaviour (no audit emission) — used by tests and any hypothetical caller that doesn't want audit logging.
- `src/alfred/surveyor/daemon.py` — `Daemon.__init__` now derives `audit_log_path = Path(cfg.state.path).parent / "vault_audit.log"` and passes it to `VaultWriter`. Mirrors the curator/janitor/distiller pattern exactly.
- `tests/test_surveyor_logging.py` — extended with four pytest cases: (a) `write_alfred_tags` produces one audit-log JSONL line with the right tool/op/path/detail; (b) `write_relationships` does the same with `detail="relationships"`; (c) the skip-if-equal short-circuit produces NO audit-log line (audit must mirror real mutations, not labeling attempts); (d) constructing a `VaultWriter` without `audit_log_path` still works for backwards compat.

## Verification

`pytest -v` — 19/19 pass:

```
tests/test_surveyor_logging.py::test_writer_appends_to_audit_log_on_tag_write PASSED
tests/test_surveyor_logging.py::test_writer_appends_to_audit_log_on_relationship_write PASSED
tests/test_surveyor_logging.py::test_writer_audit_log_skipped_when_tags_unchanged PASSED
tests/test_surveyor_logging.py::test_writer_without_audit_log_path_still_writes PASSED
```

Plus the three logging-config tests from commit `e6b5ad6` continue to pass — the audit-log wiring did not regress structured-log emission.

**Daemons not restarted** (per task instructions). Running surveyor process keeps the old writer (no audit emission) until next `alfred down` / `alfred up`. Recommended validation: after restart, confirm new entries appear in `data/vault_audit.log` with `"tool": "surveyor"` after the next labeling sweep produces a tag write.

## Alfred Learnings

- **Single-chokepoint instrumentation beats per-call-site.** The two public writer methods (`write_alfred_tags`, `write_relationships`) both eventually pass through `_write_atomic`. Hooking the audit emission there means every write — present and future — gets covered automatically, and the skip-if-equal early-return paths correctly produce no phantom audit entries. If a third public write method is added later, it gets audit logging for free as long as it routes through `_write_atomic`. Anti-pattern would have been duplicating the `append_to_audit_log` call in each public method, which is fragile (a future writer method silently lacks audit coverage) and verbose.
- **Audit-log emission must NOT match labeling attempts — it must match file mutations.** The skip-if-equal guard exists exactly to prevent labeler-driven thrash from churning the file or its git history. If audit-log emission were added at the top of `write_alfred_tags` instead of inside `_write_atomic`, every no-op skip would produce a phantom audit entry — exactly the wrong signal for drift investigations, which would then over-attribute churn to surveyor. Test `test_writer_audit_log_skipped_when_tags_unchanged` pins this contract.
- **Distinct `detail` field per write type matters for downstream attribution.** Curator uses inbox filename, janitor uses sweep_id, distiller uses run_id. Surveyor now uses the field-name string (`"alfred_tags"` or `"relationships"`) so a future query like "show me everything that touched relationships in the last week" can filter cleanly without joining against a separate state file. Worth adding to the agent-instructions documentation: when a tool has multiple distinct write types, encode the type in `detail`, not just in the path.
- **Two-commit fix when there are two independent root causes.** This bug has two structural causes (logging routing + missing audit hook) that are independent and could be fixed separately. Splitting into two commits with separate session notes makes git blame / git bisect / future debugging cleaner than one omnibus commit. Future builders: when you see "this bug has multiple sub-causes, all small," prefer N commits over 1.
