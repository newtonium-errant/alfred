---
type: routine
status: active
name: '{{title}}'
created: '{{date}}'
# Cadence — one of six shapes (see src/alfred/routine/cadence.py):
#   {type: daily}
#   {type: weekly, days: [Mon, Wed, Fri]}
#   {type: every_n_days, n: 14, anchor: 'YYYY-MM-DD'}
#   {type: monthly, day: 1}             # 1..31 or 'last'
#   {type: monthly, nth_weekday: [1, Mon]}   # 1st Mon; [-1, Fri] = last Fri
#   {type: every_n_months, n: 2, day: 15, anchor: 'YYYY-MM-DD'}
cadence: {type: daily}
# Items — list of dicts. Each item:
#   text:                 the line printed in the daily aggregator
#   priority:             aspirational | tracked | critical
#   warn_after_gap_days:  optional (tracked only); default 5
#   time:                 optional 'HH:MM' Halifax local (critical only)
items: []
# Completion log — append-only date strings per item. The
# ``alfred routine done <record> <item>`` CLI appends today's date here.
completion_log: {}
---

# {{title}}

## Items

<!-- The active items list lives in the ``items`` frontmatter above.
Edit there to add / remove / re-prioritise. The aggregator at 05:59
Halifax reads frontmatter, not this section. -->

## History

<!-- Completion history lives in the ``completion_log`` frontmatter
above (one ISO-date list per item.text). Record completions via
``alfred routine done "<routine-name>" "<item-text>"``. -->
