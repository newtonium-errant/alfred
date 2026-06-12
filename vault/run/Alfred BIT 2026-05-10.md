---
alfred_tags:
- testing/alfred-bit
created: '2026-05-10'
description: Alfred built-in test (health sweep)
janitor_note: 'LINK001 — [[process/Alfred BIT]] parent process record does not exist
  in vault. Field is run.process which janitor cannot retarget safely. DIR001 — record
  has type=run but lives in process/; expected autofix.py to handle deterministically.
  Human action: create the process record or retarget the wikilink.'
mode: quick
name: Alfred BIT 2026-05-10
overall_status: ok
process: '[[process/Alfred BIT]]'
related_orgs:
- org/Newton.md
related_projects:
- project/Alfred.md
relationships:
- confidence: 1
  context: Same Alfred BIT run series
  source: process/Alfred BIT 2026-05-10.md
  source_anchor: run Alfred BIT 2026-05-10
  target: process/Alfred BIT 2026-05-11.md
  target_anchor: run Alfred BIT 2026-05-11
  type: related-to
- confidence: 1
  context: Same Alfred BIT run series
  source: process/Alfred BIT 2026-05-10.md
  source_anchor: run Alfred BIT 2026-05-10
  target: process/Alfred BIT 2026-05-12.md
  target_anchor: run Alfred BIT 2026-05-12
  type: related-to
- confidence: 1
  context: Same Alfred BIT run series
  source: process/Alfred BIT 2026-05-10.md
  source_anchor: run Alfred BIT 2026-05-10
  target: process/Alfred BIT 2026-05-14.md
  target_anchor: run Alfred BIT 2026-05-14
  type: related-to
- confidence: 1
  context: Same Alfred BIT series
  source: process/Alfred BIT 2026-05-10.md
  source_anchor: run Alfred BIT 2026-05-10
  target: process/Alfred BIT 2026-05-15.md
  target_anchor: run Alfred BIT 2026-05-15
  type: related-to
- confidence: 0.9
  context: Same Alfred BIT series
  source: run/Alfred BIT 2026-05-10.md
  source_anchor: BIT 2026-05-10 report
  target: run/Alfred BIT 2026-05-11.md
  target_anchor: BIT 2026-05-11 report
  type: related-to
- confidence: 0.9
  context: Same Alfred BIT series
  source: run/Alfred BIT 2026-05-10.md
  source_anchor: BIT 2026-05-10 report
  target: run/Alfred BIT 2026-05-12.md
  target_anchor: BIT 2026-05-12 report
  type: related-to
started: '2026-05-10T17:58:17.279690-03:00'
status: completed
tags:
- bit
- health
- bit/ok
tool_counts:
  fail: 0
  ok: 10
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
trigger: scheduled
type: run
---

# Alfred BIT 2026-05-10

Generated at 1758 ADT.

## Summary

Alfred BIT (quick) — [ OK ]
  started:  2026-05-10T20:58:15.408274+00:00
  finished: 2026-05-10T20:58:17.279664+00:00
  elapsed:  1871 ms

[ OK ] curator  (1428 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (1039 ms) — count_tokens ok
    [ OK ] last-successful-process — inbox empty; last process 81.4h ago

[ OK ] janitor  (1043 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/janitor_state.json
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (956 ms) — count_tokens ok
    [ OK ] last-successful-sweep — last sweep 0.4h ago

[ OK ] distiller  (992 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/distiller_state.json
    [ OK ] candidate-threshold — 0.3
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (955 ms) — count_tokens ok
    [ OK ] last-successful-extraction — last extraction 14.4h ago

[ OK ] instructor  (782 ms)
    [ OK ] config-section — instructor section present
    [ OK ] state-path — data/instructor_state.json
    [ OK ] skill-file — /home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md
    [ OK ] pending-queue — pending queue length = 0
    [ OK ] retry-at-max — no records at max_retries=3

[ OK ] surveyor  (205 ms)
    [ OK ] ollama-reachable — HTTP 200
    [ OK ] milvus-lite — db: /home/andrew/alfred/data/milvus_lite.db
    [ OK ] openrouter-key — key set, model=qwen2.5:14b
    [ OK ] last-successful-cycle — last cycle 0.0h ago

[ OK ] brief  (516 ms)
    [ OK ] schedule-time — 06:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] output-dir — /home/andrew/alfred/vault/run
    [ OK ] weather-api — HTTP 200
    [ OK ] last-successful-brief — last brief: 2026-05-10 (0d ago)

[ OK ] mail  (0 ms)
    [ OK ] account:live — andrew.newton@live.ca on imap-mail.outlook.com
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox

[ OK ] talker  (265 ms)
    [ OK ] bot-token — token present (46 chars)
    [ OK ] allowed-users — 1 user(s) allowlisted
    [ OK ] stt-key — groq key present
    [ OK ] tts-key — elevenlabs key present (51 chars)
    [ OK ] capture-handler-registered — capture_batch + capture_extract modules importable
    [ OK ] anthropic-auth  (262 ms) — count_tokens ok

[ OK ] transport  (205 ms)
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
    [ OK ] last-successful-fire — last fire: 2026-05-10 (0d ago)

Totals: ok=10 warn=0 fail=0 skip=0

## Raw report (JSON)

```json
{
  "mode": "quick",
  "started_at": "2026-05-10T20:58:15.408274+00:00",
  "finished_at": "2026-05-10T20:58:17.279664+00:00",
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
          "latency_ms": 1038.6096159927547,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-process",
          "status": "ok",
          "detail": "inbox empty; last process 81.4h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/curator_state.json",
            "last_run": "2026-05-07T11:34:07.347930+00:00",
            "elapsed_hours": 81.4,
            "inbox_has_pending": false
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1427.6647079968825
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
          "latency_ms": 955.922465887852,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-sweep",
          "status": "ok",
          "detail": "last sweep 0.4h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/janitor_state.json",
            "last_sweep": "2026-05-10T20:31:35.794844+00:00",
            "elapsed_hours": 0.44
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1042.6757760578766
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
          "latency_ms": 955.0694040954113,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-extraction",
          "status": "ok",
          "detail": "last extraction 14.4h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/distiller_state.json",
            "last_extraction": "2026-05-10T06:31:24.014116+00:00",
            "elapsed_hours": 14.45
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 991.7275480693206
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
      "elapsed_ms": 782.1180539904162
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
            "last_run": "2026-05-10T20:55:18.639440+00:00",
            "elapsed_hours": 0.05
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 204.5421510701999
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
          "detail": "last brief: 2026-05-10 (0d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/brief_state.json",
            "most_recent_date": "2026-05-10",
            "today_local": "2026-05-10",
            "days_old": 0
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 516.3156019989401
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
      "elapsed_ms": 0.10875100269913673
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
          "name": "anthropic-auth",
          "status": "ok",
          "detail": "count_tokens ok",
          "latency_ms": 262.32747395988554,
          "data": {
            "model": "claude-sonnet-4-6",
            "probe": "count_tokens"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 264.91322997026145
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
      "elapsed_ms": 204.56473995000124
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
          "detail": "last fire: 2026-05-10 (0d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/daily_sync_state.json",
            "most_recent_date": "2026-05-10",
            "today_local": "2026-05-10",
            "days_old": 0
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 0.24029600899666548
    }
  ],
  "elapsed_ms": 1871.399390976876
}
```

---
*Generated by Alfred BIT daemon*
