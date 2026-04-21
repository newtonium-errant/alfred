---
type: session
created: '2026-04-21'
name: Hotfix c4 — bash_exec wiring 2026-04-21
description: KAL-LE launch-day hotfix c4 — wire bash_exec into the conversation tool-use dispatch
intent: Activate the c5 tool schema + c6 executor by adding the conversation-loop dispatch case so KAL-LE can actually run shell commands (pytest, npm, grep, git-read) instead of reporting bash_exec isn't available
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
related:
  - '[[project/KAL-LE]]'
  - '[[session/Hotfix c1 2026-04-21]]'
  - '[[session/Hotfix c2 2026-04-21]]'
  - '[[session/Hotfix c3 2026-04-21]]'
tags:
  - hotfix
  - kalle
  - bash-exec
  - tool-use
  - dogfood
status: completed
---

# Hotfix c4 — bash_exec wiring 2026-04-21

## Intent

Fourth of four launch-day hotfixes. The `bash_exec` tool schema shipped in c5 and the safety-critical executor shipped in c6, but the conversation-loop dispatch case for `tool_name == "bash_exec"` was intentionally deferred "to dogfood". Predictable result, observed live on `t.me/KalleErrantBot`:

> User: "KAL-LE, run pytest on the transport module"
> KAL-LE: "No test structure visible through vault tools... I need `bash_exec` to find the transport test path — but `bash_exec` isn't available in this environment."

The tool appeared in the schema we handed the model, but when the model emitted a `tool_use` block targeting it, the dispatcher in `conversation._execute_tool` fell through to "Unknown tool" because it only knew about the four vault ops. KAL-LE's headline capability (Bundle B — run tests, edit, read code) didn't work in practice.

## Work Completed

### `src/alfred/telegram/conversation.py`

- **New `_dispatch_bash_exec` helper** — thin adapter between the Anthropic tool_use shape and `bash_exec.execute`. Every safety guardrail (allowlist, denylist, cwd check, destructive-keyword gate, timeout, output truncation) lives in the executor; this function is pure dispatch glue. Order of checks, by design:
  1. **Tool-set gating.** Refuses explicitly (structured error, logged as `talker.bash_exec.wrong_tool_set`) when `config.instance.tool_set != "kalle"`. Second-line defence: even if Salem somehow received a `bash_exec` tool_use (classifier drift, prompt injection), the dispatcher refuses before reaching the executor.
  2. **Config presence check.** `config is None` or `config.bash_exec is None` → structured refusal `bash_exec disabled in config`. Protects against a misconfigured KAL-LE that forgot the `bash_exec:` block.
  3. **Argument sanity.** Empty/missing `command` or `cwd` → structured error before any subprocess touch.
  4. **Execute + return.** Executor is async and always returns a dict; the dict is JSON-serialised verbatim as the tool_result content so the model can reason about `exit_code` / `stdout` / `stderr` / `reason` directly.
  5. **Subprocess-failure-contract logging.** On `exit_code != 0` AND `reason == ""` (command actually ran, no executor-level refusal), emit `talker.bash_exec.nonzero_exit` with `chat_id`, `session_id`, `command`, `code`, `stderr[:500]`, `stdout_tail=stdout[-2000:] if stdout else ""`. The `stdout_tail=""` sentinel is load-bearing per builder.md so the "no diagnostic output at all" signature is grep-able.
  6. **Suppress-on-refusal.** When `reason != ""` (executor already refused via denylist / cwd / allowlist / timeout / parse), we DON'T add a duplicate contract log — the executor emits its own gate-specific warning.
- **`_execute_tool` now accepts `config: TalkerConfig | None = None`** — backwards-compatible (defaults to None for legacy callers) and is checked before the vault-op lookup so `bash_exec` doesn't confuse the scope-enforcement path. The `run_turn` tool-loop passes `config=config` on every call.
- **`run_turn` now uses `tools_for_set(config.instance.tool_set)`** instead of the hardcoded `VAULT_TOOLS`. Salem's talker now provably sees only vault tools; KAL-LE's tools list includes `bash_exec`. The `VAULT_TOOLS` legacy alias stays for upstream compat but isn't on the hot path anymore.

### `tests/telegram/test_conversation_bash_exec.py` (+11 tests)

- **Dispatch** — happy path (executor invoked with parsed args + audit path + session_id), tool-set refusal on talker, config-missing refusal on kalle-without-bash_exec, config-None refusal (backwards compat), empty-command refusal, dry-run pass-through.
- **Logging** — nonzero-exit contract log fires with all required fields (`code`, `stderr`, `stdout_tail`, `session_id`, `chat_id`); executor refusal SUPPRESSES the contract log (belt and braces against noisy duplicates). Uses a structlog processor-intercept pattern rather than caplog/capsys because the rendered-to-stdout-vs-stderr-vs-stdlib behaviour varies between isolated and full-suite runs — capturing the event dict at the processor level is render-agnostic.
- **Integration** — `run_turn` on a kalle-shaped config surfaces `bash_exec` in the tool list passed to the model; `run_turn` on a talker-shaped config DOES NOT (verified against what the model would see, not just scope check).
- **Smoke** — real `bash_exec.execute` round-trip: redirect `$HOME`, scaffold `aftermath-lab/init.py`, run `ls init.py` through the dispatcher, assert `exit_code=0`, `init.py in stdout`, audit log written at config-declared path with matching `session_id`.

## Testing

Full suite: 975 → 986 passing (+11). Pre-existing flaky test `test_failure_log_has_subprocess_contract_fields` still exhibits the documented ordering-dependent behaviour (passes alone, fails in full-suite context — same as before this change).

Isolated bash_exec dispatcher tests: 11/11 passing in 0.6s.

## Verification in situ

- Salem's talker MUST NOT expose `bash_exec` in its tool list. Confirmed via `test_run_turn_talker_instance_excludes_bash_exec`: the tool list sent to `messages.create` is asserted not to contain `bash_exec`. Only `vault_search`, `vault_read`, `vault_create`, `vault_edit`.
- KAL-LE's talker MUST expose `bash_exec`. Confirmed via `test_run_turn_uses_kalle_tool_set`: the tool list contains `bash_exec` plus the four vault tools.
- Audit log resolution:
  - Salem: no `bash_exec` section in config → no audit log path defined → dispatcher refuses before touching disk.
  - KAL-LE: `bash_exec.audit_path: "/home/andrew/.alfred/kalle/data/bash_exec.jsonl"`. Directory already exists (populated with `talker.log`, `alfred.pid`, etc.). Executor auto-creates parent on first write (`p.parent.mkdir(parents=True, exist_ok=True)` in `_audit_append`).

## Why this was deferred

Per c10's deviation: shipping the schema + executor without the dispatcher was a deliberate "wire it with real traffic in mind" pattern. In practice, deferring the 50-line dispatch glue until we'd seen KAL-LE actually reach for `bash_exec` meant we could verify the shape of the model's tool_use blocks with real prompts rather than guessing. The dogfood signal came within a day of launch (this session). Cycle time from deferral to wiring: ~24 hours.

## Alfred Learnings

- **Deferred wiring is a real pattern, not tech debt.** The c5+c6 split intentionally shipped the safety-critical executor + schema without the dispatcher, expecting the first live call to reveal any shape mismatches. It worked: the model emitted exactly the schema-shaped tool_use blocks we'd expected, the dispatcher was trivially small (~50 LoC in the happy path), and all the subtle safety lives in the well-tested executor (c6 landed with exhaustive denylist tests). Pattern confirmed: ship the guarded substrate ahead of the wiring when the substrate has a stable contract and the wiring needs real-shape validation.
- **Structlog + caplog + full-suite = don't.** Spent 15 minutes wrestling caplog because structlog's output destination (stdout vs stderr vs stdlib handler) depends on setup_logging ordering AND previous tests' logging state AND cache_logger_on_first_use. The reliable approach: a custom structlog processor that records the event dict before the renderer runs. Render-agnostic, deterministic, no global-state surprise. Worth extracting into a reusable fixture for future tests that need to assert on log events — not now, but flag it.
- **Tool-set gating is defence-in-depth.** The `tools_for_set()` call at the top of `run_turn` means Salem's model never sees `bash_exec` at all — that's line one. The `tool_set != "kalle"` refusal in `_dispatch_bash_exec` is line two (in case of classifier drift, replay attack, or a future tool-set that accidentally inherits the wrong registry entry). Costs nothing, catches a class of failures that would otherwise be silent. Pattern worth keeping: when a capability is instance-scoped, refuse at the schema exposure AND at the dispatch entry.
- **The stdout_tail='' sentinel keeps earning its keep.** Standard instinct would be to omit empty fields from a log event (looks cleaner). But the sentinel's whole point is that `grep stdout_tail=''` is the grep for "the subprocess produced zero diagnostic output" — omitting the field breaks that query. Re-validated here; noted explicitly in the dispatcher comment so the next person who "cleans up" the log call is warned.
