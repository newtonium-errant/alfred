---
alfred_tags:
- alfred/bit
created: '2026-05-28'
description: Alfred built-in test (health sweep)
janitor_note: LINK001 — [[process/Alfred BIT]] parent process record does not exist
  in vault. Same pattern as Alfred BIT 2026-05-07 and Morning Brief 2026-05-11/2026-05-18/2026-05-25.
  Cannot unambiguously fix (outside janitor scope to create missing canonical process
  record). DIR001 — record has type=run but lives in process/; expected autofix.py
  to handle deterministically.
mode: quick
name: Alfred BIT 2026-05-28
overall_status: fail
process: '[[process/Alfred BIT]]'
started: '2026-05-28T05:55:04.962649-03:00'
status: completed
tags:
- bit
- health
- bit/fail
tool_counts:
  fail: 2
  ok: 9
  skip: 0
  warn: 1
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

# Alfred BIT 2026-05-28

Generated at 0555 ADT.

## Summary

Alfred BIT (quick) — [FAIL]
  started:  2026-05-28T08:55:00.328363+00:00
  finished: 2026-05-28T08:55:04.962622+00:00
  elapsed:  4634 ms

[ OK ] curator  (3723 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2725 ms) — count_tokens ok
    [ OK ] last-successful-process — inbox empty; last process 5.1h ago

[ OK ] janitor  (2783 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/janitor_state.json
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2561 ms) — count_tokens ok
    [ OK ] last-successful-sweep — last sweep 0.1h ago

[ OK ] distiller  (2732 ms)
    [ OK ] vault-path — /home/andrew/alfred/vault
    [ OK ] state-file — data/distiller_state.json
    [ OK ] candidate-threshold — 0.3
    [ OK ] backend — backend=claude
    [ OK ] anthropic-auth  (2651 ms) — count_tokens ok
    [ OK ] last-successful-extraction — last extraction 1.5h ago

[ OK ] instructor  (1130 ms)
    [ OK ] config-section — instructor section present
    [ OK ] state-path — data/instructor_state.json
    [ OK ] skill-file — /home/andrew/alfred/src/alfred/_bundled/skills/vault-instructor/SKILL.md
    [ OK ] pending-queue — pending queue length = 0
    [ OK ] retry-at-max — no records at max_retries=3
    [ OK ] last-successful-poll — last poll: 2026-05-28T08:54:15.124170+00:00 (47s ago)

[ OK ] surveyor  (1556 ms)
    [ OK ] ollama-reachable — HTTP 200
    [ OK ] milvus-lite — db: /home/andrew/alfred/data/milvus_lite.db
    [ OK ] openrouter-key — key set, model=qwen2.5:14b
    [ OK ] last-successful-cycle — last cycle 0.1h ago

[FAIL] brief  (2234 ms)
    [ OK ] schedule-time — 06:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] output-dir — /home/andrew/alfred/vault/run
    [ OK ] weather-api — HTTP 200
    [FAIL] last-successful-brief — last brief: 2026-05-25 (3d ago — daemon may be silently failing)

[ OK ] mail  (0 ms)
    [ OK ] account:live — andrew.newton@live.ca on imap-mail.outlook.com
    [ OK ] inbox-dir — /home/andrew/alfred/vault/inbox

[ OK ] talker  (1488 ms)
    [ OK ] bot-token — token present (46 chars)
    [ OK ] allowed-users — 1 user(s) allowlisted
    [ OK ] stt-key — groq key present
    [ OK ] tts-key — elevenlabs key present (51 chars)
    [ OK ] capture-handler-registered — capture_batch + capture_extract modules importable
    [ OK ] skill-capability-audit — all 5 tools advertised in skills/vault-talker/SKILL.md (instance=Salem, tool_set=talker)
    [ OK ] anthropic-auth  (1396 ms) — count_tokens ok

[WARN] transport  (1508 ms)
    [ OK ] config-section — transport section present
    [ OK ] token-configured — token length 64
    [ OK ] port-reachable — telegram_connected=True
    [ OK ] queue-depth — pending=0 (warn at 100)
    [ OK ] dead-letter-depth — dead_letter=1 (warn at 50)
    [WARN] peer-reachable:kal-le — kal-le unreachable: ConnectError
    [WARN] peer-handshake:kal-le — kal-le unreachable
    [ OK ] peer-queue-depth:kal-le — kal-le depth=0 (warn at 100)
    [WARN] peer-reachable:hypatia — hypatia unreachable: ConnectError
    [WARN] peer-handshake:hypatia — hypatia unreachable
    [ OK ] peer-queue-depth:hypatia — hypatia depth=0 (warn at 100)

[FAIL] daily_sync  (14 ms)
    [ OK ] schedule-time — 09:00
    [ OK ] schedule-timezone — America/Halifax
    [ OK ] state-path — data/daily_sync_state.json
    [FAIL] last-successful-fire — last fire: 2026-05-25 (3d ago — daemon may be silently failing)

[ OK ] cloudflared  (53 ms)
    [ OK ] last-successful-tunnel — tunnel connections active: 4

[ OK ] gcal  (1073 ms)
    [ OK ] last-successful-gcal-sync — active probe ok; token last refreshed 14.8h ago (2026-05-27T18:04:22.886956Z)

Totals: ok=9 warn=1 fail=2 skip=0

## Raw report (JSON)

```json
{
  "mode": "quick",
  "started_at": "2026-05-28T08:55:00.328363+00:00",
  "finished_at": "2026-05-28T08:55:04.962622+00:00",
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
          "latency_ms": 2725.1394679988152,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-process",
          "status": "ok",
          "detail": "inbox empty; last process 5.1h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/curator_state.json",
            "last_run": "2026-05-28T03:49:22.805295+00:00",
            "elapsed_hours": 5.09,
            "inbox_has_pending": false
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 3722.896357998252
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
          "latency_ms": 2560.7158959974186,
          "data": {
            "model": "claude-haiku-4-5",
            "probe": "count_tokens"
          }
        },
        {
          "name": "last-successful-sweep",
          "status": "ok",
          "detail": "last sweep 0.1h ago",
          "latency_ms": null,
          "data": {
            "state_path": "data/janitor_state.json",
            "last_sweep": "2026-05-28T08:47:30.008120+00:00",
            "elapsed_hours": 0.13
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 2783.1238569997367
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
          "latency_ms": 2650.550200996804,
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
            "last_extraction": "2026-05-28T07:23:37.046245+00:00",
            "elapsed_hours": 1.52
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 2732.229385001119
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
          "detail": "last poll: 2026-05-28T08:54:15.124170+00:00 (47s ago)",
          "latency_ms": null,
          "data": {
            "state_path": "data/instructor_state.json",
            "last_run_ts": "2026-05-28T08:54:15.124170+00:00",
            "age_seconds": 47
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1130.1952619978692
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
            "last_run": "2026-05-28T08:51:18.210181+00:00",
            "elapsed_hours": 0.06
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1555.8368589991005
    },
    {
      "tool": "brief",
      "status": "fail",
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
          "status": "fail",
          "detail": "last brief: 2026-05-25 (3d ago \u2014 daemon may be silently failing)",
          "latency_ms": null,
          "data": {
            "state_path": "data/brief_state.json",
            "most_recent_date": "2026-05-25",
            "today_local": "2026-05-28",
            "days_old": 3
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 2233.784676005598
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
      "elapsed_ms": 0.08640799933345988
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
          "latency_ms": 1395.7856410052045,
          "data": {
            "model": "claude-sonnet-4-6",
            "probe": "count_tokens"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1488.4178720021737
    },
    {
      "tool": "transport",
      "status": "warn",
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
          "status": "warn",
          "detail": "kal-le unreachable: ConnectError",
          "latency_ms": null,
          "data": {
            "url": "http://127.0.0.1:8892/health",
            "peer": "kal-le"
          }
        },
        {
          "name": "peer-handshake:kal-le",
          "status": "warn",
          "detail": "kal-le unreachable",
          "latency_ms": null,
          "data": {
            "peer": "kal-le"
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
          "status": "warn",
          "detail": "hypatia unreachable: ConnectError",
          "latency_ms": null,
          "data": {
            "url": "http://127.0.0.1:8893/health",
            "peer": "hypatia"
          }
        },
        {
          "name": "peer-handshake:hypatia",
          "status": "warn",
          "detail": "hypatia unreachable",
          "latency_ms": null,
          "data": {
            "peer": "hypatia"
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
      "elapsed_ms": 1507.5595439993776
    },
    {
      "tool": "daily_sync",
      "status": "fail",
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
          "status": "fail",
          "detail": "last fire: 2026-05-25 (3d ago \u2014 daemon may be silently failing)",
          "latency_ms": null,
          "data": {
            "state_path": "data/daily_sync_state.json",
            "most_recent_date": "2026-05-25",
            "today_local": "2026-05-28",
            "days_old": 3
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 13.549424998927861
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
      "elapsed_ms": 53.09517499699723
    },
    {
      "tool": "gcal",
      "status": "ok",
      "results": [
        {
          "name": "last-successful-gcal-sync",
          "status": "ok",
          "detail": "active probe ok; token last refreshed 14.8h ago (2026-05-27T18:04:22.886956Z)",
          "latency_ms": null,
          "data": {
            "token_path": "/home/andrew/alfred/data/secrets/gcal_token.json",
            "enabled": true,
            "last_refreshed": "2026-05-27T18:04:22.886956Z",
            "age_seconds": 53440,
            "active_probe": "ok"
          }
        }
      ],
      "detail": "",
      "elapsed_ms": 1073.0265020029037
    }
  ],
  "elapsed_ms": 4634.268162000808
}
```

---
*Generated by Alfred BIT daemon*
