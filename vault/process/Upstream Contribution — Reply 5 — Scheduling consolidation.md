---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 5 — Scheduling consolidation

**Problem shape.** Every heavy daily pass — janitor deep sweep, distiller deep extraction, distiller consolidation — fired on a rolling-24h interval. Each `alfred up` restart during development reset the clock. Over two weeks the deep sweep drifted into working hours; one morning it kicked off a large LLM burn at 11:22 local while I was mid-conversation with Salem.

Worse, this interacted with the morning brief. Brief wants clean post-sweep, post-enrichment state at 06:00. With rolling scheduling, whether brief actually got that state depended on when the daemons had last been restarted.

**Solution shape.** A shared primitive: `src/alfred/common/schedule.py`.

```python
@dataclass
class ScheduleConfig:
    time: str              # "HH:MM"
    timezone: str          # e.g. "America/Halifax"
    day_of_week: str | None = None   # "Monday" … for weekly

def compute_next_fire(cfg: ScheduleConfig, now: datetime) -> datetime:
    ...
```

Wall-clock, DST-aware via `zoneinfo.ZoneInfo`. Tested against Halifax spring-forward (2026-03-08) and fall-back (2026-11-01). The API intentionally mirrors cron's "next wakeup" shape so daemons can poll for "is it time yet?" without a scheduler daemon.

Migration was four small commits:

- **brief** — zero behavior change, moved to the shared primitive as the reference case.
- **janitor** — `sweep.deep_sweep_schedule: "02:30" Halifax daily`.
- **distiller deep extraction** — `"03:30" Halifax daily`.
- **distiller consolidation** — `"04:00" Halifax Sundays` (weekly day-of-week gate is the same primitive, just with `day_of_week` set).

The overnight choreography now looks like:

```
02:30 — janitor deep sweep
03:30 — distiller deep extraction
04:00 — distiller consolidation (Sundays only)
05:55 — BIT preflight
06:00 — brief
```

30-60 minute gaps so each stage has clean state when the next starts. Brief at 06:00 sees post-sweep, post-enrichment, post-clustering vault state every morning, regardless of when the daemons were last restarted.

**What stayed rolling.** Cheap/event-responsive work kept its old cadence:

- Janitor structural sweep — 1h rolling.
- Distiller light scan — 1h rolling.
- Transport scheduler — 30s poll for `remind_at`.
- Instructor poll — 60s.
- Mail — 300s poll.

**Surveyor is out of scope.** It's a filesystem watcher with debounce polling; reacts to vault edits when they happen, no daily deep pass. Shaped correctly already.

**Tradeoffs / what we rejected.**

- **A separate scheduler daemon.** Rejected. Each daemon polls its own schedule with the shared helper. Adding a scheduler process just to own a cron-shaped API would have introduced a single-point-of-failure coordinator for independent work.
- **Hour-of-day-only configuration.** Rejected as too coarse — 02:30 vs 02:00 mattered for the stage gap.
- **Pruning the old `*_interval_hours` fields immediately.** Kept as backward-compat fallbacks; ignored when the `*_schedule` block is present. Will prune after a deprecation cycle.
- **First-boot firing.** Old behavior fired on boot then waited 24h. New behavior seeds `last_consolidation = now` so restarting at, say, 14:00 doesn't immediately fire consolidation. Contradicted the overnight-only intent.

**One known follow-up.** The morning brief fired ~16 minutes early today despite clock-aligned scheduling. Debugging under way — suspect `asyncio.sleep` drift under load. The clock-alignment math itself has good test coverage; the wait loop that consumes its output is the suspect.

**Commit range.** `3f14226..d1b4d6c` (5 commits).

Would love to hear how this echoes (or doesn't) in your thinking.
