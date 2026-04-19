---
type: session
created: '2026-04-19'
name: BIT probe fixes (env vars + weather status) 2026-04-19
description: Hotfix for two BIT probes surfaced on the first live `alfred check --full` run
intent: Make talker + brief health checks return truthful status for the real config
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred OS]]'
related:
  - '[[session/BIT — c1 skeleton 2026-04-19]]'
  - '[[session/BIT — c2 per-tool (core) 2026-04-19]]'
  - '[[session/BIT — c3 per-tool (telemetry) 2026-04-19]]'
  - '[[session/BIT — c4 CLI + preflight 2026-04-19]]'
  - '[[session/BIT — c5 BIT daemon 2026-04-19]]'
  - '[[session/BIT — c6 brief integration 2026-04-19]]'
tags:
  - bit
  - health
  - hotfix
status: completed
---

# BIT probe fixes (env vars + weather status) 2026-04-19

## Intent

The BIT system (commits `77fbfc3`..`626873b`) shipped today. First live
`alfred check --full` against the real config surfaced two defects in the
probe implementations. Fix both with one bundled commit so the next
`alfred check --full` is truthful end-to-end.

## Work Completed

### Bug 1 — talker health falsely FAILs on `${VAR}` placeholders

`src/alfred/telegram/health.py` inspected the raw unified config dict
(``raw["telegram"]["bot_token"]``, anthropic.api_key, stt.api_key) without
running env-var substitution first. Andrew's real config uses
``bot_token: "${TELEGRAM_BOT_TOKEN}"`` — env substitution happens inside
``telegram/config.py::load_from_unified``, so the daemon gets the real
token at startup. The health aggregator, however, hands over the
pre-substitution dict, and the talker probe flagged all three fields as
unresolved → three FAILs even though the talker was actually running
fine.

**Fix**: import ``_substitute_env`` from ``telegram/config.py`` and call
it on ``raw`` at the top of ``health_check`` before inspecting any
field that can contain a ``${VAR}``. Matches the pattern already used
at daemon startup; no new helper chain needed.

### Bug 2 — brief weather-api probe returns 404 but reports OK

Two defects in one probe:

1. **Wrong URL** — the probe hit ``{api_base}/`` (the API doc root).
   That path returns HTTP 404 from ``aviationweather.gov``, not a useful
   health signal. The real client (``brief/weather.py::fetch_metars``)
   hits ``{api_base}/metar?ids=<csv>&format=json``.
2. **Tolerant status mapping** — the probe returned OK for any HTTP
   response, so the 404 looked healthy. A genuine upstream regression
   would have hidden behind a green check.

**Fix** in `src/alfred/brief/health.py::_check_weather_api`:
- Build the probe URL as
  ``{api_base}/metar?ids=<first-station-id>&format=json``, matching the
  real client's request shape. If the station list contains an empty id
  we fall back to ``KJFK`` so the probe still exercises the endpoint.
- Tighten the status mapping:
  * 200 → OK
  * 4xx → WARN (service reachable; our probe URL may be off but the API
    itself is up — worth surfacing without crying wolf)
  * 5xx → FAIL (upstream service broken)
  * timeout / DNS / connection error → FAIL (service genuinely
    unreachable)

### Tests

- `tests/health/test_per_tool_telemetry.py`:
  * (new) `TestTalkerHealth::test_env_var_placeholders_are_expanded`
    — sets real env vars via ``monkeypatch.setenv`` for
    ``TELEGRAM_BOT_TOKEN``, ``GROQ_API_KEY``, ``ANTHROPIC_API_KEY``;
    passes raw config with all three fields as ``${VAR}``; asserts
    bot-token, stt-key, anthropic-auth all come back OK and the rollup
    is OK.
  * (new) `test_weather_api_200_is_ok`
  * (new) `test_weather_api_404_is_warn`  (the live-config case)
  * (new) `test_weather_api_500_is_fail`
  * (new) `test_weather_api_probes_real_metar_endpoint` — captures the
    URL passed to ``httpx.AsyncClient.get`` and asserts both the
    ``metar`` path and the ``ids=CYZX`` query param are present, so
    the probe shape can't drift back to the doc-root pattern without
    failing a test.
  * (renamed+changed) the old ``test_weather_api_unreachable_is_warn``
    is now ``test_weather_api_timeout_is_fail`` — timeouts FAIL under
    the new mapping.

### Live verification of the fixed endpoint

Before committing, confirmed the new probe URL works against the real
service:
```
$ curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "https://aviationweather.gov/api/data/metar?ids=CYZX&format=json"
HTTP 200
```

## Outcome

- Test count: 448 → 453 (+5 net: +1 talker regression, +4 brief status,
  one existing test renamed in place).
- Full suite: 453 passed (green).
- No config.yaml / .env changes, no daemon restart required — health
  checks are loaded lazily by the CLI and the BIT daemon each run.
- Next `alfred check --full` against the real config should report:
  * talker: all three env-fed fields resolve → `[ OK ] talker` with
    `bot-token`, `stt-key`, `anthropic-auth` all OK.
  * brief: weather probe hits the real `/metar` endpoint, gets HTTP
    200 → `[ OK ] brief` with `weather-api — HTTP 200`.

## Alfred Learnings

- **Gotcha — health probes must mirror the daemon's config-loading
  pipeline.** If a tool's daemon runs ``_substitute_env`` (or any
  other normalization) before reading a field, the tool's health
  module has to do the same — otherwise it reports on the raw dict,
  which is not the config the daemon is actually running against.
  Every new tool health check should start with the same
  normalization its daemon applies.
- **Pattern validated — probe the real endpoint shape, not a doc
  root.** The probe URL should match the same request shape the
  daemon uses at runtime. A generic "is this domain reachable" probe
  can give false greens when the specific API path the daemon needs
  has regressed. The brief weather probe now hits
  `/metar?ids=<first>&format=json`, mirroring ``fetch_metars``.
- **Anti-pattern confirmed — "any HTTP response is OK".** The old
  probe treated any HTTP status as OK. A tighter 200/4xx/5xx mapping
  is both safer and more informative for operators; 4xx as WARN
  preserves the "probe may be wrong" nuance without hiding upstream
  failure behind a green tick.
- **Missing knowledge — tests for a probe's URL shape.** Until this
  commit, nothing in the tests exercised the URL shape actually
  passed to httpx. Added a capturing client so future refactors
  can't silently drift the probe URL back to a useless shape.
- **Corrections — existing `test_weather_api_unreachable_is_warn`
  was wrong under the new mapping.** Renamed and flipped to FAIL.
  Timeouts / connection errors are not "WARN-the-probe-might-be-bad"
  territory — they're "the service is unreachable" territory.
