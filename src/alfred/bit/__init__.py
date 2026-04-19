"""Alfred BIT (built-in test) daemon and CLI.

The BIT daemon runs a quick ``run_all_checks`` sweep on a schedule and
writes a ``run``-type record to ``vault/process/`` so the outcome is
queryable alongside the rest of Alfred's operational data. The Morning
Brief pulls the latest BIT record via ``render_health_section`` (c6).

Modules:
    config.py  — BITConfig dataclass and ``load_from_unified``
    daemon.py  — async scheduler + BIT run routine
    state.py   — JSON state file mirroring the brief pattern
    renderer.py — turns a HealthReport into a vault ``run`` record
    cli.py     — ``alfred bit {run-now|status|history}`` handlers
"""

from __future__ import annotations
