---
alfred_tags:
- testing/alfred-bit
created: '2026-05-23'
description: Alfred built-in test (health sweep)
janitor_note: 'LINK001 — [[process/Alfred BIT]] parent process record does not exist
  in vault; only run records (process/Alfred BIT YYYY-MM-DD.md) exist. DIR001 deterministic
  scanner flag (type: run belongs in run/ directory). Same pattern as prior BIT run
  records. Human action: create the parent process/Alfred BIT.md record OR move run
  records to run/.'
mode: quick
name: Alfred BIT 2026-05-23
overall_status: fail
process: '[[process/Alfred BIT]]'
related_orgs:
- org/Newton.md
related_projects:
- project/Alfred.md
relationships:
- confidence: 1
  context: Same Alfred BIT run series
  source: process/Alfred BIT 2026-05-23.md
  source_anchor: run Alfred BIT 2026-05-23
  target: process/Alfred BIT 2026-05-24.md
  target_anchor: run Alfred BIT 2026-05-24
  type: related-to
- confidence: 1
  context: Same Alfred BIT run series
  source: process/Alfred BIT 2026-05-23.md
  source_anchor: run Alfred BIT 2026-05-23
  target: process/Alfred BIT 2026-05-25.md
  target_anchor: run Alfred BIT 2026-05-25
  type: related-to
started: '2026-05-23T05:55:03.223044-03:00'
status: completed
tags:
- bit
- health
- bit/fail
tool_counts:
  fail: 1
  ok: 11
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

# Alfred BIT 2026-05-23

Generated at 0555 ADT.

## Summary

Alfred BIT (quick) — [FAIL]
  started:  2026-05-23T08:55:00.054939+00:00
  finished: 2026-05-23T08:55:03.223015+00:00
  elapsed:  3168 ms

[ OK ] curator  (2120 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (1700 ms) — count_tokens ok
    [ OK ] last-successful-process — inbox empty; last process 2.3h ago

[ OK ] janitor  (1792 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/janitor_state.json
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (1592 ms) — count_tokens ok
    [ OK ] last-successful-sweep — last sweep 0.4h ago

[ OK ] distiller  (1746 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/distiller_state.json
    [ OK ] candidate-threshold — 0.3
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (1674 ms) — count_tokens ok
    [ OK ] last-successful-extraction — last extraction 2.4h ago

[ OK ] instructor  (1028 ms)
    [ OK ] config-section — instructor section present
    [ OK ] state-path — data/instructor_state.json
    [ OK ] skill-file — /home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md
    [ OK ] pending-queue — pending queue length = 0
    [ OK ] retry-at-max — no records at max_retries=3
    [ OK ] last-successful-poll — last poll: 2026-05-23T08:54:30.619634+00:00 (31s ago)

[ OK ] surveyor  (707 ms)
    [ OK ] ollama-reachable — HTTP 200
    [ OK ] milvus-lite — db: /home/andrew/alfred/data/milvus_lite.db
    [ OK ] openrouter-key — key set, model=qwen2.5:14b
    [ OK ] last-successful-cycle — last cycle 0.1h ago

[ OK ] brief  (1511 ms)
    [ OK ] schedule-time — 06:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] output-dir — /home/andrew/alfred/vault/run
    [ OK ] weather-api — HTTP 200
    [ OK ] last-successful-brief — last brief: 2026-05-22 (1d ago)

[ OK ] mail  (0 ms)
    [ OK ] account:live — andrew.newton@live.ca on imap-mail.outlook.com
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox

[ OK ] talker  (617 ms)
    [ OK ] bot-token — token present (46 chars)
    [ OK ] allowed-users — 1 user(s) allowlisted
    [ OK ] stt-key — groq key present
    [ OK ] tts-key — elevenlabs key present (51 chars)
    [ OK ] capture-handler-registered — capture_batch + capture_extract modules importable
    [ OK ] skill-capability-audit — all 5 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)
    [ OK ] anthropic-auth  (608 ms) — count_tokens ok

[ OK ] transport  (715 ms)
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
    [ OK ] last-successful-fire — last fire: 2026-05-22 (1d ago)

[ OK ] cloudflared  (27 ms)
    [ OK ] last-successful-tunnel — tunnel connections active: 4

[FAIL] gcal  (349 ms)
    [FAIL] last-successful-gcal-sync — active probe failed (refresh_failed; run alfred gcal authorize): ('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})

Totals: ok=11 warn=0 fail=1 skip=0

## Raw report (JSON)

```json
{
  "mode": "quick",
  "started_at": "2026-05-23T08:55:00.054939+00:00",
  "finished_at": "2026-05-23T08:55:03.223015+00:00",
  "overall_status": "fail",
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
          "latency_ms": 1700.2402829239145,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-process",
          "status": "ok",
          "detail": "inbox empty; last process 2.3h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/curator_state.json",
            "last_run": "2026-05-23T06:34:08.029583+00:00",
            "elapsed_hours": 2.35,
            "inbox_has_pending": false
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 2120.1232069870457
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
          "latency_ms": 1592.0056160539389,
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
            "last_sweep": "2026-05-23T08:32:34.912181+00:00",
            "elapsed_hours": 0.37
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1792.3553640721366
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
          "latency_ms": 1673.7740560201928,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-extraction",
          "status": "ok",
          "detail": "last extraction 2.4h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/distiller_state.json",
            "last_extraction": "2026-05-23T06:33:50.845901+00:00",
            "elapsed_hours": 2.35
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1746.4095660252497
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
          "detail": "last poll: 2026-05-23T08:54:30.619634+00:00 (31s ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/instructor_state.json",
            "last_run_ts": "2026-05-23T08:54:30.619634+00:00",
            "age_seconds": 31
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1027.795993955806
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
            "last_run": "2026-05-23T08:50:49.857569+00:00",
            "elapsed_hours": 0.07
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 707.4395030504093
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
          "detail": "last brief: 2026-05-22 (1d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/brief_state.json",
            "most_recent_date": "2026-05-22",
            "today_local": "2026-05-23",
            "days_old": 1
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1511.3979630405083
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
      "elapsed_ms": 0.11866504792124033
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
          "latency_ms": 607.7258079312742,
          "data": {
            "model": "claude-sonnet-4-6",
            "probe": "count_tokens"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 617.0671299332753
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
      "elapsed_ms": 714.6243490278721
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
          "detail": "last fire: 2026-05-22 (1d ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/daily_sync_state.json",
            "most_recent_date": "2026-05-22",
            "today_local": "2026-05-23",
            "days_old": 1
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 0.4534239415079355
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
      "elapsed_ms": 27.169264038093388
    },
    {
      "tool": "gcal",
      "status": "fail",
      "results": [
        {
          "name": "last-successful-gcal-sync",
          "status": "fail",
          "detail": "active probe failed (refresh_failed; run alfred gcal authorize): ('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})",
          "latency_ms": null,
          "data": {
            "token_path": "/home/andrew/alfred/data/secrets/gcal_token.json",
            "enabled": true,
            "last_refreshed": "2026-05-19T17:14:15.533872Z",
            "age_seconds": 315646,
            "active_probe": "failed",
            "error_class": "refresh_failed",
            "exception_type": "RefreshError",
            "exception_message": "('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 349.2122549796477
    }
  ],
  "elapsed_ms": 3168.087701080367
}
```

---
*Generated by Alfred BIT daemon*
