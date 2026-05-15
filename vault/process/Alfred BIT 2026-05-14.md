---
alfred_tags:
- alfred/bit
created: '2026-05-14'
description: Alfred built-in test (health sweep)
mode: quick
name: Alfred BIT 2026-05-14
overall_status: warn
process: '[[process/Alfred BIT]]'
started: '2026-05-14T05:55:01.839592-03:00'
status: completed
tags:
- bit
- health
- bit/warn
tool_counts:
  fail: 0
  ok: 8
  skip: 0
  warn: 2
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
trigger: scheduled
type: run
---

# Alfred BIT 2026-05-14

Generated at 0555 ADT.

## Summary

Alfred BIT (quick) — [WARN]
  started:  2026-05-14T08:55:00.078705+00:00
  finished: 2026-05-14T08:55:01.839567+00:00
  elapsed:  1761 ms

[ OK ] curator  (1442 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (1052 ms) — count_tokens ok
    [ OK ] last-successful-process — inbox empty; last process 29.3h ago

[ OK ] janitor  (1068 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/janitor_state.json
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (982 ms) — count_tokens ok
    [ OK ] last-successful-sweep — last sweep 0.5h ago

[ OK ] distiller  (959 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/distiller_state.json
    [ OK ] candidate-threshold — 0.3
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (918 ms) — count_tokens ok
    [ OK ] last-successful-extraction — last extraction 2.2h ago

[ OK ] instructor  (780 ms)
    [ OK ] config-section — instructor section present
    [ OK ] state-path — data/instructor_state.json
    [ OK ] skill-file — /home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md
    [ OK ] pending-queue — pending queue length = 0
    [ OK ] retry-at-max — no records at max_retries=3

[ OK ] surveyor  (206 ms)
    [ OK ] ollama-reachable — HTTP 200
    [ OK ] milvus-lite — db: /home/andrew/alfred/data/milvus_lite.db
    [ OK ] openrouter-key — key set, model=qwen2.5:14b
    [ OK ] last-successful-cycle — last cycle 0.1h ago

[WARN] brief  (408 ms)
    [ OK ] schedule-time — 06:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] output-dir — /home/andrew/alfred/vault/run
    [ OK ] weather-api — HTTP 200
    [WARN] last-successful-brief — last brief: 2026-05-12 (2d ago — one missed run)

[ OK ] mail  (0 ms)
    [ OK ] account:live — andrew.newton@live.ca on imap-mail.outlook.com
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox

[ OK ] talker  (259 ms)
    [ OK ] bot-token — token present (46 chars)
    [ OK ] allowed-users — 1 user(s) allowlisted
    [ OK ] stt-key — groq key present
    [ OK ] tts-key — elevenlabs key present (51 chars)
    [ OK ] capture-handler-registered — capture_batch + capture_extract modules importable
    [ OK ] skill-capability-audit — all 5 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)
    [ OK ] anthropic-auth  (253 ms) — count_tokens ok

[ OK ] transport  (235 ms)
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

[WARN] daily_sync  (6 ms)
    [ OK ] schedule-time — 09:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] state-path — data/daily_sync_state.json
    [WARN] last-successful-fire — last fire: 2026-05-12 (2d ago — one missed run)

Totals: ok=8 warn=2 fail=0 skip=0

## Raw report (JSON)

```json
{
  "mode": "quick",
  "started_at": "2026-05-14T08:55:00.078705+00:00",
  "finished_at": "2026-05-14T08:55:01.839567+00:00",
  "overall_status": "warn",
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
          "latency_ms": 1051.7654930008575,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-process",
          "status": "ok",
          "detail": "inbox empty; last process 29.3h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/curator_state.json",
            "last_run": "2026-05-13T03:34:09.286637+00:00",
            "elapsed_hours": 29.35,
            "inbox_has_pending": false
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1441.8862570018973
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
          "latency_ms": 981.8367060070159,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-sweep",
          "status": "ok",
          "detail": "last sweep 0.5h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/janitor_state.json",
            "last_sweep": "2026-05-14T08:27:42.020259+00:00",
            "elapsed_hours": 0.46
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1068.2724689977476
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
          "latency_ms": 917.5195260031614,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-extraction",
          "status": "ok",
          "detail": "last extraction 2.2h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/distiller_state.json",
            "last_extraction": "2026-05-14T06:45:13.889090+00:00",
            "elapsed_hours": 2.16
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 958.8856560003478
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
        }
      ],
      "detail": "",
      "elapsed_ms": 780.2579010021873
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
          "detail": "last cycle 0.1h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/surveyor_state.json",
            "last_run": "2026-05-14T08:51:51.222422+00:00",
            "elapsed_hours": 0.05
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 205.7650780043332
    },
    {
      "tool": "brief",
      "status": "warn",
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
          "status": "warn",
          "detail": "last brief: 2026-05-12 (2d ago \u2014 one missed run)",
          "latency_ms": null,
          "data": {
            "state_path": "data/brief_state.json",
            "most_recent_date": "2026-05-12",
            "today_local": "2026-05-14",
            "days_old": 2
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 407.53851699992083
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
      "elapsed_ms": 0.12326300202403218
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
          "detail": "all 5 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)",
          "latency_ms": null,
          "data": {
            "instance_name": "Salem",
            "tool_set": "talker",
            "skill_bundle": "vault-talker",
            "registered_count": 5,
            "advertised_count": 5
          }
        },
        {
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 253.44614101049956,
          "data": {
            "model": "claude-sonnet-4-6",
            "probe": "count_tokens"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 259.4437450024998
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
      "elapsed_ms": 235.1303530012956
    },
    {
      "tool": "daily_sync",
      "status": "warn",
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
          "status": "warn",
          "detail": "last fire: 2026-05-12 (2d ago \u2014 one missed run)",
          "latency_ms": null,
          "data": {
            "state_path": "data/daily_sync_state.json",
            "most_recent_date": "2026-05-12",
            "today_local": "2026-05-14",
            "days_old": 2
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 5.871025990927592
    }
  ],
  "elapsed_ms": 1760.8722470031353
}
```

---
*Generated by Alfred BIT daemon*
