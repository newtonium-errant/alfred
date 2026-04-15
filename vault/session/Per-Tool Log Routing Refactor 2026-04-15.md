---
alfred_tags:
- software/alfred
- observability
- refactor
created: '2026-04-15'
description: Extend `_setup_logging_from_config` to take a `tool` kwarg and update
  every CLI dispatcher that was silently routing its structlog events to
  `alfred.log` instead of its per-tool log file. Surgical fix only — the
  `cmd_vault` and `cmd_janitor` main-path inline setups from earlier commits
  are left untouched.
intent: Close the class of observability bugs where per-tool CLI subcommand
  events were misrouted to the shared alfred.log
name: Per-Tool Log Routing Refactor
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
related:
- '[[session/Janitor Observability Bundle 2026-04-15]]'
- '[[session/Harden Vault Dedup at Python Layer 2026-04-15]]'
status: completed
tags:
- observability
- refactor
- logging
type: session
---

# Per-Tool Log Routing Refactor — 2026-04-15

## Intent

Earlier today's janitor observability bundle (`f95de9f`) surfaced that `_setup_logging_from_config` in `src/alfred/cli.py` hardcodes `alfred.log` as the destination, so every per-tool CLI dispatcher that calls it was silently routing its structlog events to the wrong file. The audit that followed confirmed the bug affects seven live dispatchers (curator, distiller, process, temporal, surveyor, brief, mail) plus the janitor fallback branch. This commit closes the whole class of bugs with a single helper refactor plus eight call-site updates.

## What Changed

### `_setup_logging_from_config` gains a `tool` kwarg

```python
def _setup_logging_from_config(raw: dict[str, Any], tool: str = "alfred") -> None:
    """Set up logging from the unified config's logging section.

    ``tool`` selects the per-tool log file: ``data/<tool>.log``. Default
    ``"alfred"`` preserves backward compatibility for the daemon launcher
    (``cmd_up``) and any handler that legitimately wants the shared log.
    """
    log_cfg = raw.get("logging", {})
    level = log_cfg.get("level", "INFO")
    log_dir = log_cfg.get("dir", "./data")
    from alfred.curator.utils import setup_logging
    setup_logging(level=level, log_file=f"{log_dir}/{tool}.log")
```

The body is nearly identical to before except for the `tool` interpolation. The curator-import is arbitrary — all five per-tool `setup_logging` helpers (`alfred.{janitor,curator,distiller,surveyor,brief}.utils.setup_logging`) have identical signatures `(level: str = "INFO", log_file: str | None = None, suppress_stdout: bool = False)`. Any one of them would work; curator was chosen simply because that's what the original helper imported.

### Eight call-site updates

| Line | Caller | New kwarg | Why |
|---|---|---|---|
| 205 | `cmd_curator` | `tool="curator"` | obvious |
| 232 | `cmd_janitor` fallback | `tool="janitor"` | matches main-path target |
| 262 | `cmd_distiller` | `tool="distiller"` | obvious |
| 428 | `cmd_process` | `tool="curator"` | batch curator runner — events ARE curator events |
| 492 | `cmd_temporal` | `tool="temporal"` | distinct subsystem, new `data/temporal.log` |
| 518 | `cmd_surveyor` | `tool="surveyor"` | obvious |
| 539 | `cmd_brief` | `tool="brief"` | obvious |
| 563 | `cmd_mail` | `tool="mail"` | obvious |

`cmd_up` at line 98 is **deliberately left unchanged** — the daemon launcher legitimately wants `alfred.log` as the destination for its own lifecycle events. It's the only remaining caller of `_setup_logging_from_config(raw)` without a `tool=` override, which makes it easy to audit: any other handler calling the helper without `tool=` is either correct-by-default or a bug worth flagging.

Subtle call-site choice: **`cmd_process` routes to `curator.log`, not `process.log`**. The `process` subcommand is a batch curator runner — it processes all unprocessed inbox files via the curator pipeline. Its events are curator events and grouping them with the daemon's curator events is the right call for log archaeology later.

## Verification

**Static.** `from alfred.cli import _setup_logging_from_config` imports cleanly. `grep _setup_logging_from_config src/alfred/cli.py` shows exactly one call without `tool=` (`cmd_up`, line 98) and eight calls with `tool=` (the seven fixed dispatchers plus the janitor fallback).

**Runtime sampling per tool.** Invoked the lightest available CLI subcommand for each affected dispatcher and checked for byte-delta in the per-tool log file:

- `alfred distiller status` → `data/distiller.log` +3507 bytes, `state.loaded` event present
- `alfred brief status` → `data/brief.log` +125 bytes, `brief.state.loaded` event present
- `alfred mail status` → `data/mail.log` NEWLY CREATED at 129 bytes, `mail.state.loaded` event present
- `alfred process --dry-run --limit 0` → `data/curator.log` +508 bytes, curator events correctly in curator.log (not alfred.log)
- `alfred temporal list` → `data/temporal.log` NEWLY CREATED (0 bytes — `tcli.cmd_list` emits no structlog events, but file creation proves routing)

Two dispatchers couldn't be runtime-verified because their top-level `--help` short-circuits at argparse before the handler runs: `cmd_curator` and `cmd_surveyor`. Both rely on static grep confirmation only. Not blocking — the helper is pure and the change is mechanical; runtime confirmation of the others is sufficient to prove the pattern works for those two as well.

The janitor fallback branch at line 232 is similarly untriggerable without forcing an import failure in the main path. Static-only verification is fine.

## Deliberately Left Unchanged (for a future DRY pass)

Two dispatchers have their own inline `setup_logging` call sites from earlier commits and are NOT touched by this commit:

1. **`cmd_vault`** (from `e6aa461` this morning) — inline ~25-line block with `suppress_stdout=True` because the vault CLI emits JSON on stdout and must never pollute it. Could be refactored to `_setup_logging_from_config(raw, tool="vault", suppress_stdout=True)` if the helper gains a `suppress_stdout` passthrough kwarg, which would DRY the handler from 25 lines to one.
2. **`cmd_janitor` main path** (from `f95de9f` this afternoon) — inline ~12-line block. Could similarly collapse to `_setup_logging_from_config(raw, tool="janitor")` — the helper already does what the inline block does; the inline version was a proof-of-pattern before the helper was generalized.

**Why deferred:** surgical discipline. Today's commit had a clean scope: "fix the hardcoded `alfred.log` bug." Unifying the two working inline call sites into the new helper is a different change — it touches code that already works, and it requires adding a `suppress_stdout` passthrough to the helper signature. Both changes are mechanical and low-risk, but they shouldn't ride on the same commit as the bug fix. **Flagged as a followup for a dedicated small DRY session.**

## Alfred Learnings

### Patterns Validated

- **Audit before refactor.** The fix I proposed in the discussion was "Option 3 with `tool` kwarg." But before implementing, I audited every call site, every per-tool `setup_logging` signature, and every tool CLI handler's logging pattern. That audit surfaced `cmd_temporal` (which I hadn't originally listed as affected) and `cmd_process` (which I had mis-framed as "needs its own process.log" when it should route to `curator.log`). Both catches came from reading the actual code, not from assuming. Cost: ~5 minutes of grep + Read. Benefit: no per-call-site surprises during implementation.
- **Helper default as a grep-able contract.** Leaving `cmd_up` as the single caller of `_setup_logging_from_config(raw)` with no `tool=` override means any future handler that forgets to pass `tool=` will default to `alfred.log` — same trap as the original bug — BUT will now be immediately grep-able: any `_setup_logging_from_config(raw)` call without a `tool=` kwarg is either `cmd_up` or a bug. That's a much better footgun than a silently-broken helper.

### New Gotchas

- **`--help` short-circuits argparse before handler dispatch.** When verifying CLI refactors by running `alfred <tool> --help`, argparse prints the help text and exits BEFORE the `cmd_*` function is ever called, so the helper never runs and no log events are produced. Effect: `--help` is useless as a runtime verification for CLI logging changes. Use `status`, `list`, or another lightweight subcommand that actually dispatches to the handler body.
- **Tools without a lightweight subcommand can't be runtime-verified cheaply.** `cmd_curator` and `cmd_surveyor` both lack a `status`-like subcommand that short-circuits before the daemon loop starts. For those, static-check the call site and rely on the mechanical nature of the change.

### Missing Knowledge

- **No integration test for CLI logging routing.** Today's verification was a manual grep + byte-delta sampling. A fixture-based test that invokes each dispatcher against a temp log directory and asserts the right file gains bytes would catch regressions of this class automatically. Part of the broader "bootstrap pytest" followup.

## Followups Created By This Commit

1. **DRY `cmd_vault` and `cmd_janitor` main-path inline setups into the helper.** Both can collapse from their current inline blocks (~25 and ~12 lines respectively) to one-line calls through the new helper, once the helper gains a `suppress_stdout` passthrough kwarg. ~30-line reduction total across the two handlers. Low-risk mechanical change. One dedicated commit.
2. **Integration test for CLI logging routing.** Part of the broader pytest bootstrap followup. Would assert that each `alfred <tool> <subcommand>` invocation writes to `data/<tool>.log` and NOT to `data/alfred.log`.
