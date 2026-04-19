"""Alfred health / BIT (built-in test) package.

Provides shared dataclasses, the aggregator that fans out to per-tool
``<tool>.health.health_check`` callables, and the renderers used by
the ``alfred check`` CLI and the BIT daemon.

The aggregator exposes a small REGISTRY mapping ``tool_name -> check_callable``.
Per-tool health checks register themselves by importing this module and
calling :func:`register_check`. The aggregator deliberately excludes the
``bit`` tool from the set it probes — otherwise the BIT daemon would
recurse into checking itself.
"""

from __future__ import annotations

from .types import (
    CheckResult,
    HealthReport,
    Status,
    ToolHealth,
)

__all__ = [
    "CheckResult",
    "HealthReport",
    "Status",
    "ToolHealth",
]
