---
type: session
name: Transport c1 — Config + auth + state scaffolding
session_type: build
created: 2026-04-20
status: completed
tags:
  - transport
  - outbound-push
related:
  - "[[project/Alfred]]"
  - "[[project/Outbound Transport]]"
---

# Transport c1 — Config + auth + state scaffolding

## What shipped

First of six commits implementing the outbound-push transport. This
commit lays the module skeleton — every file below is new:

- `src/alfred/transport/__init__.py`
- `src/alfred/transport/utils.py` — `setup_logging`, `get_logger`,
  `chunk_for_telegram` paragraph-aware chunker (used by brief in c5).
- `src/alfred/transport/config.py` — `TransportConfig` with nested
  `ServerConfig`, `SchedulerConfig`, `AuthConfig` (per-peer `tokens`
  dict, keyed by peer name — locks in Stage 3.5 D2), `StateConfig`.
- `src/alfred/transport/state.py` — `TransportState` with atomic
  `.tmp → os.replace` saves, `pending_queue` / `send_log` /
  `dead_letter` lists, `pop_due(now)`, `find_recent_send()` 24h
  dedupe lookup, `append_dead_letter()`.

Config surface wired into `config.yaml` (live + example), `.env` +
`.env.example`. `ALFRED_TRANSPORT_TOKEN` generated as a fresh 64-char
hex secret and parked in `.env`. `aiohttp>=3.10` added to the `voice`
extras (installed locally; pyproject updated so a fresh install picks
it up).

## Ratified decisions instantiated

- Fixed port 8891 (rec 1), config-overridable.
- 30s poll interval (rec 7), 180-minute stale-reminder window (rec 8).
- `auth.tokens` dict keyed by peer name — Stage 3.5 D2/D7 locked in.

## Tests

- `tests/test_transport_config.py` — 6 new tests. Defaults,
  overrides, env substitution, missing env placeholder, unknown-key
  tolerance, example-config smoke.
- `tests/state/test_state_roundtrip.py` — added 4 new tests for
  transport state: roundtrip, `pop_due` splitting, 24h dedupe
  window, corrupt-file tolerance.

Suite: 597 → 607 (10 new). All green.

## Alfred Learnings

- **Pattern validated** — instructor's module scaffold (config +
  state + utils) ported cleanly to a new tool with no friction. The
  shape is generic enough that Stage 3.5 peer-routing and any future
  module can reuse it as-is.
- **Gotcha** — `aiohttp` was not installed in the venv. Added to
  `pyproject.toml` under `voice` extras and `pip install`'d locally.
  Fresh clones that `pip install -e .[voice]` or `.[all]` pick it up.
  Worth watching: if the talker daemon runs without aiohttp it will
  `ImportError` on the server import in c2.
- **Anti-pattern avoided** — resisted the urge to use `sk-` prefixes
  in test fixtures. Every token literal in the new tests uses
  `DUMMY_TRANSPORT_TEST_TOKEN` per the post-GitGuardian convention
  in builder.md.
