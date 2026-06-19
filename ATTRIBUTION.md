# Attribution

Algernon stands on a foundation that someone else built first. This document
records that lineage honestly: what came from the original **alfred** project,
and what was added on top to make **Algernon**.

## The alfred foundation

**Algernon began as a fork of [`alfred`](https://github.com/ssdavidai/alfred),
created by David Szabo-Stuban (GitHub: [ssdavidai](https://github.com/ssdavidai)).**

David's `alfred` — initial commit `131fb01` on 2026-02-19, "Initial commit:
Alfred monorepo — unified vault operations suite," published to PyPI as
`alfred-vault` — established the architecture this project still runs on:
a single Python monorepo of AI-powered tools that manage an Obsidian vault,
with a shared CLI, shared config, and shared daemon orchestration.

That foundation is licensed under the MIT License (`Copyright (c) 2025
ssdavidai`), reproduced unchanged in [`LICENSE`](LICENSE). Algernon retains
that license and adds its own work under the same terms. The fork is friendly
and disclosed — not a quiet appropriation. Both trees have diverged and are now
developed independently; Algernon does not track a live upstream.

The package name (`alfred-vault`), the CLI entry point (`alfred`), the default
config filename (`config.yaml`), and the `ALFRED_*` environment variables are
all kept from the foundation for stability. "Algernon" is the operator-facing
name for the platform that grew on top.

**Thank you, David.** The bones are good, and they made everything that
followed possible.

## Module-level map: foundation vs. Algernon-original

This is a readable map, not an exhaustive file listing. Where the origin was
ambiguous it was cross-checked against `git log --diff-filter=A` and the file
tree of David's initial commits (`131fb01` … `320e686`, his last on 2026-04-24).
Many foundation modules have since been heavily extended by Algernon work — the
column below records where the *subsystem* originated, not who touched it last.

### Foundation — alfred (David Szabo-Stuban, Feb–Apr 2026)

| Subsystem | Where | What it is |
|---|---|---|
| Vault operations layer | `src/alfred/vault/ops.py`, `schema.py`, `scope.py`, `mutation_log.py`, `obsidian.py`, `cli.py` | CRUD over Markdown+frontmatter records, the type schema, the scope-restriction model, session-scoped mutation logging, Obsidian CLI integration |
| The tool pattern | `src/alfred/{curator,janitor,distiller,surveyor}/` | The four original tools and the shared module shape: `config.py` / `daemon.py` / `state.py` / `cli.py` / `backends/` per tool |
| Agent backend pattern | `src/alfred/*/backends/{cli,http,openclaw}.py` | The pluggable CLI / HTTP / OpenClaw backend abstraction and per-tool prompt builders |
| Daemon orchestration | `src/alfred/orchestrator.py`, `daemon.py` | Multiprocess daemon manager with auto-restart; the `alfred up` re-exec / daemonize model |
| Top-level CLI + setup | `src/alfred/cli.py`, `quickstart.py` | The `alfred` argparse dispatcher and interactive setup wizard (both since grown substantially) |
| Bundled skills + scaffold approach | `src/alfred/_bundled/` | The idea of shipping SKILL.md prompts and a vault scaffold in the wheel, located via `importlib.resources` (foundation pattern; the skill *content* is largely Algernon-rewritten) |

### Algernon-original — Andrew Newton (additive, on top of the foundation)

| Subsystem | Where | What it adds |
|---|---|---|
| Multi-instance platform | `src/alfred/instance_set.py`, `config.<instance>.yaml` | Deploying the one codebase as multiple independent instances — Salem, KAL-LE, Hypatia, VERA (Stacy planned) — each with its own vault, config, and bot |
| Peer / transport protocol | `src/alfred/transport/` | Instance-to-instance messaging — the outbound-push transport server and the routing that lets instances query and hand off to each other |
| Per-instance scope enforcement | `src/alfred/vault/scope.py` (extended) | Plumbing the calling instance's identity through the validation gates so each instance routes through its own scope rules instead of a hardcoded default |
| Distiller V2 | `src/alfred/distiller/` (rebuilt) | The non-agentic extractor → Pydantic-validated → deterministic-writer pipeline that replaced the original agentic distiller |
| Talker (Telegram chat) | `src/alfred/telegram/` | Vault-grounded voice/text chat — the fifth tool, not present in the foundation; STT, TTS, document handling, GCal-aware conversation |
| Brief + watches | `src/alfred/brief/`, `src/alfred/routine/` | Scheduled morning brief assembly and the watch/routine subsystem behind it |
| Mail webhook | `src/alfred/mail/` | Inbound-email webhook receiver (bearer-token-authed) feeding the curator inbox |
| Email tiering + classification | `src/alfred/email_classifier/`, `src/alfred/tier/` | Self-correcting email triage with calibration feedback |
| Digest / dashboard / health | `src/alfred/digest/`, `src/alfred/health/`, `dashboard.py` | Per-tool telemetry, health checks, and digest assembly |
| BIT | `src/alfred/bit/` | Build-in-test / instance-self-test tooling |
| VERA → KAL-LE → GitHub ticket pipeline | across `transport` + instance config + bundled skills | The cross-instance flow that turns operator input into GitHub tickets |
| SovServ migration work | various | Ongoing platform migration, of which this attribution file is part |

If you are reading the code and are unsure who a given subsystem came from,
`git log --diff-filter=A --format='%h %an %ad' -- <path>` and a glance back at
commits `131fb01`…`320e686` will tell you. The split above is meant to be a
faithful starting point, not the final word.
