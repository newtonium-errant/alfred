---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 4 — Multi-instance MVP: KAL-LE

**Problem shape.** We wanted to prove the multi-instance architecture end-to-end with a low-stakes specialist before tackling the higher-stakes clinical instance (STAY-C, which needs a PHI firewall). KAL-LE is a coding-focused Alfred running against an aftermath-lab vault (our internal dev-knowledge repo). Salem (the daily-driver orchestrator) classifies a user message's intent and forwards coding turns to KAL-LE via the peer API; KAL-LE handles them; the response relays back through Salem with a `[KAL-LE]` prefix.

**Solution shape.**

The peer protocol is HTTP REST, JSON, localhost-only in v1. Auth is a bearer token per peer pair in `transport.auth.tokens`. Each instance has its own config (`config.kalle.yaml`), its own state and log directories (`/home/andrew/.alfred/kalle/`), its own Telegram bot token, and its own port (convention: SALEM 5005, KAL-LE 5015, STAY-C 5025, …).

Four sub-systems landed in the 11-commit arc:

1. **Config plumbing.** `--config` widened to all subcommands. `pid_path` and `instance.skill_bundle` so each instance can point at its own orchestrator state and own SKILL file. (c1)
2. **Canonical records on SALEM.** A permissions config declares which fields each peer may read from SALEM's person records. Default-deny, audit every read. (c2 + c3)
3. **Client + server.** Real `/peer/*` + `/canonical/*` handlers swap in for the 501 stubs from the transport arc. Client dispatch uses correlation IDs written to a per-peer inbox. (c3 + c4)
4. **SKILL bundle + scope.** A `kalle` scope with `edit: True, move: False, delete: False` (curation is additive — Andrew removes canonical content, nobody else). A `KALLE_CREATE_TYPES` set adds `pattern` and `principle` as KAL-LE-only record types without polluting the base `KNOWN_TYPES`. Enforcement via `kalle_types_only` rule, same shape as `talker_types_only`. (c5)

**The `bash_exec` tool (c6).** KAL-LE needs to run tests and editors on target repos. We wrote this as the safety-critical commit — 76 new tests, every denylist item has a dedicated assertion.

Invariants:

- **Deny-first ordering.** Denylist runs before allowlist so an allowlisted head token can't mask a denylisted tail.
- **`shlex.split` + `subprocess_exec`, never `shell=True`.** Shell metacharacters pass as literal argv, no expansion. Covered with tests asserting `$(whoami)` is literally `$(whoami)` when seen by the subprocess.
- **First-token allowlist:** pytest, npm, yarn, jest, mypy, ruff, black, eslint, tsc, python, python3, node, grep, rg, find, ls, cat, head, tail, wc, diff, file, stat, sort, uniq, awk, sed, git. Git requires a subcommand from a read-only set (`status, diff, log, show, blame, branch, checkout, switch, ls-files, ls-tree, cat-file, rev-parse`).
- **Denylist substrings** (case-insensitive): all git mutation verbs, `rm -rf`, `chmod`, `sudo`, `curl`, `wget`, `ssh`, `pip install`, `npm install`, `| sh`, `| bash`, `bash -c`, `sh -c`, `eval`, `exec`, `python -c`, `python3 -c`, `node -e`. The last few came out of testing — inline-code flags on interpreters are an attack vector regardless of which interpreter is allowlisted.
- **cwd gate.** `Path.expanduser().resolve()` then `is_relative_to` one of `{aftermath-lab, aftermath-alfred, aftermath-rrts, alfred}`. Symlinks caught after resolve. `/`, `$HOME`, `/tmp`, `..` escapes reject.
- **Destructive-keyword dry-run gate.** `rm -r`, `rm -f`, `truncate `, `mv `, `cp -r` force `dry_run=True` regardless of caller's flag. Belt-and-braces against the denylist.
- **300s timeout.** 10 KB per-stream truncation. Audit log in JSONL (command, cwd, exit_code, duration, session, reason — no stdout/stderr content).

Crucially: **no `git push`, no `git commit`, no PR opening.** KAL-LE can edit, test, and branch-switch. Humans run the remote-affecting operations. This isn't a temporary constraint — it's the Bundle B + Bundle D capability split we decided on up-front.

**Operator action between builder-done and live-validation.** BotFather to create the new bot, generate four tokens (`TELEGRAM_KALLE_BOT_TOKEN`, `ALFRED_KALLE_TRANSPORT_TOKEN`, `ALFRED_KALLE_PEER_TOKEN`, `ALFRED_SALEM_PEER_TOKEN`), `alfred instance new kalle` to scaffold, then `alfred --config config.kalle.yaml up --only talker,transport,instructor`. The `alfred instance new` CLI (c8) is there specifically so future instances don't rediscover the dance.

**Tradeoffs / what we rejected.**

- **Shared API key vs per-instance keys.** Shared for MVP; will split if dogfood shows rate-limit cross-talk.
- **Thread segregation for KAL-LE responses.** Rejected; `[KAL-LE]` prefix on inline responses is enough signal for a solo user.
- **Dynamic peer registry.** Rejected indefinitely. Config-driven. Revisit past ~10 instances.
- **Tool-level routing** (Salem decides per tool call whether to forward). Deferred as possible v2. Message-level routing handles the 80% case cleanly.
- **Canonical record on-disk caching on the peer side.** Rejected — 60s in-memory TTL only, fetch on expiry. Keeps SALEM the single source of truth for identity.

**Commit range.** `01bb976..fed4b73` (11 commits) + hotfixes `1f89c0b..34245da` (4 commits). The hotfixes caught: self-target guard in the peer dispatcher (Salem-to-Salem was a real mis-fire), Salem SKILL addendum for peer-routing awareness, router classifier cue tuning, and wiring `bash_exec` into the conversation tool-use dispatch (c6 shipped the executor; c4 hotfix wired it into the turn loop).

Would love to hear how this echoes (or doesn't) in your thinking — especially the Bundle-B+D capability split.
