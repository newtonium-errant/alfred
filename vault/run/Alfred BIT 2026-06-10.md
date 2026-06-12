---
alfred_tags:
- alfred/bit
created: '2026-06-10'
description: Alfred built-in test (health sweep)
janitor_note: 'LINK001 — broken wikilink [[process/Alfred BIT]] in process frontmatter
  field; parent process record does not exist in vault (verified: glob process/Alfred
  BIT.md returns nothing). Same gap already flagged on the 2026-05-11 run record.
  Human action: create process/Alfred BIT.md or retarget. DIR001 (type=run, lives
  in process/) is deterministic — awaiting autofix.py move.'
mode: quick
name: Alfred BIT 2026-06-10
overall_status: ok
process: '[[process/Alfred BIT]]'
related_orgs:
- org/Newton.md
related_projects:
- project/Alfred.md
started: '2026-06-10T05:55:04.121770-03:00'
status: completed
tags:
- bit
- health
- bit/ok
tool_counts:
  fail: 0
  ok: 12
  skip: 0
  warn: 0
tools_checked:
- curator
- janitor
- distiller
- instructor
- surveyor
- brief
- mail
- talker
- transport
- daily_sync
- cloudflared
- gcal
trigger: scheduled
type: run
---

# Alfred BIT 2026-06-10

Generated at 0555 ADT.

## Summary

Alfred BIT (quick) — [ OK ]
  started:  2026-06-10T08:55:00.042058+00:00
  finished: 2026-06-10T08:55:04.121747+00:00
  elapsed:  4080 ms

[ OK ] curator  (3114 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2746 ms) — count_tokens ok
    [ OK ] last-successful-process — inbox empty; last process 0.4h ago

[ OK ] janitor  (3023 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/janitor_state.json
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2649 ms) — count_tokens ok
    [ OK ] last-successful-sweep — last sweep 2.6h ago

[ OK ] distiller  (2626 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/distiller_state.json
    [ OK ] candidate-threshold — 0.3
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2518 ms) — count_tokens ok
    [ OK ] last-successful-extraction — last extraction 1.5h ago

[ OK ] instructor  (1512 ms)
    [ OK ] config-section — instructor section present
    [ OK ] state-path — data/instructor_state.json
    [ OK ] skill-file — /home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md
    [ OK ] pending-queue — pending queue length = 0
    [ OK ] retry-at-max — no records at max_retries=3
    [ OK ] last-successful-poll — last poll: 2026-06-10T08:54:53.086461+00:00 (9s ago)

[ OK ] surveyor  (1335 ms)
    [ OK ] ollama-reachable — HTTP 200
    [ OK ] milvus-lite — db: /home/andrew/alfred/data/milvus_lite.db
    [ OK ] openrouter-key — key set, model=qwen2.5:14b
    [ OK ] last-successful-cycle — last cycle 0.0h ago

[ OK ] brief  (1924 ms)
    [ OK ] schedule-time — 06:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] output-dir — /home/andrew/alfred/vault/run
    [ OK ] weather-api — HTTP 200
    [ OK ] last-successful-brief — last brief: 2026-06-09 (1d ago)

[ OK ] mail  (0 ms)
    [ OK ] account:live — andrew.newton@live.ca on imap-mail.outlook.com
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox

[ OK ] talker  (1250 ms)
    [ OK ] bot-token — token present (46 chars)
    [ OK ] allowed-users — 1 user(s) allowlisted
    [ OK ] stt-key — groq key present
    [ OK ] tts-key — elevenlabs key present (51 chars)
    [ OK ] capture-handler-registered — capture_batch + capture_extract modules importable
    [ OK ] skill-capability-audit — all 7 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)
    [ OK ] anthropic-auth  (1244 ms) — count_tokens ok

[ OK ] transport  (1336 ms)
    [ OK ] config-section — transport section present
    [ OK ] token-configured — token length 64
    [ OK ] port-reachable — telegram_connected=True
    [ OK ] queue-depth — pending=0 (warn at 100)
    [ OK ] dead-letter-depth — dead_letter=1 (warn at 50)
    [ OK ] peer-reachable:kal-le — kal-le reachable
    [ OK ] peer-handshake:kal-le — kal-le handshake ok (v1)
    [ OK ] peer-queue-depth:kal-le — kal-le depth=0 (warn at 100)
    [ OK ] peer-reachable:hypatia — hypatia reachable
    [ OK ] peer-handshake:hypatia — hypatia handshake ok (v1)
    [ OK ] peer-queue-depth:hypatia — hypatia depth=0 (warn at 100)

[ OK ] daily_sync  (0 ms)
    [ OK ] schedule-time — 09:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] state-path — data/daily_sync_state.json
    [ OK ] last-successful-fire — last fire: 2026-06-09 (1d ago)

[ OK ] cloudflared  (25 ms)
    [ OK ] last-successful-tunnel — tunnel connections active: 4

[ OK ] gcal  (856 ms)
    [ OK ] last-successful-gcal-sync — active probe ok; token last refreshed 60.2h ago (2026-06-07T20:45:59.742787Z)

Totals: ok=12 warn=0 fail=0 skip=0

## Raw report (JSON)

```json
{
  "mode": "quick",
  "started_at": "2026-06-10T08:55:00.042058+00:00",
  "finished_at": "2026-06-10T08:55:04.121747+00:00",
  "overall_status": "ok",
  "tools": [
    {
      "tool": "curator",
      "status": "ok",
      "results": [
        {
          "name": "vault-path",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault"
          }
        },
        {
          "name": "inbox-dir",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault/inbox",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault/inbox"
          }
        },
        {
          "name": "backend",
          "status": "ok",
          "detail": "backend=claude",
          "latency_ms": null,
          "data": {
            "backend": "claude"
          }
        },
        {
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 2745.5138839995925,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-process",
          "status": "ok",
          "detail": "inbox empty; last process 0.4h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/curator_state.json",
            "last_run": "2026-06-10T08:29:57.375243+00:00",
            "elapsed_hours": 0.42,
            "inbox_has_pending": false
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 3113.9203019993147
    },
    {
      "tool": "janitor",
      "status": "ok",
      "results": [
        {
          "name": "vault-path",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault"
          }
        },
        {
          "name": "state-file",
          "status": "ok",
          "detail": "data/janitor_state.json",
          "latency_ms": null,
          "data": {
            "path": "data/janitor_state.json"
          }
        },
        {
          "name": "backend",
          "status": "ok",
          "detail": "backend=claude",
          "latency_ms": null,
          "data": {
            "backend": "claude"
          }
        },
        {
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 2648.674033000134,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-sweep",
          "status": "ok",
          "detail": "last sweep 2.6h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/janitor_state.json",
            "last_sweep": "2026-06-10T06:21:45.890518+00:00",
            "elapsed_hours": 2.55
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 3023.256713000592
    },
    {
      "tool": "distiller",
      "status": "ok",
      "results": [
        {
          "name": "vault-path",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault"
          }
        },
        {
          "name": "state-file",
          "status": "ok",
          "detail": "data/distiller_state.json",
          "latency_ms": null,
          "data": {
            "path": "data/distiller_state.json"
          }
        },
        {
          "name": "candidate-threshold",
          "status": "ok",
          "detail": "0.3",
          "latency_ms": null,
          "data": {
            "value": 0.3
          }
        },
        {
          "name": "backend",
          "status": "ok",
          "detail": "backend=claude",
          "latency_ms": null,
          "data": {
            "backend": "claude"
          }
        },
        {
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 2517.7871370015055,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-extraction",
          "status": "ok",
          "detail": "last extraction 1.5h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/distiller_state.json",
            "last_extraction": "2026-06-10T07:24:10.959644+00:00",
            "elapsed_hours": 1.51
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 2626.3396719987213
    },
    {
      "tool": "instructor",
      "status": "ok",
      "results": [
        {
          "name": "config-section",
          "status": "ok",
          "detail": "instructor section present",
          "latency_ms": null,
          "data": {}
        },
        {
          "name": "state-path",
          "status": "ok",
          "detail": "data/instructor_state.json",
          "latency_ms": null,
          "data": {
            "path": "data/instructor_state.json"
          }
        },
        {
          "name": "skill-file",
          "status": "ok",
          "detail": "/home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md"
          }
        },
        {
          "name": "pending-queue",
          "status": "ok",
          "detail": "pending queue length = 0",
          "latency_ms": null,
          "data": {
            "pending": 0,
            "threshold": 20
          }
        },
        {
          "name": "retry-at-max",
          "status": "ok",
          "detail": "no records at max_retries=3",
          "latency_ms": null,
          "data": {}
        },
        {
          "name": "last-successful-poll",
          "status": "ok",
          "detail": "last poll: 2026-06-10T08:54:53.086461+00:00 (9s ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/instructor_state.json",
            "last_run_ts": "2026-06-10T08:54:53.086461+00:00",
            "age_seconds": 9
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1511.9064680002339
    },
    {
      "tool": "surveyor",
      "status": "ok",
      "results": [
        {
          "name": "ollama-reachable",
          "status": "ok",
          "detail": "HTTP 200",
          "latency_ms": null,
          "data": {
            "url": "http://172.22.0.1:11434/",
            "status_code": 200,
            "has_api_key": false
          }
        },
        {
          "name": "milvus-lite",
          "status": "ok",
          "detail": "db: /home/andrew/alfred/data/milvus_lite.db",
          "latency_ms": null,
          "data": {
            "uri": "/home/andrew/alfred/data/milvus_lite.db",
            "exists": true
          }
        },
        {
          "name": "openrouter-key",
          "status": "ok",
          "detail": "key set, model=qwen2.5:14b",
          "latency_ms": null,
          "data": {
            "has_key": true,
            "model": "qwen2.5:14b"
          }
        },
        {
          "name": "last-successful-cycle",
          "status": "ok",
          "detail": "last cycle 0.0h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/surveyor_state.json",
            "last_run": "2026-06-10T08:54:50.920945+00:00",
            "elapsed_hours": 0.0
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1334.8608639989834
    },
    {
      "tool": "brief",
      "status": "ok",
      "results": [
        {
          "name": "schedule-time",
          "status": "ok",
          "detail": "06:00",
          "latency_ms": null,
          "data": {
            "time": "06:00"
          }
        },
        {
          "name": "schedule-timezone",
          "status": "ok",
          "detail": "America/Halifax",
          "latency_ms": null,
          "data": {
            "timezone": "America/Halifax"
          }
        },
        {
          "name": "output-dir",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault/run",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault/run",
            "exists": true
          }
        },
        {
          "name": "weather-api",
          "status": "ok",
          "detail": "HTTP 200",
          "latency_ms": null,
          "data": {
            "url": "https://aviationweather.gov/api/data/metar?ids=CYZX&format=json",
            "status_code": 200
          }
        },
        {
          "name": "last-successful-brief",
          "status": "ok",
          "detail": "last brief: 2026-06-09 (1d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/brief_state.json",
            "most_recent_date": "2026-06-09",
            "today_local": "2026-06-10",
            "days_old": 1
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1923.9953050000622
    },
    {
      "tool": "mail",
      "status": "ok",
      "results": [
        {
          "name": "account:live",
          "status": "ok",
          "detail": "andrew.newton@live.ca on imap-mail.outlook.com",
          "latency_ms": null,
          "data": {
            "name": "live",
            "email": "andrew.newton@live.ca"
          }
        },
        {
          "name": "inbox-dir",
          "status": "ok",
          "detail": "/home/andrew/alfred/vault/inbox",
          "latency_ms": null,
          "data": {
            "path": "/home/andrew/alfred/vault/inbox"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 0.08090100163826719
    },
    {
      "tool": "talker",
      "status": "ok",
      "results": [
        {
          "name": "bot-token",
          "status": "ok",
          "detail": "token present (46 chars)",
          "latency_ms": null,
          "data": {
            "length": 46
          }
        },
        {
          "name": "allowed-users",
          "status": "ok",
          "detail": "1 user(s) allowlisted",
          "latency_ms": null,
          "data": {
            "count": 1
          }
        },
        {
          "name": "stt-key",
          "status": "ok",
          "detail": "groq key present",
          "latency_ms": null,
          "data": {
            "provider": "groq"
          }
        },
        {
          "name": "tts-key",
          "status": "ok",
          "detail": "elevenlabs key present (51 chars)",
          "latency_ms": null,
          "data": {
            "provider": "elevenlabs",
            "length": 51
          }
        },
        {
          "name": "capture-handler-registered",
          "status": "ok",
          "detail": "capture_batch + capture_extract modules importable",
          "latency_ms": null,
          "data": {}
        },
        {
          "name": "skill-capability-audit",
          "status": "ok",
          "detail": "all 7 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)",
          "latency_ms": null,
          "data": {
            "instance_name": "Salem",
            "tool_set": "talker",
            "skill_bundle": "vault-talker",
            "registered_count": 7,
            "advertised_count": 7
          }
        },
        {
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 1244.2406360005407,
          "data": {
            "model": "claude-sonnet-4-6",
            "probe": "count_tokens"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1250.4846080009884
    },
    {
      "tool": "transport",
      "status": "ok",
      "results": [
        {
          "name": "config-section",
          "status": "ok",
          "detail": "transport section present",
          "latency_ms": null,
          "data": {}
        },
        {
          "name": "token-configured",
          "status": "ok",
          "detail": "token length 64",
          "latency_ms": null,
          "data": {
            "length": 64
          }
        },
        {
          "name": "port-reachable",
          "status": "ok",
          "detail": "telegram_connected=True",
          "latency_ms": null,
          "data": {
            "url": "http://127.0.0.1:8891/health",
            "telegram_connected": true,
            "queue_depth": 0,
            "dead_letter_depth": 1
          }
        },
        {
          "name": "queue-depth",
          "status": "ok",
          "detail": "pending=0 (warn at 100)",
          "latency_ms": null,
          "data": {
            "pending": 0,
            "threshold": 100
          }
        },
        {
          "name": "dead-letter-depth",
          "status": "ok",
          "detail": "dead_letter=1 (warn at 50)",
          "latency_ms": null,
          "data": {
            "dead_letter": 1,
            "threshold": 50
          }
        },
        {
          "name": "peer-reachable:kal-le",
          "status": "ok",
          "detail": "kal-le reachable",
          "latency_ms": null,
          "data": {
            "url": "http://127.0.0.1:8892/health",
            "peer": "kal-le"
          }
        },
        {
          "name": "peer-handshake:kal-le",
          "status": "ok",
          "detail": "kal-le handshake ok (v1)",
          "latency_ms": null,
          "data": {
            "peer": "kal-le",
            "protocol_version": 1,
            "capabilities": [
              "outbound_send",
              "peer_message",
              "peer_query"
            ]
          }
        },
        {
          "name": "peer-queue-depth:kal-le",
          "status": "ok",
          "detail": "kal-le depth=0 (warn at 100)",
          "latency_ms": null,
          "data": {
            "peer": "kal-le",
            "depth": 0,
            "threshold": 100
          }
        },
        {
          "name": "peer-reachable:hypatia",
          "status": "ok",
          "detail": "hypatia reachable",
          "latency_ms": null,
          "data": {
            "url": "http://127.0.0.1:8893/health",
            "peer": "hypatia"
          }
        },
        {
          "name": "peer-handshake:hypatia",
          "status": "ok",
          "detail": "hypatia handshake ok (v1)",
          "latency_ms": null,
          "data": {
            "peer": "hypatia",
            "protocol_version": 1,
            "capabilities": [
              "outbound_send",
              "peer_message",
              "peer_query"
            ]
          }
        },
        {
          "name": "peer-queue-depth:hypatia",
          "status": "ok",
          "detail": "hypatia depth=0 (warn at 100)",
          "latency_ms": null,
          "data": {
            "peer": "hypatia",
            "depth": 0,
            "threshold": 100
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1335.9638999991148
    },
    {
      "tool": "daily_sync",
      "status": "ok",
      "results": [
        {
          "name": "schedule-time",
          "status": "ok",
          "detail": "09:00",
          "latency_ms": null,
          "data": {
            "time": "09:00"
          }
        },
        {
          "name": "schedule-timezone",
          "status": "ok",
          "detail": "America/Halifax",
          "latency_ms": null,
          "data": {
            "timezone": "America/Halifax"
          }
        },
        {
          "name": "state-path",
          "status": "ok",
          "detail": "data/daily_sync_state.json",
          "latency_ms": null,
          "data": {
            "path": "data/daily_sync_state.json",
            "exists": true
          }
        },
        {
          "name": "last-successful-fire",
          "status": "ok",
          "detail": "last fire: 2026-06-09 (1d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/daily_sync_state.json",
            "most_recent_date": "2026-06-09",
            "today_local": "2026-06-10",
            "days_old": 1
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 0.29547499980253633
    },
    {
      "tool": "cloudflared",
      "status": "ok",
      "results": [
        {
          "name": "last-successful-tunnel",
          "status": "ok",
          "detail": "tunnel connections active: 4",
          "latency_ms": null,
          "data": {
            "metrics_url": "http://localhost:20241/metrics",
            "enabled": true,
            "ha_connections": 4
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 24.640636998810805
    },
    {
      "tool": "gcal",
      "status": "ok",
      "results": [
        {
          "name": "last-successful-gcal-sync",
          "status": "ok",
          "detail": "active probe ok; token last refreshed 60.2h ago (2026-06-07T20:45:59.742787Z)",
          "latency_ms": null,
          "data": {
            "token_path": "/home/andrew/alfred/data/secrets/gcal_token.json",
            "enabled": true,
            "last_refreshed": "2026-06-07T20:45:59.742787Z",
            "age_seconds": 216542,
            "active_probe": "ok"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 855.9137120009837
    }
  ],
  "elapsed_ms": 4079.6964330002083
}
```

---
*Generated by Alfred BIT daemon*
